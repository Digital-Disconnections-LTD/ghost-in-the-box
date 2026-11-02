#!/usr/bin/env python3
"""Refresh Threads replies or consume a small worker notification batch."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.adapters.threads import ThreadsReadFailure
from farm.threads_reader import ThreadsReader
from farm.inbox import Inbox
from farm.outbox import Outbox
from farm.cli_output import (
    EXIT_AUTH, EXIT_RUNTIME, EXIT_USAGE, BodyInputError, add_account_argument, add_body_arguments,
    attach_error, guarded, read_body, require_account,
)
from farm.threads_notifications import namespace, sync_threads
from farm.threads_transport import ThreadsDependencyUnavailable


EASTERN = ZoneInfo("America/New_York")


def quiet_window(now=None) -> dict | None:
    """Keep the account off Threads from midnight until 06:00 Eastern."""
    local = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    if 6 <= local.hour < 23 or (local.hour == 23 and local.minute < 55):
        return None
    resume_day = local + timedelta(days=1) if local.hour == 23 else local
    resume = resume_day.replace(hour=6, minute=0, second=0, microsecond=0)
    return {"status": "quiet_hours",
            "next_refresh_after": resume.astimezone(timezone.utc).isoformat()}


def notify_agent(inbox: Inbox, account: str, recipient: str,
                 *, runner=subprocess.run, chat_path=None) -> dict:
    """Queue one stable, compact notice; retry the same batch after a crash."""
    quiet = quiet_window()
    if quiet:
        return quiet
    batch = inbox.notification_batch(namespace(account), recipient=recipient)
    if batch is None:
        return {"status": "none", "count": 0}
    configured = chat_path or os.environ.get("SOCIAL_FARM_CHAT_PATH")
    if not configured:
        return {"status": "pending", "count": len(json.loads(batch["comment_ids"])),
                "message_id": batch["id"], "content": batch["content"]}
    chat = Path(configured).expanduser()
    response = runner([sys.executable, str(chat), "send-agent", recipient,
                       batch["content"], "--id", batch["id"]],
                      capture_output=True, text=True, timeout=35, check=False)
    try:
        data = json.loads(response.stdout)
    except (ValueError, TypeError):
        data = {}
    if (response.returncode != 0 or data.get("id") != batch["id"] or
            data.get("durable") is not True):
        raise RuntimeError("durable_agent_notification_unavailable")
    inbox.mark_notification_sent(batch["id"])
    return {"status": "queued", "count": len(json.loads(batch["comment_ids"])),
            "message_id": batch["id"]}


INBOX_ERRORS = {
    "comment not found in this account inbox": "inbox_comment_not_found",
    "comment not held by this worker": "inbox_comment_not_claimed_by_worker",
    "comment was marked as needing no reply": "inbox_comment_skipped",
    "this comment already has a different reply draft": "inbox_reply_content_conflict",
    "idempotency key already belongs to different content": "inbox_reply_content_conflict",
}
_PEEK = "ghost-in-the-box threads-notifications --account {account} peek"
ERRORS = {
    "body_input_required": (EXIT_USAGE, "Give the reply with exactly one of --body TEXT or "
                            "--body-file PATH (use - to read stdin).",
                            "ghost-in-the-box threads-notifications draft-reply --help"),
    "body_file_unreadable": (EXIT_USAGE, "The --body-file could not be read as UTF-8 text.",
                             "Save the file as UTF-8 text, or pass the reply with --body."),
    "file_not_found": (EXIT_USAGE, "The --body-file does not exist.",
                       "Check the path, or pass the reply with --body."),
    "text_must_contain_1_to_500_characters": (EXIT_USAGE, "Threads text must contain 1 to 500 "
                                              "characters.", "Shorten the reply to 500 characters "
                                              "or fewer, then run the command again."),
    "inbox_comment_not_found": (EXIT_USAGE, "That comment is not in @{account}'s Threads inbox.",
                                _PEEK),
    "inbox_comment_not_claimed_by_worker": (EXIT_USAGE, "This worker has not claimed that "
                                            "comment.", "ghost-in-the-box threads-notifications "
                                            "--account {account} unseen --worker WORKER"),
    "inbox_comment_skipped": (EXIT_USAGE, "That comment was marked as needing no reply.", _PEEK),
    "inbox_reply_content_conflict": (EXIT_USAGE, "That comment already has a different reply "
                                     "draft.", "ghost-in-the-box outbox list --status draft"),
    "invalid_arguments": (EXIT_USAGE, "An argument is out of range.",
                          "--posts must be 1..10 and --replies-per-post 1..25."),
    "dependency_unavailable": (EXIT_AUTH, "{dependency} is not installed, so Threads replies "
                               "cannot be read.", "{install}"),
    "read_unavailable": (EXIT_RUNTIME, "Threads replies could not be read.",
                         "ghost-in-the-box threads --account {account} status"),
    "notification_unavailable": (EXIT_RUNTIME, "The worker notification was not confirmed as "
                                 "durable; it will be retried with the same batch.",
                                 "ghost-in-the-box threads-notifications --account {account} sync"),
    "operation_unavailable": (EXIT_RUNTIME, "The Threads inbox command did not complete.",
                              "ghost-in-the-box doctor"),
}
HELP = {
    "sync": "read recent replies to your posts into the local inbox",
    "peek": "show waiting comments without claiming them",
    "unseen": "claim and show new comments for one worker",
    "pending": "show comments one worker has claimed and not answered",
    "skip": "mark a claimed comment as needing no reply",
    "draft-reply": "save a reply draft for a claimed comment; nothing is sent",
}


@guarded("threads-notifications")
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     epilog="Example: ghost-in-the-box threads-notifications --account YOUR_HANDLE peek")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name):
        return commands.add_parser(name, help=HELP[name], description=HELP[name])

    sync = add("sync")
    sync.add_argument("--posts", type=int, default=3, metavar="N", help="own posts to scan, 1..10 (default 3)")
    sync.add_argument("--replies-per-post", type=int, default=25, metavar="N",
                      help="replies read per post, 1..25 (default 25)")
    sync.add_argument("--notify-agent", default=os.environ.get("SOCIAL_FARM_THREADS_NOTIFY_AGENT"), metavar="AGENT",
                      help="durable worker recipient; optional delivery via SOCIAL_FARM_CHAT_PATH")
    for name in ("peek", "unseen", "pending"):
        command = add(name)
        command.add_argument("--limit", type=int, default=10, metavar="N", help="default 10")
        if name != "peek":
            command.add_argument("--worker", default="operator", help="your reviewer name (default operator)")
    skip = add("skip")
    skip.add_argument("--comment-id", required=True, metavar="ID")
    skip.add_argument("--worker", required=True, help="the reviewer that claimed the comment")
    draft = add("draft-reply")
    draft.add_argument("--comment-id", required=True, metavar="ID")
    add_body_arguments(draft, noun="reply text")
    draft.add_argument("--worker", required=True, help="the reviewer that claimed the comment")
    add_account_argument(parser, *commands.choices.values(), help="your Threads handle")
    args = parser.parse_args(argv)
    require_account(parser, args, example="threads-notifications --account YOUR_HANDLE peek")
    if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", args.account):
        parser.error("invalid account name")
    if args.command == "sync":
        quiet = quiet_window()
        if quiet:
            print(json.dumps({"ok": True, "result": quiet}))
            return 0
    os.umask(0o077)
    try:
        body = read_body(args.body, args.body_file) if args.command == "draft-reply" else None
    except BodyInputError as exc:
        output = {"ok": False, "status": str(exc), "error_type": "BodyInputError"}
        code = attach_error(output, ERRORS, account=args.account)
        print(json.dumps(output))
        return code
    inbox = Inbox()
    key = namespace(args.account)
    try:
        if args.command == "sync":
            result = sync_threads(ThreadsReader(), inbox, args.account,
                                  posts=args.posts,
                                  replies_per_post=args.replies_per_post)
            if args.notify_agent:
                result["notification"] = notify_agent(inbox,args.account,args.notify_agent)
        elif args.command == "peek":
            result = inbox.peek(key,args.limit)
        elif args.command == "unseen":
            result = inbox.unseen(key,args.limit,worker=args.worker)
        elif args.command == "pending":
            result = inbox.pending(key,args.limit,worker=args.worker)
        elif args.command == "skip":
            inbox.skip(key,args.comment_id,worker=args.worker)
            result = {"ok":True}
        else:
            with Outbox() as outbox:
                result = inbox.draft_reply(outbox,key,args.comment_id,body,
                    worker=args.worker,platform="threads",profile_account=args.account)
        print(json.dumps({"ok":True,"result":result},ensure_ascii=False))
        return 0
    except Exception as exc:
        output = {"ok": False, "error_type": type(exc).__name__}
        if isinstance(exc, ThreadsDependencyUnavailable):
            output.update(status="dependency_unavailable", dependency=exc.dependency,
                          install=exc.install)
        elif isinstance(exc, ThreadsReadFailure):
            output["status"] = "read_unavailable"
        elif isinstance(exc, RuntimeError) and str(exc) == "durable_agent_notification_unavailable":
            output["status"] = "notification_unavailable"
        elif isinstance(exc, ValueError) and str(exc) in INBOX_ERRORS:
            output["status"] = INBOX_ERRORS[str(exc)]
        elif isinstance(exc, ValueError) and args.command == "sync":
            output["status"] = "invalid_arguments"
        else:
            # Provider and browser errors can carry private page text; keep the type only.
            output["status"] = "operation_unavailable"
        code = attach_error(output, ERRORS, account=args.account)
        print(json.dumps(output))
        return code
    finally:
        inbox.close()


if __name__ == "__main__":
    raise SystemExit(main())
