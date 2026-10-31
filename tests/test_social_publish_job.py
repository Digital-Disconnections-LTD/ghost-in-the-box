from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from farm.adapters.instagram import AuthUnavailable as InstagramAuthUnavailable
from farm.adapters.threads import AuthUnavailable as ThreadsAuthUnavailable, PublishUncertain
from farm.outbox import Outbox
from scripts.social_publish_job import run


class FakeAdapter:
    def __init__(self, error=None):
        self.error = error
        self.sent = []
        self.saved = False

    def publish(self, job):
        self.sent.append(job["id"])
        if self.error:
            raise self.error
        return {"provider_id": "provider-123", "permalink": "https://example.test/post"}

    def save_session(self):
        self.saved = True


class DirectPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "outbox.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def draft(self, platform, *, at=None):
        with Outbox(self.db_path) as box:
            row = box.create_draft(platform=platform, account="mine", body="Exact operator text")
            return box.queue(row["id"], at=at)

    def invoke(self, job_id, *, ig=None, threads=None, browser=None):
        output = io.StringIO()
        with redirect_stdout(output):
            code = run(job_id, box_factory=lambda: Outbox(self.db_path),
                       instagram_factory=lambda: ig or FakeAdapter(),
                       threads_factory=lambda: threads or FakeAdapter(),
                       browser_threads_factory=lambda: browser or FakeAdapter(
                           ThreadsAuthUnavailable("browser login required")))
        return code, json.loads(output.getvalue())

    def test_claims_only_named_job_even_when_another_is_due(self):
        first = self.draft("threads")
        second = self.draft("threads", at="2030-01-01T00:00:00Z")
        adapter = FakeAdapter()
        code, result = self.invoke(second["id"], threads=adapter)
        self.assertEqual((code, result["status"]), (0, "published"))
        self.assertEqual(adapter.sent, [second["id"]])
        self.assertEqual(result["provider_id"], "provider-123")
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(first["id"])["status"], "queued")
            self.assertEqual(box.get(second["id"])["status"], "published")

    def test_threads_auth_failure_preserves_exact_queued_job(self):
        job = self.draft("threads")
        adapter = FakeAdapter(ThreadsAuthUnavailable("HTTP 403"))
        code, result = self.invoke(job["id"], threads=adapter)
        self.assertEqual((code, result["status"]), (2, "auth_unavailable"))
        self.assertNotIn("HTTP 403", json.dumps(result))
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(job["id"])["status"], "queued")

    def test_threads_mobile_auth_falls_back_to_browser_once(self):
        job = self.draft("threads")
        mobile = FakeAdapter(ThreadsAuthUnavailable("HTTP 403"))
        browser = FakeAdapter()
        code, result = self.invoke(job["id"], threads=mobile, browser=browser)
        self.assertEqual((code, result["status"]), (0, "published"))
        self.assertEqual(mobile.sent, [job["id"]])
        self.assertEqual(browser.sent, [job["id"]])
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(job["id"])["status"], "published")

    def test_browser_uncertainty_blocks_second_send(self):
        job = self.draft("threads")
        mobile = FakeAdapter(ThreadsAuthUnavailable("HTTP 403"))
        browser = FakeAdapter(PublishUncertain("unknown"))
        code, result = self.invoke(job["id"], threads=mobile, browser=browser)
        self.assertEqual((code, result["status"]), (3, "uncertain"))
        self.assertEqual(self.invoke(job["id"])[1]["status"], "not_queued")

    def test_instagram_auth_failure_preserves_job_and_success_saves_session(self):
        job = self.draft("instagram")
        code, result = self.invoke(job["id"],
                                   ig=FakeAdapter(InstagramAuthUnavailable("session expired")))
        self.assertEqual((code, result["status"]), (2, "auth_unavailable"))
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(job["id"])["status"], "queued")
        adapter = FakeAdapter()
        code, result = self.invoke(job["id"], ig=adapter)
        self.assertEqual((code, result["status"]), (0, "published"))
        self.assertTrue(result["session_saved"])
        self.assertTrue(adapter.saved)

    def test_missing_instagram_dependency_keeps_the_job_queued(self):
        job = self.draft("instagram")
        code, result = self.invoke(job["id"], ig=FakeAdapter(
            InstagramAuthUnavailable("Instagram client dependency unavailable")))
        self.assertEqual((code, result["status"]), (2, "dependency_unavailable"))
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(job["id"])["status"], "queued")

    def test_ambiguous_provider_result_is_never_reclaimed(self):
        job = self.draft("threads")
        code, result = self.invoke(job["id"], threads=FakeAdapter(PublishUncertain("unknown")))
        self.assertEqual((code, result["status"]), (3, "uncertain"))
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(job["id"])["status"], "uncertain")
        self.assertEqual(self.invoke(job["id"])[1]["status"], "not_queued")


if __name__ == "__main__":
    unittest.main()
