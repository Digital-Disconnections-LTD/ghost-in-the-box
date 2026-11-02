"""Mint and save a Threads mobile bearer using the linked Instagram login.

The token shape follows teslashibe/threads-go's auth.go (MIT, Copyright 2026
Tesla Shibe). The current CAA/Bloks handshake is handled by installed
instagrapi; provider bodies, exceptions, and secrets never leave this module.
"""

from __future__ import annotations

from farm.config import credentials_path, instagram_sessions_dir, threads_auth_dir
from farm.session_files import is_private, protect_descriptor



from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from farm import locking as fcntl
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
DEFAULT_CREDS = credentials_path()
DEFAULT_IG_DIR = instagram_sessions_dir()
DEFAULT_AUTH_DIR = threads_auth_dir()
INSTAGRAM_TOKEN_SOURCE = "instagram_session"
THREADS_TOKEN_SOURCE = "threads_bloks"
_ACCOUNT = re.compile(r"[A-Za-z0-9._]+\Z")
_DEVICE = re.compile(r"android-[0-9a-fA-F]{13,32}\Z")
_TOKEN = re.compile(r"Bearer (IGT:2:[A-Za-z0-9:_~+./=-]{24,})\Z")
IDENTITY_URL = "https://i.instagram.com/api/v1/accounts/current_user/"
THREADS_APP_ID = "238260118697367"


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
    return path.is_file() and not path.is_symlink() and is_private(path)


def _restrict(fd: int, path: str) -> None:
    """Make a new secret file owner-only on POSIX and on native Windows."""
    try:
        protect_descriptor(fd, Path(path))
    except subprocess.CalledProcessError as exc:
        raise OSError("owner-only access could not be applied") from exc


def read_verification_code(*, stdin=None, prompt: str = "Current authenticator code: ") -> str:
    """Read one 6 or 8 digit code; hidden on a terminal, one line from a pipe."""
    stream = stdin or sys.stdin
    if stream.isatty():
        import getpass
        code = getpass.getpass(prompt, stream=sys.stderr).strip()
    else:
        code = stream.readline().strip()
    if not code.isascii() or not code.isdecimal() or len(code) not in (6, 8):
        raise ThreadsAuthError("invalid_code_format", needs_code=True)
    return code


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
                _restrict(handle.fileno(), temporary)
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
                _restrict(handle.fileno(), temporary)
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


def verify_bearer_identity(token: ThreadsToken, *, http, headers: dict | None = None,
                           timeout: float = 15) -> dict:
    """Ask the server who the bearer belongs to; fail unless it is the token's account.

    Uses the same current_user endpoint instagrapi's account_info calls, on the
    host the mobile publisher writes to. Provider bodies never leave here.
    """
    request_headers = {
        "User-Agent": "Barcelona 289.0.0.77.109 Android", "Accept": "*/*",
        "X-IG-App-ID": THREADS_APP_ID, "X-IG-Capabilities": "3brTvx0=",
        "X-IG-Connection-Type": "WIFI", **(headers or {}),
    }
    # Always check the bearer named by the token, whatever headers came in.
    request_headers["Authorization"] = "Bearer " + token.token
    try:
        response = http.get(IDENTITY_URL, params={"edit": "true"}, headers=request_headers,
                            timeout=timeout, allow_redirects=False)
    except Exception:
        raise ThreadsAuthError("identity_unavailable") from None
    if response.status_code in (401, 403):
        raise ThreadsAuthError("bearer_rejected")
    if response.status_code == 429:
        raise ThreadsAuthError("rate_limited")
    if response.status_code != 200:
        raise ThreadsAuthError("identity_unavailable")
    try:
        data = response.json()
    except ValueError:
        raise ThreadsAuthError("identity_unavailable") from None
    if isinstance(data, dict) and data.get("message") == "login_required":
        raise ThreadsAuthError("bearer_rejected")
    user = data.get("user") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        raise ThreadsAuthError("identity_unavailable")
    username = str(user.get("username") or "")
    user_id = str(user.get("pk") or user.get("pk_id") or user.get("strong_id__") or "")
    if not username or not user_id.isdecimal():
        raise ThreadsAuthError("identity_unavailable")
    if username.casefold() != token.username.casefold() or user_id != token.user_id:
        raise ThreadsAuthError("identity_mismatch")
    return {"id": user_id, "username": username}
