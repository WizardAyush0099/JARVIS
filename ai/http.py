"""Small stdlib-only HTTP helper.

Deliberately built on ``urllib`` instead of ``requests``: it keeps JARVIS's
dependency list short (fewer things that can fail to install on Raspberry Pi)
and gives us one place to enforce timeouts, retry policy and response-size caps
so a slow API can never wedge the assistant or exhaust the Pi's RAM.
"""

from __future__ import annotations

import json as jsonlib
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, Optional, Tuple

DEFAULT_USER_AGENT = "JARVIS/2.0 (Raspberry Pi personal assistant)"
DEFAULT_MAX_BYTES = 6 * 1024 * 1024  # 6 MiB is plenty for JSON/text


class HttpError(Exception):
    """Any failed HTTP call, with enough detail to classify the failure."""

    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        body: str = "",
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body
        self.url = url

    @property
    def retryable(self) -> bool:
        if self.status is None:
            return True  # network level: always worth one more try
        return self.status in (408, 409, 425, 429) or self.status >= 500

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        base = self.message
        if self.status:
            base = f"HTTP {self.status}: {base}"
        return base


def _request(
    url: str,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 30.0,
    retries: int = 2,
    backoff: float = 0.7,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Tuple[bytes, Dict[str, str]]:
    request_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Encoding": "identity",  # avoid transparent gzip on tiny devices
        "Accept": "*/*",
    }
    if headers:
        request_headers.update({k: v for k, v in headers.items() if v is not None})

    last_error: Optional[HttpError] = None
    attempts = max(1, int(retries) + 1)
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url, data=data, headers=request_headers, method=method.upper()
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise HttpError(
                        f"response larger than {max_bytes} bytes", url=url
                    )
                return raw, dict(response.headers)
        except urllib.error.HTTPError as exc:  # server answered with an error
            try:
                body = exc.read(4096).decode("utf-8", "replace")
            except Exception:
                body = ""
            error = HttpError(
                f"request failed ({exc.reason})",
                status=getattr(exc, "code", None),
                body=body,
                url=url,
            )
            if not error.retryable:
                raise error from exc
            last_error = error
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            last_error = HttpError(f"network error: {reason}", url=url)
        except (socket.timeout, TimeoutError):
            last_error = HttpError("request timed out", url=url)
        except HttpError as exc:
            if not exc.retryable:
                raise
            last_error = exc

        if attempt < attempts - 1:
            _sleep(backoff * (2**attempt))

    raise last_error or HttpError("request failed", url=url)


def _sleep(seconds: float) -> None:
    import time

    try:
        time.sleep(max(0.0, float(seconds)))
    except Exception:  # pragma: no cover - defensive
        pass


# --------------------------------------------------------------------------- #
# convenience wrappers
# --------------------------------------------------------------------------- #
def request_bytes(
    url: str,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 30.0,
    retries: int = 2,
    backoff: float = 0.7,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> bytes:
    raw, _ = _request(url, method, data, headers, timeout, retries, backoff, max_bytes)
    return raw


def request_text(
    url: str,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 30.0,
    retries: int = 2,
    backoff: float = 0.7,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    return request_bytes(url, method, data, headers, timeout, retries, backoff, max_bytes).decode(
        "utf-8", "replace"
    )


def request_json(
    url: str,
    payload: Optional[Any] = None,
    method: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 30.0,
    retries: int = 2,
    backoff: float = 0.7,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Any:
    """Send (optionally) a JSON body and decode a JSON response."""
    body: Optional[bytes] = None
    request_headers: Dict[str, str] = {"Accept": "application/json"}
    if payload is not None:
        body = jsonlib.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(dict(headers))

    if method is None:
        method = "POST" if body is not None else "GET"

    text = request_text(
        url, method=method, data=body, headers=request_headers,
        timeout=timeout, retries=retries, backoff=backoff, max_bytes=max_bytes,
    )
    if not text.strip():
        return None
    try:
        return jsonlib.loads(text)
    except ValueError as exc:
        raise HttpError(
            "response was not valid JSON", body=text[:500], url=url
        ) from exc


def with_query(url: str, params: Optional[Mapping[str, Any]]) -> str:
    if not params:
        return url
    clean = {k: v for k, v in params.items() if v is not None}
    if not clean:
        return url
    joiner = "&" if urllib.parse.urlparse(url).query else "?"
    return f"{url}{joiner}{urllib.parse.urlencode(clean)}"


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_USER_AGENT",
    "HttpError",
    "request_bytes",
    "request_json",
    "request_text",
    "with_query",
]
