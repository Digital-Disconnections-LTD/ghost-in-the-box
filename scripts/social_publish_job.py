#!/usr/bin/env python3
"""Publish queued Instagram, Threads or LinkedIn outbox jobs.

  ghost-in-the-box publish JOB_ID            send one queued job once it is due (--now skips the wait)
  ghost-in-the-box publish --dry-run JOB_ID  show the frozen payload and target account; send nothing
  ghost-in-the-box publish --due             claim and send due jobs (what the systemd timers run)

Exit codes: 0 ok, 1 runtime failure, 2 usage, 3 uncertain send, 4 auth or dependency.
"""

import argparse
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.accounts import AccountRegistry, account_key_matches
from farm.adapters import (
    EXCERPT_LIMIT, REASON_LIMIT, AuthUnavailable, DependencyUnavailable, PreSendFailure,
    PublishRejected, PublishUncertain, safe_reason,
)
from farm.adapters.instagram import InstagramAdapter
from farm.adapters.threads import ThreadsAdapter
from farm.threads_browser import BrowserThreadsAdapter
from farm.adapters.linkedin import LinkedInAdapter
from farm import cli_output
from farm.cli_output import EXIT_AUTH, EXIT_OK, EXIT_RUNTIME, EXIT_UNCERTAIN, EXIT_USAGE
from farm.config import chromium_install_command, install_command
from farm.outbox import (
    BRIEF_ONLY_HINT, PUBLISHABLE_PLATFORMS, Outbox, OutboxError, frozen_payload_matches,
    parse_time, utc_now,
)


# Most severe first: an uncertain send needs a human before anything else.
SEVERITY = (EXIT_UNCERTAIN, EXIT_AUTH, EXIT_RUNTIME, EXIT_USAGE, EXIT_OK)
# Instagram reel and carousel uploads can outlast ten minutes. I use one long
# lease instead of a heartbeat thread: a heartbeat would keep a hung process's
# claim alive forever, and Outbox.finish still accepts this token after the
# lease expires, so a late receipt is recorded rather than lost.
PUBLISH_LEASE_SECONDS = 1800
MAX_DUE_LIMIT = 50
INSTALL_HINTS = {
    "instagram": install_command("instagram"),
    "threads": f"{install_command('login')} && {chromium_install_command()}",
    "linkedin": "ghost-in-the-box doctor",
}
# Adapters owned elsewhere may still raise a plain AuthUnavailable for a
# missing package; these exact messages mean "install something", not "wait".
LEGACY_DEPENDENCY_MESSAGES = {
    "Instagram client dependency unavailable": INSTALL_HINTS["instagram"],
    "install the login extra and Playwright Chromium": INSTALL_HINTS["threads"],
}
RELEASED_STATUSES = ("auth_unavailable", "dependency_unavailable", "adapter_unavailable", "released")


def failure(status: str, code: str, message: str, hint: str | None = None, **extra) -> dict:
    """Build the shared error envelope while keeping the legacy status key."""
    return cli_output.failure(code, message, hint, status=status, **extra)


def _threads_account_action(message: str) -> str:
    if message == "browser_login_required":
        return "browser_login_required"
    if message in ("browser fallback needs one-time setup", "browser_setup_required",
                   "browser fallback needs a display or xvfb-run"):
        return "browser_setup_required"
    if message in ("retry_after_cooldown", "quiet_hours") or "HTTP 429" in message:
        return "retry_after_cooldown"
    return "check_login"


def _auth_result(exc: AuthUnavailable, job: dict, base: dict) -> dict:
    platform, account = job["platform"], job["account"]
    login = f"ghost-in-the-box login start --platform {platform} --account {account}"
    if platform != "threads":
        return failure("auth_unavailable", "auth_unavailable",
                       f"The saved {platform} session for {account} could not be used; the job stays queued.",
                       login, **base)
    action = _threads_account_action(str(exc))
    hints = {
        "browser_login_required": login,
        "browser_setup_required": "ghost-in-the-box threads-setup",
        "retry_after_cooldown": f"Threads asked for a pause; the job stays queued. Later run: ghost-in-the-box publish {job['id']}",
        "check_login": (f"Check the session: ghost-in-the-box accounts show --platform threads --account {account}; "
                        f"then sign in again: {login}"),
    }
    return failure("auth_unavailable", action,
                   f"The saved Threads session for {account} could not be used; the job stays queued.",
                   hints[action], account_action=action, **base)


def _dependency_result(platform: str, hint: str | None, base: dict) -> dict:
    return failure("dependency_unavailable", "dependency_unavailable",
                   f"An optional dependency for {platform} publishing is missing; the job stays queued.",
                   hint or INSTALL_HINTS[platform], **base)


def _accepts_before_send(method) -> bool:
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    return "before_send" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


def _send(adapter, job: dict, gate):
    if _accepts_before_send(adapter.publish):
        return adapter.publish(job, before_send=gate)
    # This adapter cannot report its send boundary, so I open the window
    # first and treat any later surprise as a possible send.
    gate()
    return adapter.publish(job)


def _settle(action, *, job: dict, base: dict, code: int, result: dict) -> tuple[int, dict]:
    """Record an outcome; if the claim was lost meanwhile, say so instead of raising."""
    try:
        action()
    except (OutboxError, ValueError, sqlite3.Error):
        return EXIT_UNCERTAIN, failure(
            "lease_lost", "lease_lost",
            f"The outbox no longer holds this claim, so the {result['status']} result was not recorded.",
            f"Inspect it: ghost-in-the-box outbox show {job['id']}", outcome=result["status"], **base)
    return code, result


def publish_claimed(box, job: dict, *, instagram_factory=InstagramAdapter,
                    threads_factory=ThreadsAdapter, browser_threads_factory=BrowserThreadsAdapter,
                    linkedin_factory=LinkedInAdapter, before_send=None,
                    release_on: tuple = ()) -> tuple[int, dict]:
    """Send one claimed job and settle its outbox state; never raises for adapter faults."""
    platform, job_id, token = job["platform"], job["id"], job["lease_token"]
    base = {"job_id": job_id, "platform": platform, "action": job["action"]}
    window = {"open": False}

    def gate():
        if before_send is not None:
            before_send()
        window["open"] = True

    def release(code, result):
        return _settle(lambda: box.release(job_id, token), job=job, base=base, code=code, result=result)

    def finish(code, result, **fields):
        return _settle(lambda: box.finish(job_id, token, **fields), job=job, base=base,
                       code=code, result=result)

    factories = {"instagram": instagram_factory, "threads": threads_factory,
                 "linkedin": linkedin_factory}
    try:
        adapter = factories[platform]()
    except DependencyUnavailable as exc:
        return release(EXIT_AUTH, _dependency_result(platform, exc.install_hint, base))
    except Exception as exc:
        return release(EXIT_AUTH, failure(
            "adapter_unavailable", "adapter_unavailable",
            f"The {platform} adapter could not start ({type(exc).__name__}); the job stays queued.",
            INSTALL_HINTS[platform], **base))
    try:
        try:
            receipt = _send(adapter, job, gate)
        except AuthUnavailable:
            if platform != "threads" or browser_threads_factory is None:
                raise
            # Mobile auth may be temporarily rejected with logout_reason 8.
            # The browser uses its own durable profile and submits at most
            # once under the same outbox claim.
            try:
                adapter = browser_threads_factory()
            except DependencyUnavailable:
                raise
            except Exception as exc:
                raise AuthUnavailable("browser fallback unavailable") from exc
            receipt = _send(adapter, job, gate)
    except Exception as exc:
        if release_on and isinstance(exc, release_on):
            return release(EXIT_OK, {"ok": True, "status": "released", "reason": safe_reason(exc), **base})
        if isinstance(exc, DependencyUnavailable):
            return release(EXIT_AUTH, _dependency_result(platform, exc.install_hint, base))
        if isinstance(exc, AuthUnavailable):
            if str(exc) in LEGACY_DEPENDENCY_MESSAGES:
                return release(EXIT_AUTH, _dependency_result(
                    platform, LEGACY_DEPENDENCY_MESSAGES[str(exc)], base))
            return release(EXIT_AUTH, _auth_result(exc, job, base))
        # Adapter text reaches stdout, MCP clients and last_error, so only a
        # sanitised short reason leaves this function. An excerpt was already
        # reduced by safe_excerpt, so it may make the reason longer.
        excerpt = getattr(exc, "excerpt", None)
        reason = safe_reason(exc, limit=REASON_LIMIT + EXCERPT_LIMIT + 2) if excerpt else safe_reason(exc)
        if isinstance(exc, PublishRejected):
            return finish(EXIT_RUNTIME, failure(
                "rejected", "provider_rejected", reason,
                f"Nothing was published. Fix the cause, then: ghost-in-the-box outbox retry-failed {job_id} --reason '...'",
                **base), outcome="failed", error=reason, failure_kind="rejected")
        if isinstance(exc, PreSendFailure):
            return finish(EXIT_RUNTIME, failure(
                "failed_before_send", "failed_before_send", reason,
                f"Nothing was sent. Fix the cause, then: ghost-in-the-box outbox retry-failed {job_id} --reason '...'",
                **base), outcome="failed", error=reason)
        uncertain_hint = (f"Check {platform} for the post, then record it: ghost-in-the-box outbox resolve "
                          f"{job_id} --outcome published|failed|queued --reason '...'")
        if isinstance(exc, PublishUncertain):
            return finish(EXIT_UNCERTAIN, failure(
                "uncertain", "publish_uncertain",
                "The provider may have received this post; it will not be resent automatically.",
                uncertain_hint, **base), outcome="uncertain",
                error=f"provider call uncertain: {reason}" if excerpt else "provider call uncertain")
        if window["open"]:
            # The send window was open, so the public request may have begun.
            # Never make this job eligible for automatic resend.
            return finish(EXIT_UNCERTAIN, failure(
                "uncertain", "adapter_error_after_send",
                f"The {platform} adapter failed after the send window opened ({type(exc).__name__}).",
                uncertain_hint, **base), outcome="uncertain",
                error=f"adapter outcome unknown ({type(exc).__name__})")
        return finish(EXIT_RUNTIME, failure(
            "failed_before_send", "adapter_error",
            f"The {platform} adapter failed before sending ({type(exc).__name__}).",
            f"Nothing was sent. Fix the cause, then: ghost-in-the-box outbox retry-failed {job_id} --reason '...'",
            **base), outcome="failed", error=f"adapter error before send ({type(exc).__name__})")

    provider_id = receipt.get("provider_id") if isinstance(receipt, dict) else None
    permalink = receipt.get("permalink") if isinstance(receipt, dict) else None
    if not provider_id:
        return finish(EXIT_UNCERTAIN, failure(
            "uncertain", "receipt_missing",
            "The adapter finished without a provider receipt; the post may be public.",
            f"Check {platform}, then: ghost-in-the-box outbox resolve {job_id} --outcome ... --reason '...'",
            **base), outcome="uncertain", error="adapter returned no provider receipt")
    try:
        published = box.finish(job_id, token, outcome="published",
                               provider_id=str(provider_id), permalink=permalink)
    except Exception as exc:
        # The post is public. Print the receipt so it is never lost, even if
        # the outbox cannot store it right now.
        return EXIT_UNCERTAIN, failure(
            "receipt_not_recorded", "receipt_not_recorded",
            f"Published, but the outbox could not record the receipt ({type(exc).__name__}).",
            (f"Record it: ghost-in-the-box outbox resolve {job_id} --outcome published "
             f"--provider-id {provider_id} --reason 'publisher receipt'"),
            provider_id=str(provider_id), permalink=permalink, **base)
    output = {"ok": True, "status": "published", **base,
              "provider_id": published["provider_id"], "permalink": published["permalink"]}
    if platform == "instagram":
        try:
            adapter.save_session()
            output["session_saved"] = True
        except Exception:
            # The receipt is already durable. Do not make the job eligible
            # for another send if session persistence fails.
            output["session_saved"] = False
    return EXIT_OK, output


def _registry_view(registry_factory, job: dict) -> tuple[dict, list[str]]:
    view = {"platform": job["platform"], "account": job["account"],
            "job_account_key": job["account_key"], "registered": False}
    blockers = []
    try:
        entry = registry_factory().status(job["platform"], job["account"])
    except Exception as exc:
        view["registry_error"] = type(exc).__name__
        return view, ["account_registry_unreadable"]
    if not entry:
        return view, ["account_not_registered"]
    view.update(registered=True, kind=entry.get("kind"), account_key=entry.get("account_key"),
                session_exists=bool(entry.get("session_exists")),
                session_private=bool(entry.get("session_private")))
    view["account_key_matches"] = account_key_matches(entry, job["account_key"])
    if not view["account_key_matches"]:
        blockers.append("account_key_mismatch")
    if not view["session_exists"]:
        blockers.append("session_missing")
    return view, blockers


def dry_run(box, job: dict, *, now: bool, registry_factory=AccountRegistry) -> tuple[int, dict]:
    """Describe what publish would send without claiming, sending or writing anything."""
    blockers = []
    if job["status"] != "queued":
        blockers.append("job_not_queued")
    due = job["scheduled_at"] is not None and parse_time(job["scheduled_at"]) <= utc_now()
    if job["status"] == "queued" and not due and not now:
        blockers.append("not_due")
    frozen = bool(job.get("body_sha256"))
    matches = frozen_payload_matches(job) if frozen else False
    if job["status"] == "queued" and not matches:
        blockers.append("payload_changed")
    if box.account_busy(job["account_key"], exclude_id=job["id"]):
        blockers.append("account_busy")
    account, account_blockers = _registry_view(registry_factory, job)
    blockers.extend(account_blockers)
    return EXIT_OK, {
        "ok": True, "status": "dry_run", "job_id": job["id"], "platform": job["platform"],
        "action": job["action"], "job_status": job["status"], "scheduled_at": job["scheduled_at"],
        "due": due,
        "payload": {key: job.get(key) for key in (
            "body", "media_path", "media_id", "target_id", "target_url", "crosspost_threads",
            "source_urls", "body_sha256", "media_sha256")} | {"frozen": frozen, "matches_frozen_hash": matches},
        "target_account": account,
        "would_publish": not blockers, "blockers": blockers, "network_requests": 0,
    }


def _publish_one(box, job_id: str, *, now: bool, dry: bool, registry_factory,
                 factories: dict) -> tuple[int, dict]:
    selected = box.get(job_id)
    if selected is None:
        return EXIT_USAGE, failure("not_found", "not_found", f"No outbox job {job_id}.",
                                   "List jobs with: ghost-in-the-box outbox list --status all",
                                   job_id=job_id)
    platform = selected["platform"]
    if platform not in PUBLISHABLE_PLATFORMS:
        return EXIT_USAGE, failure("unsupported_platform", "brief_only_platform",
                                   f"{platform} jobs cannot be published by Ghost in the Box.",
                                   BRIEF_ONLY_HINT, job_id=job_id, platform=platform)
    if dry:
        return dry_run(box, selected, now=now, registry_factory=registry_factory)
    status = selected["status"]
    if status != "queued":
        hints = {
            "draft": f"Freeze it first: ghost-in-the-box outbox queue {job_id}",
            "published": f"It is already published; see its receipt: ghost-in-the-box outbox show {job_id}",
            "uncertain": f"Reconcile it first: ghost-in-the-box outbox resolve {job_id} --outcome ... --reason '...'",
            "in_flight": f"Another publisher holds it; check again shortly: ghost-in-the-box outbox show {job_id}",
            "failed": f"Requeue it if nothing was sent: ghost-in-the-box outbox retry-failed {job_id} --reason '...'",
            "superseded": "It was retired; draft and queue a new job: ghost-in-the-box outbox draft --help",
            "cancelled": "It was cancelled; draft and queue a new job: ghost-in-the-box outbox draft --help",
        }
        return EXIT_USAGE, failure("not_queued", "not_queued", f"Job {job_id} is {status}, not queued.",
                                   hints.get(status, f"Check it: ghost-in-the-box outbox show {job_id}"),
                                   job_id=job_id, platform=platform, job_status=status)
    scheduled = selected["scheduled_at"]
    if not now and scheduled and parse_time(scheduled) > utc_now():
        return EXIT_USAGE, failure(
            "not_due", "not_due", f"Job {job_id} is scheduled for {scheduled}.",
            f"Wait until {scheduled}, or send it now with: ghost-in-the-box publish {job_id} --now",
            job_id=job_id, platform=platform, scheduled_at=scheduled)
    job = box.claim_job(job_id, platform=platform, lease_seconds=PUBLISH_LEASE_SECONDS)
    if job is None:
        return EXIT_RUNTIME, failure(
            "not_claimable", "account_busy",
            f"Job {job_id} could not be claimed; another job for this account is in flight or uncertain.",
            "See which: ghost-in-the-box outbox list", job_id=job_id, platform=platform)
    return publish_claimed(box, job, **factories)


def run(job_id: str, *, box_factory=Outbox,
        instagram_factory=InstagramAdapter,
        threads_factory=ThreadsAdapter,
        browser_threads_factory=BrowserThreadsAdapter,
        linkedin_factory=LinkedInAdapter,
        now: bool = True, dry: bool = False, registry_factory=AccountRegistry,
        envelope: bool = False) -> int:
    """Claim only job_id; never select another due job as a fallback.

    Library callers (MCP tools, the Threads agent) publish an explicitly chosen
    job and translate the status into their own hints, so ``now`` defaults to
    True and the CLI error object is left out unless ``envelope`` is set. The
    command line passes ``--now`` through and always asks for the envelope.
    """
    os.umask(0o077)
    factories = {"instagram_factory": instagram_factory, "threads_factory": threads_factory,
                 "browser_threads_factory": browser_threads_factory,
                 "linkedin_factory": linkedin_factory}
    try:
        with box_factory() as box:
            code, result = _publish_one(box, job_id, now=now, dry=dry,
                                        registry_factory=registry_factory, factories=factories)
    except OutboxError as exc:
        code, result = EXIT_USAGE, failure("outbox_refused", exc.code, str(exc), exc.hint, job_id=job_id)
    except sqlite3.Error as exc:
        code, result = EXIT_RUNTIME, _database_failure(exc, job_id=job_id)
    if not envelope:
        result.pop("error", None)
    print(json.dumps(result, ensure_ascii=False))
    return code


def _database_failure(exc: sqlite3.Error, **extra) -> dict:
    return failure("outbox_unavailable", "outbox_database_error",
                   f"The outbox database could not be used ({type(exc).__name__}).",
                   "Another process may hold it; retry shortly, or run: ghost-in-the-box doctor",
                   error_type=type(exc).__name__, **extra)


def worst(codes) -> int:
    codes = list(codes)
    return next((code for code in SEVERITY if code in codes), EXIT_OK)


def publish_due(box, *, platform: str | None = None, account: str | None = None,
                action: str | None = None, limit: int = 1, before_send=None,
                release_on: tuple = (), **factories) -> tuple[int, list[dict]]:
    """Claim and publish up to ``limit`` due jobs; returns the worst exit code and results."""
    if not 1 <= limit <= MAX_DUE_LIMIT:
        raise OutboxError(f"limit must be 1..{MAX_DUE_LIMIT}", hint="Pass a smaller --limit.")
    codes, results = [], []
    tried_ids, held_keys = set(), set()
    for _ in range(limit):
        job = box.claim_due(lease_seconds=PUBLISH_LEASE_SECONDS, platform=platform,
                            account=account, action=action, exclude_ids=tried_ids,
                            exclude_account_keys=held_keys)
        if job is None:
            break
        code, result = publish_claimed(box, job, before_send=before_send,
                                       release_on=release_on, **factories)
        codes.append(code)
        results.append(result)
        tried_ids.add(job["id"])
        if result.get("status") in RELEASED_STATUSES or result.get("outcome") in RELEASED_STATUSES:
            # The same account would fail the same way; leave its jobs queued.
            held_keys.add(job["account_key"])
    return worst(codes), results


def summarize(results: list[dict]) -> dict:
    if not results:
        return {"ok": True, "status": "idle", "processed": 0, "results": []}
    if len(results) == 1:
        return {**results[0], "processed": 1, "results": results}
    summary = {"ok": all(result.get("ok") for result in results), "status": "processed",
               "processed": len(results), "results": results}
    first_error = next((result["error"] for result in results if not result.get("ok")), None)
    if first_error:
        summary["error"] = first_error
    return summary


def run_due(*, box_factory=Outbox, platform: str | None = None, account: str | None = None,
            limit: int = 1, instagram_factory=InstagramAdapter, threads_factory=ThreadsAdapter,
            browser_threads_factory=BrowserThreadsAdapter, linkedin_factory=LinkedInAdapter) -> int:
    """Publish due jobs and print one JSON summary."""
    os.umask(0o077)
    try:
        with box_factory() as box:
            code, results = publish_due(
                box, platform=platform, account=account, limit=limit,
                instagram_factory=instagram_factory, threads_factory=threads_factory,
                browser_threads_factory=browser_threads_factory, linkedin_factory=linkedin_factory)
        result = summarize(results)
    except OutboxError as exc:
        code, result = EXIT_USAGE, failure("outbox_refused", exc.code, str(exc), exc.hint)
    except sqlite3.Error as exc:
        code, result = EXIT_RUNTIME, _database_failure(exc)
    print(json.dumps(result, ensure_ascii=False))
    return code


@cli_output.guarded("publish")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job_id", nargs="?", metavar="JOB_ID", help="exact queued outbox job ID")
    parser.add_argument("--db", metavar="PATH", help="private outbox SQLite path (default: see ghost-in-the-box config)")
    parser.add_argument("--now", action="store_true", help="publish JOB even if it is scheduled later")
    parser.add_argument("--dry-run", action="store_true",
                        help="print JOB's frozen payload and target account; send nothing")
    parser.add_argument("--due", action="store_true", help="publish due jobs instead of one named job")
    parser.add_argument("--platform", choices=PUBLISHABLE_PLATFORMS, help="with --due: only this platform")
    parser.add_argument("--account", metavar="HANDLE", help="with --due: only this account")
    parser.add_argument("--limit", type=int, default=1, metavar="N",
                        help=f"with --due: at most N jobs (1..{MAX_DUE_LIMIT}, default 1)")
    args = parser.parse_args(argv)
    if args.due:
        if args.job_id or args.now or args.dry_run:
            parser.error("--due takes no JOB, --now or --dry-run")
        if not 1 <= args.limit <= MAX_DUE_LIMIT:
            parser.error(f"--limit must be 1..{MAX_DUE_LIMIT}")
        return run_due(box_factory=lambda: Outbox(args.db), platform=args.platform,
                       account=args.account, limit=args.limit)
    if not args.job_id:
        parser.error("give a JOB ID, or --due to publish every due job")
    if args.platform or args.account or args.limit != 1:
        parser.error("--platform, --account and --limit apply only with --due")
    return run(args.job_id, box_factory=lambda: Outbox(args.db), now=args.now, dry=args.dry_run,
               envelope=True)


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
