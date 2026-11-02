#!/usr/bin/env python3
"""Read own Instagram comment pages, list unseen comments, or create local reply drafts.

  ghost-in-the-box instagram-inbox --account HANDLE sync --posts 3
  ghost-in-the-box instagram-inbox --account HANDLE unseen
  ghost-in-the-box instagram-inbox --account HANDLE draft-reply --comment-id ID --body TEXT

--account may also follow the subcommand. Exit codes: 0 ok, 1 runtime failure,
2 usage, 4 no usable Instagram session.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.accounts import AccountRegistry
from farm.cli_output import (
    add_account_argument, add_body_arguments, check_account, emit, exception_failure, guarded,
    read_body, require_account,
)
from farm.inbox import Inbox, sync_comments
from farm.instagram_session import read_with_session
from farm.outbox import Outbox

COMMAND = "instagram-inbox"
# Instagram's own comment length limit.
COMMENT_LIMIT = 2200
HELP = {
    "sync": "fetch new comments on your recent posts into the local inbox",
    "unseen": "claim unread comments into the review queue (they stop being unread)",
    "peek": "show unread comments without claiming them",
    "pending": "comments already claimed but still awaiting a reply or skip",
    "feed": "show saved timeline items from sync --feed-pages",
    "draft-reply": "save a reply to one comment as an outbox draft (sends nothing)",
    "seen": "mark one comment as read",
    "skip": "mark one claimed comment as needing no reply",
}


def _parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", metavar="PATH", help="private inbox SQLite path (default: see ghost-in-the-box config)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name):
        return sub.add_parser(name, help=HELP[name], description=HELP[name])

    sync = add("sync")
    sync.add_argument("--posts", type=int, default=3, metavar="N", help="own posts to scan, 1..10 (default 3)")
    sync.add_argument("--pages", type=int, default=1, metavar="N", help="comment pages per post, 1..3 (default 1)")
    sync.add_argument("--threads", type=int, default=0, metavar="N",
                      help="parent comments whose replies to scan per page, 0..5 (default 0)")
    sync.add_argument("--older", action="store_true", help="continue saved historical cursors")
    sync.add_argument("--feed-pages", type=int, default=0, metavar="N", help="timeline pages to keep, 0..3 (default 0)")
    unseen = add("unseen")
    unseen.add_argument("--limit", type=int, default=20, metavar="N", help="1..100 (default 20)")
    unseen.add_argument("--worker", default="operator", help="your reviewer name (default operator)")
    peek = add("peek")
    peek.add_argument("--limit", type=int, default=20, metavar="N", help="1..100 (default 20)")
    pending = add("pending")
    pending.add_argument("--limit", type=int, default=20, metavar="N", help="1..100 (default 20)")
    pending.add_argument("--worker", help="only comments claimed by this reviewer")
    feed = add("feed")
    feed.add_argument("--limit", type=int, default=20, metavar="N", help="1..100 (default 20)")
    draft = add("draft-reply")
    draft.add_argument("--comment-id", required=True, metavar="ID")
    add_body_arguments(draft, noun="reply text")
    draft.add_argument("--worker", help="the reviewer that claimed the comment")
    seen = add("seen")
    seen.add_argument("--comment-id", required=True, metavar="ID")
    skip = add("skip")
    skip.add_argument("--comment-id", required=True, metavar="ID")
    skip.add_argument("--worker", help="the reviewer that claimed the comment")
    add_account_argument(parser, *sub.choices.values(), help="your Instagram handle")
    return parser


def _check_bounds(parser, args) -> None:
    if args.command == "sync":
        if not (1 <= args.posts <= 10 and 1 <= args.pages <= 3 and 0 <= args.threads <= 5
                and 0 <= args.feed_pages <= 3):
            parser.error("sync needs --posts 1..10, --pages 1..3, --threads 0..5 and --feed-pages 0..3")
    elif hasattr(args, "limit") and not 1 <= args.limit <= 100:
        parser.error("--limit must be 1..100")


@guarded(COMMAND)
def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    account = check_account(parser, require_account(
        parser, args, example="instagram-inbox --account YOUR_HANDLE peek"))
    _check_bounds(parser, args)
    os.umask(0o077)
    body = read_body(args.body, args.body_file, limit=COMMENT_LIMIT) if args.command == "draft-reply" else None
    inbox = Inbox(args.db)
    try:
        if args.command == "sync":
            # Do not log provider exception text, headers, settings or credentials.
            logging.disable(logging.CRITICAL)
            try:
                result = read_with_session(account,
                    lambda client: sync_comments(client, inbox, account, posts=args.posts,
                        pages=args.pages, older=args.older, threads=args.threads,
                        feed_pages=args.feed_pages), registry=AccountRegistry())
            except Exception as exc:
                code, payload = exception_failure(
                    exc, command=COMMAND, reveal=False,
                    auth_hint=f"ghost-in-the-box login start --platform instagram --account {account}")
                emit(payload)
                return code
        elif args.command == "unseen":
            result = inbox.unseen(account, args.limit, worker=args.worker)
        elif args.command == "peek":
            result = inbox.peek(account, args.limit)
        elif args.command == "pending":
            result = inbox.pending(account, args.limit, worker=args.worker)
        elif args.command == "feed":
            result = inbox.feed(account, args.limit)
        elif args.command == "seen":
            inbox.mark_seen(account, args.comment_id)
            result = {"ok": True}
        elif args.command == "skip":
            inbox.skip(account, args.comment_id, worker=args.worker)
            result = {"ok": True}
        else:
            with Outbox() as outbox:
                result = inbox.draft_reply(outbox, account, args.comment_id, body, worker=args.worker)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        inbox.close()


if __name__ == "__main__":
    raise SystemExit(main())
