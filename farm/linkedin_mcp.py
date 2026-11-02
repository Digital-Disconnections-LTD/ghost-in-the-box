"""LinkedIn tools backed by the saved browser session and the durable outbox.

Reads go through farm.adapters.linkedin. Posts are outbox jobs: drafted, queued,
previewed and published by the same code paths as `ghost-in-the-box outbox` and
`ghost-in-the-box publish`, never by a second sender. No cookies, session files
or raw provider answers cross the protocol.
"""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import sqlite3

from farm import mcp_server
from farm.accounts import AccountRegistry, check_handle, handle_rule
from farm.adapters import AuthUnavailable, DependencyUnavailable, PreSendFailure, ReadFailure, safe_reason
from farm.adapters.linkedin import (
    ALT_TEXT_LIMIT, BASE, MAX_IMAGE_BYTES, SHARE_QUERY_ENV, LinkedInAdapter, media_summary, share_query_id,
)
from farm.mcp_server import ToolInputError, boolean, choice, integer, string
from farm.outbox import JOB_STATES, Outbox, parse_time, utc_now, utc_text
from farm.session_files import read_session
from scripts.social_publish_job import dry_run as publish_dry_run, run as publish_exact_job

AccountSelectionError = mcp_server.AccountSelectionError
SERVER_NAME = "ghost-in-the-box-linkedin"
SERVER_TITLE = "Ghost in the Box: LinkedIn"
ACCOUNT_ENV = "SOCIAL_FARM_LINKEDIN_ACCOUNT"
SESSION_KIND = "cookie_jar"
TEXT_LIMIT = 3000
TIME_EXAMPLE = "2026-11-02T09:00:00-05:00"
LIST_FILTERS = ["active", "all", *JOB_STATES]
PROVIDER_ID_PATTERN = r"^urn:li:(?:share|ugcPost):[0-9]{1,30}$"
# Handles that appear in docs and examples; they are refused unless really registered.
PLACEHOLDER_ACCOUNTS = frozenset({
    "your_handle", "your-handle", "your_public_handle", "your-public-handle",
    "example_creator", "example-creator",
})
MAX_MB = MAX_IMAGE_BYTES // (1024 * 1024)


def login_hint(account):
    return f"sign in with: ghost-in-the-box login start --platform linkedin --account {account}"


def valid_handle(handle):
    try:
        check_handle("linkedin", handle)
    except ValueError:
        return False
    return True


def is_placeholder(account):
    return account.casefold() in PLACEHOLDER_ACCOUNTS


def resolve_account(explicit=None, *, environ=None, registry_factory=AccountRegistry):
    """Pick the account: --account, SOCIAL_FARM_LINKEDIN_ACCOUNT, else the only registered one."""
    return mcp_server.select_account(
        explicit, platform="linkedin", label="LinkedIn", env_var=ACCOUNT_ENV,
        environ=environ, registry_factory=registry_factory, valid_handle=valid_handle,
        invalid_message=f"--account must be {handle_rule('linkedin')}",
        invalid_hint="pass your public profile handle (the part after linkedin.com/in/), "
                     "for example --account jane-doe-123",
        placeholders=PLACEHOLDER_ACCOUNTS, login_hint=login_hint)


def tool(name, title, description, properties=None, required=(), *, read_only=False, external=False,
         destructive=False):
    return mcp_server.make_tool("linkedin", name, title, description, properties, required,
                                read_only=read_only, external=external, destructive=destructive)


JOB_ID = string("Exact LinkedIn outbox job ID from linkedin_draft_post or linkedin_list_jobs.", maximum=100)
LIMIT = integer("Maximum items in this page (LinkedIn allows 1-25).", 10, 1, 25)
START = integer("Offset of the first item; use next_start from the previous page.", 0, 0, 10000)
HANDLE = string("The member's public profile handle: the part after linkedin.com/in/, without @.",
                maximum=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
CONVERSATION_ID = string("Conversation ID exactly as returned by linkedin_conversations.", maximum=300,
                         pattern=r"^[A-Za-z0-9_=+/:.,()-]+$")
REASON = string("The evidence you checked or what you corrected.", maximum=4000)

TOOLS = [
    tool("status", "LinkedIn account status",
         "Check the configured LinkedIn account without any network request. Reports whether its saved "
         "browser session file is present, private and complete, the registry kind, whether the share "
         "transport is configured, and this account's job counts; returns no cookies. Call this first.",
         read_only=True),
    tool("me", "Confirm the LinkedIn identity",
         "Confirm which LinkedIn member the saved session belongs to, with one bounded read. "
         "Returns the public handle and member ID, and fails if the session belongs to another account.",
         read_only=True, external=True),
    tool("profile", "Read a LinkedIn profile",
         "Read one LinkedIn member's profile by public handle (the part after linkedin.com/in/). "
         "Returns LinkedIn's profile view data as received.",
         {"handle": HANDLE}, ["handle"], read_only=True, external=True),
    tool("posts", "Read own LinkedIn posts",
         "Read one bounded page of the connected account's own recent LinkedIn posts. "
         "Use next_start to read the following page.",
         {"limit": LIMIT, "start": START}, read_only=True, external=True),
    tool("conversations", "List LinkedIn conversations",
         "List one bounded page of the connected account's recent LinkedIn message conversations. "
         "Pass a conversation ID to linkedin_messages; this server never sends messages.",
         {"limit": LIMIT, "start": START}, read_only=True, external=True),
    tool("messages", "Read a LinkedIn conversation",
         "Read one bounded page of messages in one LinkedIn conversation. "
         "The conversation ID comes from linkedin_conversations; nothing is sent.",
         {"conversation_id": CONVERSATION_ID, "limit": LIMIT, "start": START}, ["conversation_id"],
         read_only=True, external=True),
    tool("draft_post", "Draft a LinkedIn post",
         "Save exact post text, with at most one local image and its alt text, as a LinkedIn outbox draft for "
         "this account. Sends nothing. Reuse idempotency_key to get the same draft back on a retry; different "
         "content with the same key is refused. Returns the job ID and the image summary the publisher will "
         "check; next call linkedin_queue_job.",
         {"text": string(f"Exact post text, 1-{TEXT_LIMIT} characters, including line breaks.",
                         maximum=TEXT_LIMIT),
          "source_url": string("Optional source link kept on the job record; it is not added to the text.",
                               maximum=2048, pattern=r"^https?://\S+$"),
          "media_path": string(f"Optional absolute path to one local JPEG, PNG, GIF or WebP image of at most "
                               f"{MAX_MB} MiB. A LinkedIn post carries one image at most."),
          "alt_text": string(f"Optional alt text describing the image, one line of at most {ALT_TEXT_LIMIT} "
                             "characters. Needs media_path.", maximum=ALT_TEXT_LIMIT),
          "idempotency_key": string("Stable unique key for this intended post.", maximum=200)},
         ["text"]),
    tool("list_jobs", "List LinkedIn jobs",
         "List this account's LinkedIn outbox jobs, newest first. "
         "The default shows active jobs: draft, queued, in_flight and uncertain.",
         {"status": choice("Job status filter.", LIST_FILTERS, "active"),
          "limit": integer("Maximum jobs.", 20, 1, 100)}, read_only=True),
    tool("show_job", "Inspect one LinkedIn job",
         "Inspect one LinkedIn job's exact text, image, alt text, schedule, last error and receipt. "
         "Lease tokens are omitted.", {"job_id": JOB_ID}, ["job_id"], read_only=True),
    tool("queue_job", "Queue a LinkedIn draft",
         "Freeze a LinkedIn draft's exact text and image hash and schedule it. Sends nothing; "
         "linkedin_publish_job sends it. Without scheduled_at the job is due now.",
         {"job_id": JOB_ID,
          "scheduled_at": string(f"Optional ISO 8601 time with a timezone, for example {TIME_EXAMPLE}.",
                                 maximum=100)},
         ["job_id"]),
    tool("cancel_job", "Cancel a LinkedIn job",
         "Cancel a LinkedIn draft or queued job before it is published. "
         "This cannot be undone: a cancelled job is never sent.",
         {"job_id": JOB_ID, "reason": string("Optional reason kept on the job record.", maximum=1000)},
         ["job_id"], destructive=True),
    tool("dry_run", "Preview a LinkedIn publish",
         "Preview what linkedin_publish_job would send for one job, without any network request. Shows the "
         "frozen text, the image summary, the target account and any blockers, with a hint for each blocker; "
         "changes nothing.",
         {"job_id": JOB_ID,
          "now": boolean("Ignore the schedule, as linkedin_publish_job with now=true would.")},
         ["job_id"], read_only=True),
    tool("publish_job", "Publish a LinkedIn job",
         "Publish exactly one queued LinkedIn job through the outbox publisher; this makes a public LinkedIn "
         "post. A job scheduled later is refused as not_due unless now is true. A published job returns its "
         "receipt again. An ambiguous send becomes uncertain and is never resent: check the feed and call "
         "linkedin_resolve_uncertain. An auth failure keeps the job queued.",
         {"job_id": JOB_ID, "now": boolean("Send even if the job is scheduled for later.")},
         ["job_id"], external=True, destructive=True),
    tool("retry_failed", "Retry a failed LinkedIn job",
         "Requeue a LinkedIn job that failed before sending, once you corrected the cause. "
         "Needs unchanged frozen content and a reason. Refuses uncertain jobs and jobs that may already be "
         "public (possibly_sent). Does not send.",
         {"job_id": JOB_ID, "reason": REASON}, ["job_id", "reason"]),
    tool("resolve_uncertain", "Resolve an uncertain LinkedIn job",
         "Record what really happened to an uncertain LinkedIn job after checking the account's feed. "
         "published needs the observed provider_id; queued makes it publishable again and needs direct "
         "evidence that nothing was posted; failed retires it. Does not send.",
         {"job_id": JOB_ID,
          "outcome": choice("What the evidence shows.", ["published", "queued", "failed"]),
          "reason": REASON,
          "provider_id": string("For published: the post's URN, for example urn:li:share:7000000000000000000 "
                                "(from its feed/update link).", maximum=100, pattern=PROVIDER_ID_PATTERN)},
         ["job_id", "outcome", "reason"]),
]
TOOL_BY_NAME = {item["name"]: item for item in TOOLS}

SUMMARY_FIELDS = ("id", "platform", "account", "action", "status", "scheduled_at", "attempts",
                  "provider_id", "permalink", "created_at", "updated_at")
DETAIL_FIELDS = SUMMARY_FIELDS + ("body", "media_path", "media_alt", "media_sha256", "body_sha256",
                                  "source_urls", "last_error", "failure_kind", "lease_until")
NEXT_AFTER_DRAFT = ("preview it with linkedin_dry_run after queueing it with linkedin_queue_job, "
                    "then send it with linkedin_publish_job")


def _job_summary(job, *, detail=False):
    return {key: job.get(key) for key in (DETAIL_FIELDS if detail else SUMMARY_FIELDS)}


class LinkedInTools:
    def __init__(self, *, account, outbox_path=None, registry=None, adapter_factory=None):
        if not isinstance(account, str) or not valid_handle(account):
            raise ToolInputError("invalid configured account", code="invalid_account")
        self.account = account
        self.outbox_path = outbox_path
        self.registry = registry or AccountRegistry()
        self.adapter_factory = adapter_factory or (lambda: LinkedInAdapter(self.registry))

    def box(self):
        return Outbox(self.outbox_path)

    def selected(self, box, job_id):
        job = box.get(job_id)
        if not job or job["platform"] != "linkedin" or job["account"] != self.account:
            raise ToolInputError("LinkedIn job not found for the configured account", code="job_not_found",
                                 hint="list this account's job IDs with linkedin_list_jobs")
        return job

    def call(self, name, arguments):
        if name not in TOOL_BY_NAME:
            raise ToolInputError("unknown LinkedIn tool", code="unknown_tool",
                                 hint="call tools/list for the available names")
        self.require_real_account()
        args = mcp_server.prepare_call(TOOL_BY_NAME, name, arguments, unknown="unknown LinkedIn tool")
        return getattr(self, name.removeprefix("linkedin_"))(**args)

    def require_real_account(self):
        # A copied example config must not quietly read and write an empty fake account.
        if is_placeholder(self.account) and self.registry.get("linkedin", self.account) is None:
            raise ToolInputError(f"'{self.account}' is the documentation placeholder, not a registered account",
                code="placeholder_account",
                hint="restart the server with --account set to your own registered LinkedIn handle")

    def _read(self, operation):
        adapter = self.adapter_factory()
        try:
            return operation(adapter)
        finally:
            _close(adapter)

    # --- local status -------------------------------------------------------

    def _session_view(self, entry):
        view = {"present": False, "private": False, "regular_file": False, "cookies_present": False}
        if not entry:
            return view
        path = Path(entry["session_file"])
        view["present"] = bool(entry.get("session_exists"))
        view["private"] = bool(entry.get("session_private"))
        view["regular_file"] = path.is_file() and not path.is_symlink()
        if view["present"] and view["private"] and view["regular_file"]:
            try:
                saved = read_session(path)
                names = {cookie.get("name") for cookie in saved.get("cookies", []) if isinstance(cookie, dict)}
                view["cookies_present"] = (saved.get("platform") == "linkedin" and
                                           str(saved.get("account", "")).casefold() == self.account.casefold()
                                           and {"li_at", "JSESSIONID"} <= names)
            except (OSError, ValueError, TypeError, AttributeError):
                view["cookies_present"] = False
        return view

    def status(self):
        problems = []
        entry = None
        try:
            entry = self.registry.status("linkedin", self.account)
        except (OSError, ValueError):
            problems.append("registry_unreadable")
        if entry is None and not problems:
            problems.append("account_not_registered")
        kind = entry.get("kind") if entry else None
        session = self._session_view(entry)
        if entry:
            if kind != SESSION_KIND:
                problems.append("wrong_session_kind")
            if not session["present"]:
                problems.append("session_missing")
            elif not session["regular_file"]:
                problems.append("session_not_regular_file")
            elif not session["private"]:
                problems.append("session_not_private")
            elif not session["cookies_present"]:
                problems.append("session_incomplete")
        configured = bool((os.environ.get(SHARE_QUERY_ENV) or "").strip())
        try:
            share_query_id()
            query_valid = True
        except PreSendFailure:
            query_valid = False
            problems.append("share_query_id_invalid")
        with self.box() as box:
            counts = {row["status"]: row["count"] for row in box.db.execute(
                "SELECT status,COUNT(*) AS count FROM jobs WHERE platform='linkedin' AND account=? GROUP BY status",
                (self.account,))}
        result = {"ok": True, "account": self.account, "registered": entry is not None,
                  "registry_kind": kind, "expected_kind": SESSION_KIND, "session": session,
                  "transport": {"ready": not problems, "share_query_id": "environment" if configured else "default",
                                "share_query_id_valid": query_valid, "problems": problems},
                  "jobs": counts, "publishing": "explicit queued job IDs only, through linkedin_publish_job",
                  "network_requests": 0}
        if problems:
            # Problems without a specific fix (unregistered, missing or incomplete session) need a new login.
            result["hint"] = STATUS_HINTS.get(problems[0]) or login_hint(self.account)
        return result

    # --- reads --------------------------------------------------------------

    def me(self):
        identity = self._read(lambda adapter: adapter.me(self.account))
        return {"ok": True, "account": self.account, "identity": identity}

    def profile(self, handle):
        data = self._read(lambda adapter: adapter.profile(self.account, handle))
        return {"ok": True, "account": self.account, "handle": handle, "profile": data}

    def posts(self, limit=10, start=0):
        data = self._read(lambda adapter: adapter.own_posts(self.account, limit=limit, start=start))
        return {"ok": True, "account": self.account, "limit": limit, "start": start,
                "next_start": start + limit, "posts": data}

    def conversations(self, limit=10, start=0):
        data = self._read(lambda adapter: adapter.conversations(self.account, limit=limit, start=start))
        return {"ok": True, "account": self.account, "limit": limit, "start": start,
                "next_start": start + limit, "conversations": data}

    def messages(self, conversation_id, limit=10, start=0):
        data = self._read(lambda adapter: adapter.messages(self.account, conversation_id, limit=limit, start=start))
        return {"ok": True, "account": self.account, "conversation_id": conversation_id, "limit": limit,
                "start": start, "next_start": start + limit, "messages": data}

    # --- outbox -------------------------------------------------------------

    def draft_post(self, text, source_url=None, media_path=None, alt_text=None, idempotency_key=None):
        if media_path is not None and not Path(media_path).is_absolute():
            raise ToolInputError("media_path must be an absolute local path", code="invalid_media",
                                 hint="pass the image's absolute path, for example /home/me/photo.png")
        key = f"linkedin-mcp:post:{self.account}:{idempotency_key}" if idempotency_key else None
        with self.box() as box:
            job = box.create_draft(platform="linkedin", account=self.account, body=text,
                                   media_path=media_path, media_alt=alt_text,
                                   source_urls=[source_url] if source_url else None, idempotency_key=key)
        return {"ok": True, "job_id": job["id"], "job": _job_summary(job, detail=True),
                "media": media_summary(job), "next_step": NEXT_AFTER_DRAFT}

    def list_jobs(self, status="active", limit=20):
        where = "platform='linkedin' AND account=?"
        params = [self.account]
        if status == "active":
            where += " AND status IN ('draft','queued','in_flight','uncertain')"
        elif status != "all":
            where += " AND status=?"
            params.append(status)
        with self.box() as box:
            rows = box.db.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ?",
                                  (*params, limit))
            jobs = [_job_summary(dict(row)) for row in rows]
        return {"ok": True, "account": self.account, "status": status, "jobs": jobs}

    def show_job(self, job_id):
        with self.box() as box:
            job = self.selected(box, job_id)
        return {"ok": True, "job": _job_summary(job, detail=True), "media": media_summary(job)}

    def queue_job(self, job_id, scheduled_at=None):
        if scheduled_at is not None:
            try:
                parse_time(scheduled_at)
            except ValueError:
                raise ToolInputError(f"scheduled_at {scheduled_at!r} is not an ISO 8601 time with a timezone",
                                     code="invalid_time",
                                     hint=f"pass scheduled_at like {TIME_EXAMPLE}, or end it with Z for UTC") from None
        with self.box() as box:
            self.selected(box, job_id)
            job = box.queue(job_id, at=scheduled_at)
        return {"ok": True, "job": _job_summary(job, detail=True), "media": media_summary(job),
                "next_step": "preview it with linkedin_dry_run, then send it with linkedin_publish_job"}

    def cancel_job(self, job_id, reason=None):
        with self.box() as box:
            self.selected(box, job_id)
            job = box.cancel(job_id, reason=reason)
        return {"ok": True, "job": _job_summary(job, detail=True)}

    def dry_run(self, job_id, now=False):
        with self.box() as box:
            job = self.selected(box, job_id)
            _code, result = publish_dry_run(box, job, now=now, registry_factory=lambda: self.registry)
        hints = {**BLOCKER_HINTS, "account_not_registered": login_hint(self.account),
                 "session_missing": login_hint(self.account)}
        result["blocker_hints"] = {blocker: hints.get(blocker, "inspect the job with linkedin_show_job")
                                   for blocker in result.get("blockers", [])}
        return result

    def publish_job(self, job_id, now=False):
        with self.box() as box:
            job = self.selected(box, job_id)
            if job["status"] == "published":
                return {"ok": True, "status": "published", "already_published": True, "job_id": job_id,
                        "platform": "linkedin", "provider_id": job["provider_id"], "permalink": job["permalink"]}
        created = []

        def adapter():
            created.append(self.adapter_factory())
            return created[-1]

        # The exact-job publisher owns the claim, the before_send boundary, ambiguity
        # and receipts. Its last line is one bounded JSON result.
        captured = io.StringIO()
        try:
            with redirect_stdout(captured):
                publish_exact_job(job_id, box_factory=self.box, linkedin_factory=adapter, now=now,
                                  registry_factory=lambda: self.registry, envelope=True)
        finally:
            for item in created:
                _close(item)
        return json.loads(captured.getvalue().splitlines()[-1])

    def retry_failed(self, job_id, reason):
        with self.box() as box:
            self.selected(box, job_id)
            job = box.retry_failed(job_id, reason=reason)
        return {"ok": True, "job": _job_summary(job, detail=True),
                "next_step": "send it with linkedin_publish_job"}

    def resolve_uncertain(self, job_id, outcome, reason, provider_id=None):
        if outcome == "published" and not provider_id:
            raise ToolInputError("outcome published needs the observed provider_id", code="provider_id_required",
                                 hint="open the post on the account's LinkedIn feed and pass the urn:li:share:N "
                                      "or urn:li:ugcPost:N from its feed/update link as provider_id")
        if provider_id and outcome != "published":
            raise ToolInputError("provider_id applies only to outcome published", code="invalid_arguments",
                                 hint="leave out provider_id for outcome queued or failed")
        with self.box() as box:
            job = self.selected(box, job_id)
            # A crashed publisher may leave the job leased. Only an expired lease becomes
            # uncertain; a live sender keeps ownership and can still record its receipt.
            if job["status"] == "in_flight" and job["lease_until"] and job["lease_until"] <= utc_text(utc_now()):
                box.expire_leases()
                job = self.selected(box, job_id)
            if job["status"] != "uncertain":
                raise ToolInputError(f"job is {job['status']}, not uncertain", code="not_uncertain",
                                     hint="check its status with linkedin_show_job; only an uncertain job "
                                          "can be resolved")
            permalink = f"{BASE}/feed/update/{provider_id}/" if provider_id else None
            job = box.resolve_uncertain(job_id, outcome=outcome, reason=reason, provider_id=provider_id,
                                        permalink=permalink)
        return {"ok": True, "job": _job_summary(job, detail=True)}


def _close(adapter):
    http = getattr(adapter, "http", None)
    close = getattr(http, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


# --- errors ------------------------------------------------------------------

INTERNAL_HINT = "run ghost-in-the-box doctor; for a job, inspect it with linkedin_show_job before trying again"
LOCAL_REJECT_HINT = ("the local outbox refused this change; check the job with linkedin_show_job or "
                     "linkedin_list_jobs and adjust the arguments")
UNCERTAIN_HINT = ("do not draft it again; check the account's LinkedIn feed for the post, then call "
                  "linkedin_resolve_uncertain with what you saw")
STATUS_HINTS = {
    "registry_unreadable": "run ghost-in-the-box doctor and ghost-in-the-box accounts list",
    "session_not_regular_file": "run ghost-in-the-box doctor; the saved session must be a regular file",
    "session_not_private": "run ghost-in-the-box doctor; it prints the command that makes the session private",
    "share_query_id_invalid": f"fix or unset {SHARE_QUERY_ENV}; ghost-in-the-box config shows its value",
}
BLOCKER_HINTS = {
    "job_not_queued": "queue a draft with linkedin_queue_job; for other states see linkedin_show_job",
    "not_due": "wait until scheduled_at, or call linkedin_publish_job with now=true",
    "payload_changed": "the frozen text or image changed; draft the post again with linkedin_draft_post",
    "account_busy": "another job for this account is in flight or uncertain; find it with linkedin_list_jobs "
                    "and resolve it with linkedin_resolve_uncertain",
    "account_registry_unreadable": "run ghost-in-the-box doctor and ghost-in-the-box accounts list",
    "account_key_mismatch": "the job belongs to an older registration of this account; draft it again with "
                            "linkedin_draft_post",
    "media_invalid": "the image fails LinkedIn's checks (see media.problem); draft the post again with a valid "
                     "image using linkedin_draft_post",
}
# Outbox refusals carry CLI hints; MCP clients get the matching tool or argument instead.
OUTBOX_HINTS = {
    "not_found": "list this account's job IDs with linkedin_list_jobs",
    "not_draft": "check its status with linkedin_show_job; only a draft can be queued",
    "not_uncertain": "check its status with linkedin_show_job; only an uncertain job can be resolved",
    "not_failed": "check its status with linkedin_show_job; only a failed job can be retried",
    "not_cancellable": ("resolve an uncertain job with linkedin_resolve_uncertain; a failed job can be "
                        "requeued with linkedin_retry_failed"),
    "job_leased": "a publisher holds the job; check it again shortly with linkedin_show_job",
    "already_published": ("it is already public; linkedin_show_job shows the receipt. Delete the post on "
                          "LinkedIn if it should not be public"),
    "idempotency_conflict": "use a new idempotency_key, or find the existing job with linkedin_list_jobs",
    "alt_requires_media": "pass media_path with the image, or leave out alt_text",
    "invalid_alt_text": f"keep alt_text on one line and at most {ALT_TEXT_LIMIT} characters",
    "linkedin_single_image_only": "pass one image in media_path; carousels are for Instagram",
    "linkedin_text_required": "give the post text in text",
    "invalid_media": f"pass media_path to one JPEG, PNG, GIF or WebP image of at most {MAX_MB} MiB",
    "payload_changed": "the frozen text or image changed; draft the post again with linkedin_draft_post",
    "possibly_sent": ("check the account's LinkedIn feed; if nothing was published, draft the post again with "
                      "linkedin_draft_post"),
}
PUBLISH_HINTS = {
    "not_found": "list this account's job IDs with linkedin_list_jobs",
    "not_queued": "check its status with linkedin_show_job",
    "not_claimable": ("another job for this account is in flight or uncertain; find it with linkedin_list_jobs, "
                      "resolve it with linkedin_resolve_uncertain, then publish again"),
    "dependency_unavailable": "run ghost-in-the-box doctor; the job stays queued",
    "adapter_unavailable": "run ghost-in-the-box doctor; the job stays queued",
    "rejected": ("nothing was published; inspect it with linkedin_show_job. If the cause was outside the post, "
                 "call linkedin_retry_failed; otherwise draft a corrected post with linkedin_draft_post"),
    "failed_before_send": ("nothing was sent; inspect it with linkedin_show_job, correct the cause, then call "
                           "linkedin_retry_failed"),
    "uncertain": UNCERTAIN_HINT,
    "lease_lost": UNCERTAIN_HINT,
    "outbox_refused": "inspect the job with linkedin_show_job",
    "outbox_unavailable": "call again shortly; if it persists, run ghost-in-the-box doctor",
    "unsupported_platform": "list LinkedIn job IDs with linkedin_list_jobs",
}
NOT_QUEUED_HINTS = {
    "draft": "queue it with linkedin_queue_job, then call linkedin_publish_job",
    "uncertain": "resolve it with linkedin_resolve_uncertain before anything else",
    "failed": "if nothing was sent, requeue it with linkedin_retry_failed",
    "published": "it is already published; linkedin_show_job shows the receipt",
    "in_flight": "another publisher holds it; check again shortly with linkedin_show_job",
    "cancelled": "it was cancelled; draft the post again with linkedin_draft_post",
    "superseded": "it was retired; draft the post again with linkedin_draft_post",
}
# These tools never contact LinkedIn, so their local refusals are safe to show.
LOCAL_DETAIL_TOOLS = frozenset(item["name"] for item in TOOLS if not item["annotations"]["openWorldHint"])


def tool_error(code, message, hint):
    return mcp_server.tool_error(code, message, hint, fallback_hint=INTERNAL_HINT)


def _structured_failure(result, account):
    """Give an unsuccessful publisher result the shared error object with tool hints."""
    if result.get("ok") is not False:
        return result
    status = result.get("status")
    status = status if isinstance(status, str) and re.fullmatch(r"[a-z_]{1,80}", status) else "tool_failed"
    existing = result.get("error") if isinstance(result.get("error"), dict) else {}
    code = existing.get("code") if isinstance(existing.get("code"), str) else status
    message = (existing.get("message") if isinstance(existing.get("message"), str)
               else f"the publisher stopped with status {status}")
    hint = PUBLISH_HINTS.get(status)
    if status == "auth_unavailable":
        hint = login_hint(account) + "; the job stays queued"
    elif status == "not_queued":
        hint = NOT_QUEUED_HINTS.get(result.get("job_status"), hint)
    elif status == "not_due":
        hint = (f"wait until {result.get('scheduled_at')}, or call linkedin_publish_job again with now=true")
    elif status == "receipt_not_recorded":
        hint = ("the post is live; once linkedin_show_job reports the job uncertain, call "
                f"linkedin_resolve_uncertain with outcome published and provider_id {result.get('provider_id')}")
    elif hint is None:
        hint = existing.get("hint") if isinstance(existing.get("hint"), str) else "inspect the job with linkedin_show_job"
    return {**result, "error": tool_error(code, message, hint)}


def _tool_result(service, name, arguments):
    try:
        result = mcp_server.quiet_call(service.call, name, arguments)
        if not isinstance(result, dict):
            raise TypeError("tool returned a non-object result")
        result = _structured_failure(result, service.account)
    except ToolInputError as error:
        result = {"ok": False, "error": tool_error(error.code, str(error), error.hint)}
    except DependencyUnavailable as error:
        result = {"ok": False, "error": tool_error("dependency_unavailable", safe_reason(error),
                                                   error.install_hint or "run ghost-in-the-box doctor")}
    except AuthUnavailable as error:
        result = {"ok": False, "error": tool_error(
            "auth_unavailable", safe_reason(error),
            login_hint(service.account) + "; if you just signed in, LinkedIn may be throttling, so wait a few "
            "minutes and call linkedin_status")}
    except ReadFailure as error:
        reason = safe_reason(error)
        if reason == "LinkedIn session belongs to a different account":
            failure = tool_error("account_mismatch", reason, login_hint(service.account))
        else:
            failure = tool_error("read_unavailable", reason,
                                 f"call linkedin_status to check the session; if it is ready, call {name} again later")
        result = {"ok": False, "error": failure}
    except PreSendFailure as error:
        result = {"ok": False, "error": tool_error("rejected", safe_reason(error), LOCAL_REJECT_HINT)}
    except sqlite3.Error as error:
        result = {"ok": False, "error": tool_error(
            "outbox_unavailable", f"the local outbox database could not be used ({type(error).__name__})",
            PUBLISH_HINTS["outbox_unavailable"]), "error_type": type(error).__name__}
    except ValueError as error:
        code = getattr(error, "code", None)
        if type(error).__module__.startswith("farm.") and isinstance(code, str) and str(error):
            # Our own coded refusals (the outbox's, the registry's) are written to be shown.
            hint = OUTBOX_HINTS.get(code, getattr(error, "hint", None))
            failure = tool_error(code, str(error)[:500], hint if isinstance(hint, str) else LOCAL_REJECT_HINT)
        elif name in LOCAL_DETAIL_TOOLS and str(error):
            failure = tool_error("rejected", str(error)[:500], LOCAL_REJECT_HINT)
        else:
            failure = tool_error("internal_error", mcp_server.WITHHELD, INTERNAL_HINT)
        result = {"ok": False, "error": failure, "error_type": type(error).__name__}
    except Exception as error:
        result = {"ok": False, "error": tool_error("internal_error", mcp_server.WITHHELD, INTERNAL_HINT),
                  "error_type": type(error).__name__}
    return mcp_server.tool_content(result)


INSTRUCTIONS = ("LinkedIn uses the configured saved browser session and the durable outbox. "
    "Call linkedin_status first: it checks the account and session locally, without a network request. "
    "Read with linkedin_me, linkedin_profile, linkedin_posts, linkedin_conversations and linkedin_messages. "
    "To post: linkedin_draft_post, linkedin_queue_job, linkedin_dry_run, then linkedin_publish_job, which makes "
    "a real public post. A post carries text and at most one image with alt text. Never draft again to retry "
    "an uncertain send; check the feed and call linkedin_resolve_uncertain. "
    "Failed calls return error.code, error.message and error.hint; follow the hint.")

SERVER = mcp_server.Server(name=SERVER_NAME, title=SERVER_TITLE, instructions=INSTRUCTIONS, tools=TOOLS,
                           call=_tool_result, unknown_tool="Unknown LinkedIn tool")


def dispatch(service, message, session=None):
    return SERVER.dispatch(service, message, session)


def serve(service, stdin=None, stdout=None):
    return SERVER.serve(service, stdin, stdout)


def tool_catalog(tools=None):
    """One-line summaries of the LinkedIn tools, for docs and quick inspection."""
    return mcp_server.tool_catalog(TOOLS if tools is None else tools)


def server_entry():
    return {"server": SERVER_NAME, "command": "ghost-in-the-box linkedin-mcp --account HANDLE",
            "note": "Uses LinkedIn's unofficial web routes through your saved browser session; they may change. "
                    "Posting is experimental and goes through the outbox.",
            "tools": tool_catalog()}


def catalog():
    return mcp_server.catalog_document("ghost-in-the-box linkedin-mcp --list-tools", [server_entry()])
