#!/usr/bin/env python3
"""Manage the private, durable social publishing outbox."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.outbox import Outbox


def summary(job: dict) -> dict:
    return {key: job[key] for key in (
        "id", "platform", "account", "action", "status", "scheduled_at",
        "attempts", "provider_id", "permalink", "created_at", "updated_at"
    )}


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="private SQLite path; defaults under ~/.batbox/social-farm-state")
    commands = parser.add_subparsers(dest="command", required=True)

    draft = commands.add_parser("draft", help="create a draft from a UTF-8 body file")
    draft.add_argument("--platform", required=True)
    draft.add_argument("--account", required=True)
    draft.add_argument("--body-file", required=True)
    draft.add_argument("--action", choices=("post", "reply"), default="post")
    draft.add_argument("--account-key", help="shared session key, e.g. meta:example_creator")
    draft.add_argument("--media")
    draft.add_argument("--media-id", help="parent media ID for a comment reply")
    draft.add_argument("--target-id")
    draft.add_argument("--target-url")
    draft.add_argument("--crosspost-threads", action="store_true")
    draft.add_argument("--source-url", action="append", default=[])
    draft.add_argument("--idempotency-key")

    queue = commands.add_parser("queue", help="freeze and schedule a draft")
    queue.add_argument("id")
    queue.add_argument("--at", help="ISO 8601 time with timezone; default now")

    claim = commands.add_parser("claim", help="atomically claim one due job")
    claim.add_argument("--lease-seconds", type=int, default=600)
    claim.add_argument("--platform")

    finish = commands.add_parser("finish", help="record a provider result for a claimed job")
    finish.add_argument("id")
    finish.add_argument("--lease-token", required=True)
    finish.add_argument("--outcome", choices=("published", "uncertain", "failed"), required=True)
    finish.add_argument("--provider-id")
    finish.add_argument("--permalink")
    finish.add_argument("--error")

    resolve = commands.add_parser("resolve", help="reconcile an uncertain job")
    resolve.add_argument("id")
    resolve.add_argument("--outcome", choices=("published", "queued", "failed"), required=True)
    resolve.add_argument("--reason", required=True)
    resolve.add_argument("--provider-id")
    resolve.add_argument("--permalink")

    retry_failed = commands.add_parser("retry-failed", help="requeue one proven pre-send failure")
    retry_failed.add_argument("id")
    retry_failed.add_argument("--reason", required=True)

    supersede_failed = commands.add_parser("supersede-failed", help="retire a failed frozen draft while retaining its record")
    supersede_failed.add_argument("id")
    supersede_failed.add_argument("--reason", required=True)

    listing = commands.add_parser("list", help="show bounded job summaries")
    listing.add_argument("--status", default=None)
    listing.add_argument("--limit", type=int, default=20)

    show = commands.add_parser("show", help="show one full private job")
    show.add_argument("id")

    args = parser.parse_args()
    try:
        with Outbox(args.db) as box:
            if args.command == "draft":
                body = Path(args.body_file).read_text(encoding="utf-8")
                result = box.create_draft(
                    platform=args.platform, account=args.account, body=body,
                    action=args.action, account_key=args.account_key,
                    media_path=args.media, media_id=args.media_id, target_id=args.target_id,
                    target_url=args.target_url, source_urls=args.source_url,
                    idempotency_key=args.idempotency_key,
                    crosspost_threads=args.crosspost_threads,
                )
                result = summary(result)
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
            elif args.command == "list":
                result = [summary(job) for job in box.list(status=args.status, limit=args.limit)]
            else:
                result = box.get(args.id)
                if result is None:
                    raise ValueError("job not found")
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
