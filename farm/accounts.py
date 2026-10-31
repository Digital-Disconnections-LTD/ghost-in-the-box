"""Local references to social account sessions, without storing credentials."""

from __future__ import annotations

from farm.config import STATE_ROOT



from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile


DEFAULT_REGISTRY = STATE_ROOT / 'accounts.json'
SESSION_KINDS = ("instagrapi_settings", "browser_profile", "oauth_file")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AccountRegistry:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("SOCIAL_FARM_ACCOUNTS_FILE") or DEFAULT_REGISTRY).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _lock(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema": 1, "accounts": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema") != 1 or not isinstance(data.get("accounts"), dict):
            raise ValueError("unrecognized account registry")
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

    def register(
        self, *, platform: str, account: str, kind: str,
        session_file: str | Path, account_key: str | None = None,
    ) -> dict:
        if not platform.strip() or not account.strip() or kind not in SESSION_KINDS:
            raise ValueError("platform, account and supported kind are required")
        session = Path(session_file).expanduser().resolve()
        if not session.exists():
            raise ValueError(f"session path does not exist: {session}")
        if kind != "browser_profile" and not session.is_file():
            raise ValueError("session path must be a file")
        if kind == "browser_profile" and not session.is_dir():
            raise ValueError("browser profile path must be a directory")
        key = f"{platform}:{account}"
        with self._lock():
            data = self._read()
            existing = data["accounts"].get(key)
            entry = {
                "platform": platform,
                "account": account,
                "account_key": account_key or key,
                "kind": kind,
                "session_file": str(session),
                "created_at": existing["created_at"] if existing else _now(),
                "updated_at": _now(),
            }
            data["accounts"][key] = entry
            self._write(data)
        return self.status(platform, account)

    def get(self, platform: str, account: str) -> dict | None:
        with self._lock():
            entry = self._read()["accounts"].get(f"{platform}:{account}")
        return dict(entry) if entry else None

    def status(self, platform: str, account: str) -> dict | None:
        entry = self.get(platform, account)
        if entry is None:
            return None
        path = Path(entry["session_file"])
        entry["session_exists"] = path.exists()
        entry["session_private"] = path.exists() and (path.stat().st_mode & 0o077) == 0
        return entry

    def list(self) -> list[dict]:
        with self._lock():
            entries = list(self._read()["accounts"].values())
        return [self.status(item["platform"], item["account"]) for item in sorted(
            entries, key=lambda value: (value["platform"], value["account"])
        )]
