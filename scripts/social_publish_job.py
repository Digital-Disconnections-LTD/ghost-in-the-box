#!/usr/bin/env python3
"""Publish one explicitly named queued Instagram or Threads job now."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.adapters.instagram import (
    AuthUnavailable as InstagramAuthUnavailable,
    InstagramAdapter,
    PreSendFailure as InstagramPreSendFailure,
    PublishUncertain as InstagramPublishUncertain,
)
from farm.adapters.threads import (
    AuthUnavailable as ThreadsAuthUnavailable,
    ThreadsAdapter,
    PreSendFailure as ThreadsPreSendFailure,
    PublishUncertain as ThreadsPublishUncertain,
)
from farm.threads_browser import BrowserThreadsAdapter
from farm.outbox import Outbox


def run(job_id: str, *, box_factory=Outbox,
        instagram_factory=InstagramAdapter,
        threads_factory=ThreadsAdapter,
        browser_threads_factory=BrowserThreadsAdapter) -> int:
    """Claim only job_id; never select another due job as a fallback."""
    os.umask(0o077)
    with box_factory() as box:
        selected = box.get(job_id)
        if selected is None:
            print(json.dumps({"ok": False, "status": "not_found", "job_id": job_id}))
            return 2
        platform = selected["platform"]
        if platform not in ("instagram", "threads"):
            print(json.dumps({"ok": False, "status": "unsupported_platform", "job_id": job_id}))
            return 2
        if selected["status"] != "queued":
            print(json.dumps({"ok": False, "status": "not_queued", "job_id": job_id}))
            return 2
        job = box.claim_job(job_id, platform=platform)
        if job is None:
            print(json.dumps({"ok": False, "status": "not_claimable", "job_id": job_id}))
            return 2

        try:
            adapter = instagram_factory() if platform == "instagram" else threads_factory()
        except Exception:
            box.release(job["id"], job["lease_token"])
            print(json.dumps({"ok": False, "status": "adapter_unavailable",
                              "job_id": job_id, "platform": platform}))
            return 2
        try:
            try:
                receipt = adapter.publish(job)
            except ThreadsAuthUnavailable:
                # Mobile auth may be temporarily rejected with logout_reason 8.
                # The browser uses its own durable profile and submits at most
                # once under the same outbox claim.
                try:
                    adapter = browser_threads_factory()
                except Exception as exc:
                    raise ThreadsAuthUnavailable("browser fallback unavailable") from exc
                receipt = adapter.publish(job)
        except (InstagramAuthUnavailable, ThreadsAuthUnavailable) as exc:
            box.release(job["id"], job["lease_token"])
            result = {"ok": False, "status": (
                          "dependency_unavailable" if platform == "instagram" and
                          str(exc) == "Instagram client dependency unavailable" else
                          "auth_unavailable"),
                      "job_id": job_id, "platform": platform}
            if platform == "threads":
                result["account_action"] = (
                    "browser_login_required" if str(exc) == "browser_login_required" else
                    "browser_setup_required" if str(exc) in (
                        "browser fallback needs one-time setup", "browser_setup_required") else
                    "retry_after_cooldown"
                )
            print(json.dumps(result))
            return 2
        except (InstagramPreSendFailure, ThreadsPreSendFailure) as exc:
            failed = box.finish(job["id"], job["lease_token"],
                                outcome="failed", error=str(exc))
            print(json.dumps({"ok": False, "status": "failed_before_send",
                              "job_id": failed["id"], "platform": platform}))
            return 2
        except (InstagramPublishUncertain, ThreadsPublishUncertain):
            uncertain = box.finish(job["id"], job["lease_token"],
                                   outcome="uncertain", error="provider call uncertain")
            print(json.dumps({"ok": False, "status": "uncertain",
                              "job_id": uncertain["id"], "platform": platform}))
            return 3
        except Exception:
            # An unknown adapter error may have occurred after the public
            # request began. Never make this job eligible for automatic resend.
            uncertain = box.finish(job["id"], job["lease_token"],
                                   outcome="uncertain", error="adapter outcome unknown")
            print(json.dumps({"ok": False, "status": "uncertain",
                              "job_id": uncertain["id"], "platform": platform}))
            return 3

        published = box.finish(job["id"], job["lease_token"],
                               outcome="published", provider_id=receipt["provider_id"],
                               permalink=receipt.get("permalink"))
        output = {"ok": True, "status": "published", "job_id": published["id"],
                  "platform": platform, "provider_id": published["provider_id"],
                  "permalink": published["permalink"]}
        if platform == "instagram":
            try:
                adapter.save_session()
                output["session_saved"] = True
            except Exception:
                # The receipt is already durable. Do not make the job eligible
                # for another send if session persistence fails.
                output["session_saved"] = False
        print(json.dumps(output))
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", help="exact queued outbox job ID")
    parser.add_argument("--db", help="private outbox SQLite path")
    args = parser.parse_args(argv)
    return run(args.job_id, box_factory=lambda: Outbox(args.db))


if __name__ == "__main__":
    # Agents sometimes invoke this file with the system Python. Move to the
    # installed account runtime before claiming a job, so a missing package
    # cannot strand an otherwise valid queued post or reply.
    if importlib.util.find_spec("instagrapi") is None:
        installed = Path(os.environ.get("SOCIAL_FARM_IG_PYTHON") or
                         Path(sys.executable))
        # A venv's python may be a symlink to /usr/bin/python3. Comparing the
        # resolved binaries would mistake system Python for the installed venv.
        if installed.is_file() and Path(sys.executable).absolute() != installed.absolute():
            os.execv(str(installed), [str(installed), __file__, *sys.argv[1:]])
    raise SystemExit(main())
