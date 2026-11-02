"""Shared contract for locally owned social platform adapters.

Every publishing adapter raises these exceptions so one router can decide
whether a claimed outbox job is released, failed, or held as uncertain:

- ``AuthUnavailable``: saved auth could not be used before any public write.
  The job goes back to the queue unchanged.
- ``DependencyUnavailable``: an optional package or browser is missing. The
  job goes back to the queue and the user gets an install hint.
- ``PreSendFailure``: nothing was published and the job cannot send as is.
- ``PublishRejected``: the provider definitely refused the write (a 4xx
  answer), so nothing was published.
- ``PublishUncertain``: a write may have reached the provider (timeout, 5xx,
  unreadable answer). The job must be reconciled, never resent automatically.
"""

from __future__ import annotations

import re
from typing import Callable, Protocol, runtime_checkable

REASON_LIMIT = 160
_REASON_TEXT = re.compile(r"[A-Za-z0-9 .,:;()/'_\-]+")
# A long unbroken run with digits looks like a token, cookie or ID, not prose.
_SECRET_LIKE = re.compile(r"(?=[A-Za-z0-9+/=_\-]*\d)[A-Za-z0-9+/=_\-]{24,}")


class PreSendFailure(Exception):
    """Nothing was published; the job can be marked failed without reconciliation."""


class AuthUnavailable(PreSendFailure):
    """Saved account auth could not be used before any write; keep the job queued."""


class DependencyUnavailable(AuthUnavailable):
    """An optional dependency is missing before any write; keep the job queued.

    I subclass AuthUnavailable (itself a PreSendFailure) so older callers that
    release the job on auth problems keep it queued instead of failing it.
    """

    def __init__(self, message: str, *, install_hint: str | None = None):
        super().__init__(message)
        self.install_hint = install_hint


class PublishRejected(PreSendFailure):
    """The provider definitely refused the write; nothing was published."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PublishUncertain(Exception):
    """A write may have reached the provider; reconcile before any retry."""


class ReadFailure(Exception):
    """A bounded provider read failed; response bodies stay private."""


@runtime_checkable
class PublishingAdapter(Protocol):
    def publish(self, job: dict, *, before_send: Callable[[], None] | None = None) -> dict:
        """Send one claimed job once and return ``{"provider_id", "permalink"}``.

        ``before_send`` runs after every local check and immediately before the
        first byte of a public write; raising from it must leave nothing sent.
        """
        ...


@runtime_checkable
class ReconcilingAdapter(Protocol):
    def reconcile(self, job: dict) -> dict:
        """Read provider state for an uncertain job without sending anything."""
        ...


@runtime_checkable
class IdentityAdapter(Protocol):
    def me(self, account: str) -> dict:
        """Return the live provider identity for the saved account session."""
        ...


@runtime_checkable
class SocialAdapter(PublishingAdapter, ReconcilingAdapter, IdentityAdapter, Protocol):
    """An adapter that can publish, reconcile, and confirm identity."""


def safe_reason(exc: BaseException, *, limit: int = REASON_LIMIT) -> str:
    """Return a short reason for exc that cannot carry a response body or secret.

    Adapter messages here are fixed phrases, codes and class names. Anything
    else (quotes, braces, '=', '@', long token-like runs, newlines) is dropped
    in favour of the exception class name and any HTTP status code.
    """
    text = " ".join(str(exc).split())
    if text and len(text) <= limit and _REASON_TEXT.fullmatch(text) and not _SECRET_LIKE.search(text):
        return text
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__} (HTTP {status})" if isinstance(status, int) else type(exc).__name__


def definite_rejection(status_code: int | None) -> bool:
    """Return True when an HTTP answer proves the write was not applied.

    408 (request timeout) and 409 (conflict, possibly an existing post) do not
    prove that, so they stay uncertain like 5xx answers.
    """
    return status_code is not None and 400 <= status_code < 500 and status_code not in (408, 409)


def request_never_sent(exc: BaseException) -> bool:
    """Return True when a requests error proves no request bytes left the machine."""
    try:
        import requests
        from urllib3.exceptions import NewConnectionError
    except ImportError:
        return False
    local = (requests.exceptions.ConnectTimeout, requests.exceptions.InvalidURL,
             requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema,
             requests.exceptions.InvalidHeader, requests.exceptions.InvalidJSONError)
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, local) or isinstance(current, NewConnectionError):
            return True
        if isinstance(current, requests.exceptions.ConnectionError):
            reason = current.args[0] if current.args else None
            reason = getattr(reason, "reason", reason)
            return isinstance(reason, NewConnectionError)
        current = current.__cause__ or current.__context__
    return False
