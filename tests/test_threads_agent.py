from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from farm.accounts import AccountRegistry
from farm.adapters.threads import AuthUnavailable, PreSendFailure, PublishUncertain
from farm.inbox import Inbox
from farm.outbox import Outbox
from farm.threads_cadence import ThreadsCadence
from scripts.threads_agent import InputError, ProfileBusy, ThreadsAgent, job_summary, main


class FakeBrowser:
    def __init__(self):
        self.calls = []
        self.error = None

    def publish(self, job):
        self.calls.append((job["id"], job["account"], job["action"]))
        if self.error:
            raise self.error
        return {"provider_id": "123456789",
                "permalink": f"https://www.threads.com/@{job['account']}/post/AbCd"}


class ThreadsAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "outbox.sqlite3"
        self.inbox_db = self.root / "inbox.sqlite3"
        self.registry = AccountRegistry(self.root / "accounts.json")
        self.environment = patch.dict(os.environ, {"SOCIAL_FARM_ACCOUNTS_FILE": str(self.registry.path)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        self.time_patch = patch("farm.outbox.utc_now", side_effect=lambda: self.now)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.sessions = self.root / "ig"
        self.sessions.mkdir()
        self.state = self.root / "scrapling"
        self.browser = FakeBrowser()
        self.runner = Mock()
        self.reader = Mock()
        self.agent = ThreadsAgent(
            registry=self.registry, db=self.db, state_dir=self.state,
            session_dir=self.sessions, python=sys.executable, runner=self.runner,
            cadence_factory=lambda account: ThreadsCadence(account, directory=self.root / "cadence"),
            browser_factory=lambda: self.browser, reader=self.reader, clock=lambda: self.now,
            inbox_factory=lambda: Inbox(self.inbox_db),
            post_marker=self.root / "threadspost",
        )
        for account in ("one", "two"):
            session = self.make_session(account)
            for platform in ("instagram", "threads"):
                self.registry.register(platform=platform, account=account,
                                       kind="instagrapi_settings", session_file=session,
                                       account_key=f"meta:{account}")
            self.make_profile(account)
        self.body = self.root / "message.txt"
        self.body.write_text("One complete thought.", encoding="utf-8")

    def make_session(self, account):
        session = self.sessions / f"{account}.json"
        session.write_text(json.dumps({"authorization_data": {
            "sessionid": "PRIVATE_TEST_SESSION", "ds_user_id": "12345"}}))
        session.chmod(0o600)
        return session

    def make_profile(self, account):
        directory = self.state / account
        (directory / "profile").mkdir(parents=True, exist_ok=True)
        marker = directory / "verified-cookies.json"
        marker.write_text(json.dumps({"version": 1, "account": account,
                                      "cookies": [{"value": "PRIVATE_TEST_COOKIE"}]}))
        marker.chmod(0o600)

    def create(self, key="message-one", account="one", **kwargs):
        return self.agent.create(account, body_file=str(self.body), idempotency_key=key, **kwargs)

    def comment(self, account="one", comment_id="789", worker="operator"):
        inbox = Inbox(self.inbox_db)
        try:
            inbox.save_external_comment(
                account=f"threads:{account}", comment_id=comment_id, media_id="123",
                parent_id=None, url=f"https://www.threads.com/@reader/post/R{comment_id}",
                media_url=f"https://www.threads.com/@{account}/post/P123",
                author="reader", body="A question for operator.", timestamp=self.now.isoformat(),
            )
            if worker:
                inbox.unseen(f"threads:{account}", worker=worker)
        finally:
            inbox.close()

    def reply_inbox(self, **kwargs):
        return self.agent.reply_inbox("one", comment_id="789", body_file=str(self.body), **kwargs)

    def test_inbox_reply_consumes_comment_and_retains_source_target(self):
        self.comment()
        result = self.reply_inbox()
        self.assertEqual(result["status"], "published")
        inbox = Inbox(self.inbox_db)
        try:
            self.assertEqual(inbox.pending("threads:one", worker="operator"), [])
            self.assertEqual(inbox.unseen("threads:one", worker="operator"), [])
            row = inbox.db.execute("SELECT * FROM comments WHERE account='threads:one'").fetchone()
            self.assertEqual(row["draft_job_id"], result["job_id"])
            self.assertEqual(row["response_state"], "drafted")
        finally:
            inbox.close()
        with Outbox(self.db) as box:
            job = box.get(result["job_id"])
            self.assertEqual((job["account"], job["action"], job["target_id"], job["media_id"]),
                             ("one", "reply", "789", "789"))
            self.assertEqual(job["target_url"], "https://www.threads.com/@reader/post/R789")
            self.assertIn("https://www.threads.com/@one/post/P123", job["source_urls"])

    def test_inbox_queue_only_resumes_the_same_job_without_a_new_key(self):
        self.comment()
        queued = self.reply_inbox(queue_only=True)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(self.browser.calls, [])
        published = self.reply_inbox()
        again = self.reply_inbox()
        self.assertEqual(queued["job_id"], published["job_id"])
        self.assertTrue(again["already_published"])
        self.assertEqual(len(self.browser.calls), 1)
        self.body.write_text("A different reply.")
        with self.assertRaisesRegex(InputError, "inbox_reply_content_conflict"):
            self.reply_inbox()
        self.assertEqual(len(self.browser.calls), 1)

    def test_inbox_reply_requires_operator_claim_and_correct_account(self):
        self.comment(worker="another-worker")
        with self.assertRaisesRegex(InputError, "inbox_comment_not_claimed_by_worker"):
            self.reply_inbox()
        with self.assertRaisesRegex(InputError, "inbox_comment_not_found"):
            self.agent.reply_inbox("two", comment_id="789", body_file=str(self.body))
        self.comment(comment_id="790", worker=None)
        with self.assertRaisesRegex(InputError, "inbox_comment_not_claimed_by_worker"):
            self.agent.reply_inbox("one", comment_id="790", body_file=str(self.body))
        with Outbox(self.db) as box:
            self.assertEqual(box.list(status="all"), [])
        self.assertEqual(self.browser.calls, [])

    def test_inbox_reply_uses_the_selected_worker_claim(self):
        self.comment(worker="community-helper")
        result = self.reply_inbox(worker="community-helper", queue_only=True)
        self.assertEqual(result["status"], "queued")
        with Outbox(self.db) as box:
            self.assertEqual(box.get(result["job_id"])["target_id"], "789")
        self.assertEqual(self.browser.calls, [])

    def test_invalid_body_leaves_inbox_comment_pending(self):
        self.comment()
        self.body.write_text("x" * 501)
        with self.assertRaisesRegex(InputError, "text_must_contain_1_to_500_characters"):
            self.reply_inbox()
        inbox = Inbox(self.inbox_db)
        try:
            self.assertEqual(len(inbox.pending("threads:one", worker="operator")), 1)
        finally:
            inbox.close()
        self.assertEqual(self.browser.calls, [])

    def test_inbox_auth_failure_retains_binding_for_exact_retry(self):
        self.comment()
        self.browser.error = AuthUnavailable("browser_login_required")
        failed = self.reply_inbox()
        self.assertEqual(failed["status"], "auth_unavailable")
        inbox = Inbox(self.inbox_db)
        try:
            self.assertEqual(inbox.pending("threads:one", worker="operator"), [])
        finally:
            inbox.close()
        self.browser.error = None
        published = self.reply_inbox()
        self.assertEqual(published["status"], "published")
        self.assertEqual(failed["job_id"], published["job_id"])
        self.assertEqual(len({call[0] for call in self.browser.calls}), 1)

    def test_uncertain_inbox_reply_remains_bound_and_cannot_resend(self):
        self.comment()
        self.browser.error = PublishUncertain("private provider failure")
        result = self.reply_inbox()
        again = self.reply_inbox()
        self.assertEqual((result["status"], again["status"]), ("uncertain", "uncertain"))
        self.assertEqual(result["job_id"], again["job_id"])
        self.assertEqual(len(self.browser.calls), 1)

    def test_reconcile_uncertain_reply_from_exact_receipt(self):
        self.comment()
        self.browser.error = PublishUncertain("provider receipt delayed")
        sent = self.reply_inbox()
        self.reader.reconcile.return_value = {
            "status": "published", "provider_id": "456789",
            "permalink": "https://www.threads.com/@one/post/Receipt"}
        confirmed = self.agent.reconcile("one", sent["job_id"])
        self.assertEqual(confirmed["status"], "published")
        self.assertEqual(confirmed["provider_id"], "456789")
        self.assertEqual(self.agent.reconcile("one", sent["job_id"]), confirmed)
        self.assertEqual(len(self.browser.calls), 1)
        self.reader.reconcile.assert_called_once()

    def test_unconfirmed_receipt_keeps_uncertain_job(self):
        self.browser.error = PublishUncertain("provider receipt delayed")
        sent = self.create()
        self.reader.reconcile.return_value = {
            "status": "unconfirmed", "reason": "receipt_not_found"}
        result = self.agent.reconcile("one", sent["job_id"])
        self.assertEqual(result["status"], "unconfirmed")
        with Outbox(self.db) as box:
            self.assertEqual(box.get(sent["job_id"])["status"], "uncertain")
        self.assertEqual(len(self.browser.calls), 1)

    def test_skipped_inbox_comment_cannot_be_reopened_by_reply(self):
        self.comment()
        inbox = Inbox(self.inbox_db)
        try:
            inbox.skip("threads:one", "789", worker="operator")
        finally:
            inbox.close()
        with self.assertRaisesRegex(InputError, "inbox_comment_skipped"):
            self.reply_inbox()
        self.assertEqual(self.browser.calls, [])

    def test_repeating_a_post_returns_the_original_receipt_without_another_send(self):
        first = self.create()
        self.assertTrue((self.root / "threadspost").exists())
        self.assertEqual((self.root / "threadspost").stat().st_mode & 0o077, 0)
        first_mtime = (self.root / "threadspost").stat().st_mtime
        again = self.create()
        self.assertEqual(first["status"], "published")
        self.assertTrue(again["already_published"])
        self.assertEqual(first["job_id"], again["job_id"])
        self.assertEqual(first["permalink"], again["permalink"])
        self.assertEqual(len(self.browser.calls), 1)
        self.assertEqual((self.root / "threadspost").stat().st_mtime, first_mtime)
        self.body.write_text("Changed text with the same intent key.")
        with self.assertRaisesRegex(InputError, "idempotency_key_content_conflict"):
            self.create()
        self.assertEqual(len(self.browser.calls), 1)

    def test_originals_wait_one_hour_and_daytime_replies_have_no_interval(self):
        first = self.create()
        self.now += timedelta(minutes=59, seconds=59)
        waiting = self.create("message-two")
        self.assertEqual(waiting["status"], "cadence_wait")
        self.assertEqual(waiting["next_original_at"], "2026-10-26T13:00:00Z")
        with Outbox(self.db) as box:
            self.assertEqual(box.get(waiting["job_id"])["attempts"], 0)
        replied = self.create("reply-one", action="reply", post_id="123456789",
                              url=first["permalink"])
        self.assertEqual(replied["status"], "published")
        self.now += timedelta(seconds=1)
        published = self.agent.publish("one", waiting["job_id"])
        self.assertEqual(published["status"], "published")
        self.assertEqual([entry[2] for entry in self.browser.calls], ["post", "reply", "post"])

    def test_exact_publish_counts_existing_receipts_from_outside_the_harness(self):
        with Outbox(self.db) as box:
            prior = box.create_draft(platform="threads", account="one", body="Existing public post")
            box.queue(prior["id"])
            held = box.claim_job(prior["id"])
            box.finish(prior["id"], held["lease_token"], outcome="published", provider_id="900")
        self.now += timedelta(minutes=30)
        result = self.create()
        self.assertEqual(result["status"], "cadence_wait")
        self.assertEqual(self.browser.calls, [])
        self.assertEqual(self.agent.status("one")["next_original_at"], "2026-10-26T13:00:00Z")

    def test_quiet_hours_preserve_post_and_reply_jobs_without_any_browser_call(self):
        self.now = datetime(2026, 10, 27, 4, tzinfo=timezone.utc)
        post = self.create()
        reply = self.create("quiet-reply", action="reply", post_id="123",
                            url="https://www.threads.com/@reader/post/abc")
        for result in (post, reply):
            self.assertEqual(result["status"], "quiet_hours")
            self.assertEqual(result["next_active_at"], "2026-10-27T10:00:00Z")
            self.assertEqual(result["next_active_local"], "2026-10-27T06:00:00-04:00")
            with Outbox(self.db) as box:
                job = box.get(result["job_id"])
                self.assertEqual((job["status"], job["attempts"]), ("queued", 0))
        self.assertEqual(self.browser.calls, [])
        self.assertEqual(self.agent.status("one")["status"], "ready")
        self.assertEqual(self.agent.job("one", reply["job_id"])["job"]["status"], "queued")
        self.now = datetime(2026, 10, 27, 10, tzinfo=timezone.utc)
        self.assertEqual(self.agent.publish("one", post["job_id"])["status"], "published")
        self.assertEqual(self.agent.publish("one", reply["job_id"])["status"], "published")

    def test_quiet_hours_skip_reads_and_follow_dst_to_the_next_local_six(self):
        for now, expected in (
            (datetime(2026, 3, 8, 5, tzinfo=timezone.utc), "2026-03-08T10:00:00Z"),
            (datetime(2026, 11, 1, 4, tzinfo=timezone.utc), "2026-11-01T11:00:00Z"),
        ):
            self.now = now
            for operation in ("me", "own", "replies"):
                result = self.agent.read("one", operation, post_id="123",
                                         url="https://www.threads.com/@reader/post/abc")
                self.assertEqual(result["status"], "quiet_hours")
                self.assertEqual(result["next_active_at"], expected)
        self.reader.read.assert_not_called()
        self.now = datetime(2026, 11, 1, 11, tzinfo=timezone.utc)
        self.reader.read.return_value = {"posts": []}
        self.assertTrue(self.agent.read("one", "own")["ok"])
        self.reader.read.assert_called_once()

    def test_provider_quiet_stop_during_a_run_preserves_the_exact_queued_job(self):
        self.now = datetime(2026, 10, 27, 3, 59, 59, tzinfo=timezone.utc)
        def before_submit(job):
            self.now = datetime(2026, 10, 27, 4, tzinfo=timezone.utc)
            raise AuthUnavailable("quiet_hours")
        self.browser.publish = Mock(side_effect=before_submit)
        result = self.create()
        self.assertEqual(result["status"], "quiet_hours")
        with Outbox(self.db) as box:
            self.assertEqual(box.get(result["job_id"])["status"], "queued")
        self.browser.publish.assert_called_once()

    def test_provider_guard_minute_returns_the_next_morning(self):
        self.now = datetime(2026, 10, 27, 3, 59, 30, tzinfo=timezone.utc)
        self.browser.error = AuthUnavailable("quiet_hours")
        result = self.create()
        self.assertEqual(result["status"], "quiet_hours")
        self.assertEqual(result["next_active_at"], "2026-10-27T10:00:00Z")
        self.assertEqual(result["next_active_local"], "2026-10-27T06:00:00-04:00")
        self.assertEqual(len(self.browser.calls), 1)

    def test_inbox_reply_during_quiet_hours_is_consumed_into_one_queued_job(self):
        self.comment()
        self.now = datetime(2026, 10, 27, 4, tzinfo=timezone.utc)
        result = self.reply_inbox()
        self.assertEqual(result["status"], "quiet_hours")
        self.assertEqual(self.browser.calls, [])
        inbox = Inbox(self.inbox_db)
        try:
            self.assertEqual(inbox.pending("threads:one", worker="operator"), [])
        finally:
            inbox.close()
        self.now = datetime(2026, 10, 27, 10, tzinfo=timezone.utc)
        published = self.reply_inbox()
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["job_id"], result["job_id"])

    def test_competing_originals_serialize_before_send(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(self.create, ("competing-one", "competing-two")))
        self.assertCountEqual([result["status"] for result in results], ["published", "cadence_wait"])
        self.assertEqual(len(self.browser.calls), 1)

    def test_account_isolation_and_exact_job_selection(self):
        one = self.create("older", queue_only=True)
        later = self.create("selected", queue_only=True)
        self.assertEqual(self.agent.publish("one", later["job_id"])["status"], "published")
        with self.assertRaisesRegex(InputError, "job_account_mismatch"):
            self.agent.publish("two", one["job_id"])
        with Outbox(self.db) as box:
            self.assertEqual(box.get(one["job_id"])["status"], "queued")
        other = self.create("selected", account="two")
        self.assertEqual(other["status"], "published")
        self.assertNotEqual(other["job_id"], later["job_id"])
        self.assertEqual([entry[0] for entry in self.browser.calls], [later["job_id"], other["job_id"]])

    def test_uncertain_send_cannot_repeat_or_send_a_different_reply(self):
        self.browser.error = PublishUncertain("PRIVATE_PROVIDER_BODY")
        result = self.create()
        again = self.create()
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(again["status"], "uncertain")
        self.assertEqual(len(self.browser.calls), 1)
        self.browser.error = None
        reply = self.create("reply", action="reply", post_id="123", url="https://threads.net/@reader/post/abc")
        self.assertEqual(reply["status"], "not_claimable")
        self.assertEqual(len(self.browser.calls), 1)
        self.assertNotIn("PRIVATE_PROVIDER_BODY", json.dumps(result))

    def test_pre_send_failure_is_not_automatically_requeued(self):
        self.browser.error = PreSendFailure("private adapter detail")
        result = self.create()
        self.assertEqual(result["status"], "failed_before_send")
        self.assertEqual(self.create()["status"], "failed")
        self.assertEqual(len(self.browser.calls), 1)
        self.assertIsNone(self.agent.status("one")["next_original_at"])

    def test_retry_exact_pre_send_job_uses_same_id(self):
        self.browser.error = PreSendFailure(
            "Scrapling stopped before submit (prepare_composer:reply_control_missing)")
        failed = self.create()
        self.assertEqual(failed["status"], "failed_before_send")
        self.assertIn("retry", failed["next_step"])
        self.browser.error = None
        posted = self.agent.retry("one", failed["job_id"])
        self.assertEqual(posted["status"], "published")
        self.assertEqual(posted["job_id"], failed["job_id"])
        self.assertEqual(len(self.browser.calls), 2)

    def test_missing_reply_target_returns_actionable_failure_code(self):
        self.browser.error = PreSendFailure(
            "Scrapling stopped before submit (read_before:target_unavailable)")
        result = self.create("reply", action="reply", post_id="123",
                             url="https://www.threads.com/@reader/post/abc")
        self.assertEqual(result["status"], "failed_before_send")
        self.assertEqual((result["failure_phase"], result["failure_reason"]),
                         ("read_before", "target_unavailable"))

    def test_job_summary_exposes_only_fixed_composer_failure_code(self):
        summary = job_summary({"id": "job", "last_error":
            "Scrapling stopped before submit (prepare_composer:composer_missing)"})
        self.assertEqual(summary["failure_phase"], "prepare_composer")
        self.assertEqual(summary["failure_reason"], "composer_missing")
        self.assertNotIn("last_error", summary)

    def test_auth_failure_keeps_the_exact_job_queued(self):
        self.browser.error = AuthUnavailable("browser_login_required")
        result = self.create()
        self.assertEqual(result["status"], "auth_unavailable")
        with Outbox(self.db) as box:
            self.assertEqual(box.get(result["job_id"])["status"], "queued")
        self.assertEqual(len(self.browser.calls), 1)
        self.assertIsNone(self.agent.status("one")["next_original_at"])

    def test_missing_scrapling_profile_stops_before_any_browser_attempt(self):
        (self.state / "one" / "verified-cookies.json").unlink()
        result = self.create()
        self.assertEqual(result["status"], "auth_unavailable")
        self.assertEqual(result["account_action"], "browser_login_required")
        self.assertEqual(self.browser.calls, [])
        with Outbox(self.db) as box:
            self.assertEqual(box.get(result["job_id"])["attempts"], 0)

    def test_reads_pass_explicit_target_and_are_bounded(self):
        self.reader.read.return_value = {"posts": [{"id": "456", "body": "Reader reply"}]}
        result = self.agent.read("two", "replies", limit=5, post_id="123",
                                 url="https://threads.net/@reader/post/abc/")
        self.reader.read.assert_called_once_with(
            "two", "replies", limit=5, post_id="123", target_url="https://www.threads.com/@reader/post/abc")
        self.assertEqual(result["result"]["posts"][0]["id"], "456")
        for limit in (0, 26):
            with self.assertRaises(InputError):
                self.agent.read("one", "own", limit=limit)
        self.assertEqual(self.reader.read.call_count, 1)

    def test_target_read_returns_clear_missing_post_result(self):
        self.reader.read.return_value = {"target_unavailable": True}
        result = self.agent.read("one", "target", post_id="123",
                                 url="https://www.threads.com/@reader/post/abc")
        self.assertEqual(result["status"], "target_unavailable")
        self.assertFalse(result["ok"])

    def test_search_and_feed_use_account_session_without_a_job(self):
        self.reader.read.return_value = {"posts": [{"id": "456", "body": "local AI"}]}
        searched = self.agent.read("one", "search", limit=5, query="local AI")
        self.assertEqual(searched["result"]["posts"][0]["id"], "456")
        self.reader.read.assert_called_with("one", "search", limit=5, post_id="",
                                            target_url="", query="local AI")
        self.agent.read("one", "feed", limit=4)
        self.reader.read.assert_called_with("one", "feed", limit=4, post_id="", target_url="")
        with self.assertRaisesRegex(InputError, "query_must_contain_2_to_120_characters"):
            self.agent.read("one", "search", query=" ")

    def test_bad_reply_target_never_creates_a_job_or_calls_a_provider(self):
        for value in ("https://evil.test/@one/post/abc", "https://threads.com@evil.test/@one/post/abc",
                      "https://threads.com/@one/post/abc?secret=private", "http://threads.com/@one/post/abc"):
            with self.assertRaises(InputError):
                self.create(action="reply", post_id="123", url=value)
        with self.assertRaises(InputError):
            self.create(action="reply", post_id="abc", url="https://threads.com/@one/post/abc")
        with Outbox(self.db) as box:
            self.assertEqual(box.list(status="all"), [])
        self.assertEqual(self.browser.calls, [])

    def test_status_and_existing_connection_do_not_make_network_calls_or_expose_secrets(self):
        status = self.agent.status("one")
        result = self.agent.connect("one")
        self.assertEqual(status["status"], "ready")
        self.assertTrue(result["existing_profile"])
        self.runner.assert_not_called()
        self.reader.read.assert_not_called()
        self.assertEqual(self.browser.calls, [])
        output = json.dumps([status, result])
        self.assertNotIn("PRIVATE_TEST", output)
        self.assertNotIn("session_file", output)
        self.assertNotIn("cookies", output)

    def test_new_account_connects_from_its_own_private_instagram_session_once(self):
        session = self.make_session("three")
        def linked_login(*args, **kwargs):
            self.make_profile("three")
            return subprocess.CompletedProcess(args[0], 0,
                stdout=json.dumps({"ok": True, "status": "saved", "cookie": "PRIVATE_PROVIDER_COOKIE"}))
        self.runner.side_effect = linked_login
        result = self.agent.connect("three")
        self.assertEqual(result["status"], "connected")
        self.runner.assert_called_once()
        self.assertEqual(self.runner.call_args.args[0][-4:], ["--account", "three", "--mode", "linked"])
        for platform in ("instagram", "threads"):
            entry = self.registry.get(platform, "three")
            self.assertEqual(entry["session_file"], str(session.resolve()))
            self.assertEqual(entry["account_key"], "meta:three")
        self.assertNotIn("PRIVATE_PROVIDER_COOKIE", json.dumps(result))

    def test_connect_rejects_nonprivate_session_before_subprocess(self):
        self.make_session("three").chmod(0o644)
        with self.assertRaisesRegex(InputError, "private_instagram_session_required"):
            self.agent.connect("three")
        self.runner.assert_not_called()
        self.assertIsNone(self.registry.get("threads", "three"))

    def test_cli_does_not_echo_private_exception_bodies(self):
        output = io.StringIO()
        with patch("scripts.threads_agent.ThreadsAgent") as factory, redirect_stdout(output):
            factory.return_value.read.side_effect = RuntimeError("secret=PRIVATE_PROVIDER_TOKEN")
            code = main(["--account", "one", "read", "own"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue()), {"ok": False, "status": "operation_unavailable"})

    def test_cli_distinguishes_local_profile_contention_from_provider_limits(self):
        output = io.StringIO()
        with patch("scripts.threads_agent.ThreadsAgent") as factory, redirect_stdout(output):
            factory.return_value.read.side_effect = ProfileBusy("local browser profile remained busy")
            code = main(["--account", "one", "read", "search", "--query", "local AI"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue()), {"ok": False, "status": "profile_busy"})


if __name__ == "__main__":
    unittest.main()
