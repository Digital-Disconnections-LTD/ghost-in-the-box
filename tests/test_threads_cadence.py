from datetime import datetime, timedelta, timezone
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo
import unittest

from farm.outbox import Outbox
from farm.adapters.threads import AuthUnavailable
from farm.threads_cadence import ThreadsCadence, in_quiet_hours
from scripts.threads_publish_once import run


NY = ZoneInfo("America/New_York")


class FixedRng:
    def __init__(self, *values):
        self.values = list(values)

    def randint(self, low, high):
        assert (low, high) == (3600, 7200)
        return self.values.pop(0)


class FakeAdapter:
    def __init__(self):
        self.sent = []

    def publish(self, job, *, before_send):
        before_send()
        self.sent.append((job["action"], job["id"]))
        return {"provider_id": "999", "permalink": "https://www.threads.com/@mine/post/abc"}


class ThreadsCadenceTests(unittest.TestCase):
    def test_new_york_quiet_boundaries_across_dst(self):
        for day in ("2026-03-08", "2026-11-01", "2026-10-26"):
            with self.subTest(day=day):
                self.assertTrue(in_quiet_hours(datetime.fromisoformat(day + "T00:00:00").replace(tzinfo=NY)))
                self.assertTrue(in_quiet_hours(datetime.fromisoformat(day + "T05:59:59").replace(tzinfo=NY)))
                self.assertFalse(in_quiet_hours(datetime.fromisoformat(day + "T06:00:00").replace(tzinfo=NY)))

    def test_randomized_deadline_persists_across_processes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cadence"
            start = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
            first = ThreadsCadence("mine", directory=path, rng=FixedRng(3600))
            with first.locked():
                self.assertFalse(first.due(start))
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(first.lock_path.stat().st_mode & 0o777, 0o600)
            second = ThreadsCadence("mine", directory=path, rng=FixedRng(7200))
            with second.locked():
                self.assertFalse(second.due(start + timedelta(minutes=59)))
                self.assertTrue(second.due(start + timedelta(hours=1)))
                next_time = second.record_post_attempt(start + timedelta(hours=1))
                self.assertEqual(next_time, start + timedelta(hours=3))
            third = ThreadsCadence("mine", directory=path, rng=FixedRng())
            with third.locked():
                self.assertFalse(third.due(start + timedelta(hours=2)))
                self.assertTrue(third.due(start + timedelta(hours=3)))

    def test_claim_filters_and_release_preserve_unsent_job(self):
        with TemporaryDirectory() as directory:
            with Outbox(Path(directory) / "outbox.sqlite3") as box:
                post = box.create_draft(platform="threads", account="mine", body="Queued post")
                reply = box.create_draft(platform="threads", account="mine", action="reply",
                                         body="Queued reply", media_id="123", target_id="123")
                box.queue(post["id"])
                box.queue(reply["id"])
                held = box.claim_due(platform="threads", account="mine", action="reply")
                self.assertEqual(held["id"], reply["id"])
                self.assertEqual(box.release(held["id"], held["lease_token"])["status"], "queued")
                held = box.claim_due(platform="threads", account="mine", action="post")
                self.assertEqual(held["id"], post["id"])
                box.release(held["id"], held["lease_token"])

    def test_runner_spaces_posts_but_allows_daytime_reply(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "outbox.sqlite3"
            cadence_dir = root / "cadence"
            adapter = FakeAdapter()
            with Outbox(db_path) as box:
                for body in ("First operator post", "Second operator post"):
                    row = box.create_draft(platform="threads", account="mine", body=body)
                    box.queue(row["id"])
            start = datetime(2026, 10, 26, 8, tzinfo=NY)
            rng = FixedRng(3600, 7200, 3600)
            cadence_factory = lambda account: ThreadsCadence(account, directory=cadence_dir, rng=rng)
            def invoke(when):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run("mine", box_factory=lambda: Outbox(db_path),
                               adapter_factory=lambda: adapter,
                               cadence_factory=cadence_factory,
                               clock=lambda: when)
                return code, json.loads(output.getvalue())

            self.assertEqual(invoke(start)[1]["status"], "cadence_wait")
            self.assertEqual(invoke(start + timedelta(hours=1))[1]["status"], "published")
            self.assertEqual(invoke(start + timedelta(hours=1, minutes=2))[1]["status"], "cadence_wait")
            with Outbox(db_path) as box:
                reply = box.create_draft(platform="threads", account="mine", action="reply",
                                         body="operator reply", media_id="123", target_id="123")
                box.queue(reply["id"])
            self.assertEqual(invoke(start + timedelta(hours=1, minutes=3))[1]["status"], "published")
            self.assertEqual([action for action, _ in adapter.sent], ["post", "reply"])
            self.assertEqual(invoke(start + timedelta(hours=3))[1]["status"], "published")
            self.assertEqual([action for action, _ in adapter.sent], ["post", "reply", "post"])

    def test_runner_does_not_claim_during_quiet_hours(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "outbox.sqlite3"
            with Outbox(db_path) as box:
                job = box.create_draft(platform="threads", account="mine", action="reply",
                                       body="operator reply", media_id="123", target_id="123")
                box.queue(job["id"])
            output = io.StringIO()
            with redirect_stdout(output):
                code = run("mine", box_factory=lambda: Outbox(db_path),
                           clock=lambda: datetime(2026, 10, 27, 1, tzinfo=NY))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "quiet_hours")
            with Outbox(db_path) as box:
                self.assertEqual(box.get(job["id"])["status"], "queued")
            self.assertFalse((root / "cadence").exists())

    def test_clock_crossing_midnight_releases_claimed_reply(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "outbox.sqlite3"
            with Outbox(db_path) as box:
                job = box.create_draft(platform="threads", account="mine", action="reply",
                                       body="operator reply", media_id="123", target_id="123")
                box.queue(job["id"])
            clock_values = iter((datetime(2026, 10, 26, 23, 59, tzinfo=NY),
                                 datetime(2026, 10, 27, 0, 0, tzinfo=NY)))
            adapter = FakeAdapter()
            output = io.StringIO()
            with redirect_stdout(output):
                code = run("mine", box_factory=lambda: Outbox(db_path),
                           adapter_factory=lambda: adapter,
                           cadence_factory=lambda account: ThreadsCadence(
                               account, directory=root / "cadence", rng=FixedRng(3600)),
                           clock=lambda: next(clock_values))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "quiet_hours")
            self.assertEqual(adapter.sent, [])
            with Outbox(db_path) as box:
                self.assertEqual(box.get(job["id"])["status"], "queued")

    def test_auth_failure_keeps_job_queued_and_cools_down(self):
        class AuthFailAdapter:
            calls = 0
            def publish(self, job, *, before_send):
                self.calls += 1
                raise AuthUnavailable("temporary provider rejection")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "outbox.sqlite3"
            with Outbox(db_path) as box:
                job = box.create_draft(platform="threads", account="mine", action="reply",
                                       body="operator reply", media_id="123", target_id="123")
                box.queue(job["id"])
            failing = AuthFailAdapter()
            start = datetime(2026, 10, 26, 8, tzinfo=NY)
            rng = FixedRng(3600)
            cadence_factory = lambda account: ThreadsCadence(account, directory=root / "cadence", rng=rng)
            def invoke(when, adapter):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run("mine", box_factory=lambda: Outbox(db_path),
                               adapter_factory=lambda: adapter,
                               cadence_factory=cadence_factory, clock=lambda: when)
                return code, json.loads(output.getvalue())

            self.assertEqual(invoke(start, failing)[1]["status"], "auth_unavailable")
            with Outbox(db_path) as box:
                self.assertEqual(box.get(job["id"])["status"], "queued")
            self.assertEqual(invoke(start + timedelta(minutes=10), failing)[1]["status"],
                             "auth_cooldown")
            self.assertEqual(failing.calls, 1)
            successful = FakeAdapter()
            self.assertEqual(invoke(start + timedelta(minutes=30), successful)[1]["status"],
                             "published")
            self.assertEqual(len(successful.sent), 1)


if __name__ == "__main__":
    unittest.main()
