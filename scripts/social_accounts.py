#!/usr/bin/env python3
"""Register and inspect local social account session references."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.accounts import AccountRegistry, SESSION_KINDS


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", help="private registry path")
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--platform", required=True)
    register.add_argument("--account", required=True)
    register.add_argument("--kind", choices=SESSION_KINDS, required=True)
    register.add_argument("--session-file", required=True)
    register.add_argument("--account-key")
    show = commands.add_parser("show")
    show.add_argument("platform")
    show.add_argument("account")
    commands.add_parser("list")
    args = parser.parse_args()
    try:
        registry = AccountRegistry(args.registry)
        if args.command == "register":
            result = registry.register(
                platform=args.platform, account=args.account, kind=args.kind,
                session_file=args.session_file, account_key=args.account_key,
            )
        elif args.command == "show":
            result = registry.status(args.platform, args.account)
            if result is None:
                raise ValueError("account not registered")
        else:
            result = registry.list()
        print(json.dumps({"ok": True, "result": result}))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
