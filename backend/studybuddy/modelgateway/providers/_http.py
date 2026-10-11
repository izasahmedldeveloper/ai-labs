"""Shared HTTP status -> ProviderError mapping. Messages carry the status code only, never bodies or keys."""
from ..types import ProviderError


def status_error(status: int, headers) -> ProviderError:
    if status == 429:
        try:
            retry = float(headers.get("retry-after")) if headers.get("retry-after") else None
        except ValueError:
            retry = None
        return ProviderError("rate_limit", "http 429", retry)
    if status in (401, 403):
        return ProviderError("auth", f"http {status}")
    if status in (400, 404, 413, 422):
        return ProviderError("bad_request", f"http {status}")
    return ProviderError("server", f"http {status}")
