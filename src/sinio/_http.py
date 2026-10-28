"""HTTP transport: retries, backoff, and mapping of statuses to exceptions."""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any, NamedTuple

from .errors import (
    SinioApiError,
    SinioAuthError,
    SinioConnectionError,
    SinioRateLimitError,
    SinioRequestError,
    SinioServerError,
)


class HttpResponse(NamedTuple):
    status: int
    headers: dict[str, str]
    body: str


#: ``(url, payload, headers, timeout) -> HttpResponse``. Swappable in tests, or to
#: plug ``requests``/``httpx`` in a project that already depends on one.
Sender = Callable[[str, bytes, dict[str, str], float], HttpResponse]


def urllib_sender(url: str, payload: bytes, headers: dict[str, str], timeout: float) -> HttpResponse:
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return HttpResponse(
                status=response.status,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=response.read().decode("utf-8", errors="replace"),
            )
    except urllib.error.HTTPError as error:
        # An HTTP error is still a response: the status decides what happens next.
        return HttpResponse(
            status=error.code,
            headers={key.lower(): value for key, value in error.headers.items()},
            body=error.read().decode("utf-8", errors="replace"),
        )
    except (urllib.error.URLError, OSError) as error:
        raise SinioConnectionError(f"Request to {url} failed: {error}") from error


def parse_retry_after(header: str | None, now: float | None = None) -> float | None:
    """``Retry-After`` is either a delay in seconds or an HTTP date. Both are accepted."""
    if header is None:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    reference = time.time() if now is None else now
    return max(0.0, moment.timestamp() - reference)


def _is_retriable(status: int) -> bool:
    """A 429 or a 5xx is retried; ingestion is idempotent so a replay is only ever a duplicate."""
    return status == 429 or status >= 500


class Transport:
    def __init__(
        self,
        base_url: str,
        timeout: float,
        max_retries: int,
        retry_base_delay: float,
        user_agent: str,
        sender: Sender | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._user_agent = user_agent
        self._send = sender or urllib_sender
        self._sleep = sleep or time.sleep

    def post(self, path: str, body: Any, headers: dict[str, str]) -> Any:
        url = f"{self._base_url}{path}"
        payload = json.dumps(body).encode("utf-8")
        request_headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": self._user_agent,
            **headers,
        }
        attempt = 0

        while True:
            try:
                response = self._send(url, payload, dict(request_headers), self._timeout)
            except SinioConnectionError:
                if attempt >= self._max_retries:
                    raise
                self._sleep(self._backoff(attempt, None))
                attempt += 1
                continue

            if 200 <= response.status < 300:
                return _read_json(response.body)

            retry_after = parse_retry_after(response.headers.get("retry-after"))
            if _is_retriable(response.status) and attempt < self._max_retries:
                self._sleep(self._backoff(attempt, retry_after))
                attempt += 1
                continue

            raise _to_api_error(response, retry_after)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """Exponential backoff with full jitter, unless the server said how long to wait."""
        if retry_after is not None:
            return retry_after
        ceiling = min(self._retry_base_delay * 2.0**attempt, 30.0)
        return random.random() * ceiling  # noqa: S311 -- jitter, not a security decision


def _read_json(body: str) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return body


def _to_api_error(response: HttpResponse, retry_after: float | None) -> SinioApiError:
    body = _read_json(response.body)
    code = body.get("code") if isinstance(body, dict) and isinstance(body.get("code"), str) else None
    message = f"{response.status}: {_extract_message(body) or 'no message'}"

    if response.status in (401, 403):
        return SinioAuthError(message, response.status, body, code)
    if response.status == 429:
        return SinioRateLimitError(message, response.status, body, retry_after)
    if response.status >= 500:
        return SinioServerError(message, response.status, body, code)
    return SinioRequestError(message, response.status, body, code)


def _extract_message(body: Any) -> str | None:
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return None
    message = body.get("message")
    # NestJS's ValidationPipe returns an array of messages, one per failed rule.
    if isinstance(message, list):
        return "; ".join(str(item) for item in message)
    return message if isinstance(message, str) else None
