#!/usr/bin/env python3
"""Refresh a private Threads browser profile with the saved Instagram login."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import termios

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.config import STATE_ROOT
from farm.threads_browser import browser_command

BASE = STATE_ROOT / 'threads-browser-fallback'
CREDS = Path.home() / ".batbox/social-farm-creds/ig.json"
BRIDGE = Path(__file__).with_name("threads_browser_login_bridge.mjs")
STATUSES = {"saved", "two_factor_required", "checkpoint_required",
            "login_rejected", "browser_login_unavailable", "identity_unverified",
            "local_inputs_invalid", "provider_throttled"}
STAGES = {"setup", "browser_launch", "login_navigation", "login_form",
          "instagram_choice", "credentials_submit", "two_factor", "identity_check"}


def _read_code() -> str:
    if sys.stdin.isatty():
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
        raise ValueError("invalid_code_format")
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default="example_creator")
    parser.add_argument("--verification-code-stdin", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if not re.fullmatch(r"[A-Za-z0-9._]+", args.account):
        print(json.dumps({"ok": False, "status": "local_inputs_invalid", "needs_code": False}))
        return 2
    try:
        code = _read_code() if args.verification_code_stdin else ""
        if (not CREDS.is_file() or CREDS.is_symlink() or
                CREDS.stat().st_mode & 0o077):
            raise ValueError("credentials permissions")
        credentials = json.loads(CREDS.read_text(encoding="utf-8"))
        if (credentials.get("username", "").casefold() != args.account.casefold() or
                not credentials.get("password")):
            raise ValueError("account credentials")
        node = Path(os.environ.get("SOCIAL_FARM_NODE") or shutil.which("node") or
                    BASE / "node-v24.22.1-linux-x64/bin/node")
        source = Path(os.environ.get("SOCIAL_FARM_THREADS_MCP_SOURCE") or BASE / "source")
        if not node.is_file() or not (source / "build/browser/session.js").is_file():
            print(json.dumps({"ok": False, "status": "browser_setup_required",
                              "needs_code": False}))
            return 2
        parent = BASE / "profile"
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        active = parent / args.account
        lock_fd = os.open(parent / f".{args.account}.lock",
                          os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            temporary = Path(tempfile.mkdtemp(prefix=".login-", dir=parent))
            try:
                if active.is_dir():
                    shutil.copytree(active, temporary, dirs_exist_ok=True)
                os.chmod(temporary, 0o700)
                env = os.environ.copy()
                env.update({"THREADS_PROFILE_DIR": str(temporary),
                            "SOCIAL_FARM_THREADS_MCP_SOURCE": str(source),
                            "CLOAKBROWSER_CACHE_DIR": str(BASE / "binary"),
                            "DEBUG": "false"})
                payload = {"username": args.account,
                           "password": credentials["password"], "code": code}
                response = subprocess.run(browser_command(node, BRIDGE),
                                          input=json.dumps(payload), text=True,
                                          capture_output=True, timeout=180,
                                          check=False, env=env)
                lines = response.stdout.strip().splitlines()
                result = json.loads(lines[-1]) if lines else {}
                status = result.get("status")
                if status not in STATUSES:
                    status = "browser_login_unavailable"
                stage = result.get("stage") if result.get("stage") in STAGES else "unknown"
                if status == "saved":
                    backup = parent / f".{args.account}.previous"
                    if backup.exists():
                        shutil.rmtree(backup)
                    if active.exists():
                        os.replace(active, backup)
                    try:
                        os.replace(temporary, active)
                    except OSError:
                        if backup.exists():
                            os.replace(backup, active)
                        raise
                    if backup.exists():
                        shutil.rmtree(backup)
                print(json.dumps({"ok": status == "saved", "status": status,
                                  "needs_code": status == "two_factor_required",
                                  "stage": stage}))
                return 0 if status == "saved" else 2
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    except ValueError:
        print(json.dumps({"ok": False, "status": "local_inputs_invalid",
                          "needs_code": False}))
        return 2
    except Exception:
        print(json.dumps({"ok": False, "status": "browser_login_unavailable",
                          "needs_code": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
