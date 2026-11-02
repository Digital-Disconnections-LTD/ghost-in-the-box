#!/usr/bin/env python3
"""Log in to Instagram and save a reusable instagrapi session.

Run with the Instagram extra installed. A current authenticator code is read
from stdin when --verification-code-stdin is given; it is never stored or echoed.
Credentials are a private JSON file with username and password; run
`ghost-in-the-box config` to see the default location. The browser login wizard
(`ghost-in-the-box login start --platform instagram`) needs no password file.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

try:
    from instagrapi import Client
except ImportError:
    Client = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.cli_output import EXIT_AUTH, EXIT_USAGE, emit, failure, guarded
from farm.config import credentials_path, install_command, instagram_sessions_dir


CREDENTIALS = credentials_path()
STATE_DIR = instagram_sessions_dir()
WIZARD = "ghost-in-the-box login start --platform instagram --account YOUR_HANDLE"


def _credentials_failure(path: Path, reason: str, hint: str) -> dict:
    message = f"Instagram credentials at {path} {reason}."
    # Top-level message and hint stay for callers that read them before the envelope.
    return failure("private_credentials_unavailable", message, hint, message=message, hint=hint,
                   credentials_path=str(path))


@guarded("instagram-login")
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verification-code-stdin", action="store_true",
                        help="read a current 6- or 8-digit authenticator code from stdin")
    parser.add_argument("--credentials", type=Path, default=CREDENTIALS, metavar="PATH",
                        help="private JSON with username and password (default: see ghost-in-the-box config)")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if Client is None:
        emit(failure("dependency_unavailable", "instagram-login needs the 'instagram' extra.",
                     install_command("instagram")))
        return EXIT_AUTH

    code = ""
    if args.verification_code_stdin:
        code = sys.stdin.readline().strip()
        if not code.isdecimal() or len(code) not in (6, 8):
            emit(failure("invalid_code_format", "expected a current 6- or 8-digit code on stdin",
                         "echo 123456 | ghost-in-the-box instagram-login --verification-code-stdin"))
            return EXIT_USAGE

    credential_path = args.credentials.expanduser()
    example = '{"username": "YOUR_HANDLE", "password": "..."}'
    try:
        mode = credential_path.lstat().st_mode
    except OSError:
        emit(_credentials_failure(
            credential_path, "were not found",
            f"create that file containing {example} and run chmod 600 on it, "
            f"or pass --credentials PATH; no password needed with: {WIZARD}"))
        return EXIT_USAGE
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        emit(_credentials_failure(
            credential_path, "are not a regular file", f"replace it with a regular file containing {example}"))
        return EXIT_USAGE
    if mode & 0o077:
        emit(_credentials_failure(
            credential_path, "are readable by other users", f"chmod 600 '{credential_path}'"))
        return EXIT_USAGE
    try:
        credentials = json.loads(credential_path.read_text())
        if (not isinstance(credentials, dict) or
                not isinstance(credentials.get("username"), str) or
                not re.fullmatch(r"[A-Za-z0-9._]{1,30}", credentials["username"]) or
                not isinstance(credentials.get("password"), str) or not credentials["password"]):
            raise ValueError("username and password are required")
    except (OSError, ValueError, AttributeError):
        emit(_credentials_failure(
            credential_path, "need a valid Instagram username and a password",
            f"the file must contain {example}"))
        return EXIT_USAGE
    client = Client()
    client.delay_range = [2, 5]
    path = STATE_DIR / f"{credentials['username']}.json"
    try:
        if path.is_file():
            client.load_settings(path)
        client.login(
            credentials["username"],
            credentials["password"],
            verification_code=code,
        )
        if not client.user_id:
            raise RuntimeError("login returned without a user ID")

        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".ig-session-", dir=STATE_DIR)
        os.close(fd)
        try:
            if not client.dump_settings(temp_name):
                raise RuntimeError("session settings were not saved")
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

        print(json.dumps({
            "ok": True,
            "username": credentials["username"],
            "user_id": str(client.user_id),
            "session_state": str(path),
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "next": (f"ghost-in-the-box accounts register --platform instagram --account {credentials['username']} "
                     f"--kind instagrapi_settings --session-file '{path}'"),
        }))
        return 0
    except Exception as exc:
        # Provider error text may contain authentication context.
        message = "Instagram did not complete the login; nothing was saved."
        hint = ("if Instagram asked for a code, rerun with --verification-code-stdin; "
                f"or use the browser wizard: {WIZARD}")
        emit(failure("login_failed", message, hint, error_type=type(exc).__name__, message=message, hint=hint))
        return EXIT_AUTH


if __name__ == "__main__":
    raise SystemExit(main())
