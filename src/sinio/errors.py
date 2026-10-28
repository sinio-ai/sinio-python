"""Exceptions raised by the SDK."""

from __future__ import annotations

from typing import Any


class SinioError(Exception):
    """Base class for every error the SDK raises."""


class SinioConnectionError(SinioError):
    """The request never got a usable HTTP response (DNS, TCP, TLS, timeout)."""


class SinioApiError(SinioError):
    """A 4xx or 5xx response."""

    def __init__(
        self,
        message: str,
        status: int,
        body: Any = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.code = code


class SinioAuthError(SinioApiError):
    """401 / 403 -- invalid, revoked, or insufficiently scoped key. Never replay."""


class SinioRequestError(SinioApiError):
    """400 / 404 / 413 -- the request itself is wrong. Fix it before replaying."""


class SinioRateLimitError(SinioApiError):
    """429 -- quota exceeded. ``retry_after_seconds`` mirrors the ``Retry-After`` header."""

    def __init__(
        self,
        message: str,
        status: int,
        body: Any = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, status, body)
        self.retry_after_seconds = retry_after_seconds


class SinioServerError(SinioApiError):
    """5xx -- temporary. Safe to replay verbatim: ingestion is idempotent."""


class SinioValidationError(SinioError):
    """Raised before any network call, when the input cannot be a valid request."""
