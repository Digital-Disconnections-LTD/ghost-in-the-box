#!/usr/bin/env python3
"""Manage the private, durable social publishing outbox.

  ghost-in-the-box outbox draft --platform threads --account HANDLE --body-file post.txt
  ghost-in-the-box outbox list
  ghost-in-the-box outbox queue JOB_ID [--at 2026-11-02T09:00:00-05:00]
  ghost-in-the-box publish --dry-run JOB_ID

Drafting and queueing never send anything; `ghost-in-the-box publish` does.
Exit codes: 0 ok, 1 runtime failure, 2 usage or refused request.
"""

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.accounts import check_handle, handle_rule
from farm.cli_output import (
    EXIT_USAGE, add_body_arguments, choice_error, fail, guarded, read_body, succeed,
)
from farm.outbox import (
    ACTIONS, JOB_STATES, PUBLISHABLE_PLATFORMS, Outbox, OutboxError, check_platform,
    parse_time,
)

COMMAND = "outbox"
LIST_FILTERS = ("active", "all", *JOB_STATES)
TIME_EXAMPLE = "2026-11-02T09:00:00-05:00"


def summary(job: dict) -> dict:
    return {key: job[key] for key in (
        "id", "platform", "account", "action", "status", "scheduled_at",
        "attempts", "provider_id", "permalink", "created_at", "updated_at"
    )}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", metavar="PATH",
                        help="private outbox SQLite path (default: see ghost-in-the-box config)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name, text):
        return commands.add_parser(name, help=text, description=text[0].upper() + text[1:] + ".")

    draft = add("draft", "save exact text (and optional media) as a draft; sends nothing")
    draft.add_argument("--platform", required=True, metavar="PLATFORM",
                       help="one of: " + ", ".join(PUBLISHABLE_PLATFORMS))
    draft.add_argument("--account", required=True, metavar="HANDLE", help="your handle on that platform")
    add_body_arguments(draft, noun="post text")
    draft.add_argument("--action", choices=ACTIONS, default="post")
    draft.add_argument("--account-key", metavar="KEY",
                       help="shared session key, e.g. meta:HANDLE for Instagram and Threads")
    draft.add_argument("--media", metavar="PATH", help="image, video or carousel manifest to attach")
    draft.add_argument("--media-id", metavar="ID", help="parent media ID for a comment reply")
    draft.add_argument("--target-id", metavar="ID", help="comment or post ID being replied to")
    draft.add_argument("--target-url", metavar="URL", help="link to the item being replied to")
    draft.add_argument("--crosspost-threads", action="store_true",
                       help="Instagram post only: also share it to the linked Threads account")
    draft.add_argument("--source-url", action="append", default=[], metavar="URL",
                       help="source kept with the draft; repeatable")
    draft.add_argument("--idempotency-key", metavar="KEY",
                       help="stable key; repeating it returns the same draft instead of a duplicate")

    queue = add("queue", "freeze a draft's text and schedule it")
    queue.add_argument("id", metavar="JOB_ID")
    queue.add_argument("--at", metavar="TIME", help=f"ISO 8601 time with a timezone, e.g. {TIME_EXAMPLE}; default now")

    claim = add("claim", "worker step: atomically claim one due job (publish --due does this for you)")
    claim.add_argument("--lease-seconds", type=int, default=600, metavar="N", help="claim length (default 600)")
    claim.add_argument("--platform", metavar="PLATFORM",
                       help="default: any of " + ", ".join(PUBLISHABLE_PLATFORMS))

    finish = add("finish", "worker step: record a provider result for a claimed job")
    finish.add_argument("id", metavar="JOB_ID")
    finish.add_argument("--lease-token", required=True, metavar="TOKEN")
    finish.add_argument("--outcome", choices=("published", "uncertain", "failed"), required=True)
    finish.add_argument("--provider-id", metavar="ID")
    finish.add_argument("--permalink", metavar="URL")
    finish.add_argument("--error", metavar="TEXT")

    resolve = add("resolve", "record what really happened to an uncertain job")
    resolve.add_argument("id", metavar="JOB_ID")
    resolve.add_argument("--outcome", choices=("published", "queued", "failed"), required=True)
    resolve.add_argument("--reason", required=True, metavar="TEXT", help="the evidence you checked")
    resolve.add_argument("--provider-id", metavar="ID")
    resolve.add_argument("--permalink", metavar="URL")

    retry_failed = add("retry-failed", "requeue one job that failed before sending")
    retry_failed.add_argument("id", metavar="JOB_ID")
    retry_failed.add_argument("--reason", required=True, metavar="TEXT", help="what you fixed")

    supersede_failed = add("supersede-failed", "retire a failed frozen draft while keeping its record")
    supersede_failed.add_argument("id", metavar="JOB_ID")
    supersede_failed.add_argument("--reason", required=True, metavar="TEXT")

    cancel = add("cancel", "cancel a draft or queued job before it is claimed")
    cancel.add_argument("id", metavar="JOB_ID")
    cancel.add_argument("--reason", metavar="TEXT", help="kept on the job record")

    listing = add("list", "show recent job summaries (active jobs by default)")
    listing.add_argument("--status", default="active", metavar="STATUS",
                         help="active (default), all, or one of: " + ", ".join(JOB_STATES))
    listing.add_argument("--limit", type=int, default=20, metavar="N", help="1..100 (default 20)")

    show = add("show", "show one job in full, including its text")
    show.add_argument("id", metavar="JOB_ID")
    return parser


def _refused(exc: OutboxError) -> int:
    return fail(exc.code, str(exc), exc.hint, EXIT_USAGE)


@guarded(COMMAND, database="outbox", value_code="invalid_request")
def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = _parser()
    args = parser.parse_args(argv)
    # Cheap request checks run before any file read or database open.
    if args.command in ("draft", "claim") and args.platform is not None:
        try:
            check_platform(args.platform)
        except OutboxError as exc:
            return _refused(exc)
    if args.command == "draft":
        try:
            args.account = check_handle(args.platform, args.account.removeprefix("@"))
        except ValueError:
            return fail("invalid_account", f"--account {args.account!r} is not {handle_rule(args.platform)}",
                        "pass your public handle as shown on your profile, without @", EXIT_USAGE)
    if args.command == "list":
        if args.status not in LIST_FILTERS:
            message, hint = choice_error("--status", args.status, LIST_FILTERS)
            return fail("invalid_status", message, hint, EXIT_USAGE)
        if not 1 <= args.limit <= 100:
            return fail("invalid_request", "--limit must be 1..100",
                        "ghost-in-the-box outbox list --limit 20", EXIT_USAGE)
    if args.command == "queue" and args.at:
        try:
            parse_time(args.at)
        except ValueError:
            return fail("invalid_time", f"--at {args.at!r} is not an ISO 8601 time with a timezone",
                        f"ghost-in-the-box outbox queue {args.id} --at {TIME_EXAMPLE} (or end the time with Z for UTC)",
                        EXIT_USAGE)
    body = None
    if args.command == "draft":
        body = read_body(args.body, args.body_file, limit=None, allow_blank=bool(args.media))
    with Outbox(args.db) as box:
        if args.command == "draft":
            result = summary(box.create_draft(
                platform=args.platform, account=args.account, body=body,
                action=args.action, account_key=args.account_key,
                media_path=args.media, media_id=args.media_id, target_id=args.target_id,
                target_url=args.target_url, source_urls=args.source_url,
                idempotency_key=args.idempotency_key,
                crosspost_threads=args.crosspost_threads,
            ))
        elif args.command == "queue":
            result = summary(box.queue(args.id, at=args.at))
        elif args.command == "claim":
            result = box.claim_due(lease_seconds=args.lease_seconds, platform=args.platform)
        elif args.command == "finish":
            result = summary(box.finish(
                args.id, args.lease_token, outcome=args.outcome,
                provider_id=args.provider_id, permalink=args.permalink,
                error=args.error,
            ))
        elif args.command == "resolve":
            result = summary(box.resolve_uncertain(
                args.id, outcome=args.outcome, reason=args.reason,
                provider_id=args.provider_id, permalink=args.permalink,
            ))
        elif args.command == "retry-failed":
            result = summary(box.retry_failed(args.id, reason=args.reason))
        elif args.command == "supersede-failed":
            result = summary(box.supersede_failed(args.id, reason=args.reason))
        elif args.command == "cancel":
            result = summary(box.cancel(args.id, reason=args.reason))
        elif args.command == "list":
            status = None if args.status == "active" else args.status
            result = [summary(job) for job in box.list(status=status, limit=args.limit)]
        else:
            result = box.get(args.id)
            if result is None:
                raise OutboxError("job not found", code="not_found",
                                  hint="List jobs with: ghost-in-the-box outbox list --status all")
    return succeed(result)


if __name__ == "__main__":
    raise SystemExit(main())
