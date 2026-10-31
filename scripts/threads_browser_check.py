#!/usr/bin/env python3
"""Seed or verify the private persistent Threads browser session without posting."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.threads_browser import BrowserThreadsAdapter
from farm.adapters.threads import AuthUnavailable


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    args = parser.parse_args(argv)
    try:
        result = BrowserThreadsAdapter().verify(args.account)
        print(json.dumps({"ok": True, **result}))
        return 0
    except AuthUnavailable as exc:
        action = str(exc) if str(exc) in (
            "browser_login_required", "browser_setup_required",
            "retry_after_cooldown") else "retry_after_cooldown"
        if str(exc) == "browser fallback needs one-time setup":
            action = "browser_setup_required"
        print(json.dumps({"ok": False, "status": "auth_unavailable",
                          "account": args.account,
                          "account_action": action}))
        return 2
    except Exception:
        print(json.dumps({"ok": False, "status": "bridge_unavailable",
                          "account": args.account}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
