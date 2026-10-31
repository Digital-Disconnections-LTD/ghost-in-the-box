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
import uuid

from farm.accounts import AccountRegistry


DEFAULT_DB = STATE_ROOT / 'outbox.sqlite3'
ACTIVE_STATES = ("draft", "queued", "in_flight", "uncertain")
PLATFORMS = ("instagram", "threads", "facebook", "linkedin", "tiktok", "youtube")
ACTIONS = ("post", "reply")


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
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
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
            );
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, scheduled_at, created_at);
            CREATE INDEX IF NOT EXISTS jobs_account ON jobs(account_key, status);
        """)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Outbox":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def get(self, job_id: str) -> dict | None:
        return _dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

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
        if platform not in PLATFORMS or action not in ACTIONS:
            raise ValueError("unsupported platform or action")
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
        if account_key is not None and account_key != canonical_key:
            raise ValueError("account key differs from the registered account")
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
                        raise ValueError("idempotency key already belongs to different content")
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
                raise ValueError("job not found")
            if row["status"] == "queued":
                self.db.execute("COMMIT")
                return self.get(job_id)
            if row["status"] != "draft":
                raise ValueError("only a draft can be queued")
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

    def claim_due(self, *, lease_seconds: int = 600, platform: str | None = None,
                  account: str | None = None, action: str | None = None) -> dict | None:
        if lease_seconds < 30:
            raise ValueError("lease_seconds must be at least 30")
        if action is not None and action not in ACTIONS:
            raise ValueError("unsupported action")
        now_dt = utc_now()
        now = utc_text(now_dt)
        lease_until = utc_text(now_dt + timedelta(seconds=lease_seconds))
        token = str(uuid.uuid4())
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                """UPDATE jobs SET status='uncertain',last_error='lease expired before receipt',
                   lease_token=NULL,lease_until=NULL,updated_at=?
                   WHERE status='in_flight' AND lease_until<=?""",
                (now, now),
            )
            filters = []
            params: list[str] = [now]
            for name, value in (("platform", platform), ("account", account),
                                ("action", action)):
                if value is not None:
                    filters.append(f"AND j.{name}=?")
                    params.append(value)
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
                   attempts=attempts+1,updated_at=? WHERE id=?""",
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
            self.db.execute(
                """UPDATE jobs SET status='uncertain',last_error='lease expired before receipt',
                   lease_token=NULL,lease_until=NULL,updated_at=?
                   WHERE status='in_flight' AND lease_until<=?""",
                (now, now),
            )
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if (row is None or row["status"] != "queued" or
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
                   attempts=attempts+1,updated_at=? WHERE id=?""",
                (token, lease_until, now, job_id),
            )
            self.db.execute("COMMIT")
            return self.get(job_id)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def finish(
        self,
        job_id: str,
        lease_token: str,
        *,
        outcome: str,
        provider_id: str | None = None,
        permalink: str | None = None,
        error: str | None = None,
    ) -> dict:
        if outcome not in ("published", "uncertain", "failed"):
            raise ValueError("outcome must be published, uncertain or failed")
        if outcome == "published" and not provider_id:
            raise ValueError("published needs provider_id")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] != "in_flight" or row["lease_token"] != lease_token:
                raise ValueError("job is not held by this lease")
            self.db.execute(
                """UPDATE jobs SET status=?,lease_token=NULL,lease_until=NULL,
                   provider_id=?,permalink=?,last_error=?,updated_at=? WHERE id=?""",
                (outcome, provider_id, permalink, error, utc_text(utc_now()), job_id),
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
            changed = self.db.execute(
                """UPDATE jobs SET status='queued',lease_token=NULL,lease_until=NULL,
                   updated_at=? WHERE id=? AND status='in_flight' AND lease_token=?""",
                (utc_text(utc_now()), job_id, lease_token),
            ).rowcount
            if changed != 1:
                raise ValueError("job is not held by this lease")
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
                raise ValueError("job is not uncertain")
            self.db.execute(
                """UPDATE jobs SET status=?,provider_id=?,permalink=?,last_error=?,
                   updated_at=? WHERE id=?""",
                (outcome, provider_id, permalink, f"reconciled: {reason}", utc_text(utc_now()), job_id),
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
                raise ValueError("job is not failed")
            if not frozen_payload_matches(_dict(row)):
                raise ValueError("frozen job payload changed")
            self.db.execute(
                """UPDATE jobs SET status='queued',scheduled_at=?,lease_token=NULL,
                   lease_until=NULL,last_error=?,updated_at=? WHERE id=?""",
                (utc_text(utc_now()), f"retried after pre-send failure: {reason}",
                 utc_text(utc_now()), job_id),
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
                raise ValueError("job is not failed")
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
