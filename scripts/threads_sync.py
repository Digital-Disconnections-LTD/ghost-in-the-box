#!/usr/bin/env python3
"""Read a bounded slice of Threads through the registered shared session."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.adapters.threads import ThreadsReadFailure
from farm.threads_reader import ThreadsReader


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("me")
    own = commands.add_parser("own", help="one page of own posts")
    own.add_argument("--limit", type=int, default=10)
    own.add_argument("--cursor", default="")
    replies = commands.add_parser("replies", help="one page of replies to a post")
    replies.add_argument("--post-id", required=True)
    replies.add_argument("--limit", type=int, default=10)
    replies.add_argument("--cursor", default="")
    args = parser.parse_args()
    reader = ThreadsReader()
    try:
        if args.command == "me":
            result = reader.me(args.account)
        elif args.command == "own":
            result = reader.own_posts(args.account, limit=args.limit, cursor=args.cursor)
        else:
            result = reader.replies(args.account, args.post_id,
                                    limit=args.limit, cursor=args.cursor)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except (ThreadsReadFailure, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
