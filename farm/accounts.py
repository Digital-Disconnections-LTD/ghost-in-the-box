"""Local references to social account sessions, without storing credentials."""

from __future__ import annotations

from farm.config import STATE_ROOT



from contextlib import contextmanager
from datetime import datetime, timezone
from farm import locking as fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable


DEFAULT_REGISTRY = STATE_ROOT / 'accounts.json'
SESSION_KINDS = ("instagrapi_settings", "browser_profile", "oauth_file", "cookie_jar")
# Platforms the login wizard, publishers and readers know how to use.
KNOWN_PLATFORMS = ("instagram", "threads", "linkedin", "substack")
# One person's Instagram and Threads share a Meta login, so their jobs must
# serialize on one outbox key; the outbox uses the same fallback.
META_PLATFORMS = ("instagram", "threads")
# Each platform's public handle shape: (pattern, label, description).
_META_HANDLE = (re.compile(r"[A-Za-z0-9_.]{1,30}"), "1-30 letters, digits, '.' or '_'")
HANDLE_RULES = {
    "instagram": (*_META_HANDLE, "Instagram"),
    "threads": (*_META_HANDLE, "Threads"),
    "linkedin": (re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"),
                 "1-100 letters, digits, '.', '_' or '-', starting with a letter or digit", "LinkedIn"),
    "substack": (re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}"),
                 "1-63 letters, digits, '-' or '_', starting with a letter or digit", "Substack"),
}


def handle_rule(platform: str) -> str:
    """'an Instagram handle of 1-30 ...'; unknown platforms get the strictest rule."""
    _, rule, label = HANDLE_RULES.get(platform, (*_META_HANDLE, "public"))
    return f"{'an' if label[0] in 'AEIOU' else 'a'} {label} handle of {rule} (without @)"


def check_handle(platform: str, handle: str) -> str:
    """Return handle when it has the platform's public handle shape, else raise ValueError."""
    pattern = HANDLE_RULES.get(platform, _META_HANDLE)[0]
    if not isinstance(handle, str) or not pattern.fullmatch(handle):
        raise ValueError(f"use {handle_rule(platform)}")
    return handle


class RegistryError(ValueError):
    """A registry failure with a stable code and a next step for the user."""

    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_error(self) -> dict:
        return {"code": self.code, "message": self.message, "hint": self.hint}


def default_account_key(platform: str, account: str) -> str:
    return f"meta:{account}" if platform in META_PLATFORMS else f"{platform}:{account}"


def account_key_matches(entry: dict | None, job_key: str | None) -> bool:
    """True when a job's account_key belongs to this registration, including pre-migration keys."""
    if not entry or not job_key:
        return False
    return job_key == entry.get("account_key") or job_key in (entry.get("legacy_account_keys") or ())


def _migrate(entry: dict) -> dict:
    """Older registrations used <platform>:<account> for Meta platforms; read both, write meta:."""
    platform, account = entry.get("platform"), entry.get("account")
    legacy = f"{platform}:{account}"
    if platform in META_PLATFORMS and entry.get("account_key") == legacy:
        entry = dict(entry, account_key=default_account_key(platform, account))
        entry["legacy_account_keys"] = sorted(set(entry.get("legacy_account_keys") or ()) | {legacy})
    return entry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AccountRegistry:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("SOCIAL_FARM_ACCOUNTS_FILE") or DEFAULT_REGISTRY).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _lock(self):
        with fcntl.exclusive_lock(self.lock_path):
            yield

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema": 1, "accounts": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            data = None
        if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("accounts"), dict):
            raise RegistryError("registry_unreadable", f"unrecognized account registry: {self.path}",
                                f"restore {self.path} from a backup, or move it aside and register accounts again")
        data["accounts"] = {key: _migrate(value) for key, value in data["accounts"].items()}
        return data

    def _write(self, data: dict) -> None:
        fd, name = tempfile.mkstemp(prefix=".accounts-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _validated(platform: str, account: str, kind: str, session_file: str | Path) -> Path:
        if (not isinstance(platform, str) or not isinstance(account, str) or not platform.strip()
                or not account.strip() or kind not in SESSION_KINDS):
            raise RegistryError("invalid_registration", "platform, account and supported kind are required",
                                "kinds: " + ", ".join(SESSION_KINDS))
        session = Path(session_file).expanduser().resolve()
        login = f"ghost-in-the-box login start --platform {platform} --account {account}"
        if not session.exists():
            raise RegistryError("session_missing", f"session path does not exist: {session}",
                                f"save a session first: {login}")
        if kind != "browser_profile" and not session.is_file():
            raise RegistryError("invalid_registration", f"session path must be a file: {session}")
        if kind == "browser_profile" and not session.is_dir():
            raise RegistryError("invalid_registration", f"browser profile path must be a directory: {session}")
        return session

    def register_many(self, registrations: Iterable[dict]) -> list[dict]:
        """Validate every registration, then apply them in one locked registry write."""
        prepared = []
        for item in registrations:
            session = self._validated(item["platform"], item["account"], item["kind"], item["session_file"])
            prepared.append((item, session))
        if not prepared:
            return []
        written = []
        with self._lock():
            data = self._read()
            for item, session in prepared:
                platform, account = item["platform"], item["account"]
                key = f"{platform}:{account}"
                existing = data["accounts"].get(key)
                entry = {
                    "platform": platform,
                    "account": account,
                    "account_key": item.get("account_key") or default_account_key(platform, account),
                    "kind": item["kind"],
                    "session_file": str(session),
                    "created_at": existing["created_at"] if existing else _now(),
                    "updated_at": _now(),
                }
                if existing and existing.get("legacy_account_keys"):
                    entry["legacy_account_keys"] = existing["legacy_account_keys"]
                data["accounts"][key] = _migrate(entry)
                written.append(data["accounts"][key])
            self._write(data)
        # Statuses come from what was written so nothing can fail after the commit point.
        return [self._with_status(dict(entry)) for entry in written]

    def register(
        self, *, platform: str, account: str, kind: str,
        session_file: str | Path, account_key: str | None = None,
    ) -> dict:
        return self.register_many([{"platform": platform, "account": account, "kind": kind,
                                    "session_file": session_file, "account_key": account_key}])[0]

    def get(self, platform: str, account: str) -> dict | None:
        with self._lock():
            entry = self._read()["accounts"].get(f"{platform}:{account}")
        return dict(entry) if entry else None

    def status(self, platform: str, account: str) -> dict | None:
        entry = self.get(platform, account)
        if entry is None:
            return None
        return self._with_status(entry)

    @staticmethod
    def _with_status(entry: dict) -> dict:
        path = Path(entry["session_file"])
        try:
            mode = path.stat().st_mode
        except OSError:
            mode = None
        entry["session_exists"] = mode is not None
        entry["session_private"] = mode is not None and (os.name == "nt" or (mode & 0o077) == 0)
        return entry

    def list(self) -> list[dict]:
        with self._lock():
            entries = list(self._read()["accounts"].values())
        return [self.status(item["platform"], item["account"]) for item in sorted(
            entries, key=lambda value: (value["platform"], value["account"])
        )]
