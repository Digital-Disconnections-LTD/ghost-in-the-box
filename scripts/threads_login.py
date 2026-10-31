#!/usr/bin/env python3
"""Mint a local Threads bearer using the linked Instagram account.

Pass --verification-code-stdin when an authenticator app challenge is expected.
The code is read once from stdin, never saved, and hidden when stdin is a TTY.
"""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.threads_auth import ThreadsAuthError, load_inputs, login


def _read_code() -> str:
    if sys.stdin.isatty():
        import termios
        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        hidden = termios.tcgetattr(fd)
        hidden[3] &= ~termios.ECHO
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, hidden)
            code = sys.stdin.readline().strip()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
            print(file=sys.stderr)
    else:
        code = sys.stdin.readline().strip()
    if not code.isascii() or not code.isdecimal() or len(code) not in (6, 8):
        raise ThreadsAuthError("invalid_code_format", needs_code=True)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verification-code-stdin", action="store_true")
    parser.add_argument("--force-refresh", action="store_true",
                        help="refresh the linked IG session and Threads bearer together")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        code = _read_code() if args.verification_code_stdin else ""
        inputs = load_inputs()
        login(inputs, verification_code=code, force_refresh=args.force_refresh)
        print(json.dumps({"ok": True, "status": "saved", "needs_code": False}))
        return 0
    except ThreadsAuthError as exc:
        print(json.dumps({"ok": False, "status": exc.status,
                          "needs_code": exc.needs_code}))
        return 2 if exc.needs_code else 1
    except Exception:
        # Never print a provider exception or traceback: either may contain
        # credentials, an authenticator code, or a response body.
        print(json.dumps({"ok": False, "status": "internal_error",
                          "needs_code": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
