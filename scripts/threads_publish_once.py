#!/usr/bin/env python3
"""Publish at most one queued Threads job within the local cadence window.

The systemd timer runs this file. Cadence (quiet hours, post spacing, auth
cooldown) is decided here; claiming, sending and recording go through the same
path as `ghost-in-the-box publish --due --platform threads`.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.cli_output import guarded
from farm.adapters.threads import PreSendFailure, ThreadsAdapter
from farm.config import threads_cadence_dir
from farm.outbox import Outbox, OutboxError
from farm.threads_browser import BrowserThreadsAdapter
from farm.threads_cadence import CadenceError, ThreadsCadence, in_quiet_hours
from scripts.social_publish_job import (
    EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, RELEASED_STATUSES, failure, publish_due,
)


class QuietHoursStarted(PreSendFailure):
    """Local clock crossed into quiet hours before the public request."""


# A send may have happened for these, so they count toward post spacing.
ATTEMPTED_STATUSES = ("published", "uncertain", "receipt_not_recorded", "lease_lost")


def run(account: str, *, box_factory=Outbox, adapter_factory=ThreadsAdapter,
        cadence_factory=ThreadsCadence,
        clock=lambda: datetime.now(timezone.utc), browser_factory=None) -> int:
    """Publish one due reply, or one due post when the cadence allows it.

    ``browser_factory`` enables the same browser fallback `publish` uses when
    the mobile session is unavailable; ``main`` turns it on.
    """
    os.umask(0o077)
    now = clock()
    if in_quiet_hours(now):
        print(json.dumps({"ok": True, "status": "quiet_hours"}))
        return EXIT_OK
    try:
        cadence = cadence_factory(account)
        with cadence.locked(), box_factory() as box:
            if not cadence.auth_ready(now):
                print(json.dumps({"ok": True, "status": "auth_cooldown"}))
                return EXIT_OK
            post_due = cadence.due(now)

            def before_send():
                if in_quiet_hours(clock()):
                    raise QuietHoursStarted("Threads quiet hours began before send")

            options = {"platform": "threads", "account": account, "limit": 1,
                       "before_send": before_send, "release_on": (QuietHoursStarted,),
                       "threads_factory": adapter_factory,
                       "browser_threads_factory": browser_factory}
            code, results = publish_due(box, action="reply", **options)
            if not results and post_due:
                code, results = publish_due(box, action="post", **options)
            if not results:
                print(json.dumps({"ok": True, "status": "idle" if post_due else "cadence_wait"}))
                return EXIT_OK
            result = results[0]
            status = result.get("status")
            if status == "released":
                # The provider was not called. The original job stays queued.
                print(json.dumps({"ok": True, "status": "quiet_hours", "job_id": result["job_id"]}))
                return EXIT_OK
            if status in RELEASED_STATUSES:
                cadence.hold_auth(clock())
            if result.get("action") == "post" and status in ATTEMPTED_STATUSES:
                cadence.record_post_attempt(clock())
            print(json.dumps(result, ensure_ascii=False))
            return code
    except OutboxError as exc:
        print(json.dumps(failure("outbox_refused", exc.code, str(exc), exc.hint)))
        return EXIT_USAGE
    except sqlite3.Error as exc:
        print(json.dumps(failure("outbox_unavailable", "outbox_database_error",
                                 f"The outbox database could not be used ({type(exc).__name__}).",
                                 "Another process may hold it; retry shortly, or run: ghost-in-the-box doctor")))
        return EXIT_RUNTIME
    except (CadenceError, OSError, ValueError) as exc:
        print(json.dumps(failure("cadence_state_error", "cadence_state_error",
                                 f"The local Threads cadence state could not be used ({type(exc).__name__}).",
                                 f"Check the permissions of {threads_cadence_dir()}, or run: ghost-in-the-box doctor")))
        return EXIT_RUNTIME


@guarded("publish")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, metavar="HANDLE", help="your Threads handle")
    parser.add_argument("--db", metavar="PATH", help="private outbox SQLite path (default: see ghost-in-the-box config)")
    args = parser.parse_args(argv)
    return run(args.account, box_factory=lambda: Outbox(args.db),
               browser_factory=BrowserThreadsAdapter)


if __name__ == "__main__":
    raise SystemExit(main())
