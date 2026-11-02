"""Persistent per-account Threads post spacing and New York quiet hours."""

from __future__ import annotations

from farm.config import threads_cadence_dir
from farm.session_files import is_private, protect_descriptor


from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from farm import locking as fcntl
import json
import os
from pathlib import Path
import random
import re
import tempfile
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_CADENCE_DIR = threads_cadence_dir()
_ACCOUNT = re.compile(r"[A-Za-z0-9._]+\Z")
MIN_POST_GAP = 3600
MAX_POST_GAP = 7200


class CadenceError(Exception):
    """Local cadence state cannot safely be used."""


def _lock_owner_only(fd: int) -> None:
    # The lock file holds nothing; skip a per-call icacls run on native Windows.
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)


def in_quiet_hours(now: datetime) -> bool:
    """Quiet from local midnight inclusive to 06:00 exclusive, including DST."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must have a timezone")
    return now.astimezone(NEW_YORK).hour < 6


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must have a timezone")
    return now.astimezone(timezone.utc)


class ThreadsCadence:
    def __init__(self, account: str, *, directory: Path = DEFAULT_CADENCE_DIR,
                 rng=None):
        if not _ACCOUNT.fullmatch(account):
            raise ValueError("invalid account")
        self.account = account
        self.directory = Path(directory)
        self.path = self.directory / f"{account}.json"
        self.lock_path = self.directory / f"{account}.lock"
        self.rng = rng or random.SystemRandom()
        self._locked = False

    @contextmanager
    def locked(self):
        """Hold the cadence lock through a publish attempt and state update."""
        if self.directory.is_symlink() or self.lock_path.is_symlink():
            raise CadenceError("cadence directory is a symlink")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _lock_owner_only(fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._locked = True
            yield self
        finally:
            self._locked = False
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _require_lock(self):
        if not self._locked:
            raise CadenceError("cadence lock is required")

    def _read(self) -> dict | None:
        self._require_lock()
        if not self.path.exists():
            return None
        if self.path.is_symlink() or not is_private(self.path):
            raise CadenceError("cadence file is not private")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (data.get("schema") != 1 or data.get("account") != self.account or
                    not isinstance(data.get("next_post_at"), str)):
                raise ValueError("invalid cadence schema")
            next_time = datetime.fromisoformat(data["next_post_at"])
            _utc(next_time)
            last_text = data.get("last_post_at")
            if last_text is not None:
                _utc(datetime.fromisoformat(last_text))
            retry_text = data.get("auth_retry_after")
            if retry_text is not None:
                _utc(datetime.fromisoformat(retry_text))
            return data
        except (OSError, ValueError, TypeError, AttributeError):
            raise CadenceError("cadence file is invalid") from None

    def _interval(self) -> timedelta:
        seconds = self.rng.randint(MIN_POST_GAP, MAX_POST_GAP)
        if not MIN_POST_GAP <= seconds <= MAX_POST_GAP:
            raise CadenceError("cadence random interval is invalid")
        return timedelta(seconds=seconds)

    def _write(self, *, next_post_at: datetime, last_post_at: datetime | None,
               auth_retry_after: datetime | None = None) -> None:
        self._require_lock()
        data = {"schema": 1, "account": self.account,
                "next_post_at": _utc(next_post_at).isoformat(),
                "last_post_at": _utc(last_post_at).isoformat() if last_post_at else None,
                "auth_retry_after": (_utc(auth_retry_after).isoformat()
                                     if auth_retry_after else None)}
        fd, temporary = tempfile.mkstemp(prefix=".threads-cadence-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                protect_descriptor(handle.fileno(), Path(temporary))
                json.dump(data, handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def due(self, now: datetime) -> bool:
        """Initialize first delay once, then test the persisted deadline."""
        current = _utc(now)
        data = self._read()
        if data is None:
            self._write(next_post_at=current + self._interval(), last_post_at=None)
            return False
        return current >= _utc(datetime.fromisoformat(data["next_post_at"]))

    def record_post_attempt(self, now: datetime) -> datetime:
        """Space the next post after a confirmed or ambiguous public attempt."""
        current = _utc(now)
        next_time = current + self._interval()
        self._write(next_post_at=next_time, last_post_at=current)
        return next_time

    def auth_ready(self, now: datetime) -> bool:
        """Avoid repeated provider checks during a temporary auth failure."""
        current = _utc(now)
        data = self._read()
        if data is None or not data.get("auth_retry_after"):
            return True
        return current >= _utc(datetime.fromisoformat(data["auth_retry_after"]))

    def hold_auth(self, now: datetime, *, minutes: int = 30) -> datetime:
        """Persist a cooldown while retaining the post deadline."""
        current = _utc(now)
        data = self._read()
        if data is None:
            next_post_at = current + self._interval()
            last_post_at = None
        else:
            next_post_at = datetime.fromisoformat(data["next_post_at"])
            last_post_at = (datetime.fromisoformat(data["last_post_at"])
                            if data.get("last_post_at") else None)
        retry_at = current + timedelta(minutes=minutes)
        self._write(next_post_at=next_post_at, last_post_at=last_post_at,
                    auth_retry_after=retry_at)
        return retry_at
