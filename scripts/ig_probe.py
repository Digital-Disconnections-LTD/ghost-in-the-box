#!/usr/bin/env python3
"""Log in to Instagram and save a reusable instagrapi session.

Run with the Instagram extra installed. A current authenticator code is read
from stdin when --verification-code-stdin is given; it is never stored or echoed.
Credentials remain in ~/.batbox/social-farm-creds/ig.json.
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

from instagrapi import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.config import STATE_ROOT


CREDENTIALS = Path.home() / ".batbox/social-farm-creds/ig.json"
STATE_DIR = STATE_ROOT / 'ig'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verification-code-stdin", action="store_true")
    parser.add_argument("--credentials", type=Path, default=CREDENTIALS,
                        help="private JSON with username and password")
    args = parser.parse_args()
    os.umask(0o077)

    code = ""
    if args.verification_code_stdin:
        code = sys.stdin.readline().strip()
        if not code.isdecimal() or len(code) not in (6, 8):
            print(json.dumps({"ok": False, "error": "expected a current 6- or 8-digit code on stdin"}))
            return 2

    try:
        credential_path = args.credentials.expanduser()
        mode = credential_path.stat().st_mode
        if credential_path.is_symlink() or not stat.S_ISREG(mode) or mode & 0o077:
            raise ValueError("credentials must be a private regular file")
        credentials = json.loads(credential_path.read_text())
        if (not isinstance(credentials, dict) or
                not isinstance(credentials.get("username"), str) or
                not re.fullmatch(r"[A-Za-z0-9._]{1,30}", credentials["username"]) or
                not isinstance(credentials.get("password"), str) or not credentials["password"]):
            raise ValueError("username and password are required")
    except (OSError, ValueError, AttributeError):
        print(json.dumps({"ok": False, "error": "private_credentials_unavailable"}))
        return 2
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
        }))
        return 0
    except Exception as exc:
        # Provider error text may contain authentication context.
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
