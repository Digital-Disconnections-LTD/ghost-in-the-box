import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from farm.outbox import JOB_STATES, SCHEMA_VERSION, Outbox, OutboxError, frozen_payload_matches
from _env import IsolatedStateTestCase


class OutboxTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.box = Outbox(self.root / "private/outbox.sqlite3")

    def tearDown(self):
        self.box.close()
        self.temp.cleanup()

    def draft(self, **kwargs):
        return self.box.create_draft(
            platform=kwargs.pop("platform", "instagram"),
            account=kwargs.pop("account", "mine"),
            body=kwargs.pop("body", "A sourced thought."),
            **kwargs,
        )

    def test_private_state_and_idempotent_draft(self):
        one = self.draft(idempotency_key="conversation-42")  # gitleaks:allow -- local deduplication fixture
        two = self.draft(idempotency_key="conversation-42")  # gitleaks:allow -- local deduplication fixture
        self.assertEqual(one["id"], two["id"])
        with self.assertRaisesRegex(ValueError, "different content"):
            self.draft(body="Different", idempotency_key="conversation-42")  # gitleaks:allow -- local deduplication fixture
        self.assertEqual(self.box.path.stat().st_mode & 0o777, 0o600)

    def test_queue_freezes_content_and_serializes_account(self):
        media = self.root / "image.jpg"
        media.write_bytes(b"photo bytes")
        first = self.draft(media_path=str(media), source_urls=["https://example.org/a"],
                           account_key="meta:mine")
        second = self.draft(platform="threads", account_key="meta:mine", body="A reply",
                            action="reply", media_id="media-1",
                            target_id="123", target_url="https://threads.net/t/123")
        queued = self.box.queue(first["id"])
        self.assertEqual(self.box.queue(first["id"])["id"], first["id"])
        self.box.queue(second["id"])
        self.assertEqual(queued["media_sha256"], hashlib.sha256(b"photo bytes").hexdigest())
        self.assertEqual(queued["body_sha256"], hashlib.sha256(first["body"].encode()).hexdigest())
        self.assertTrue(frozen_payload_matches(queued))
        media.write_bytes(b"changed after queue")
        self.assertFalse(frozen_payload_matches(queued))
        media.write_bytes(b"photo bytes")
        held = self.box.claim_due()
        self.assertEqual(held["id"], first["id"])
        self.assertEqual(held["source_urls"], ["https://example.org/a"])
        self.assertIsNone(self.box.claim_due())
        with self.assertRaisesRegex(ValueError, "lease"):
            self.box.finish(first["id"], "wrong", outcome="published", provider_id="remote-1")
        done = self.box.finish(first["id"], held["lease_token"],
                               outcome="published", provider_id="remote-1",
                               permalink="https://instagram.com/p/one")
        self.assertEqual(done["status"], "published")
        self.assertEqual(self.box.claim_due()["id"], second["id"])

    def test_expired_lease_is_uncertain_and_blocks_account(self):
        first = self.draft()
        second = self.draft(body="another")
        self.box.queue(first["id"])
        self.box.queue(second["id"])
        held = self.box.claim_due(lease_seconds=30)
        self.box.db.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00Z' WHERE id=?", (held["id"],))
        self.assertIsNone(self.box.claim_due())
        uncertain = self.box.get(first["id"])
        self.assertEqual(uncertain["status"], "uncertain")
        self.assertIsNone(uncertain["lease_token"])
        with self.assertRaisesRegex(ValueError, "lease"):
            self.box.finish(first["id"], "someone-else",
                            outcome="published", provider_id="remote-1")
        self.box.resolve_uncertain(first["id"], outcome="published",
                                   provider_id="remote-1", reason="provider permalink found")
        self.assertEqual(self.box.claim_due()["id"], second["id"])

    def expire(self, job_id):
        self.box.db.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00Z' WHERE id=?", (job_id,))
        self.assertEqual(self.box.expire_leases(), 1)

    def test_original_sender_records_receipt_after_its_lease_expired(self):
        first = self.draft()
        self.box.queue(first["id"])
        held = self.box.claim_due(lease_seconds=30)
        self.expire(held["id"])
        done = self.box.finish(first["id"], held["lease_token"], outcome="published",
                               provider_id="remote-1", permalink="https://example.test/p/1")
        self.assertEqual((done["status"], done["provider_id"]), ("published", "remote-1"))
        self.assertEqual(done["last_error"], "receipt recorded after the lease expired")
        self.assertIsNone(done["expired_lease_token"])
        with self.assertRaisesRegex(ValueError, "lease"):
            self.box.finish(first["id"], held["lease_token"], outcome="failed")

    def test_late_receipt_never_overrides_a_human_resolution(self):
        first = self.draft()
        self.box.queue(first["id"])
        held = self.box.claim_due(lease_seconds=30)
        self.expire(held["id"])
        self.box.resolve_uncertain(first["id"], outcome="failed", reason="checked: not on profile")
        with self.assertRaises(OutboxError) as caught:
            self.box.finish(first["id"], held["lease_token"], outcome="published", provider_id="x")
        self.assertEqual(caught.exception.code, "lease_lost")
        self.assertEqual(self.box.get(first["id"])["status"], "failed")

    def test_unsent_job_released_after_lease_expiry_returns_to_queue(self):
        first = self.draft()
        self.box.queue(first["id"])
        held = self.box.claim_due(lease_seconds=30)
        self.expire(held["id"])
        released = self.box.release(first["id"], held["lease_token"])
        self.assertEqual(released["status"], "queued")
        self.assertIsNone(released["last_error"])

    def test_retry_refuses_failure_reached_by_reconciling_an_uncertain_send(self):
        draft = self.draft()
        self.box.queue(draft["id"])
        held = self.box.claim_due()
        self.box.finish(draft["id"], held["lease_token"], outcome="uncertain")
        resolved = self.box.resolve_uncertain(draft["id"], outcome="failed", reason="unclear")
        self.assertEqual(resolved["failure_kind"], "reconciled")
        with self.assertRaises(OutboxError) as caught:
            self.box.retry_failed(draft["id"], reason="try again")
        self.assertEqual(caught.exception.code, "possibly_sent")
        self.assertEqual(self.box.get(draft["id"])["status"], "failed")

    def test_provider_rejection_can_be_retried_explicitly(self):
        draft = self.draft()
        self.box.queue(draft["id"])
        held = self.box.claim_due()
        failed = self.box.finish(draft["id"], held["lease_token"], outcome="failed",
                                 error="HTTP 400", failure_kind="rejected")
        self.assertEqual(failed["failure_kind"], "rejected")
        queued = self.box.retry_failed(draft["id"], reason="fixed caption")
        self.assertEqual((queued["status"], queued["failure_kind"]), ("queued", None))

    def test_resolve_to_queued_refuses_a_changed_frozen_payload(self):
        draft = self.draft()
        self.box.queue(draft["id"])
        held = self.box.claim_due()
        self.box.finish(draft["id"], held["lease_token"], outcome="uncertain")
        self.box.db.execute("UPDATE jobs SET body='tampered' WHERE id=?", (draft["id"],))
        with self.assertRaises(OutboxError) as caught:
            self.box.resolve_uncertain(draft["id"], outcome="queued", reason="not on profile")
        self.assertEqual(caught.exception.code, "payload_changed")
        self.assertEqual(self.box.get(draft["id"])["status"], "uncertain")
        self.box.db.execute("UPDATE jobs SET body=? WHERE id=?", (draft["body"], draft["id"]))
        self.assertEqual(self.box.resolve_uncertain(
            draft["id"], outcome="queued", reason="not on profile")["status"], "queued")

    def test_brief_only_platforms_cannot_be_drafted_queued_or_claimed(self):
        for platform in ("facebook", "tiktok", "youtube"):
            with self.subTest(platform=platform):
                with self.assertRaises(OutboxError) as caught:
                    self.draft(platform=platform)
                self.assertEqual(caught.exception.code, "brief_only_platform")
                self.assertIn("brief", caught.exception.hint)
        with self.assertRaises(OutboxError) as caught:
            self.draft(platform="myspace")
        self.assertEqual(caught.exception.code, "unsupported_platform")
        legacy = self.draft()
        self.box.db.execute("UPDATE jobs SET platform='facebook',account_key='facebook:mine' WHERE id=?",
                            (legacy["id"],))
        with self.assertRaises(OutboxError):
            self.box.queue(legacy["id"])
        self.box.db.execute("UPDATE jobs SET status='queued',scheduled_at='2000-01-01T00:00:00Z' WHERE id=?",
                            (legacy["id"],))
        self.assertIsNone(self.box.claim_due())
        self.assertIsNone(self.box.claim_job(legacy["id"]))
        with self.assertRaises(OutboxError):
            self.box.claim_due(platform="facebook")

    def test_claim_can_skip_tried_jobs_and_held_accounts(self):
        first = self.draft()
        other = self.draft(platform="linkedin", account="other", body="LinkedIn text")
        self.box.queue(first["id"])
        self.box.queue(other["id"])
        self.assertIsNone(self.box.claim_due(exclude_ids=[first["id"], other["id"]]))
        held = self.box.claim_due(exclude_account_keys=["meta:mine"])
        self.assertEqual(held["id"], other["id"])

    def test_cancel_retires_only_unclaimed_jobs(self):
        draft = self.draft(body="draft one")
        queued = self.draft(body="queued one")
        self.box.queue(queued["id"])
        self.assertEqual(self.box.cancel(draft["id"])["status"], "cancelled")
        cancelled = self.box.cancel(queued["id"], reason="changed my mind")
        self.assertEqual((cancelled["status"], cancelled["last_error"]),
                         ("cancelled", "cancelled: changed my mind"))
        self.assertEqual(self.box.cancel(queued["id"])["status"], "cancelled")
        self.assertIsNone(self.box.claim_due())
        leased = self.draft(body="leased one")
        self.box.queue(leased["id"])
        held = self.box.claim_due()
        with self.assertRaises(OutboxError) as caught:
            self.box.cancel(leased["id"])
        self.assertEqual(caught.exception.code, "job_leased")
        self.box.finish(leased["id"], held["lease_token"], outcome="published", provider_id="p-1")
        with self.assertRaises(OutboxError) as caught:
            self.box.cancel(leased["id"])
        self.assertEqual(caught.exception.code, "already_published")
        with self.assertRaises(OutboxError) as caught:
            self.box.cancel("missing")
        self.assertEqual(caught.exception.code, "not_found")

    def test_platform_worker_claims_only_its_jobs(self):
        instagram = self.draft()
        threads = self.draft(platform="threads", account="another", body="A Threads draft")
        self.box.queue(instagram["id"])
        self.box.queue(threads["id"])
        held = self.box.claim_due(platform="threads")
        self.assertEqual(held["id"], threads["id"])
        self.assertEqual(self.box.claim_due(platform="instagram")["id"], instagram["id"])

    def test_instagram_draft_and_inbox_reply_share_one_account_lease(self):
        first = self.draft(body="A post without an explicit account key")
        second = self.draft(body="A reply", action="reply", media_id="1_2",
                            target_id="123", account_key="meta:mine")
        self.box.queue(first["id"])
        self.box.queue(second["id"])
        claimed = self.box.claim_due(platform="instagram")
        self.assertEqual(claimed["account_key"], "meta:mine")
        self.assertIsNone(self.box.claim_due(platform="instagram"))
        with self.assertRaisesRegex(ValueError, "account key differs"):
            self.draft(body="Bypass", account_key="other:mine")

    def test_reply_requires_target_and_schedule_requires_timezone(self):
        with self.assertRaisesRegex(ValueError, "target_id"):
            self.draft(action="reply")
        with self.assertRaisesRegex(ValueError, "media_id"):
            self.draft(action="reply", target_id="comment-1")
        draft = self.draft()
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.box.queue(draft["id"], at="2026-10-26T20:00:00")
        self.assertEqual(self.box.get(draft["id"])["status"], "draft")

    def test_explicit_retry_of_failed_before_send_job_preserves_frozen_body(self):
        draft = self.draft(body="A reply", action="reply", media_id="1_2", target_id="123")
        self.box.queue(draft["id"])
        held = self.box.claim_due()
        self.box.finish(draft["id"], held["lease_token"], outcome="failed",
                        error="ModuleNotFoundError")
        with self.assertRaisesRegex(ValueError, "reason"):
            self.box.retry_failed(draft["id"], reason="")
        queued = self.box.retry_failed(draft["id"], reason="installed runtime selected")
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["body"], "A reply")
        self.assertTrue(frozen_payload_matches(queued))
        self.assertEqual(self.box.claim_due()["id"], draft["id"])

    def test_superseded_failed_draft_stays_as_record_and_cannot_send(self):
        draft = self.draft(body="Inaccurate wording")
        self.box.queue(draft["id"])
        held = self.box.claim_due()
        self.box.finish(draft["id"], held["lease_token"], outcome="failed",
                        error="composer missing")
        retired = self.box.supersede_failed(draft["id"], reason="revised wording")
        self.assertEqual(retired["status"], "superseded")
        self.assertEqual(retired["body"], "Inaccurate wording")
        self.assertIsNone(self.box.claim_due())
        with self.assertRaisesRegex(ValueError, "not failed"):
            self.box.retry_failed(draft["id"], reason="try again")

    def test_state_refusals_are_coded_with_the_command_that_shows_the_job(self):
        job = self.draft(platform="threads")
        refusals = (
            (lambda: self.box.retry_failed(job["id"], reason="fixed"), "not_failed"),
            (lambda: self.box.supersede_failed(job["id"], reason="old"), "not_failed"),
            (lambda: self.box.resolve_uncertain(job["id"], outcome="failed", reason="seen"), "not_uncertain"),
        )
        for action, code in refusals:
            with self.subTest(code=code):
                with self.assertRaises(OutboxError) as raised:
                    action()
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.hint, f"Check it with: ghost-in-the-box outbox show {job['id']}")
        self.box.queue(job["id"])
        self.box.cancel(job["id"])
        with self.assertRaises(OutboxError) as raised:
            self.box.queue(job["id"])
        self.assertEqual((raised.exception.code, str(raised.exception)), ("not_draft", "job is cancelled, not draft"))
        for action in (lambda: self.box.queue("missing"), lambda: self.box.cancel("missing")):
            with self.assertRaises(OutboxError) as raised:
                action()
            self.assertEqual(raised.exception.code, "not_found")
            self.assertIn("outbox list --status all", raised.exception.hint)

    def test_content_and_account_conflicts_carry_codes_and_hints(self):
        first = self.draft(idempotency_key="intended-post")
        with self.assertRaises(OutboxError) as raised:
            self.draft(body="Different", idempotency_key="intended-post")
        self.assertEqual(raised.exception.code, "idempotency_conflict")
        self.assertIn(first["id"], raised.exception.hint)
        with self.assertRaises(OutboxError) as raised:
            self.draft(account_key="meta:someone_else")
        self.assertEqual(raised.exception.code, "account_key_mismatch")
        self.assertTrue(raised.exception.hint)

    def test_job_states_cover_every_status_the_outbox_writes(self):
        self.assertEqual(JOB_STATES, ("draft", "queued", "in_flight", "uncertain", "published", "failed",
                                      "superseded", "cancelled"))


class OutboxSchemaTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "outbox.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def version(self):
        db = sqlite3.connect(self.path)
        try:
            return db.execute("SELECT version FROM schema_version").fetchone()[0]
        finally:
            db.close()

    def test_new_database_records_the_current_schema_version(self):
        Outbox(self.path).close()
        self.assertEqual(self.version(), SCHEMA_VERSION)
        Outbox(self.path).close()
        self.assertEqual(self.version(), SCHEMA_VERSION)

    def test_unversioned_database_migrates_forward_and_marks_failures(self):
        db = sqlite3.connect(self.path)
        db.execute("""CREATE TABLE jobs (id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE,
            platform TEXT NOT NULL, account TEXT NOT NULL, account_key TEXT NOT NULL,
            action TEXT NOT NULL, body TEXT NOT NULL, media_path TEXT, media_sha256 TEXT,
            body_sha256 TEXT, media_id TEXT, target_id TEXT, target_url TEXT,
            crosspost_threads INTEGER NOT NULL DEFAULT 0, source_urls TEXT NOT NULL,
            status TEXT NOT NULL, scheduled_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            lease_token TEXT, lease_until TEXT, provider_id TEXT, permalink TEXT,
            last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        body = "old text"
        digest = hashlib.sha256(body.encode()).hexdigest()
        for job_id, error in (("reconciled-job", "reconciled: unclear"), ("presend-job", "HTTP 400")):
            db.execute("""INSERT INTO jobs (id,platform,account,account_key,action,body,body_sha256,
                source_urls,status,last_error,created_at,updated_at)
                VALUES (?,'instagram','mine','meta:mine','post',?,?,?,'failed',?,'t','t')""",
                (job_id, body, digest, json.dumps([]), error))
        db.commit()
        db.close()
        with Outbox(self.path) as box:
            self.assertEqual(box.get("reconciled-job")["failure_kind"], "reconciled")
            self.assertEqual(box.get("presend-job")["failure_kind"], "pre_send")
            with self.assertRaises(OutboxError):
                box.retry_failed("reconciled-job", reason="again")
            self.assertEqual(box.retry_failed("presend-job", reason="again")["status"], "queued")
        self.assertEqual(self.version(), SCHEMA_VERSION)

    def test_database_from_a_newer_release_is_refused(self):
        Outbox(self.path).close()
        db = sqlite3.connect(self.path)
        db.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION + 1,))
        db.commit()
        db.close()
        with self.assertRaises(OutboxError) as caught:
            Outbox(self.path)
        self.assertEqual(caught.exception.code, "schema_too_new")


if __name__ == "__main__":
    unittest.main()
