"""Durable, locally owned publishing outbox.

This module does not contact a social network. Adapters claim one immutable
job, publish it, then record the provider receipt. A lost claim becomes
uncertain and requires reconciliation rather than an automatic repost.
"""

from __future__ import annotations

from farm.config import STATE_ROOT


from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Callable
import uuid

from farm.accounts import AccountRegistry, account_key_matches


DEFAULT_DB = STATE_ROOT / 'outbox.sqlite3'
ACTIVE_STATES = ("draft", "queued", "in_flight", "uncertain")
JOB_STATES = (*ACTIVE_STATES, "published", "failed", "superseded", "cancelled")
PUBLISHABLE_PLATFORMS = ("instagram", "threads", "linkedin")
BRIEF_ONLY_PLATFORMS = ("facebook", "tiktok", "youtube")
PLATFORMS = PUBLISHABLE_PLATFORMS
ACTIONS = ("post", "reply")
FAILURE_KINDS = ("pre_send", "rejected")
SCHEMA_VERSION = 2
BRIEF_ONLY_HINT = ("Facebook, TikTok and YouTube are brief-only: run `ghost-in-the-box brief` and post "
                   "the brief yourself. The outbox publishes to " + ", ".join(PUBLISHABLE_PLATFORMS) + ".")


class OutboxError(ValueError):
    """A refused outbox operation with a stable machine-readable code."""

    def __init__(self, message: str, *, code: str = "invalid_request", hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


def check_platform(platform: str) -> None:
    if platform in BRIEF_ONLY_PLATFORMS:
        raise OutboxError(f"{platform} is a brief-only platform; the outbox cannot publish it",
                          code="brief_only_platform", hint=BRIEF_ONLY_HINT)
    if platform not in PUBLISHABLE_PLATFORMS:
        raise OutboxError(f"unsupported platform: {platform}", code="unsupported_platform",
                          hint="Use one of: " + ", ".join(PUBLISHABLE_PLATFORMS) + ".")


def _state_error(job_id: str, row, wanted: str) -> OutboxError:
    """Why a job is not in the state an operation needs, with the command that shows it."""
    if not row:
        return OutboxError(f"job {job_id} not found", code="not_found",
                           hint="List jobs with: ghost-in-the-box outbox list --status all")
    return OutboxError(f"job is {row['status']}, not {wanted}", code=f"not_{wanted}",
                       hint=f"Check it with: ghost-in-the-box outbox show {job_id}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime:
    if not value:
        return utc_now()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("time needs a timezone")
    return parsed.astimezone(timezone.utc)


def file_sha256(path: str | None) -> str | None:
    if not path:
        return None
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"media file missing: {source}")
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def frozen_payload_matches(job: dict) -> bool:
    """Check queued bytes immediately before an adapter makes a network call."""
    if not job.get("body_sha256"):
        return False
    if hashlib.sha256(job["body"].encode("utf-8")).hexdigest() != job["body_sha256"]:
        return False
    try:
        return file_sha256(job.get("media_path")) == job.get("media_sha256")
    except ValueError:
        return False


def _dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    result["source_urls"] = json.loads(result["source_urls"])
    return result


_BASE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        idempotency_key TEXT UNIQUE,
        platform TEXT NOT NULL,
        account TEXT NOT NULL,
        account_key TEXT NOT NULL,
        action TEXT NOT NULL,
        body TEXT NOT NULL,
        media_path TEXT,
        media_sha256 TEXT,
        body_sha256 TEXT,
        media_id TEXT,
        target_id TEXT,
        target_url TEXT,
        crosspost_threads INTEGER NOT NULL DEFAULT 0,
        source_urls TEXT NOT NULL,
        status TEXT NOT NULL,
        scheduled_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        lease_token TEXT,
        lease_until TEXT,
        provider_id TEXT,
        permalink TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, scheduled_at, created_at)",
    "CREATE INDEX IF NOT EXISTS jobs_account ON jobs(account_key, status)",
    """CREATE TABLE IF NOT EXISTS schema_version (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        version INTEGER NOT NULL
    )""",
)


def _migrate_to_2(db: sqlite3.Connection) -> None:
    """Keep the expired lease token for a late receipt and record why a job failed."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    if "expired_lease_token" not in columns:
        db.execute("ALTER TABLE jobs ADD COLUMN expired_lease_token TEXT")
    if "failure_kind" not in columns:
        db.execute("ALTER TABLE jobs ADD COLUMN failure_kind TEXT")
    db.execute("""UPDATE jobs SET failure_kind=CASE WHEN last_error LIKE 'reconciled:%'
                  THEN 'reconciled' ELSE 'pre_send' END
                  WHERE status='failed' AND failure_kind IS NULL""")


# Forward-only migrations: append (version, function) and raise SCHEMA_VERSION.
# Each function runs once inside the opening transaction and must tolerate a
# database that already has its columns.
MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = ((2, _migrate_to_2),)


class Outbox:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("SOCIAL_FARM_OUTBOX_DB") or DEFAULT_DB).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA busy_timeout=5000")
            self._migrate()
        except BaseException:
            self.db.close()
            raise

    def _schema_version(self) -> int | None:
        try:
            row = self.db.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None

    def _migrate(self) -> None:
        if self._schema_version() == SCHEMA_VERSION:
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for statement in _BASE_SCHEMA:
                self.db.execute(statement)
            # A database without a version row predates versioning: it has
            # exactly the base (version 1) jobs table.
            version = self._schema_version() or 1
            if version > SCHEMA_VERSION:
                raise OutboxError(
                    f"outbox database uses schema {version}; this version understands {SCHEMA_VERSION}",
                    code="schema_too_new", hint="Upgrade Ghost in the Box before using this outbox.")
            for target, migrate in MIGRATIONS:
                if version < target:
                    migrate(self.db)
                    version = target
            self.db.execute("""INSERT INTO schema_version(id,version) VALUES (1,?)
                               ON CONFLICT(id) DO UPDATE SET version=excluded.version""", (version,))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Outbox":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def get(self, job_id: str) -> dict | None:
        return _dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def account_busy(self, account_key: str, *, exclude_id: str | None = None) -> bool:
        """True when another job for this account is in flight or awaiting reconciliation."""
        return self.db.execute(
            """SELECT 1 FROM jobs WHERE account_key=? AND id IS NOT ?
               AND status IN ('in_flight','uncertain') LIMIT 1""",
            (account_key, exclude_id),
        ).fetchone() is not None

    def create_draft(
        self,
        *,
        platform: str,
        account: str,
        body: str,
        action: str = "post",
        account_key: str | None = None,
        media_path: str | None = None,
        media_id: str | None = None,
        target_id: str | None = None,
        target_url: str | None = None,
        crosspost_threads: bool = False,
        source_urls: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        check_platform(platform)
        if action not in ACTIONS:
            raise OutboxError(f"unsupported action: {action}", hint="Use post or reply.")
        if not account.strip() or not (body.strip() or media_path):
            raise ValueError("account and body or media are required")
        if action == "reply" and not target_id:
            raise ValueError("reply requires target_id")
        if action == "reply" and not media_id:
            raise ValueError("reply requires media_id")
        if crosspost_threads and (platform != "instagram" or action != "post"):
            raise ValueError("Threads cross-post applies only to an Instagram post")
        media = str(Path(media_path).expanduser().resolve()) if media_path else None
        sources = list(dict.fromkeys(source_urls or []))
        now = utc_text(utc_now())
        key = idempotency_key or None
        registered = AccountRegistry().get(platform, account)
        canonical_key = (registered["account_key"] if registered else
                         f"meta:{account}" if platform in ("instagram", "threads") else
                         f"{platform}:{account}")
        if (account_key is not None and account_key != canonical_key and
                not account_key_matches(registered, account_key)):
            raise OutboxError("account key differs from the registered account", code="account_key_mismatch",
                              hint=f"Leave out --account-key, or check: ghost-in-the-box accounts show "
                                   f"--platform {platform} --account {account}")
        fields = (platform, account, canonical_key, action,
                  body, media, media_id, target_id, target_url, int(crosspost_threads),
                  json.dumps(sources, ensure_ascii=False))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if key:
                existing = self.db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
                if existing:
                    old = tuple(existing[name] for name in (
                        "platform", "account", "account_key", "action", "body", "media_path",
                        "media_id", "target_id", "target_url", "crosspost_threads", "source_urls"))
                    if old != fields:
                        raise OutboxError("idempotency key already belongs to different content",
                                          code="idempotency_conflict",
                                          hint=f"Use a new --idempotency-key, or inspect the existing job: "
                                               f"ghost-in-the-box outbox show {existing['id']}")
                    self.db.execute("COMMIT")
                    return _dict(existing)
            job_id = str(uuid.uuid4())
            self.db.execute(
                """INSERT INTO jobs
                   (id,idempotency_key,platform,account,account_key,action,body,media_path,
                    media_id,target_id,target_url,crosspost_threads,source_urls,
                    status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)""",
                (job_id, key, *fields, now, now),
            )
            self.db.execute("COMMIT")
            return self.get(job_id)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def queue(self, job_id: str, *, at: str | None = None) -> dict:
        now = utc_text(utc_now())
        scheduled = utc_text(parse_time(at))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise _state_error(job_id, row, "draft")
            if row["status"] == "queued":
                self.db.execute("COMMIT")
                return self.get(job_id)
            if row["status"] != "draft":
                raise _state_error(job_id, row, "draft")
            check_platform(row["platform"])
            media_hash = file_sha256(row["media_path"])
            body_hash = hashlib.sha256(row["body"].encode("utf-8")).hexdigest()
            self.db.execute(
                """UPDATE jobs SET status='queued',scheduled_at=?,media_sha256=?,
                   body_sha256=?,updated_at=? WHERE id=?""",
                (scheduled, media_hash, body_hash, now, job_id),
            )
            self.db.execute("COMMIT")
            return self.get(job_id)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _expire_leases(self, now: str) -> None:
        """Move every expired in-flight job to uncertain; call inside a transaction."""
        # The expired token is kept so the original sender can still record a
        # receipt it obtained after its lease ran out.
        self.db.execute(
            """UPDATE jobs SET status='uncertain',last_error='lease expired before receipt',
               expired_lease_token=lease_token,lease_token=NULL,lease_until=NULL,updated_at=?
               WHERE status='in_flight' AND lease_until<=?""",
            (now, now),
        )

    def expire_leases(self) -> int:
        """Mark expired claims uncertain and return how many changed."""
        now = utc_text(utc_now())
        self.db.execute("BEGIN IMMEDIATE")
        try:
            before = self.db.total_changes
            self._expire_leases(now)
            changed = self.db.total_changes - before
            self.db.execute("COMMIT")
            return changed
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def claim_due(self, *, lease_seconds: int = 600, platform: str | None = None,
                  account: str | None = None, action: str | None = None,
                  platforms: tuple[str, ...] | list[str] | None = None,
                  exclude_ids: tuple[str, ...] | list[str] | set[str] = (),
                  exclude_account_keys: tuple[str, ...] | list[str] | set[str] = ()) -> dict | None:
        """Claim the oldest due job; without filters only publishable platforms qualify."""
        if lease_seconds < 30:
            raise ValueError("lease_seconds must be at least 30")
        if action is not None and action not in ACTIONS:
            raise ValueError("unsupported action")
        if platform is not None:
            check_platform(platform)
            allowed = (platform,)
        else:
            allowed = tuple(platforms) if platforms is not None else PUBLISHABLE_PLATFORMS
            for name in allowed:
                check_platform(name)
        if not allowed:
            return None
        now_dt = utc_now()
        now = utc_text(now_dt)
        lease_until = utc_text(now_dt + timedelta(seconds=lease_seconds))
        token = str(uuid.uuid4())
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._expire_leases(now)
            filters = ["AND j.platform IN (" + ",".join("?" * len(allowed)) + ")"]
            params: list[str] = [now, *allowed]
            for name, value in (("account", account), ("action", action)):
                if value is not None:
                    filters.append(f"AND j.{name}=?")
                    params.append(value)
            for column, values in (("id", exclude_ids), ("account_key", exclude_account_keys)):
                values = tuple(values)
                if values:
                    filters.append(f"AND j.{column} NOT IN (" + ",".join("?" * len(values)) + ")")
                    params.extend(values)
            row = self.db.execute(
                """SELECT j.id FROM jobs AS j
                   WHERE j.status='queued' AND j.scheduled_at<=?
                   """ + " ".join(filters) + """
                   AND NOT EXISTS (
                       SELECT 1 FROM jobs AS busy
                       WHERE busy.account_key=j.account_key
                         AND busy.status IN ('in_flight','uncertain')
                   )
                   ORDER BY j.scheduled_at,j.created_at LIMIT 1""",
                tuple(params),
            ).fetchone()
            if not row:
                self.db.execute("COMMIT")
                return None
            self.db.execute(
                """UPDATE jobs SET status='in_flight',lease_token=?,lease_until=?,
                   expired_lease_token=NULL,attempts=attempts+1,updated_at=? WHERE id=?""",
                (token, lease_until, now, row["id"]),
            )
            self.db.execute("COMMIT")
            return self.get(row["id"])
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def claim_job(self, job_id: str, *, platform: str | None = None,
                  lease_seconds: int = 600) -> dict | None:
        """Claim exactly one queued job for an explicit direct publish."""
        if lease_seconds < 30:
            raise ValueError("lease_seconds must be at least 30")
        now_dt = utc_now()
        now = utc_text(now_dt)
        lease_until = utc_text(now_dt + timedelta(seconds=lease_seconds))
        token = str(uuid.uuid4())
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._expire_leases(now)
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if (row is None or row["status"] != "queued" or
                    row["platform"] not in PUBLISHABLE_PLATFORMS or
                    (platform is not None and row["platform"] != platform)):
                self.db.execute("COMMIT")
                return None
            busy = self.db.execute(
                """SELECT 1 FROM jobs WHERE account_key=? AND id<>?
                   AND status IN ('in_flight','uncertain') LIMIT 1""",
                (row["account_key"], job_id),
            ).fetchone()
            if busy:
                self.db.execute("COMMIT")
                return None
            self.db.execute(
                """UPDATE jobs SET status='in_flight',lease_token=?,lease_until=?,
                   expired_lease_token=NULL,attempts=attempts+1,updated_at=? WHERE id=?""",
                (token, lease_until, now, job_id),
            )
            self.db.execute("COMMIT")
            return self.get(job_id)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _held(self, row: sqlite3.Row | None, lease_token: str) -> bool:
        """True for the live lease, or for the sender whose lease expired meanwhile."""
        if row is None or not lease_token:
            return False
        if row["status"] == "in_flight":
            return row["lease_token"] == lease_token
        return row["status"] == "uncertain" and row["expired_lease_token"] == lease_token

    def finish(
        self,
        job_id: str,
        lease_token: str,
        *,
        outcome: str,
        provider_id: str | None = None,
        permalink: str | None = None,
        error: str | None = None,
        failure_kind: str = "pre_send",
    ) -> dict:
        """Record a sender's result; an expired-but-unresolved lease still counts."""
        if outcome not in ("published", "uncertain", "failed"):
            raise ValueError("outcome must be published, uncertain or failed")
        if outcome == "published" and not provider_id:
            raise ValueError("published needs provider_id")
        if failure_kind not in FAILURE_KINDS:
            raise ValueError("failure_kind must be " + " or ".join(FAILURE_KINDS))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not self._held(row, lease_token):
                raise OutboxError("job is not held by this lease", code="lease_lost",
                                  hint="Another process or a reconciliation already changed this job.")
            if row["status"] == "uncertain" and outcome == "published" and not error:
                error = "receipt recorded after the lease expired"
            self.db.execute(
                """UPDATE jobs SET status=?,lease_token=NULL,lease_until=NULL,expired_lease_token=NULL,
                   provider_id=?,permalink=?,last_error=?,failure_kind=?,updated_at=? WHERE id=?""",
                (outcome, provider_id, permalink, error,
                 failure_kind if outcome == "failed" else None, utc_text(utc_now()), job_id),
            )
            self.db.execute("COMMIT")
            return self.get(job_id)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def release(self, job_id: str, lease_token: str) -> dict:
        """Return an unsent claimed job to its original queued state."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not self._held(row, lease_token):
                raise OutboxError("job is not held by this lease", code="lease_lost",
                                  hint="Another process or a reconciliation already changed this job.")
            self.db.execute(
                """UPDATE jobs SET status='queued',lease_token=NULL,lease_until=NULL,
                   expired_lease_token=NULL,updated_at=?,
                   last_error=CASE WHEN status='uncertain' THEN NULL ELSE last_error END
                   WHERE id=?""",
                (utc_text(utc_now()), job_id),
            )
            self.db.execute("COMMIT")
            return self.get(job_id)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def resolve_uncertain(
        self, job_id: str, *, outcome: str, reason: str,
        provider_id: str | None = None, permalink: str | None = None,
    ) -> dict:
        if outcome not in ("published", "queued", "failed"):
            raise ValueError("resolution must be published, queued or failed")
        if not reason.strip() or (outcome == "published" and not provider_id):
            raise ValueError("resolution needs evidence and published needs provider_id")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] != "uncertain":
                raise _state_error(job_id, row, "uncertain")
            if outcome == "queued" and not frozen_payload_matches(_dict(row)):
                raise OutboxError("frozen job payload changed", code="payload_changed",
                                  hint="Draft and queue a new job with the intended content.")
            self.db.execute(
                """UPDATE jobs SET status=?,provider_id=?,permalink=?,last_error=?,
                   expired_lease_token=NULL,failure_kind=?,updated_at=? WHERE id=?""",
                (outcome, provider_id, permalink, f"reconciled: {reason}",
                 "reconciled" if outcome == "failed" else None, utc_text(utc_now()), job_id),
            )
            self.db.execute("COMMIT")
            return self.get(job_id)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def retry_failed(self, job_id: str, *, reason: str) -> dict:
        """Explicitly requeue a failed-before-send job with its frozen payload."""
        if not reason.strip():
            raise ValueError("retry needs a reason")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] != "failed":
                raise _state_error(job_id, row, "failed")
            if row["failure_kind"] not in FAILURE_KINDS:
                # It reached failed by reconciling an uncertain send, so the
                # provider may already show it. Retrying could post it twice.
                raise OutboxError(
                    "job failed while reconciling an uncertain send and may already be public",
                    code="possibly_sent",
                    hint="Check the provider. If nothing was published, draft and queue a new job.")
            if not frozen_payload_matches(_dict(row)):
                raise OutboxError("frozen job payload changed", code="payload_changed",
                                  hint="Draft and queue a new job with the intended content.")
            self.db.execute(
                """UPDATE jobs SET status='queued',scheduled_at=?,lease_token=NULL,
                   lease_until=NULL,expired_lease_token=NULL,failure_kind=NULL,
                   last_error=?,updated_at=? WHERE id=?""",
                (utc_text(utc_now()), f"retried after pre-send failure: {reason}",
                 utc_text(utc_now()), job_id),
            )
            self.db.execute("COMMIT")
            return self.get(job_id)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def cancel(self, job_id: str, *, reason: str | None = None) -> dict:
        """Retire a draft or queued job before any publisher claims it."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise _state_error(job_id, row, "cancellable")
            status = row["status"]
            if status == "cancelled":
                self.db.execute("COMMIT")
                return self.get(job_id)
            if status == "in_flight":
                raise OutboxError("job is leased by a publisher and cannot be cancelled",
                                  code="job_leased", hint="Wait for the publisher to finish, then check it.")
            if status == "published":
                raise OutboxError("job is already published", code="already_published",
                                  hint="Delete the post on the platform if it should not be public.")
            if status not in ("draft", "queued"):
                raise OutboxError(f"a {status} job cannot be cancelled", code="not_cancellable",
                                  hint=("Resolve an uncertain job with `outbox resolve`; retire a failed "
                                        "one with `outbox supersede-failed`."))
            note = reason.strip() if reason and reason.strip() else "operator request"
            self.db.execute(
                """UPDATE jobs SET status='cancelled',last_error=?,updated_at=? WHERE id=?""",
                (f"cancelled: {note}", utc_text(utc_now()), job_id),
            )
            self.db.execute("COMMIT")
            return self.get(job_id)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def supersede_failed(self, job_id: str, *, reason: str) -> dict:
        """Retain an invalid failed draft without allowing its frozen text to send."""
        if not reason.strip():
            raise ValueError("superseding needs a reason")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] != "failed":
                raise _state_error(job_id, row, "failed")
            self.db.execute(
                "UPDATE jobs SET status='superseded',last_error=?,updated_at=? WHERE id=?",
                (f"superseded failed draft: {reason}", utc_text(utc_now()), job_id),
            )
            self.db.execute("COMMIT")
            return self.get(job_id)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def list(self, *, status: str | None = None, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        if status is None:
            where = "status IN ('draft','queued','in_flight','uncertain')"
            params: tuple = ()
        elif status == "all":
            where = "1=1"
            params = ()
        else:
            where = "status=?"
            params = (status,)
        rows = self.db.execute(
            f"SELECT * FROM jobs WHERE {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_dict(row) for row in rows]
