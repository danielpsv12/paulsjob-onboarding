"""Read-only reconnaissance against the Paul's Job API.

Throwaway script: it only issues GETs and prints response shapes so we know what
this account actually contains before building anything. Never prints the key.
"""
import json
import os
import uuid
import sys
import urllib.error
import urllib.request

try:
    # Verify through the OS trust store. Needed on machines where antivirus or a
    # corporate proxy inspects TLS: their injected root CA is trusted by Windows
    # but rejected by OpenSSL 3.x. Verification stays enabled either way.
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

# (label, method, path, body) - GET everywhere except the job search, which is a
# read-only POST. The documented GET /recruiting/jobs is deprecated AND already
# removed from the deployment (404), so we do not use it.
ENDPOINTS = [
    ("jobs (search)", "POST", "/recruiting/jobs/search-jobs", {"PerPage": 5}),
    ("company default pipeline template", "GET", "/recruiting/job-step-templates/pipelines/default", None),
    ("company pipeline templates", "GET", "/recruiting/job-step-templates/pipelines", None),
    ("platform template library", "GET", "/recruiting/template-library/pipelines", None),
    ("library default", "GET", "/recruiting/template-library/pipelines/default", None),
    ("job categories", "GET", "/recruiting/jobs/categories", None),
    ("career levels", "GET", "/recruiting/jobs/career-levels", None),
    ("employment types", "GET", "/recruiting/jobs/employment-types", None),
    ("working hours", "GET", "/recruiting/jobs/working-hours", None),
    ("company agents", "GET", "/company/agents", None),
    ("applications", "GET", "/recruiting/applications?PerPage=5", None),
    ("job step categories", "GET", "/recruiting/job-step-categories", None),
    ("company profile", "GET", "/company/profile", None),
    ("persons", "POST", "/company/person/list", {"PerPage": 5}),
    ("credits", "GET", "/company/credits", None),
    ("api key scopes", "GET", "/company/api-keys/available-scopes", None),
]


def load_env(path=".env"):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def shape(value, depth=0):
    """Describe a JSON value without dumping customer data."""
    pad = "  " * depth
    if isinstance(value, dict):
        keys = list(value.keys())
        return f"object({len(keys)}) keys={keys[:14]}"
    if isinstance(value, list):
        if not value:
            return "array(0) [EMPTY]"
        return f"array({len(value)}) first={shape(value[0], depth + 1)}"
    if isinstance(value, str):
        return f"str({len(value)})"
    return type(value).__name__


def main():
    env = load_env()
    key = env.get("PAULSJOB_API_KEY") or os.environ.get("PAULSJOB_API_KEY", "")
    base = (env.get("PAULSJOB_BASE_URL") or "https://api.paulsjob.ai/dev").rstrip("/")
    if not key:
        sys.exit("PAULSJOB_API_KEY is empty. Put it in .env (it is gitignored) and re-run.")
    print(f"key loaded: {len(key)} chars, ends with ...{key[-4:]}")
    correlation_id = str(uuid.uuid4())
    print(f"base: {base}/v1")
    print(f"correlation id: {correlation_id}\n")

    for label, method, path, body in ENDPOINTS:
        url = f"{base}/v1{path}"
        headers = {
            "x-company-api-key": key,
            "Accept": "application/json",
            # Cloudflare rejects the default Python-urllib agent (error 1010),
            # so the client identifies itself explicitly.
            "User-Agent": "paulsjob-onboarding-probe/0.1",
            # Tracing headers the API description asks clients to send.
            "paul-correlation-id": correlation_id,
            "paul-request-id": str(uuid.uuid4()),
        }
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")[:300]
            print(f"[{exc.code}] {label:34s} {path}\n      {raw}\n")
            continue
        except Exception as exc:  # noqa: BLE001 - recon script, report and move on
            print(f"[ERR] {label:34s} {path}\n      {type(exc).__name__}: {exc}\n")
            continue

        data = body.get("data", body) if isinstance(body, dict) else body
        print(f"[{status}] {label:34s} {method} {path}")
        print(f"      envelope: {shape(body)}")
        print(f"      data:     {shape(data)}")
        if isinstance(data, list) and data:
            print(f"      sample:   {json.dumps(data[0], ensure_ascii=False)[:400]}")
        elif isinstance(data, dict):
            print(f"      sample:   {json.dumps(data, ensure_ascii=False)[:400]}")
        print()


if __name__ == "__main__":
    main()
