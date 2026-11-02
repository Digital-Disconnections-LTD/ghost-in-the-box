from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from farm.adapters import AuthUnavailable, DependencyUnavailable, PreSendFailure, PublishUncertain
from farm.outbox import Outbox
from farm.threads_cadence import ThreadsCadence
from scripts import ig_publish_once, threads_publish_once
from _env import IsolatedStateTestCase


class Adapter:
    def __init__(self, error=None, *, after_boundary=False):
        self.error = error
        self.after_boundary = after_boundary
        self.sent = []
        self.saved = False

    def publish(self, job, *, before_send=None):
        self.sent.append(job["id"])
        if self.error and not self.after_boundary:
            raise self.error
        if before_send is not None:
            before_send()
        if self.error:
            raise self.error
        return {"provider_id": "provider-1", "permalink": "https://example.test/p/1"}

    def save_session(self):
        self.saved = True


class InstagramTimerTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "outbox.sqlite3"
        self.image = Path(self.temp.name) / "photo.jpg"
        self.image.write_bytes(b"jpeg bytes")

    def queue(self, at=None):
        with Outbox(self.db) as box:
            draft = box.create_draft(platform="instagram", account="mine", body="caption",
                                     media_path=str(self.image))
            return box.queue(draft["id"], at=at)

    def tick(self, adapter):
        output = io.StringIO()
        with redirect_stdout(output):
            code = ig_publish_once.run(box_factory=lambda: Outbox(self.db),
                                       adapter_factory=lambda: adapter)
        return code, json.loads(output.getvalue())

    def job(self, job_id):
        with Outbox(self.db) as box:
            return box.get(job_id)

    def test_expired_session_releases_the_job_instead_of_failing_it(self):
        job = self.queue()
        code, result = self.tick(Adapter(AuthUnavailable("Instagram session recovery unavailable")))
        self.assertEqual((code, result["status"]), (4, "auth_unavailable"))
        self.assertEqual(self.job(job["id"])["status"], "queued")
        code, result = self.tick(Adapter())
        self.assertEqual((code, result["status"]), (0, "published"))
        self.assertTrue(result["session_saved"])

    def test_missing_dependency_releases_the_job_with_an_install_hint(self):
        job = self.queue()
        code, result = self.tick(Adapter(DependencyUnavailable(
            "Instagram client dependency unavailable", install_hint="pip install -e '.[instagram]'")))
        self.assertEqual((code, result["error"]["code"]), (4, "dependency_unavailable"))
        self.assertEqual(self.job(job["id"])["status"], "queued")

    def test_real_pre_send_failure_still_fails_the_job(self):
        job = self.queue()
        code, result = self.tick(Adapter(PreSendFailure("wrong account")))
        self.assertEqual((code, result["status"]), (1, "failed_before_send"))
        self.assertEqual(self.job(job["id"])["status"], "failed")

    def test_unexpected_error_fails_before_send_and_is_uncertain_after(self):
        before = self.queue()
        code, result = self.tick(Adapter(RuntimeError("bug")))
        self.assertEqual((code, result["status"]), (1, "failed_before_send"))
        self.assertEqual(self.job(before["id"])["status"], "failed")
        after = self.queue()
        code, result = self.tick(Adapter(RuntimeError("bug"), after_boundary=True))
        self.assertEqual((code, result["status"]), (3, "uncertain"))
        saved = self.job(after["id"])
        self.assertEqual((saved["status"], saved["lease_token"]), ("uncertain", None))

    def test_provider_uncertainty_is_held_for_reconciliation(self):
        job = self.queue()
        code, result = self.tick(Adapter(PublishUncertain("timeout"), after_boundary=True))
        self.assertEqual((code, result["status"]), (3, "uncertain"))
        self.assertEqual(self.job(job["id"])["status"], "uncertain")

    def test_idle_and_future_jobs_are_not_claimed(self):
        self.assertEqual(self.tick(Adapter())[1]["status"], "idle")
        future = self.queue(at="2099-01-01T00:00:00Z")
        adapter = Adapter()
        code, result = self.tick(adapter)
        self.assertEqual((code, result["status"], adapter.sent), (0, "idle", []))
        self.assertEqual(self.job(future["id"])["status"], "queued")


class ThreadsTimerTests(IsolatedStateTestCase):
    NY = ZoneInfo("America/New_York")

    def setUp(self):
        super().setUp()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "outbox.sqlite3"
        with Outbox(self.db) as box:
            draft = box.create_draft(platform="threads", account="mine", action="reply",
                                     body="reply text", media_id="123", target_id="123")
            self.job = box.queue(draft["id"])

    def tick(self, mobile, browser=None, when=None):
        when = when or datetime(2026, 10, 26, 12, tzinfo=self.NY)
        output = io.StringIO()
        with redirect_stdout(output):
            code = threads_publish_once.run(
                "mine", box_factory=lambda: Outbox(self.db), adapter_factory=lambda: mobile,
                cadence_factory=lambda account: ThreadsCadence(account, directory=self.root / "cadence"),
                clock=lambda: when, browser_factory=(lambda: browser) if browser else None)
        return code, json.loads(output.getvalue())

    def status(self):
        with Outbox(self.db) as box:
            return box.get(self.job["id"])["status"]

    def test_portable_account_publishes_through_the_browser_fallback(self):
        mobile = Adapter(AuthUnavailable("portable browser session selected"))
        browser = Adapter()
        code, result = self.tick(mobile, browser)
        self.assertEqual((code, result["status"]), (0, "published"))
        self.assertEqual((mobile.sent, browser.sent), ([self.job["id"]], [self.job["id"]]))
        self.assertEqual(self.status(), "published")

    def test_missing_browser_dependency_keeps_the_job_queued_and_says_why(self):
        mobile = Adapter(AuthUnavailable("portable browser session selected"))
        browser = Adapter(DependencyUnavailable("playwright missing", install_hint="pip install playwright"))
        code, result = self.tick(mobile, browser)
        self.assertEqual((code, result["status"]), (4, "dependency_unavailable"))
        self.assertEqual(result["error"]["hint"], "pip install playwright")
        self.assertEqual(self.status(), "queued")


if __name__ == "__main__":
    unittest.main()
