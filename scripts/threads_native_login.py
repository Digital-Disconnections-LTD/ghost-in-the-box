#!/usr/bin/env python3
"""Experimental Threads-app Bloks login with stored private credentials.

Use --verification-code-stdin to read a current 6/8 digit authenticator code
only if the provider returns a two-factor challenge. Terminal echo is hidden.
No live validation or public write is performed after saving the bearer.
"""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.threads_auth import ThreadsAuthError, load_inputs
from farm.threads_native_auth import login


def _read_code() -> str:
    if sys.stdin.isatty():
        import termios
        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        hidden = termios.tcgetattr(fd)
        hidden[3] &= ~termios.ECHO
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, hidden)
            print("Current authenticator code: ", end="", file=sys.stderr, flush=True)
            code = sys.stdin.readline().strip()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
            print(file=sys.stderr)
        return code
    return sys.stdin.readline().strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verification-code-stdin", action="store_true",
                        help="read a current code from stdin only if challenged")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        login(load_inputs(), code_provider=_read_code if args.verification_code_stdin else None)
        print(json.dumps({"ok": True, "status": "saved", "needs_code": False}))
        return 0
    except ThreadsAuthError as exc:
        print(json.dumps({"ok": False, "status": exc.status,
                          "needs_code": exc.needs_code}))
        return 2 if exc.needs_code else 1
    except Exception:
        print(json.dumps({"ok": False, "status": "internal_error", "needs_code": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
