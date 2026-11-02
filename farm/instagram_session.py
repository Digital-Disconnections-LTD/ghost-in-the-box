"""One durable Instagram login shared with the linked Threads account.

Reads may retry after a rejected saved session. A refresh uses the same account
lock and atomic settings writer as Threads, so both clients see the newest
rotating session on their next request.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from farm import locking as fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Callable, TypeVar

from farm.accounts import AccountRegistry
from farm.session_files import is_private, protect_descriptor
from farm import threads_auth

T = TypeVar("T")
_AUTH_ERRORS = {"LoginRequired", "ClientLoginRequired"}
_RETRY_DELAY = timedelta(minutes=30)


_LOGIN = "ghost-in-the-box login start --platform instagram --account {account}"
_MESSAGES = {
    "session_unavailable": ("No usable saved Instagram session.", _LOGIN),
    "account_mismatch": ("The saved Instagram session belongs to a different account.", _LOGIN),
    "auth_cooldown": ("Instagram rejected the saved session recently; automatic refresh is paused "
                      "for 30 minutes.", _LOGIN),
    "session_save_failed": ("The refreshed Instagram session could not be saved.",
                            "ghost-in-the-box doctor"),
    "checkpoint_required": ("Instagram wants a security check in the app or browser.", _LOGIN),
}


class InstagramSessionError(Exception):
    """A fixed local status; provider responses and credentials stay private.

    str(error) stays the stable status code; message and hint explain it.
    """

    def __init__(self, code: str, *, message: str | None = None, hint: str | None = None,
                 account: str | None = None):
        super().__init__(code)
        self.code = code
        default_message, default_hint = _MESSAGES.get(
            code, ("Instagram did not accept the saved session.", _LOGIN))
        self.message = message or default_message
        hint = hint or default_hint
        self.hint = hint.format(account=account or "YOUR_HANDLE") if "{account}" in hint else hint

    def to_error(self) -> dict:
        return {"code": self.code, "message": self.message, "hint": self.hint}


def _new_client():
    from instagrapi import Client
    client = Client()
    client.delay_range = [2, 5]
    return client


def _session_path(account: str, registry: AccountRegistry) -> Path:
    entry = registry.status("instagram", account)
    unavailable = lambda message, hint=None: InstagramSessionError(
        "session_unavailable", message=message, hint=hint, account=account)
    if not entry:
        raise unavailable(f"No Instagram session is registered for {account} in {registry.path}.")
    path = Path(entry["session_file"])
    if entry["kind"] != "instagrapi_settings":
        raise unavailable(f"The Instagram registration for {account} is a {entry['kind']}, "
                          "not instagrapi settings.")
    if not entry["session_exists"]:
        raise unavailable(f"The registered Instagram session file is missing: {path}")
    if not entry["session_private"]:
        raise unavailable(f"The Instagram session file is readable by other users: {path}",
                          f"chmod 600 '{path}'")
    if path.is_symlink():
        raise unavailable(f"The Instagram session file must not be a symlink: {path}")
    return path


def _connect(account: str, path: Path, client_factory: Callable):
    client = client_factory()
    client.load_settings(path)
    info = client.account_info()
    if (not client.user_id or str(info.pk) != str(client.user_id) or
            info.username.casefold() != account.casefold()):
        raise InstagramSessionError("account_mismatch", account=account)
    return client


@contextmanager
def _recovery_lock(path: Path):
    with fcntl.exclusive_lock(path.with_name(path.name + ".recovery.lock")):
        yield


def _cooldown(path: Path) -> Path:
    return path.with_name(path.name + ".recovery.json")


def _read_retry_after(path: Path) -> datetime | None:
    try:
        file = _cooldown(path)
        if not is_private(file):
            return datetime.now(timezone.utc) + _RETRY_DELAY
        value = json.loads(file.read_text(encoding="utf-8"))["retry_after"]
        return datetime.fromisoformat(value)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_retry_after(path: Path, when: datetime) -> None:
    target = _cooldown(path)
    fd, temporary = tempfile.mkstemp(prefix=".ig-recovery-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            protect_descriptor(handle.fileno(), Path(temporary))
            json.dump({"retry_after": when.isoformat()}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _recover(account: str, path: Path, original_mtime_ns: int,
             client_factory: Callable, refresh: Callable[[], None]) -> None:
    with _recovery_lock(path):
        # Another caller may have refreshed while this read was in flight.
        if path.stat().st_mtime_ns != original_mtime_ns:
            try:
                _connect(account, path, client_factory)
                return
            except Exception as exc:
                if type(exc).__name__ not in _AUTH_ERRORS:
                    raise
        retry_after = _read_retry_after(path)
        if retry_after and retry_after > datetime.now(timezone.utc):
            raise InstagramSessionError(
                "auth_cooldown", account=account,
                message="Instagram rejected the saved session recently; automatic refresh resumes after "
                        f"{retry_after.isoformat()}.")
        try:
            refresh()
            _connect(account, path, client_factory)
        except threads_auth.ThreadsAuthError as exc:
            if not exc.needs_code:
                _write_retry_after(path, datetime.now(timezone.utc) + _RETRY_DELAY)
            raise InstagramSessionError(exc.status, account=account) from None
        except InstagramSessionError:
            raise
        except Exception as exc:
            _write_retry_after(path, datetime.now(timezone.utc) + _RETRY_DELAY)
            raise InstagramSessionError(type(exc).__name__, account=account) from None
        _cooldown(path).unlink(missing_ok=True)


def _persist_rotated_settings(client, path: Path, loaded_mtime_ns: int,
                              account: str) -> None:
    """Keep session cookies updated without replacing a newer login."""
    if not hasattr(client, "get_settings"):
        return
    settings = client.get_settings()
    if not isinstance(settings, dict):
        raise InstagramSessionError("session_save_failed", account=account)
    try:
        with fcntl.exclusive_lock(path.with_name(path.name + ".lock")):
            _replace_rotated_settings(client, settings, path, loaded_mtime_ns, account)
    except (OSError, ValueError, TypeError, threads_auth.ThreadsAuthError):
        raise InstagramSessionError("session_save_failed", account=account) from None


def _replace_rotated_settings(client, settings: dict, path: Path, loaded_mtime_ns: int,
                              account: str) -> None:
    if path.stat().st_mtime_ns != loaded_mtime_ns:
        return
    if json.loads(path.read_text(encoding="utf-8")) == settings:
        return
    threads_auth._save_ig_settings(settings, path)
    # Keep legacy Instagram-derived bearer files current, while
    # preserving credentials minted by the native Threads login.
    authorization = str(getattr(client, "authorization", "") or "")
    match = threads_auth._TOKEN.fullmatch(authorization)
    if not match:
        return
    try:
        old = threads_auth.load_token(account)
    except threads_auth.ThreadsAuthError:
        old = None
    auth_data = settings.get("authorization_data") or {}
    device_id = str((settings.get("uuids") or {}).get("android_device_id") or "")
    user_id = str(auth_data.get("ds_user_id") or "")
    if (old and getattr(old, "source", threads_auth.INSTAGRAM_TOKEN_SOURCE) ==
            threads_auth.INSTAGRAM_TOKEN_SOURCE and
            old.token != match.group(1) and
            old.user_id == user_id and old.device_id == device_id):
        threads_auth.save_token(threads_auth.ThreadsToken(
            token=match.group(1), user_id=user_id,
            device_id=device_id, username=account,
            issued_at=datetime.now(timezone.utc).isoformat()))


def read_with_session(account: str, operation: Callable[[object], T], *,
                      registry: AccountRegistry | None = None,
                      client_factory: Callable | None = None,
                      refresh: Callable[[], None] | None = None) -> T:
    """Run an idempotent Instagram read, refreshing a rejected session once."""
    registry = registry or AccountRegistry()
    client_factory = client_factory or _new_client
    path = _session_path(account, registry)
    original_mtime_ns = path.stat().st_mtime_ns
    try:
        client = _connect(account, path, client_factory)
        result = operation(client)
        _persist_rotated_settings(client, path, original_mtime_ns, account)
        return result
    except Exception as exc:
        if type(exc).__name__ not in _AUTH_ERRORS:
            raise

    if refresh is None:
        def refresh() -> None:
            inputs = threads_auth.load_inputs()
            if (inputs.username.casefold() != account.casefold() or
                    inputs.session_path.resolve() != path.resolve()):
                raise InstagramSessionError("account_mismatch", account=account)
            threads_auth.login(inputs, force_refresh=True)

    _recover(account, path, original_mtime_ns, client_factory, refresh)
    loaded_mtime_ns = path.stat().st_mtime_ns
    client = _connect(account, path, client_factory)
    result = operation(client)
    _persist_rotated_settings(client, path, loaded_mtime_ns, account)
    return result
