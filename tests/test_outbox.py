import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from farm.outbox import Outbox, frozen_payload_matches


class OutboxTests(unittest.TestCase):
    def setUp(self):
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
        with self.assertRaisesRegex(ValueError, "lease"):
            self.box.finish(first["id"], held["lease_token"],
                            outcome="published", provider_id="remote-1")
        self.box.resolve_uncertain(first["id"], outcome="published",
                                   provider_id="remote-1", reason="provider permalink found")
        self.assertEqual(self.box.claim_due()["id"], second["id"])

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


if __name__ == "__main__":
    unittest.main()
