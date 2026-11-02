#!/usr/bin/env python3
"""Account-aware Threads commands for agents, backed by the durable outbox.

post and reply freeze the exact text as a queued outbox job and send nothing;
add --now to publish it immediately. Every result is one JSON line.
"""

from __future__ import annotations

import argparse
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from farm.config import install_command, instagram_sessions_dir, threads_post_marker
from farm.session_files import O_NOFOLLOW, is_private
from farm.accounts import AccountRegistry
from farm.adapters.threads import (AuthUnavailable, PreSendFailure, PublishUncertain,
                                   ThreadsReadFailure)
from farm.inbox import Inbox
from farm.outbox import Outbox, parse_time, utc_text
from farm.threads_auth import (DEFAULT_AUTH_DIR, ThreadsAuthError, load_inputs, load_token,
                               login as legacy_mobile_login, read_verification_code,
                               verify_bearer_identity)
from farm.threads_browser import BrowserThreadsAdapter
from farm.threads_cadence import CadenceError, NEW_YORK, ThreadsCadence, in_quiet_hours
from farm.cli_output import (
    EXIT_AUTH, EXIT_RUNTIME, EXIT_UNCERTAIN, EXIT_USAGE, BodyInputError, add_account_argument,
    add_body_arguments, attach_error, guarded, read_body as read_body_input, require_account,
)
from farm.threads_native_auth import login as native_mobile_login
from farm.threads_scrapling import BASE, PYTHON, ProfileBusy, ScraplingThreadsAdapter, verified_profile
from farm.threads_notifications import namespace
from farm.threads_transport import (CONNECT_COMMAND, LOGIN_COMMAND, ThreadsDependencyUnavailable,
                                    module_available, resolve)
from scripts.social_publish_job import run as publish_exact


IG_SESSIONS = instagram_sessions_dir()
LOGIN = Path(__file__).with_name("threads_scrapling_login.py")
LOGIN_STATUSES = {
    "saved", "linked_account_unavailable", "identity_unverified", "profile_busy",
    "local_inputs_invalid", "browser_unavailable", "browser_action_unavailable",
    "two_factor_required", "checkpoint_required", "provider_throttled",
    "login_rejected", "unexpected_site", "dependency_unavailable",
}
COMMAND = "ghost-in-the-box threads --account {account}"


class InputError(ValueError):
    """A fixed local error code, safe to include in terminal JSON."""

    exit_code = EXIT_USAGE

    def __init__(self, code: str, message: str | None = None, hint: str | None = None):
        self.code, self.message, self.hint = code, message, hint
        super().__init__(code)

    def result(self) -> dict:
        result = {"ok": False, "status": self.code}
        if self.message or self.hint:
            result["error"] = {"code": self.code, "message": self.message, "hint": self.hint}
        return result


class AccountError(InputError):
    """The account's registration or sign-in cannot be used for this command."""

    exit_code = EXIT_AUTH


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


def read_body(body_file: str | None = None, *, body: str | None = None, stdin=None) -> str:
    try:
        return read_body_input(body, body_file, stdin=stdin)
    except BodyInputError as exc:
        raise InputError(str(exc)) from None


def _next_step(result: dict, step: str) -> None:
    """Point at the threads command; the shared publisher's hint names outbox verbs."""
    result["next_step"] = step
    if isinstance(result.get("error"), dict):
        result["error"]["hint"] = step


def derived_key(action: str, post_id: str, url: str, body: str) -> str:
    """Same text and target give the same key, so a repeated command reuses one job."""
    digest = hashlib.sha256("\0".join((action, post_id, url, body)).encode("utf-8")).hexdigest()
    return "auto-" + digest[:32]


class ThreadsAgent:
    def __init__(self, *, registry=None, db=None, state_dir=BASE,
                 session_dir=IG_SESSIONS, python=PYTHON, runner=subprocess.run,
                 cadence_factory=ThreadsCadence, publisher=publish_exact,
                 reader=None, browser_factory=None, inbox_factory=Inbox,
                 clock=lambda: datetime.now(timezone.utc), post_marker=None,
                 portable_factory=None, available=module_available,
                 auth_dir=DEFAULT_AUTH_DIR, http_factory=None):
        self.registry = registry or AccountRegistry()
        self.db = db
        self.state_dir, self.session_dir = Path(state_dir), Path(session_dir)
        self.python, self.runner = Path(python), runner
        self.cadence_factory, self.publisher, self.clock = cadence_factory, publisher, clock
        self.inbox_factory = inbox_factory
        self.post_marker = Path(post_marker) if post_marker is not None else threads_post_marker()
        self.available, self.auth_dir = available, Path(auth_dir)
        self.reader = reader or ScraplingThreadsAdapter(
            registry=self.registry, state_dir=self.state_dir, python=self.python)
        self.browser_factory = browser_factory or (lambda: BrowserThreadsAdapter(
            registry=self.registry, scrapling_state_dir=self.state_dir))
        if portable_factory is None:
            def portable_factory():
                from farm.threads_portable import PortableThreadsAdapter
                return PortableThreadsAdapter(registry=self.registry)
        self.portable_factory = portable_factory
        if http_factory is None:
            def http_factory():
                import requests
                return requests.Session()
        self.http_factory = http_factory

    def linked(self, account: str) -> dict:
        """Return a Threads registration the agent can use, or say why not."""
        threads = self.registry.status("threads", account)
        if not threads:
            raise AccountError("linked_account_registration_required")
        kind = threads["kind"]
        if kind == "cookie_jar":
            return threads
        if kind != "instagrapi_settings":
            raise AccountError(
                "registration_unsupported",
                f"Threads registration kind '{kind}' is not supported by the threads agent. "
                "Supported kinds: cookie_jar (the login wizard) and instagrapi_settings "
                "(a linked Instagram session).",
                LOGIN_COMMAND.format(account=account))
        ig = self.registry.status("instagram", account)
        if not ig:
            raise AccountError("linked_account_registration_required")
        if (ig["kind"] != "instagrapi_settings" or threads["account_key"] != ig["account_key"]
                or threads["session_file"] != ig["session_file"]):
            raise AccountError("linked_account_registration_mismatch")
        return threads

    def transport(self, account: str, purpose: str = "agent"):
        return resolve(account, registry=self.registry, purpose=purpose,
                       scrapling_state_dir=self.state_dir, python=self.python,
                       auth_dir=self.auth_dir, available=self.available)

    def _reader(self, account: str):
        entry = self.registry.status("threads", account)
        if entry and entry["kind"] == "cookie_jar":
            return self.portable_factory()
        return self.reader

    @staticmethod
    def _transport_failure(transport, **fields) -> dict:
        if transport.dependency:
            return {"ok": False, "status": "dependency_unavailable", **fields,
                    "transport": transport.name, "dependency": transport.dependency,
                    "install": transport.install, "reason": transport.reason}
        return {"ok": False, "status": "auth_unavailable", **fields,
                "transport": transport.name, "account_action": "browser_login_required",
                "reason": transport.reason, "next_step": transport.next_step}

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
        """Reuse a wizard login, or link a private Instagram session with one bounded login."""
        threads = self.registry.status("threads", account)
        if threads and threads["kind"] == "cookie_jar":
            if refresh:
                raise AccountError(
                    "login_wizard_refresh_required",
                    "This account signed in with the login wizard; refresh it there.",
                    LOGIN_COMMAND.format(account=account))
            if not threads["session_exists"] or not threads["session_private"]:
                raise AccountError("threads_login_required")
            return {"ok": True, "status": "connected", "account": account,
                    "transport": "portable", "existing_login": True}
        session = self.session_dir / f"{account}.json"
        if (session.is_symlink() or not session.is_file()
                or not is_private(session)):
            raise AccountError("private_instagram_session_required")
        ig = self.registry.get("instagram", account)
        account_key = ig["account_key"] if ig else f"meta:{account}"
        for entry in (ig, threads):
            if entry and (entry["kind"] != "instagrapi_settings"
                          or Path(entry["session_file"]) != session.resolve()
                          or entry["account_key"] != account_key):
                raise AccountError("linked_account_registration_mismatch")
        for platform, entry in (("instagram", ig), ("threads", threads)):
            if entry is None:
                self.registry.register(platform=platform, account=account,
                                       kind="instagrapi_settings", session_file=session,
                                       account_key=account_key)
        if verified_profile(account, self.state_dir) and not refresh:
            return {"ok": True, "status": "connected", "account": account,
                    "existing_profile": True}
        if not self.python.is_file():
            raise AccountError("scrapling_runtime_unavailable")
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
            result = {"ok": connected, "status": "connected" if connected else status,
                      "account": account, "needs_code": data.get("needs_code") is True}
            if status == "dependency_unavailable":
                result.update(dependency=str(data.get("dependency") or "scrapling"),
                              install=str(data.get("install") or ""))
            return result
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
        self.post_marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # O_NOFOLLOW is 0 on Windows; the is_symlink check above covers it there.
        fd = os.open(self.post_marker, os.O_CREAT | os.O_WRONLY | O_NOFOLLOW, 0o600)
        os.close(fd)
        os.utime(self.post_marker, (stamp, stamp))

    def status(self, account: str) -> dict:
        try:
            entry = self.linked(account)
            linked = True
            private = bool(entry["session_exists"] and entry["session_private"])
        except InputError:
            entry, linked, private = self.registry.status("threads", account), False, False
        transport = self.transport(account)
        publish_job = self.transport(account, "publish_job")
        cadence = self.cadence_factory(account)
        with cadence.locked(), Outbox(self.db) as box:
            next_at = self.next_post_at(box, cadence, account)
            self.update_post_marker(box, account)
            counts = {row["status"]: row["count"] for row in box.db.execute(
                "SELECT status,COUNT(*) AS count FROM jobs WHERE platform='threads' "
                "AND account=? GROUP BY status", (account,))}
        ready = linked and transport.ready
        kind = entry["kind"] if entry else None
        return {"ok": True,
                "status": ("ready" if ready else "dependency_unavailable"
                           if linked and transport.dependency else "connect_required"),
                "account": account, "registration_kind": kind,
                "linked_registration": linked,
                "session_private": private,
                "instagram_session_private": private and kind == "instagrapi_settings",
                "scrapling_profile_verified": verified_profile(account, self.state_dir),
                "transport": transport.as_dict(),
                "publish_job_transport": publish_job.as_dict(),
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
        transport = self.transport(account)
        if not transport.ready:
            return self._transport_failure(transport, account=account)
        quiet = self.quiet_hours()
        if quiet:
            return {**quiet, "account": account}
        kwargs = {"limit": limit, "post_id": post_id, "target_url": url}
        if operation == "search":
            kwargs["query"] = query.strip()
        result = self._reader(account).read(account, operation, **kwargs)
        if result.get("target_unavailable"):
            return {"ok": False, "status": "target_unavailable", "account": account,
                    "target_id": post_id, "target_url": url}
        return {"ok": True, "account": account, "transport": transport.name, "result": result}

    def _queued(self, account: str, job: dict, **extra) -> dict:
        result = {"ok": job["status"] in {"queued", "published"},
                  "status": job["status"], "job_id": job["id"], "job": job_summary(job), **extra}
        if job["status"] == "queued":
            result["next_step"] = f"{COMMAND.format(account=account)} publish {job['id']}"
        return result

    def create(self, account: str, *, body_file: str | None = None, body: str | None = None,
               idempotency_key: str | None = None, action="post", post_id="", url="",
               now=False, stdin=None) -> dict:
        """Freeze one exact job and queue it; publish it only when now is True."""
        self.linked(account)
        text = read_body(body_file, body=body, stdin=stdin)
        if action == "reply":
            post_id, url = target_id(post_id), target_url(url)
        if idempotency_key is None:
            idempotency_key = derived_key(action, post_id, url, text)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
            raise InputError("invalid_idempotency_key")
        with Outbox(self.db) as box:
            try:
                job = box.create_draft(
                    platform="threads", account=account, action=action, body=text,
                    media_id=post_id or None, target_id=post_id or None, target_url=url or None,
                    idempotency_key=f"threads-agent:{account}:{action}:{idempotency_key}",
                )
            except ValueError:
                raise InputError("idempotency_key_content_conflict") from None
            if job["status"] == "draft":
                job = box.queue(job["id"])
        if not now:
            return self._queued(account, job, idempotency_key=idempotency_key)
        return self.publish(account, job["id"])

    def reply_inbox(self, account: str, *, comment_id: str, body_file: str | None = None,
                    body: str | None = None, now=False, worker="operator", stdin=None) -> dict:
        """Bind this worker's claimed comment to one exact reply job."""
        self.linked(account)
        comment_id = target_id(comment_id)
        text = read_body(body_file, body=body, stdin=stdin)
        with closing(self.inbox_factory()) as inbox, Outbox(self.db) as box:
            try:
                job = inbox.draft_reply(
                    box, namespace(account), comment_id, text, worker=worker,
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
        if not now:
            return self._queued(account, job)
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
            transport = self.transport(account)
            if not transport.ready:
                return self._transport_failure(transport, job_id=job_id)
            evidence = self._reader(account).reconcile(job)
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
        command = COMMAND.format(account=account)
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
            transport = self.transport(account)
            if not transport.ready:
                return self._transport_failure(transport, job_id=job_id)
            try:
                browser = _BrowserOnce(self.browser_factory())
            except Exception:
                return {"ok": False, "status": "adapter_unavailable", "job_id": job_id}
            # BrowserThreadsAdapter selects the portable or verified Scrapling
            # transport. The existing exact publisher owns claim/release/uncertainty rules.
            output = io.StringIO()
            with redirect_stdout(output):
                self.publisher(job_id, box_factory=lambda: Outbox(self.db),
                               threads_factory=lambda: browser,
                               browser_threads_factory=lambda: browser)
            result = json.loads(output.getvalue().strip().splitlines()[-1])
            saved = box.get(job_id)
            if (saved["status"] == "queued" and
                    isinstance(browser.auth_error, ThreadsDependencyUnavailable)):
                return {"ok": False, "status": "dependency_unavailable", "job_id": job_id,
                        "transport": transport.name,
                        "dependency": browser.auth_error.dependency,
                        "install": browser.auth_error.install}
            if saved["status"] == "failed":
                summary = job_summary(saved)
                for key in ("failure_phase", "failure_reason"):
                    if key in summary:
                        result[key] = summary[key]
                if result.get("status") == "failed_before_send":
                    _next_step(result, f"{command} retry {job_id}")
            if saved["status"] == "uncertain":
                _next_step(result, f"{command} reconcile {job_id}")
            if saved["status"] == "queued":
                quiet = self.quiet_hours(provider_stopped=str(browser.auth_error) == "quiet_hours")
                if quiet:
                    return {**quiet, "job_id": job_id}
                if result.get("status") == "auth_unavailable":
                    _next_step(result, {
                        "browser_login_required": transport.next_step or (
                            LOGIN_COMMAND if transport.name == "portable" else CONNECT_COMMAND
                        ).format(account=account),
                        "browser_setup_required": f"ghost-in-the-box threads-setup --account {account}",
                    }.get(result.get("account_action"), f"{command} publish {job_id}"))
            if job["action"] == "post" and saved["status"] in {"published", "uncertain"}:
                try:
                    cadence.record_post_attempt(self.clock())
                except (CadenceError, OSError, ValueError):
                    # The durable outbox receipt still enforces the minimum gap.
                    result["cadence_recorded"] = False
                if saved["status"] == "published":
                    self.update_post_marker(box, account)
            return result

    def check(self, account: str) -> dict:
        """Ask Threads who each usable session belongs to; never posts."""
        entry = self.linked(account)
        quiet = self.quiet_hours()
        if quiet:
            return {**quiet, "account": account}
        transport = self.transport(account)
        checks = {}
        if transport.ready:
            try:
                verified = self.browser_factory().verify(account)
                checks[transport.name] = {
                    "ok": verified.get("status") == "verified" and verified.get("account") == account,
                    "status": "verified" if verified.get("account") == account else "identity_unverified"}
            except ThreadsDependencyUnavailable as exc:
                checks[transport.name] = {"ok": False, "status": "dependency_unavailable",
                                          "dependency": exc.dependency, "install": exc.install}
            except AuthUnavailable as exc:
                checks[transport.name] = {"ok": False, "status": (
                    "quiet_hours" if str(exc) == "quiet_hours" else "auth_unavailable")}
            except (PreSendFailure, PublishUncertain, ThreadsReadFailure):
                checks[transport.name] = {"ok": False, "status": "identity_unverified"}
        if entry["kind"] == "instagrapi_settings":
            try:
                token = load_token(account, self.auth_dir)
            except ThreadsAuthError:
                token = None
            if token is not None:
                http = self.http_factory()
                try:
                    identity = verify_bearer_identity(token, http=http)
                    checks["mobile"] = {"ok": True, "status": "verified",
                                        "user_id": identity["id"]}
                except ThreadsAuthError as exc:
                    checks["mobile"] = {"ok": False, "status": exc.status}
                finally:
                    close = getattr(http, "close", None)
                    if callable(close):
                        close()
        if not checks:
            return {**self._transport_failure(transport, account=account),
                    "checks": checks}
        failed = next((item for item in checks.values() if not item["ok"]), None)
        result = {"ok": failed is None, "status": "verified" if failed is None else failed["status"],
                  "account": account, "transport": transport.name, "checks": checks}
        if failed and failed["status"] == "dependency_unavailable":
            result.update(dependency=failed["dependency"], install=failed["install"])
        return result

    def mobile_login(self, account: str, *, native=False, code_from_stdin=False,
                     force_refresh=False, stdin=None) -> dict:
        """Mint the Threads mobile bearer from the private credentials file."""
        entry = self.linked(account)
        if entry["kind"] != "instagrapi_settings":
            raise AccountError("mobile_login_requires_linked_instagram")
        if native and force_refresh:
            raise InputError("force_refresh_not_supported_with_native")
        inputs = load_inputs()
        if inputs.username.casefold() != account.casefold():
            raise AccountError("credentials_account_mismatch")
        if native:
            provider = (lambda: read_verification_code(stdin=stdin)) if code_from_stdin else None
            native_mobile_login(inputs, code_provider=provider, auth_dir=self.auth_dir)
        else:
            code = read_verification_code(stdin=stdin) if code_from_stdin else ""
            legacy_mobile_login(inputs, verification_code=code, force_refresh=force_refresh,
                                auth_dir=self.auth_dir)
        return {"ok": True, "status": "saved", "account": account, "needs_code": False,
                "transport": "mobile"}


_PUBLISH = COMMAND + " publish {job_id}"
_JOB = COMMAND + " job {job_id}"
_RECONCILE = COMMAND + " reconcile {job_id}"
_STATUS = COMMAND + " status"
_LOGIN = LOGIN_COMMAND
_CONNECT = CONNECT_COMMAND
_JOB_STATE = (EXIT_RUNTIME, "Job {job_id} is {code}, so this command cannot act on it.", _JOB)
_HELP = "ghost-in-the-box threads {command} --help"
_PEEK = "ghost-in-the-box threads-notifications --account {account} peek"

ERRORS = {
    # usage
    "invalid_target_url": (EXIT_USAGE, "--target-url must be an https://www.threads.com/@handle/"
                           "post/CODE link with no query or fragment.",
                           "Copy the post's Share > Copy link URL, then drop anything after '?'."),
    "numeric_target_id_required": (EXIT_USAGE, "--target-id must be the numeric Threads post ID.",
                                   COMMAND + " read target --target-url URL"),
    "text_must_contain_1_to_500_characters": (EXIT_USAGE, "Threads text must contain 1 to 500 "
                                              "characters.", "Shorten the text to 500 characters "
                                              "or fewer, then run the command again."),
    "body_input_required": (EXIT_USAGE, "Give the text with exactly one of --body TEXT or "
                            "--body-file PATH (use - to read stdin).", _HELP),
    "body_file_unreadable": (EXIT_USAGE, "The --body-file could not be read as UTF-8 text.",
                             "Save the file as UTF-8 text, or pass the text with --body."),
    "file_not_found": (EXIT_USAGE, "The --body-file does not exist.",
                       "Check the path, or pass the text with --body."),
    "invalid_idempotency_key": (EXIT_USAGE, "--idempotency-key may use letters, digits, '.', '_', "
                                "':' and '-' (1 to 128 characters).",
                                "Pick a key like post-2026-11-01, or leave it out to derive one."),
    "idempotency_key_content_conflict": (EXIT_USAGE, "This idempotency key already belongs to "
                                         "different text.", "Use a new --idempotency-key, or omit "
                                         "it to derive one from the text."),
    "limit_must_be_1_to_25": (EXIT_USAGE, "--limit must be between 1 and 25.",
                              COMMAND + " read own --limit 10"),
    "query_must_contain_2_to_120_characters": (EXIT_USAGE, "--query must contain 2 to 120 "
                                               "printable characters.",
                                               COMMAND + " read search --query 'local software'"),
    "job_not_found": (EXIT_USAGE, "No outbox job {job_id} exists.", "ghost-in-the-box outbox list"),
    "job_account_mismatch": (EXIT_USAGE, "Job {job_id} is not a Threads job for @{account}.",
                             "ghost-in-the-box outbox show {job_id}"),
    "inbox_comment_not_found": (EXIT_USAGE, "That comment is not in @{account}'s Threads inbox.",
                                "ghost-in-the-box threads-notifications --account {account} peek"),
    "inbox_comment_not_claimed_by_worker": (EXIT_USAGE, "This worker has not claimed that comment.",
                                            "ghost-in-the-box threads-notifications --account "
                                            "{account} unseen --worker WORKER"),
    "inbox_comment_skipped": (EXIT_USAGE, "That comment was marked as needing no reply.", _PEEK),
    "inbox_reply_content_conflict": (EXIT_USAGE, "That comment already has a different reply "
                                     "draft.", "ghost-in-the-box outbox list --status draft"),
    "force_refresh_not_supported_with_native": (EXIT_USAGE, "--force-refresh refreshes the "
                                                "Instagram session and cannot be combined with "
                                                "--native.", COMMAND + " mobile-login --force-refresh"),
    "invalid_code_format": (EXIT_USAGE, "The authenticator code must be 6 or 8 digits.",
                            "Pipe a current code: echo 123456 | " + COMMAND +
                            " mobile-login --verification-code-stdin"),
    # account, sign-in or dependency
    "linked_account_registration_required": (EXIT_AUTH, "No usable Threads registration exists "
                                             "for @{account}.", _LOGIN),
    "linked_account_registration_mismatch": (EXIT_AUTH, "The Threads and Instagram registrations "
                                             "for @{account} do not share one session file and "
                                             "account key.", _LOGIN),
    "registration_unsupported": (EXIT_AUTH, "This Threads registration kind is not supported.",
                                 _LOGIN),
    "threads_login_required": (EXIT_AUTH, "The saved Threads login for @{account} is missing or "
                               "readable by other users.", _LOGIN),
    "login_wizard_refresh_required": (EXIT_AUTH, "This account signed in with the login wizard.",
                                      _LOGIN),
    "private_instagram_session_required": (EXIT_AUTH, "connect needs a private (chmod 600) "
                                           "Instagram session file for @{account}.",
                                           "Use " + _LOGIN + " instead, or save an Instagram "
                                           "session with ghost-in-the-box instagram-login."),
    "scrapling_runtime_unavailable": (EXIT_AUTH, "The Python set by SOCIAL_FARM_SCRAPLING_PYTHON "
                                      "does not exist.", "Point SOCIAL_FARM_SCRAPLING_PYTHON at a "
                                      "Python with the browser extra."),
    "auth_unavailable": (EXIT_AUTH, "@{account}'s Threads sign-in cannot be used right now.",
                         "{next_step}"),
    "dependency_unavailable": (EXIT_AUTH, "{dependency} is not installed, so the Threads transport "
                               "cannot run. Nothing was sent.", "{install}"),
    "mobile_login_requires_linked_instagram": (EXIT_AUTH, "mobile-login needs a Threads "
                                               "registration linked to an Instagram session.",
                                               _CONNECT),
    "credentials_account_mismatch": (EXIT_AUTH, "The private credentials file is for a different "
                                     "account than @{account}.",
                                     "Fix the username in the file shown by: ghost-in-the-box config"),
    "local_inputs_invalid": (EXIT_AUTH, "The credentials file or Instagram session for "
                             "@{account} is missing, not private or incomplete.", "ghost-in-the-box doctor"),
    "auth_backend_unavailable": (EXIT_AUTH, "The Instagram client is not installed.",
                                 install_command("instagram")),
    "two_factor_required": (EXIT_AUTH, "Threads asked for an authenticator code.",
                            "Re-run with --verification-code-stdin."),
    "two_factor_unavailable": (EXIT_AUTH, "Threads asked for a second factor this command cannot "
                               "answer.", _LOGIN),
    "code_rejected": (EXIT_AUTH, "The authenticator code was rejected.",
                      "Wait for a new code and re-run once."),
    "checkpoint_required": (EXIT_AUTH, "Threads requires a security checkpoint.",
                            "Finish it in the Threads or Instagram app, then retry."),
    "login_rejected": (EXIT_AUTH, "Threads rejected the login.", _LOGIN),
    "token_missing": (EXIT_AUTH, "The login finished without a Threads bearer.", _LOGIN),
    "identity_mismatch": (EXIT_AUTH, "A saved session or bearer belongs to a different account "
                          "than @{account}.", _LOGIN),
    "bearer_rejected": (EXIT_AUTH, "Threads rejected the saved mobile bearer.",
                        COMMAND + " mobile-login"),
    "local_token_invalid": (EXIT_AUTH, "The saved mobile bearer is missing or not private.",
                            COMMAND + " mobile-login"),
    "linked_account_unavailable": (EXIT_AUTH, "Threads did not offer the linked Instagram "
                                   "account.", _LOGIN),
    "identity_unverified": (EXIT_AUTH, "Threads could not confirm the session is @{account}.",
                            _LOGIN),
    # runtime
    "quiet_hours": (EXIT_RUNTIME, "Threads is quiet from midnight to 06:00 New York time.",
                    "Retry after {next_active_local}."),
    "cadence_wait": (EXIT_RUNTIME, "Original posts are spaced at least an hour apart.",
                     "After {next_original_at}: " + _PUBLISH),
    "failed_before_send": (EXIT_RUNTIME, "Job {job_id} stopped before anything was sent and is "
                           "marked failed.", "{next_step}"),
    "not_claimable": (EXIT_RUNTIME, "Another job for this account is in flight or uncertain.",
                      "Reconcile it first: " + COMMAND + " reconcile JOB_ID"),
    "not_queued": _JOB_STATE,
    "adapter_unavailable": (EXIT_RUNTIME, "The Threads browser could not be prepared.", _STATUS),
    "target_unavailable": (EXIT_RUNTIME, "The target post was not found on Threads.",
                           COMMAND + " read target --target-url URL"),
    "read_unavailable": (EXIT_RUNTIME, "The Threads read did not complete.", _STATUS),
    "profile_busy": (EXIT_RUNTIME, "Another command is using this account's browser profile.",
                     "Wait for it to finish, then retry."),
    "local_state_unavailable": (EXIT_RUNTIME, "Local state (outbox, cadence or files) could not "
                                "be used.", "ghost-in-the-box doctor"),
    "operation_unavailable": (EXIT_RUNTIME, "The Threads command did not complete.", _STATUS),
    "connection_unavailable": (EXIT_RUNTIME, "The browser login did not report a result.", _CONNECT),
    "browser_unavailable": (EXIT_RUNTIME, "The browser could not run the login.",
                            "ghost-in-the-box threads-setup --account {account}"),
    "browser_action_unavailable": (EXIT_RUNTIME, "The browser login stopped on an unexpected "
                                   "page.", _LOGIN),
    "provider_throttled": (EXIT_RUNTIME, "Threads is rate limiting this account.",
                           "Wait before retrying."),
    "rate_limited": (EXIT_RUNTIME, "Threads is rate limiting this account.",
                     "Wait before retrying."),
    "unexpected_site": (EXIT_RUNTIME, "The browser left Threads or Instagram.", _LOGIN),
    "identity_unavailable": (EXIT_RUNTIME, "Threads did not answer the identity check.",
                             "Retry later."),
    "request_failed": (EXIT_RUNTIME, "The Threads login request did not complete.",
                       "Retry later."),
    "local_auth_failed": (EXIT_RUNTIME, "The Threads login failed locally.", "ghost-in-the-box doctor"),
    "session_save_failed": (EXIT_RUNTIME, "The refreshed Instagram session could not be saved.",
                            "ghost-in-the-box doctor"),
    "token_save_failed": (EXIT_RUNTIME, "The Threads bearer could not be saved.", "ghost-in-the-box doctor"),
    "unconfirmed": (EXIT_RUNTIME, "No exact receipt was found; job {job_id} stays uncertain.",
                    "Check the profile on Threads, then reconcile again or resolve it with "
                    "ghost-in-the-box outbox resolve."),
    "inbox_reply_unavailable": (EXIT_RUNTIME, "The inbox reply could not be drafted.", _PEEK),
    "draft": _JOB_STATE, "queued": (EXIT_RUNTIME, "Job {job_id} is queued.", _PUBLISH),
    "in_flight": _JOB_STATE, "failed": (EXIT_RUNTIME, "Job {job_id} failed and was not sent.", _JOB),
    "superseded": _JOB_STATE, "published": (EXIT_RUNTIME, "Job {job_id} is already published.",
                                            _JOB),
    # uncertain
    "uncertain": (EXIT_UNCERTAIN, "Job {job_id} may have been sent; the outcome is unknown.",
                  "Reconcile before any retry: " + _RECONCILE),
}

HELP = {
    "connect": "use the login-wizard session, or link a saved Instagram session to a "
               "verified browser profile",
    "status": "local registration, transport and outbox status; no provider request",
    "check": "ask Threads who each saved session belongs to; posts nothing",
    "read": "bounded reads through the selected transport",
    "post": "freeze text as a queued post job; add --now to publish it immediately",
    "reply": "freeze text as a queued reply job; add --now to publish it immediately",
    "publish": "publish one queued job now (respects quiet hours and post spacing)",
    "job": "show one outbox job; read only",
    "reconcile": "confirm an uncertain job from a provider receipt; never resends",
    "retry": "requeue and publish a job that failed before any submit",
    "reply-inbox": "reply to a claimed inbox comment as a queued job; add --now to publish",
    "mobile-login": "mint the Threads mobile bearer from the private credentials file",
}


@guarded("threads")
def main(argv=None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", metavar="PATH", help="private outbox path (normally use the shared default)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name):
        return commands.add_parser(name, help=HELP[name], description=HELP[name])

    def add_body(command):
        add_body_arguments(command, noun="message text")

    def add_now(command):
        timing = command.add_mutually_exclusive_group()
        timing.add_argument("--now", action="store_true", help="publish immediately after queueing")
        timing.add_argument("--queue-only", action="store_true",
                            help="queue without sending (the default; kept for old scripts)")

    connect = add("connect")
    connect.add_argument("--refresh", action="store_true",
                         help="run one linked login even if a profile exists")
    add("status")
    add("check")
    read = add("read")
    read.add_argument("operation", choices=("me", "own", "replies", "target", "search", "feed"))
    read.add_argument("--limit", type=int, default=10, metavar="N", help="1..25 (default 10)")
    read.add_argument("--target-id", default="", metavar="ID", help="numeric post ID for replies/target")
    read.add_argument("--target-url", default="", metavar="URL", help="post link for replies/target")
    read.add_argument("--query", default="", help="topic phrase for search")
    for name in ("post", "reply"):
        command = add(name)
        add_body(command)
        command.add_argument("--idempotency-key", metavar="KEY",
                             help="stable key for this intended message (default: derived from "
                                  "the text and target)")
        add_now(command)
        if name == "reply":
            command.add_argument("--target-id", required=True, metavar="ID", help="numeric post ID")
            command.add_argument("--target-url", required=True, metavar="URL", help="link to the post")
    for name in ("publish", "job", "reconcile", "retry"):
        add(name).add_argument("job_id", metavar="JOB_ID")
    inbox_reply = add("reply-inbox")
    inbox_reply.add_argument("--comment-id", required=True, metavar="ID")
    add_body(inbox_reply)
    add_now(inbox_reply)
    inbox_reply.add_argument("--worker", default="operator", help="the reviewer that claimed the comment")
    mobile = add("mobile-login")
    mobile.add_argument("--native", action="store_true",
                        help="experimental Threads-app login instead of the Instagram session")
    mobile.add_argument("--verification-code-stdin", action="store_true",
                        help="read a current authenticator code from stdin")
    mobile.add_argument("--force-refresh", action="store_true",
                        help="refresh the linked Instagram session and bearer together")
    add_account_argument(parser, *commands.choices.values(), help="your Threads handle", type=account_name)
    args = parser.parse_args(argv)
    require_account(parser, args, example="threads --account YOUR_HANDLE status")
    job_id = getattr(args, "job_id", None)
    exit_override = None
    try:
        agent = ThreadsAgent(db=args.db)
        if args.command == "connect":
            result = agent.connect(args.account, refresh=args.refresh)
        elif args.command == "status":
            result = agent.status(args.account)
        elif args.command == "check":
            result = agent.check(args.account)
        elif args.command == "read":
            result = agent.read(args.account, args.operation, limit=args.limit,
                                post_id=args.target_id, url=args.target_url, query=args.query)
        elif args.command in {"post", "reply"}:
            result = agent.create(args.account, body_file=args.body_file, body=args.body,
                                  idempotency_key=args.idempotency_key, action=args.command,
                                  post_id=getattr(args, "target_id", ""),
                                  url=getattr(args, "target_url", ""), now=args.now)
        elif args.command == "publish":
            result = agent.publish(args.account, args.job_id)
        elif args.command == "reconcile":
            result = agent.reconcile(args.account, args.job_id)
        elif args.command == "retry":
            result = agent.retry(args.account, args.job_id)
        elif args.command == "reply-inbox":
            result = agent.reply_inbox(args.account, comment_id=args.comment_id,
                                       body_file=args.body_file, body=args.body,
                                       now=args.now, worker=args.worker)
        elif args.command == "mobile-login":
            result = agent.mobile_login(args.account, native=args.native,
                                        code_from_stdin=args.verification_code_stdin,
                                        force_refresh=args.force_refresh)
        else:
            result = agent.job(args.account, args.job_id)
    except InputError as exc:
        result, exit_override = exc.result(), exc.exit_code
    except ThreadsDependencyUnavailable as exc:
        result = {"ok": False, "status": "dependency_unavailable",
                  "dependency": exc.dependency, "install": exc.install}
    except ThreadsAuthError as exc:
        result = {"ok": False, "status": exc.status, "needs_code": exc.needs_code}
    except ProfileBusy:
        result = {"ok": False, "status": "profile_busy"}
    except ThreadsReadFailure:
        result = {"ok": False, "status": "read_unavailable"}
    except (OSError, ValueError, CadenceError):
        result = {"ok": False, "status": "local_state_unavailable"}
    except Exception:
        # Provider exception text may include credentials, HTML or private URLs.
        result = {"ok": False, "status": "operation_unavailable"}
    code = attach_error(result, ERRORS, account=args.account, job_id=job_id or result.get("job_id"),
                        command=args.command)
    if exit_override is not None and result.get("status") not in ERRORS:
        code = exit_override
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
