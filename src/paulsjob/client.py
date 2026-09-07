"""HTTP client for the Paul's Job API.

Everything that is awkward about talking to this API is handled here once, so
the onboarding logic above it stays readable:

* auth, and a User-Agent (Cloudflare rejects the default Python one with 1010)
* the tracing headers the API asks clients to send, used for idempotency
* retries with exponential backoff and jitter
* the two different pagination styles the API uses
* the API key never reaching a log line
"""

import json
import logging
import os
import random
import time
import urllib.parse
import uuid

import requests

from .errors import ConfigError, from_status

try:
    # Verify TLS through the OS trust store. Needed on machines where antivirus
    # or a corporate proxy inspects HTTPS: their injected root CA is trusted by
    # the OS but rejected by OpenSSL 3.x ("Basic Constraints of CA cert not
    # marked critical"). Verification stays ON - this is not verify=False.
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover - optional, only needed on such machines
    pass

log = logging.getLogger("paulsjob")

DEFAULT_BASE_URL = "https://api.paulsjob.ai/dev"
API_VERSION = "v1"
USER_AGENT = "paulsjob-onboarding/0.1"

# The spec documents no 429 and no Retry-After header, but Cloudflare fronts the
# API, so undocumented limits certainly exist. We retry defensively rather than
# trusting that silence.
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0


def load_dotenv(path=".env"):
    """Minimal .env reader. Avoids a dependency for four lines of parsing."""
    if not os.path.exists(path):
        return {}
    values = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def redact(text, secret):
    """Remove the API key from anything that might be printed or logged."""
    if not secret or not text:
        return text
    return str(text).replace(secret, "***REDACTED***")


class PaulsJobClient:
    def __init__(self, api_key=None, base_url=None, dry_run=False, timeout=30):
        env = load_dotenv()
        self.api_key = api_key or env.get("PAULSJOB_API_KEY") or os.environ.get("PAULSJOB_API_KEY", "")
        raw_base = base_url or env.get("PAULSJOB_BASE_URL") or os.environ.get("PAULSJOB_BASE_URL") or DEFAULT_BASE_URL
        if not self.api_key:
            raise ConfigError(
                "PAULSJOB_API_KEY is not set. Copy .env.example to .env and add your key "
                "(generate it in the Paul's Job dashboard under settings)."
            )

        # The task's .env.example gives the base without the version segment,
        # while the OpenAPI servers block includes it. We keep the env var
        # exactly as specified and join the version here, tolerating a base that
        # already carries it.
        base = raw_base.rstrip("/")
        if not base.endswith("/" + API_VERSION):
            base = f"{base}/{API_VERSION}"
        self.base_url = base

        self.dry_run = dry_run
        self.timeout = timeout
        # One correlation id per run ties every request of this onboarding run
        # together in Paul's Job's own logs - useful when asking them why a
        # specific run behaved oddly.
        self.correlation_id = str(uuid.uuid4())
        self.session = requests.Session()
        self.calls = 0

    # ---------------------------------------------------------------- requests

    def request(self, method, path, params=None, json_body=None, idempotency_key=None, read_only=False):
        """Perform one API call, with retries. Returns the decoded `data` field.

        `read_only` marks a POST that only reads - this API uses POST for search
        and list endpoints. Dry-run mode must still perform those, otherwise it
        cannot tell you what a real run would do.
        """
        url = f"{self.base_url}{path}"
        # A stable request id makes a retry the *same* request rather than a
        # second one, which is what stops a retried job creation from creating
        # two jobs.
        request_id = idempotency_key or str(uuid.uuid4())
        headers = {
            "x-company-api-key": self.api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "paul-correlation-id": self.correlation_id,
            "paul-request-id": request_id,
        }

        if self.dry_run and method != "GET" and not read_only:
            log.info("DRY-RUN  %s %s  body=%s", method, path, json.dumps(json_body, ensure_ascii=False)[:200])
            return {"_dry_run": True}

        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.calls += 1
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                # Connection-level failure: no response at all. Retry, because a
                # GET or a request id-stamped write is safe to repeat.
                last_error = exc
                if attempt == MAX_ATTEMPTS:
                    raise ConfigError(f"{method} {path} failed after {attempt} attempts: {redact(exc, self.api_key)}")
                self._sleep_before_retry(attempt, None)
                continue

            log.debug("%s %s -> %s (attempt %s)", method, path, response.status_code, attempt)

            if response.ok:
                return self._unwrap(response)

            message, body = self._error_message(response)
            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                self._sleep_before_retry(attempt, response.headers.get("Retry-After"))
                continue

            raise from_status(
                response.status_code,
                redact(message, self.api_key),
                path,
                method,
                request_id=request_id,
                body=body,
                retry_after=response.headers.get("Retry-After"),
            )

        raise ConfigError(f"{method} {path} exhausted retries: {redact(last_error, self.api_key)}")

    def _sleep_before_retry(self, attempt, retry_after):
        """Honour Retry-After when present, else exponential backoff with jitter."""
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        else:
            # Jitter matters: without it, a batch of parallel runs would retry
            # in lockstep and hit the same limit again together.
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
        log.warning("retrying in %.1fs (attempt %s/%s)", delay, attempt, MAX_ATTEMPTS)
        time.sleep(delay)

    @staticmethod
    def _unwrap(response):
        """Return the `data` field of the standard {status, message, data} envelope."""
        if not response.content:
            return None
        try:
            payload = response.json()
        except ValueError:
            return response.text
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    @staticmethod
    def _error_message(response):
        try:
            payload = response.json()
        except ValueError:
            return response.text[:300], None
        if isinstance(payload, dict):
            # Cloudflare answers in a different shape than the API itself.
            return payload.get("message") or payload.get("detail") or payload.get("title") or str(payload), payload
        return str(payload), payload

    # ------------------------------------------------------------ convenience

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def delete(self, path):
        return self.request("DELETE", path)

    def post(self, path, json_body=None, params=None, idempotency_key=None, read_only=False):
        return self.request(
            "POST",
            path,
            params=params,
            json_body=json_body,
            idempotency_key=idempotency_key,
            read_only=read_only,
        )

    # ------------------------------------------------------------- pagination

    def paginate_cursor(self, path, items_key, params=None, page_size=50):
        """Iterate an endpoint that pages with an opaque LastEvaluatedKey cursor.

        Used by templates, agents and applications. The cursor is base64 and
        contains '=', '+' and '/', so it must be URL-encoded when sent back -
        passing it raw is a quiet way to lose results.
        """
        query = dict(params or {})
        query["PerPage"] = page_size
        seen_cursors = set()
        while True:
            data = self.get(path, params=query) or {}
            for item in data.get(items_key) or []:
                yield item
            cursor = data.get("LastEvaluatedKey") or ""
            if not cursor:
                return
            # Defensive: a server that keeps handing back the same cursor would
            # otherwise loop forever.
            if cursor in seen_cursors:
                log.warning("pagination cursor repeated on %s, stopping", path)
                return
            seen_cursors.add(cursor)
            query["LastEvaluatedKey"] = urllib.parse.quote(cursor, safe="")

    def paginate_pages(self, path, items_key, json_body=None, page_size=50):
        """Iterate an endpoint that pages with Page/PerPage and reports TotalPage.

        Used by job search, which is a POST. Note the documented
        GET /recruiting/jobs is deprecated and already returns 404 on the
        deployment, so the search endpoint is the only way to list jobs.
        """
        page = 1
        while True:
            body = dict(json_body or {})
            body.update({"Page": page, "PerPage": page_size})
            data = self.post(path, json_body=body, read_only=True) or {}
            items = data.get(items_key) or []
            for item in items:
                yield item
            total_pages = data.get("TotalPage") or 0
            if page >= total_pages or not items:
                return
            page += 1
