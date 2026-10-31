"""Mint and save a Threads mobile bearer using the linked Instagram login.

The token shape follows teslashibe/threads-go's auth.go (MIT, Copyright 2026
Tesla Shibe). The current CAA/Bloks handshake is handled by installed
instagrapi; provider bodies, exceptions, and secrets never leave this module.
"""

from __future__ import annotations

from farm.config import STATE_ROOT



from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import tempfile
DEFAULT_CREDS = Path.home() / ".batbox/social-farm-creds/ig.json"
DEFAULT_IG_DIR = STATE_ROOT / 'ig'
DEFAULT_AUTH_DIR = STATE_ROOT / 'threads-auth'
INSTAGRAM_TOKEN_SOURCE = "instagram_session"
THREADS_TOKEN_SOURCE = "threads_bloks"
_ACCOUNT = re.compile(r"[A-Za-z0-9._]+\Z")
_DEVICE = re.compile(r"android-[0-9a-fA-F]{13,32}\Z")
_TOKEN = re.compile(r"Bearer (IGT:2:[A-Za-z0-9:_~+./=-]{24,})\Z")


class ThreadsAuthError(Exception):
    """A public, fixed error code; never includes a provider response."""

    def __init__(self, status: str, *, needs_code: bool = False):
        self.status = status
        self.needs_code = needs_code
        super().__init__(status)


@dataclass(frozen=True)
class LoginInputs:
    username: str
    password: str
    device_id: str
    user_id: str
    session_path: Path


@dataclass(frozen=True)
class ThreadsToken:
    token: str
    user_id: str
    device_id: str
    username: str
    issued_at: str
    # Old bearer files came from instagrapi and did not record their origin.
    source: str = INSTAGRAM_TOKEN_SOURCE

    def as_dict(self) -> dict[str, str]:
        return {"token": self.token, "user_id": self.user_id,
                "device_id": self.device_id, "username": self.username,
                "issued_at": self.issued_at, "source": self.source}


def _private_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o077 == 0


def load_inputs(creds_path: Path = DEFAULT_CREDS,
                ig_dir: Path = DEFAULT_IG_DIR) -> LoginInputs:
    """Read existing IG credentials and stable device/user IDs, without output."""
    try:
        if not _private_file(creds_path):
            raise ValueError("credentials permissions")
        credentials = json.loads(creds_path.read_text(encoding="utf-8"))
        username = credentials["username"]
        password = credentials["password"]
        if not isinstance(username, str) or not _ACCOUNT.fullmatch(username):
            raise ValueError("invalid account name")
        if not isinstance(password, str) or not password:
            raise ValueError("missing password")
        session_path = ig_dir / f"{username}.json"
        if not _private_file(session_path):
            raise ValueError("session permissions")
        session = json.loads(session_path.read_text(encoding="utf-8"))
        device_id = session["uuids"]["android_device_id"]
        user_id = str(session["authorization_data"]["ds_user_id"])
        if not isinstance(device_id, str) or not _DEVICE.fullmatch(device_id):
            raise ValueError("device ID")
        if not user_id.isdecimal():
            raise ValueError("user ID")
        return LoginInputs(username, password, device_id, user_id, session_path)
    except (OSError, ValueError, TypeError, KeyError):
        raise ThreadsAuthError("local_inputs_invalid") from None


@contextmanager
def _quiet_provider():
    """Contain library diagnostics that may include credentials or responses."""
    prior = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with redirect_stdout(sink), redirect_stderr(sink):
                yield
    finally:
        logging.disable(prior)


def _new_client(client_factory):
    if client_factory is not None:
        return client_factory()
    try:
        from instagrapi import Client
    except ImportError:
        raise ThreadsAuthError("auth_backend_unavailable") from None
    return Client()


def _login_failure(exc: Exception, verification_code: str) -> ThreadsAuthError:
    """Map provider exception types to fixed, non-sensitive statuses."""
    kind = type(exc).__name__
    if kind == "TwoFactorRequired":
        return ThreadsAuthError("two_factor_required", needs_code=True)
    if kind in {"ChallengeRequired", "CheckpointRequired"}:
        return ThreadsAuthError("checkpoint_required")
    if kind in {"ChallengeError", "ChallengeCodeInvalid"} and verification_code:
        return ThreadsAuthError("code_rejected", needs_code=True)
    if kind in {"PleaseWaitFewMinutes", "ClientThrottledError", "RateLimitError"}:
        return ThreadsAuthError("rate_limited")
    return ThreadsAuthError("login_rejected")


def login(inputs: LoginInputs, *, verification_code: str = "",
          force_refresh: bool = False, client_factory=None,
          auth_dir: Path = DEFAULT_AUTH_DIR) -> ThreadsToken:
    """Reuse one IG session, or refresh IG and Threads auth together under lock."""
    if verification_code and (not verification_code.isascii() or
                              not verification_code.isdecimal() or
                              len(verification_code) not in (6, 8)):
        raise ThreadsAuthError("invalid_code_format")
    lock_path = inputs.session_path.with_name(inputs.session_path.name + ".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            with _quiet_provider():
                client = _new_client(client_factory)
                try:
                    client.load_settings(inputs.session_path, override_app_version=True)
                except Exception:
                    raise ThreadsAuthError("local_inputs_invalid") from None
                if str(client.android_device_id) != inputs.device_id:
                    raise ThreadsAuthError("identity_mismatch")

                refresh = bool(verification_code) or force_refresh
                username = inputs.username
                if not refresh:
                    try:
                        info = client.account_info()
                        username = str(info.username)
                        if username.casefold() != inputs.username.casefold():
                            raise ThreadsAuthError("identity_mismatch")
                        if not _TOKEN.fullmatch(str(client.authorization or "")):
                            refresh = True
                    except ThreadsAuthError:
                        raise
                    except Exception as exc:
                        if type(exc).__name__ in ("LoginRequired", "ClientLoginRequired"):
                            refresh = True
                        else:
                            raise

                if refresh:
                    logged_in = client.login(inputs.username, inputs.password,
                                             relogin=True,
                                             verification_code=verification_code)
                    if not logged_in:
                        raise ThreadsAuthError("login_rejected")
                match = _TOKEN.fullmatch(str(client.authorization or ""))
                if not match:
                    raise ThreadsAuthError("token_missing")
                authenticated_id = str((client.authorization_data or {}).get("ds_user_id") or "")
                if (authenticated_id != inputs.user_id or
                        str(client.android_device_id) != inputs.device_id or
                        str(getattr(client, "user_id", authenticated_id)) != inputs.user_id):
                    raise ThreadsAuthError("identity_mismatch")
                token = ThreadsToken(match.group(1), authenticated_id, inputs.device_id,
                                     username, datetime.now(timezone.utc).isoformat())
                settings = client.get_settings()
            if (str((settings.get("authorization_data") or {}).get("ds_user_id")) != inputs.user_id or
                    str((settings.get("uuids") or {}).get("android_device_id")) != inputs.device_id):
                raise ThreadsAuthError("identity_mismatch")
            _save_ig_settings(settings, inputs.session_path)
            save_token(token, auth_dir)
            return token
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except ThreadsAuthError:
        raise
    except Exception as exc:
        raise _login_failure(exc, verification_code) from None
    finally:
        os.close(lock_fd)


def _save_ig_settings(settings: dict, destination: Path) -> None:
    """Atomically replace the linked IG settings before publishing its bearer."""
    try:
        fd, temporary = tempfile.mkstemp(prefix=".ig-session-", dir=destination.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(settings, handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError:
        raise ThreadsAuthError("session_save_failed") from None


def save_token(token: ThreadsToken, auth_dir: Path = DEFAULT_AUTH_DIR) -> Path:
    """Atomically save a bearer separate from the Instagram settings file."""
    if (not _ACCOUNT.fullmatch(token.username) or
            token.source not in (INSTAGRAM_TOKEN_SOURCE, THREADS_TOKEN_SOURCE)):
        raise ThreadsAuthError("local_inputs_invalid")
    try:
        if auth_dir.is_symlink():
            raise ThreadsAuthError("token_save_failed")
        auth_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(auth_dir, 0o700)
        destination = auth_dir / f"{token.username}.json"
        if token.source == INSTAGRAM_TOKEN_SOURCE:
            try:
                previous = load_token(token.username, auth_dir)
            except ThreadsAuthError:
                previous = None
            # Instagram recovery and native Threads login use the same account
            # lock. A later IG refresh must preserve a Threads-app bearer.
            if previous and previous.source == THREADS_TOKEN_SOURCE:
                return destination
        fd, temporary = tempfile.mkstemp(prefix=".threads-auth-", dir=auth_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(token.as_dict(), handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return destination
    except OSError:
        raise ThreadsAuthError("token_save_failed") from None


def load_token(username: str, auth_dir: Path = DEFAULT_AUTH_DIR) -> ThreadsToken:
    """Read only a private bearer file for the requested account."""
    try:
        if not _ACCOUNT.fullmatch(username):
            raise ValueError("account")
        path = auth_dir / f"{username}.json"
        if not _private_file(path):
            raise ValueError("permissions")
        data = json.loads(path.read_text(encoding="utf-8"))
        token = ThreadsToken(**data)
        if (token.username.casefold() != username.casefold() or
                token.source not in (INSTAGRAM_TOKEN_SOURCE, THREADS_TOKEN_SOURCE) or
                not token.user_id.isdecimal() or
                not _DEVICE.fullmatch(token.device_id) or
                not re.fullmatch(r"IGT:2:[A-Za-z0-9:_~+./=-]{24,}", token.token)):
            raise ValueError("token identity")
        return token
    except (OSError, TypeError, KeyError, ValueError):
        raise ThreadsAuthError("local_token_invalid") from None
