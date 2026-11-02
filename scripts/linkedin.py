#!/usr/bin/env python3
"""Bounded LinkedIn session reads. This command never publishes or sends messages.

  ghost-in-the-box linkedin --account HANDLE me
  ghost-in-the-box linkedin --account HANDLE profile OTHER_HANDLE
  ghost-in-the-box linkedin --account HANDLE posts --limit 10

--account may also follow the subcommand. Exit codes: 0 ok, 1 runtime failure,
2 usage, 4 not signed in.
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.adapters.linkedin import LinkedInAdapter
from farm.cli_output import (
    add_account_argument, check_account, emit, exception_failure, guarded, require_account, succeed,
)

COMMAND = "linkedin"
HELP = {
    "me": "confirm which LinkedIn account the saved session belongs to",
    "profile": "read another member's public profile by handle",
    "posts": "read your own recent posts",
    "conversations": "list your recent message conversations",
    "messages": "read the messages in one conversation",
}


def login_hint(account: str) -> str:
    return f"ghost-in-the-box login start --platform linkedin --account {account}"


def _parser() -> tuple[argparse.ArgumentParser, list]:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    subs = [commands.add_parser("me", help=HELP["me"], description=HELP["me"])]
    profile = commands.add_parser("profile", help=HELP["profile"], description=HELP["profile"])
    profile.add_argument("handle", metavar="OTHER_HANDLE", help="the member's public profile handle")
    subs.append(profile)
    for name in ("posts", "conversations", "messages"):
        command = commands.add_parser(name, help=HELP[name], description=HELP[name])
        command.add_argument("--limit", type=int, default=10, metavar="N", help="1..25 (default 10)")
        command.add_argument("--start", type=int, default=0, metavar="N", help="offset 0..10000 (default 0)")
        if name == "messages":
            command.add_argument("conversation_id", metavar="CONVERSATION_ID",
                                 help="ID from the conversations command")
        subs.append(command)
    add_account_argument(parser, *subs, help="your signed-in LinkedIn public handle")
    return parser, subs


@guarded(COMMAND)
def main(argv=None):
    parser, _ = _parser()
    args = parser.parse_args(argv)
    account = check_account(parser, require_account(parser, args,
                                                    example="linkedin --account YOUR_HANDLE me"),
                            platform="linkedin")
    if hasattr(args, "limit") and not (1 <= args.limit <= 25 and 0 <= args.start <= 10000):
        parser.error("--limit must be 1..25 and --start 0..10000")
    adapter = None
    try:
        adapter = LinkedInAdapter()
        if args.command == "me":
            result = adapter.me(account)
        elif args.command == "profile":
            result = adapter.profile(account, args.handle)
        elif args.command == "posts":
            result = adapter.own_posts(account, limit=args.limit, start=args.start)
        elif args.command == "conversations":
            result = adapter.conversations(account, limit=args.limit, start=args.start)
        else:
            result = adapter.messages(account, args.conversation_id, limit=args.limit, start=args.start)
        return succeed(result)
    except Exception as exc:
        # LinkedIn exceptions can quote page content, so only fixed reasons are shown.
        code, payload = exception_failure(exc, command=COMMAND, auth_hint=login_hint(account), reveal=False)
        emit(payload)
        return code
    finally:
        if adapter is not None:
            adapter.http.close()

if __name__ == "__main__":
    raise SystemExit(main())
