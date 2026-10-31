#!/usr/bin/env python3
"""Account-aware Threads commands for agents, backed by the durable outbox."""

from __future__ import annotations

import argparse
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from farm.config import STATE_ROOT
from farm.accounts import AccountRegistry
from farm.adapters.threads import AuthUnavailable, ThreadsReadFailure
from farm.inbox import Inbox
from farm.outbox import Outbox, parse_time, utc_text
from farm.threads_browser import BrowserThreadsAdapter
from farm.threads_cadence import CadenceError, NEW_YORK, ThreadsCadence, in_quiet_hours
from farm.threads_scrapling import BASE, PYTHON, ProfileBusy, ScraplingThreadsAdapter, verified_profile
from farm.threads_notifications import namespace
from scripts.social_publish_job import run as publish_exact


IG_SESSIONS = STATE_ROOT / 'ig'
LOGIN = Path(__file__).with_name("threads_scrapling_login.py")
LOGIN_STATUSES = {
    "saved", "linked_account_unavailable", "identity_unverified", "profile_busy",
    "local_inputs_invalid", "browser_unavailable", "browser_action_unavailable",
    "two_factor_required", "checkpoint_required", "provider_throttled",
    "login_rejected", "unexpected_site",
}


class InputError(ValueError):
    """A fixed local error code, safe to include in terminal JSON."""


class _BrowserOnce:
    """Reuse an auth failure when the shared publisher asks for its fallback."""
    def __init__(self, adapter):
        self.adapter = adapter
        self.auth_error = None

    def publish(self, job):
        if self.auth_error is not None:
            raise self.auth_error
        try:
            return self.adapter.publish(job)
        except AuthUnavailable as exc:
            self.auth_error = exc
            raise


def account_name(value: str) -> str:
    account = value.removeprefix("@").lower()
    if account in {".", ".."} or not re.fullmatch(r"[a-z0-9._]{1,30}", account):
        raise argparse.ArgumentTypeError("use a Threads handle without a URL")
    return account


def target_url(value: str) -> str:
    parsed = urlparse(value)
    if (parsed.scheme != "https" or parsed.netloc not in {
            "threads.com", "www.threads.com", "threads.net", "www.threads.net"}
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"/@[A-Za-z0-9._]{1,30}/post/[A-Za-z0-9_-]+/?", parsed.path)):
        raise InputError("invalid_target_url")
    return "https://www.threads.com" + parsed.path.rstrip("/")


def target_id(value: str) -> str:
    if not re.fullmatch(r"[0-9]{1,30}", value):
        raise InputError("numeric_target_id_required")
    return value


def job_summary(job: dict) -> dict:
    summary = {key: job.get(key) for key in (
        "id", "account", "action", "status", "scheduled_at", "attempts",
        "provider_id", "permalink", "target_id", "target_url",
    )}
    error = str(job.get("last_error") or "")
    match = re.fullmatch(r"Scrapling stopped before submit \(([a-z_]+)(?::([a-z_]+))?\)", error)
    if match:
        summary["failure_phase"] = match.group(1)
        summary["failure_reason"] = match.group(2) or "unknown"
    return summary


def read_body(body_file: str) -> str:
    body = Path(body_file).expanduser().read_text(encoding="utf-8")
    if not body.strip() or len(body) > 500:
        raise InputError("text_must_contain_1_to_500_characters")
    return body


class ThreadsAgent:
    def __init__(self, *, registry=None, db=None, state_dir=BASE,
                 session_dir=IG_SESSIONS, python=PYTHON, runner=subprocess.run,
                 cadence_factory=ThreadsCadence, publisher=publish_exact,
                 reader=None, browser_factory=None, inbox_factory=Inbox,
                 clock=lambda: datetime.now(timezone.utc), post_marker=None):
        self.registry = registry or AccountRegistry()
        self.db = db
        self.state_dir, self.session_dir = Path(state_dir), Path(session_dir)
        self.python, self.runner = Path(python), runner
        self.cadence_factory, self.publisher, self.clock = cadence_factory, publisher, clock
        self.inbox_factory = inbox_factory
        self.post_marker = Path(post_marker) if post_marker is not None else Path.home() / "threadspost"
        self.reader = reader or ScraplingThreadsAdapter(
            registry=self.registry, state_dir=self.state_dir, python=self.python)
        self.browser_factory = browser_factory or (lambda: BrowserThreadsAdapter(
            registry=self.registry, scrapling_state_dir=self.state_dir))

    def linked(self, account: str) -> dict:
        ig = self.registry.status("instagram", account)
        threads = self.registry.status("threads", account)
        if not ig or not threads:
            raise InputError("linked_account_registration_required")
        if (ig["kind"] != "instagrapi_settings" or threads["kind"] != ig["kind"]
                or threads["account_key"] != ig["account_key"]
                or threads["session_file"] != ig["session_file"]):
            raise InputError("linked_account_registration_mismatch")
        return threads

    def quiet_hours(self, *, provider_stopped=False) -> dict | None:
        now = self.clock()
        if not in_quiet_hours(now) and not provider_stopped:
            return None
        local = now.astimezone(NEW_YORK)
        morning = local.replace(hour=6, minute=0, second=0, microsecond=0)
        if local >= morning:
            morning += timedelta(days=1)
        return {"ok": False, "status": "quiet_hours",
                "next_active_at": utc_text(morning), "next_active_local": morning.isoformat()}

    def connect(self, account: str, *, refresh=False) -> dict:
        """Link an existing private Instagram session; run one bounded login."""
        session = self.session_dir / f"{account}.json"
        if (session.is_symlink() or not session.is_file()
                or session.stat().st_mode & 0o077):
            raise InputError("private_instagram_session_required")
        ig = self.registry.get("instagram", account)
        threads = self.registry.get("threads", account)
        account_key = ig["account_key"] if ig else f"meta:{account}"
        for entry in (ig, threads):
            if entry and (entry["kind"] != "instagrapi_settings"
                          or Path(entry["session_file"]) != session.resolve()
                          or entry["account_key"] != account_key):
                raise InputError("linked_account_registration_mismatch")
        for platform, entry in (("instagram", ig), ("threads", threads)):
            if entry is None:
                self.registry.register(platform=platform, account=account,
                                       kind="instagrapi_settings", session_file=session,
                                       account_key=account_key)
        if verified_profile(account, self.state_dir) and not refresh:
            return {"ok": True, "status": "connected", "account": account,
                    "existing_profile": True}
        if not self.python.is_file():
            raise InputError("scrapling_runtime_unavailable")
        try:
            response = self.runner([str(self.python), str(LOGIN), "--account", account,
                                    "--mode", "linked"], capture_output=True, text=True,
                                   timeout=240, check=False)
            data = json.loads(response.stdout.strip().splitlines()[-1])
            status = data.get("status")
            if status not in LOGIN_STATUSES:
                status = "browser_unavailable"
            connected = (response.returncode == 0 and data.get("ok") is True
                         and status == "saved" and verified_profile(account, self.state_dir))
            if status == "saved" and not connected:
                status = "identity_unverified"
            return {"ok": connected, "status": "connected" if connected else status,
                    "account": account, "needs_code": data.get("needs_code") is True}
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError, AttributeError):
            return {"ok": False, "status": "connection_unavailable", "account": account}

    def next_post_at(self, box: Outbox, cadence, account: str) -> datetime | None:
        # The outbox also accounts for exact-job sends made outside this CLI.
        row = box.db.execute(
            """SELECT MAX(updated_at) AS last_at FROM jobs WHERE account=?
               AND action='post' AND status IN ('published','uncertain','in_flight')
               AND (platform='threads' OR (platform='instagram' AND crosspost_threads=1))""",
            (account,),
        ).fetchone()
        data = cadence._read()
        values = [parse_time(value) for value in (
            row["last_at"], data.get("last_post_at") if data else None) if value]
        return max(values) + timedelta(hours=1) if values else None

    def update_post_marker(self, box: Outbox, account: str) -> None:
        """Mirror the last confirmed original for a simple human-readable mtime."""
        row = box.db.execute(
            """SELECT MAX(updated_at) AS last_at FROM jobs WHERE account=?
               AND action='post' AND status='published'
               AND (platform='threads' OR (platform='instagram' AND crosspost_threads=1))""",
            (account,),
        ).fetchone()
        if not row["last_at"] or self.post_marker.is_symlink():
            return
        stamp = parse_time(row["last_at"]).timestamp()
        if self.post_marker.exists() and self.post_marker.stat().st_mtime >= stamp:
            return
        fd = os.open(self.post_marker, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.utime(self.post_marker, (stamp, stamp))

    def status(self, account: str) -> dict:
        try:
            entry = self.linked(account)
            linked = True
            private = bool(entry["session_exists"] and entry["session_private"])
        except InputError:
            linked, private = False, False
        cadence = self.cadence_factory(account)
        with cadence.locked(), Outbox(self.db) as box:
            next_at = self.next_post_at(box, cadence, account)
            self.update_post_marker(box, account)
            counts = {row["status"]: row["count"] for row in box.db.execute(
                "SELECT status,COUNT(*) AS count FROM jobs WHERE platform='threads' "
                "AND account=? GROUP BY status", (account,))}
        ready = linked and verified_profile(account, self.state_dir) and self.python.is_file()
        return {"ok": True, "status": "ready" if ready else "connect_required",
                "account": account, "linked_registration": linked,
                "instagram_session_private": private,
                "scrapling_profile_verified": verified_profile(account, self.state_dir),
                "outbox_counts": counts,
                "post_marker": str(self.post_marker),
                "next_original_at": utc_text(next_at) if next_at else None}

    def read(self, account: str, operation: str, *, limit=10,
             post_id="", url="", query="") -> dict:
        self.linked(account)
        if not 1 <= limit <= 25:
            raise InputError("limit_must_be_1_to_25")
        if operation in {"replies", "target"}:
            post_id, url = target_id(post_id), target_url(url)
        if operation == "search" and (not 2 <= len(query.strip()) <= 120 or
                                      any(ord(char) < 32 for char in query)):
            raise InputError("query_must_contain_2_to_120_characters")
        quiet = self.quiet_hours()
        if quiet:
            return {**quiet, "account": account}
        kwargs = {"limit": limit, "post_id": post_id, "target_url": url}
        if operation == "search":
            kwargs["query"] = query.strip()
        result = self.reader.read(account, operation, **kwargs)
        if result.get("target_unavailable"):
            return {"ok": False, "status": "target_unavailable", "account": account,
                    "target_id": post_id, "target_url": url}
        return {"ok": True, "account": account, "result": result}

    def create(self, account: str, *, body_file: str, idempotency_key: str,
               action="post", post_id="", url="", queue_only=False) -> dict:
        self.linked(account)
        body = read_body(body_file)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
            raise InputError("invalid_idempotency_key")
        if action == "reply":
            post_id, url = target_id(post_id), target_url(url)
        with Outbox(self.db) as box:
            try:
                job = box.create_draft(
                    platform="threads", account=account, action=action, body=body,
                    media_id=post_id or None, target_id=post_id or None, target_url=url or None,
                    idempotency_key=f"threads-agent:{account}:{action}:{idempotency_key}",
                )
            except ValueError:
                raise InputError("idempotency_key_content_conflict") from None
            if job["status"] == "draft":
                job = box.queue(job["id"])
        if queue_only:
            return {"ok": job["status"] in {"queued", "published"},
                    "status": job["status"], "job_id": job["id"], "job": job_summary(job)}
        return self.publish(account, job["id"])

    def reply_inbox(self, account: str, *, comment_id: str, body_file: str,
                    queue_only=False, worker="operator") -> dict:
        """Bind this worker's claimed comment to one exact reply job."""
        self.linked(account)
        comment_id = target_id(comment_id)
        body = read_body(body_file)
        with closing(self.inbox_factory()) as inbox, Outbox(self.db) as box:
            try:
                job = inbox.draft_reply(
                    box, namespace(account), comment_id, body, worker=worker,
                    platform="threads", profile_account=account,
                )
            except ValueError as exc:
                status = {
                    "comment not found in this account inbox": "inbox_comment_not_found",
                    "comment not held by this worker": "inbox_comment_not_claimed_by_worker",
                    "comment was marked as needing no reply": "inbox_comment_skipped",
                    "this comment already has a different reply draft": "inbox_reply_content_conflict",
                    "idempotency key already belongs to different content": "inbox_reply_content_conflict",
                }.get(str(exc), "inbox_reply_unavailable")
                raise InputError(status) from None
            if job["status"] == "draft":
                job = box.queue(job["id"])
        if queue_only:
            return {"ok": job["status"] in {"queued", "published"},
                    "status": job["status"], "job_id": job["id"], "job": job_summary(job)}
        return self.publish(account, job["id"])

    def get_job(self, box: Outbox, account: str, job_id: str) -> dict:
        job = box.get(job_id)
        if job is None:
            raise InputError("job_not_found")
        if job["platform"] != "threads" or job["account"] != account:
            raise InputError("job_account_mismatch")
        return job

    def job(self, account: str, job_id: str) -> dict:
        with Outbox(self.db) as box:
            job = self.get_job(box, account, job_id)
        return {"ok": True, "job_id": job["id"], "job": job_summary(job)}

    def reconcile(self, account: str, job_id: str) -> dict:
        """Confirm an existing send from a provider receipt, without resending."""
        self.linked(account)
        with Outbox(self.db) as box:
            job = self.get_job(box, account, job_id)
            if job["status"] == "published":
                return {"ok": True, "status": "published", "job_id": job_id,
                        "provider_id": job["provider_id"], "permalink": job["permalink"]}
            if job["status"] != "uncertain":
                return {"ok": False, "status": job["status"], "job_id": job_id}
            evidence = self.reader.reconcile(job)
            if evidence.get("status") != "published":
                return {"ok": False, "status": "unconfirmed", "job_id": job_id,
                        "reason": evidence.get("reason", "receipt_not_found")}
            saved = box.resolve_uncertain(
                job_id, outcome="published", reason="exact provider receipt",
                provider_id=evidence["provider_id"], permalink=evidence["permalink"])
            if saved["action"] == "post":
                self.update_post_marker(box, account)
            return {"ok": True, "status": "published", "job_id": job_id,
                    "provider_id": saved["provider_id"], "permalink": saved["permalink"]}

    def retry(self, account: str, job_id: str) -> dict:
        """Retry one frozen job known to have stopped before any submit gesture."""
        self.linked(account)
        with Outbox(self.db) as box:
            job = self.get_job(box, account, job_id)
            if job["status"] != "failed" or not str(job.get("last_error") or "").startswith(
                    "Scrapling stopped before submit ("):
                return {"ok": False, "status": job["status"], "job_id": job_id}
            box.retry_failed(job_id, reason="operator retried exact pre-send job")
        return self.publish(account, job_id)

    def publish(self, account: str, job_id: str) -> dict:
        self.linked(account)
        cadence = self.cadence_factory(account)
        with cadence.locked(), Outbox(self.db) as box:
            job = self.get_job(box, account, job_id)
            if job["status"] == "published":
                if job["action"] == "post":
                    self.update_post_marker(box, account)
                return {"ok": True, "status": "published", "job_id": job_id,
                        "already_published": True, "provider_id": job["provider_id"],
                        "permalink": job["permalink"]}
            if job["status"] != "queued":
                return {"ok": False, "status": job["status"], "job_id": job_id}
            quiet = self.quiet_hours()
            if quiet:
                return {**quiet, "job_id": job_id}
            if job["action"] == "post":
                next_at = self.next_post_at(box, cadence, account)
                if next_at and self.clock() < next_at:
                    return {"ok": False, "status": "cadence_wait", "job_id": job_id,
                            "next_original_at": utc_text(next_at)}
            if not verified_profile(account, self.state_dir):
                return {"ok": False, "status": "auth_unavailable", "job_id": job_id,
                        "account_action": "browser_login_required"}
            try:
                browser = _BrowserOnce(self.browser_factory())
            except Exception:
                return {"ok": False, "status": "adapter_unavailable", "job_id": job_id}
            # BrowserThreadsAdapter selects the verified Scrapling transport.
            # The existing exact publisher owns claim/release/uncertainty rules.
            output = io.StringIO()
            with redirect_stdout(output):
                self.publisher(job_id, box_factory=lambda: Outbox(self.db),
                               threads_factory=lambda: browser,
                               browser_threads_factory=lambda: browser)
            result = json.loads(output.getvalue().strip().splitlines()[-1])
            saved = box.get(job_id)
            if saved["status"] == "failed":
                summary = job_summary(saved)
                for key in ("failure_phase", "failure_reason"):
                    if key in summary:
                        result[key] = summary[key]
                if result.get("status") == "failed_before_send":
                    result["next_step"] = (
                        f"python3 scripts/threads_agent.py --account {account} retry {job_id}")
            if saved["status"] == "uncertain":
                result["next_step"] = (
                    f"python3 scripts/threads_agent.py --account {account} reconcile {job_id}")
            if saved["status"] == "queued":
                quiet = self.quiet_hours(provider_stopped=str(browser.auth_error) == "quiet_hours")
                if quiet:
                    return {**quiet, "job_id": job_id}
            if job["action"] == "post" and saved["status"] in {"published", "uncertain"}:
                try:
                    cadence.record_post_attempt(self.clock())
                except (CadenceError, OSError, ValueError):
                    # The durable outbox receipt still enforces the minimum gap.
                    result["cadence_recorded"] = False
                if saved["status"] == "published":
                    self.update_post_marker(box, account)
            return result


def main(argv=None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, type=account_name)
    parser.add_argument("--db", help="private outbox path (normally use the shared default)")
    commands = parser.add_subparsers(dest="command", required=True)
    connect = commands.add_parser("connect", help="reuse or connect the saved Instagram-linked session")
    connect.add_argument("--refresh", action="store_true", help="run one linked login even if a profile exists")
    commands.add_parser("status", help="local account and outbox status; no provider request")
    read = commands.add_parser("read", help="bounded Scrapling reads")
    read.add_argument("operation", choices=("me", "own", "replies", "target", "search", "feed"))
    read.add_argument("--limit", type=int, default=10)
    read.add_argument("--target-id", default="")
    read.add_argument("--target-url", default="")
    read.add_argument("--query", default="", help="topic phrase for search")
    for name in ("post", "reply"):
        command = commands.add_parser(name, help="freeze text and publish that exact job")
        command.add_argument("--body-file", required=True)
        command.add_argument("--idempotency-key", required=True, help="stable key for this intended message")
        command.add_argument("--queue-only", action="store_true", help="freeze a job without a public send")
        if name == "reply":
            command.add_argument("--target-id", required=True)
            command.add_argument("--target-url", required=True)
    for name in ("publish", "job", "reconcile", "retry"):
        command = commands.add_parser(name, help="publish or inspect one exact outbox job")
        command.add_argument("job_id")
    inbox_reply = commands.add_parser("reply-inbox", help="reply to and consume a claimed comment")
    inbox_reply.add_argument("--comment-id", required=True)
    inbox_reply.add_argument("--body-file", required=True)
    inbox_reply.add_argument("--queue-only", action="store_true")
    inbox_reply.add_argument("--worker", default="operator")
    args = parser.parse_args(argv)
    try:
        agent = ThreadsAgent(db=args.db)
        if args.command == "connect":
            result = agent.connect(args.account, refresh=args.refresh)
        elif args.command == "status":
            result = agent.status(args.account)
        elif args.command == "read":
            result = agent.read(args.account, args.operation, limit=args.limit,
                                post_id=args.target_id, url=args.target_url, query=args.query)
        elif args.command in {"post", "reply"}:
            result = agent.create(args.account, body_file=args.body_file,
                                  idempotency_key=args.idempotency_key, action=args.command,
                                  post_id=getattr(args, "target_id", ""),
                                  url=getattr(args, "target_url", ""), queue_only=args.queue_only)
        elif args.command == "publish":
            result = agent.publish(args.account, args.job_id)
        elif args.command == "reconcile":
            result = agent.reconcile(args.account, args.job_id)
        elif args.command == "retry":
            result = agent.retry(args.account, args.job_id)
        elif args.command == "reply-inbox":
            result = agent.reply_inbox(args.account, comment_id=args.comment_id,
                                      body_file=args.body_file, queue_only=args.queue_only,
                                      worker=args.worker)
        else:
            result = agent.job(args.account, args.job_id)
    except InputError as exc:
        result = {"ok": False, "status": str(exc)}
    except ProfileBusy:
        result = {"ok": False, "status": "profile_busy"}
    except ThreadsReadFailure:
        result = {"ok": False, "status": "read_unavailable"}
    except (OSError, ValueError, CadenceError):
        result = {"ok": False, "status": "local_state_unavailable"}
    except Exception:
        # Provider exception text may include credentials, HTML or private URLs.
        result = {"ok": False, "status": "operation_unavailable"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 3 if result.get("status") == "uncertain" else 2


if __name__ == "__main__":
    raise SystemExit(main())
