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
from farm.threads_reader import ThreadsReader
from farm.inbox import Inbox
from farm.outbox import Outbox
from farm.threads_notifications import namespace, sync_threads


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default="example_creator")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync")
    sync.add_argument("--posts", type=int, default=3)
    sync.add_argument("--replies-per-post", type=int, default=25)
    sync.add_argument("--notify-agent", default=os.environ.get("SOCIAL_FARM_THREADS_NOTIFY_AGENT"),
                      help="durable worker recipient; optional delivery via SOCIAL_FARM_CHAT_PATH")
    for name in ("peek", "unseen", "pending"):
        command = commands.add_parser(name)
        command.add_argument("--limit", type=int, default=10)
        if name != "peek":
            command.add_argument("--worker", default="operator")
    skip = commands.add_parser("skip")
    skip.add_argument("--comment-id", required=True)
    skip.add_argument("--worker", required=True)
    draft = commands.add_parser("draft-reply")
    draft.add_argument("--comment-id", required=True)
    draft.add_argument("--body", required=True)
    draft.add_argument("--worker", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", args.account):
        parser.error("invalid account name")
    if args.command == "sync":
        quiet = quiet_window()
        if quiet:
            print(json.dumps({"ok": True, "result": quiet}))
            return 0
    os.umask(0o077)
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
                result = inbox.draft_reply(outbox,key,args.comment_id,args.body,
                    worker=args.worker,platform="threads",profile_account=args.account)
        print(json.dumps({"ok":True,"result":result},ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok":False,"error_type":type(exc).__name__}))
        return 2
    finally:
        inbox.close()


if __name__ == "__main__":
    raise SystemExit(main())
