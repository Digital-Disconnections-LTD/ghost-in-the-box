"""Private portable session files; never include passwords or browser profiles."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

def account_name(value: str) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value)
            or value.endswith(".") or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value, re.I)):
        raise ValueError("use an account handle containing letters, numbers, dots, _ or -")
    return value


def protect(path: Path) -> None:
    if os.name == "nt":
        # chmod on Windows only changes the read-only bit. Remove inherited ACLs.
        owner = subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{owner}:(F)"],
                       capture_output=True, check=True)
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
"""Pass to os.open; 0 on Windows, where callers check is_symlink() first instead."""


def protect_descriptor(fd: int, path: Path) -> None:
    """Make an open file owner-only before secrets are written to it."""
    if os.name == "nt" or not hasattr(os, "fchmod"):
        protect(Path(path))
    else:
        os.fchmod(fd, 0o600)


def is_private(path: Path) -> bool:
    """Owner-only on POSIX; Windows privacy comes from the ACL protect() sets."""
    if os.name == "nt":
        return True
    return Path(path).stat().st_mode & 0o077 == 0


def stage_session(path: Path, data: dict) -> Path:
    """Write a private, fsynced temporary file beside path and return it, unpublished."""
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".session-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            protect(temporary)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def write_session(path: Path, data: dict) -> None:
    path = Path(path).expanduser()
    temporary = stage_session(path, data)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_session(path: Path) -> dict:
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"session must be a regular file: {path}")
    if not is_private(path):
        raise ValueError(f"session must be private (chmod 600 {path})")
    if path.stat().st_size > 5_000_000:
        raise ValueError("session exceeds the file size limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid session document")
    return data
