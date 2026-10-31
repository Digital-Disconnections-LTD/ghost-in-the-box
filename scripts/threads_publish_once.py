#!/usr/bin/env python3
"""Publish at most one queued Threads job within the local cadence window."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.adapters.threads import (
    AuthUnavailable, ThreadsAdapter, PreSendFailure, PublishUncertain,
)
from farm.outbox import Outbox
from farm.threads_cadence import CadenceError, ThreadsCadence, in_quiet_hours


class QuietHoursStarted(PreSendFailure):
    """Local clock crossed into quiet hours before the public request."""


def run(account: str, *, box_factory=Outbox, adapter_factory=ThreadsAdapter,
        cadence_factory=ThreadsCadence,
        clock=lambda: datetime.now(timezone.utc)) -> int:
    os.umask(0o077)
    now = clock()
    if in_quiet_hours(now):
        print(json.dumps({"ok": True, "status": "quiet_hours"}))
        return 0
    try:
        cadence = cadence_factory(account)
        with cadence.locked(), box_factory() as box:
            if not cadence.auth_ready(now):
                print(json.dumps({"ok": True, "status": "auth_cooldown"}))
                return 0
            post_due = cadence.due(now)
            job = box.claim_due(platform="threads", account=account, action="reply")
            if job is None and post_due:
                job = box.claim_due(platform="threads", account=account, action="post")
            if job is None:
                print(json.dumps({"ok": True,
                                  "status": "idle" if post_due else "cadence_wait"}))
                return 0

            def before_send():
                if in_quiet_hours(clock()):
                    raise QuietHoursStarted("Threads quiet hours began before send")

            try:
                receipt = adapter_factory().publish(job, before_send=before_send)
            except AuthUnavailable:
                box.release(job["id"], job["lease_token"])
                cadence.hold_auth(clock())
                print(json.dumps({"ok": False, "status": "auth_unavailable"}))
                return 2
            except QuietHoursStarted:
                # The provider was not called. Keep the original queued job.
                box.release(job["id"], job["lease_token"])
                print(json.dumps({"ok": True, "status": "quiet_hours"}))
                return 0
            except PreSendFailure as exc:
                failed = box.finish(job["id"], job["lease_token"],
                                    outcome="failed", error=str(exc))
                print(json.dumps({"ok": False, "status": "failed_before_send",
                                  "job_id": failed["id"], "reason": str(exc)}))
                return 2
            except PublishUncertain as exc:
                uncertain = box.finish(job["id"], job["lease_token"],
                                       outcome="uncertain", error=str(exc))
                if job["action"] == "post":
                    cadence.record_post_attempt(clock())
                print(json.dumps({"ok": False, "status": "uncertain",
                                  "job_id": uncertain["id"]}))
                return 3
            published = box.finish(job["id"], job["lease_token"],
                                   outcome="published", provider_id=receipt["provider_id"],
                                   permalink=receipt["permalink"])
            if job["action"] == "post":
                cadence.record_post_attempt(clock())
            print(json.dumps({"ok": True, "status": "published", "job_id": published["id"],
                              "provider_id": published["provider_id"],
                              "permalink": published["permalink"]}))
            return 0
    except (CadenceError, OSError, ValueError):
        print(json.dumps({"ok": False, "status": "cadence_state_error"}))
        return 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    args = parser.parse_args(argv)
    return run(args.account)


if __name__ == "__main__":
    raise SystemExit(main())
