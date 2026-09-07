"""Typed errors for the Paul's Job API.

The API answers with a consistent envelope ({status, message, data}), so the
status code is the reliable signal - not the message text. Two different auth
middlewares are in play and they word the same condition differently
("unauthorized, missing authorization header" vs "Unauthorized, missing
authorization header or API key"), which is exactly why nothing here matches on
strings.
"""


class PaulsJobError(Exception):
    """Base class for every error raised by this client."""


class ConfigError(PaulsJobError):
    """Local misconfiguration - missing key, unreadable input file."""


class ApiError(PaulsJobError):
    """A non-2xx response from the API."""

    def __init__(self, status, message, path, method="GET", request_id=None, body=None):
        self.status = status
        self.message = message
        self.path = path
        self.method = method
        self.request_id = request_id
        self.body = body
        trace = f" [request-id {request_id}]" if request_id else ""
        super().__init__(f"{method} {path} -> {status}: {message}{trace}")


class AuthError(ApiError):
    """401 - the key is missing, malformed or rejected."""


class PermissionError_(ApiError):
    """403 - authenticated, but this key may not do that.

    Seen in practice on /company/api-keys/available-scopes, which requires a
    bearer token rather than a company API key. Distinguishing this from 401
    matters: retrying or re-issuing the key will not help here.
    """


class NotFoundError(ApiError):
    """404 - the resource does not exist. Sometimes an expected answer.

    A fresh account legitimately returns 404 for the company default pipeline
    template, so callers treat this as "absent", not "broken".
    """


class ValidationError(ApiError):
    """400/422 - the payload was understood but refused.

    The onboarding flow relies on this: POST /steps/init answers 422 when no
    pipeline template fits the job and no company default exists.
    """


class RateLimitError(ApiError):
    """429 - too many requests. Carries Retry-After when the server sent one."""

    def __init__(self, *args, retry_after=None, **kwargs):
        self.retry_after = retry_after
        super().__init__(*args, **kwargs)


class ServerError(ApiError):
    """5xx - the API failed. Safe to retry idempotent calls."""


def from_status(status, message, path, method, request_id=None, body=None, retry_after=None):
    """Map an HTTP status onto the matching exception type."""
    common = dict(path=path, method=method, request_id=request_id, body=body)
    if status == 401:
        return AuthError(status, message, **common)
    if status == 403:
        return PermissionError_(status, message, **common)
    if status == 404:
        return NotFoundError(status, message, **common)
    if status in (400, 422):
        return ValidationError(status, message, **common)
    if status == 429:
        return RateLimitError(status, message, retry_after=retry_after, **common)
    if status >= 500:
        return ServerError(status, message, **common)
    return ApiError(status, message, **common)
