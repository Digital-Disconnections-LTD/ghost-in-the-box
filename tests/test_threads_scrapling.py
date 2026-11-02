import hashlib
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, Mock, patch

from farm.adapters.threads import AuthUnavailable, PublishUncertain
from farm.threads_browser import BrowserThreadsAdapter
from farm.threads_scrapling import ProfileBusy, ScraplingThreadsAdapter, verified_profile
from farm.threads_transport import ThreadsDependencyUnavailable
from scripts import threads_scrapling_bridge as bridge
from scripts import threads_scrapling_login as login


class Registry:
    def status(self, platform, account):
        return {"kind": "instagrapi_settings", "account_key": "meta:mine", "session_file": "/private/ig.json"}


class ScraplingPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.account_dir = login.private_dir(self.root / "mine")
        login.private_dir(self.account_dir / "profile")
        login.save_json(self.account_dir / "verified-cookies.json", {
            "version": 1, "account": "mine", "cookies": []})
        body = "Exact approved text"
        self.job = {"id": "exact-job", "platform": "threads", "account": "mine", "account_key": "meta:mine",
                    "action": "post", "body": body, "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                    "media_path": None, "media_sha256": None}
        self.command = {"mode": "publish", "job": self.job}
        self.page = Mock()
        self.identity = Mock(return_value=(True, []))
        self.submit = Mock()
        self.prepare = Mock(return_value=self.submit)
        self.capture = Mock(side_effect=[[], [self.post("123")]])

    def tearDown(self):
        self.temp.cleanup()

    def post(self, pk, *, body=None):
        return {"pk": pk, "code": "AbCd", "user": {"username": "mine"},
                "caption": {"text": body or self.job["body"]}}

    def execute(self):
        return bridge.execute(self.page, self.command, self.account_dir,
                              capture=self.capture, identity=self.identity, prepare=self.prepare)

    def test_one_submit_and_new_receipt_readback(self):
        result = self.execute()
        self.assertEqual(result, {"status": "published", "provider_id": "123",
                                 "permalink": "https://www.threads.com/@mine/post/AbCd"})
        self.submit.assert_called_once()
        self.assertEqual(self.capture.call_count, 2)
        self.assertEqual(login.private_json(bridge.attempt_path(self.account_dir, self.job)), result)

    def test_missing_identity_never_prepares_or_submits(self):
        self.identity.return_value = (False, [])
        self.assertEqual(self.execute()["status"], "auth_unavailable")
        self.prepare.assert_not_called()
        self.submit.assert_not_called()
        self.capture.assert_not_called()

    def test_prior_matching_post_stops_before_composer(self):
        self.capture.side_effect = [[self.post("100")]]
        self.assertEqual(self.execute()["status"], "possible_duplicate")
        self.prepare.assert_not_called()

    def test_submit_exception_is_uncertain_and_journal_blocks_retry(self):
        self.submit.side_effect = RuntimeError("private provider body")
        self.assertEqual(self.execute()["status"], "uncertain")
        self.assertEqual(self.execute()["status"], "possible_duplicate")
        self.submit.assert_called_once()
        path = bridge.attempt_path(self.account_dir, self.job)
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertNotIn("private provider", path.read_text())

    def test_quiet_window_releases_before_journal_or_submit(self):
        with patch.object(bridge, "public_window", return_value=False):
            self.assertEqual(self.execute()["status"], "quiet_hours")
        self.submit.assert_not_called()
        self.assertFalse(bridge.attempt_path(self.account_dir, self.job).exists())
        self.assertFalse(bridge.public_window(datetime(2026,10,26,9,59,tzinfo=timezone.utc)))
        self.assertTrue(bridge.public_window(datetime(2026,10,26,10,0,tzinfo=timezone.utc)))
        self.assertFalse(bridge.public_window(datetime(2026,10,27,3,59,tzinfo=timezone.utc)))

    def test_receipt_miss_retries_only_reads(self):
        self.capture.side_effect = [[], [], [], [], [], [], []]
        self.assertEqual(self.execute()["status"], "uncertain")
        self.submit.assert_called_once()
        self.assertEqual(self.capture.call_count, 7)

    def test_reconcile_observed_reply_without_another_submit(self):
        self.job.update(action="reply", target_id="90", media_id="90",
                        target_url="https://www.threads.com/@other/post/Target")
        parent = {"pk": "90", "code": "Target", "user": {"username": "other"},
                  "caption": {"text": "A question"}}
        reply = self.post("123")
        reply["taken_at"] = datetime.now(timezone.utc).timestamp()
        reply["text_post_app_info"] = {"is_reply": True,
                                       "reply_to_author": {"username": "other"}}
        login.save_json(bridge.attempt_path(self.account_dir, self.job), {
            "status": "attempted", "body_sha256": self.job["body_sha256"],
            "at": datetime.now(timezone.utc).isoformat()})
        self.command["mode"] = "reconcile"
        self.capture.side_effect = [[parent, reply]]
        result = self.execute()
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["provider_id"], "123")
        self.submit.assert_not_called()
        self.assertEqual(self.execute(), result)

    def test_reconcile_requires_direct_reply_author(self):
        self.job.update(action="reply", target_id="90", media_id="90",
                        target_url="https://www.threads.com/@other/post/Target")
        reply = self.post("123")
        reply["taken_at"] = datetime.now(timezone.utc).timestamp()
        reply["text_post_app_info"] = {"is_reply": True,
                                       "reply_to_author": {"username": "someone_else"}}
        login.save_json(bridge.attempt_path(self.account_dir, self.job), {
            "status": "attempted", "body_sha256": self.job["body_sha256"],
            "at": datetime.now(timezone.utc).isoformat()})
        self.command["mode"] = "reconcile"
        self.capture.side_effect = [[reply]]
        self.assertEqual(self.execute()["status"], "unconfirmed")
        self.submit.assert_not_called()

    def test_read_failures_after_submit_cannot_send_again(self):
        self.capture.side_effect = [[], RuntimeError(), RuntimeError(), RuntimeError()]
        self.assertEqual(self.execute()["status"], "uncertain")
        self.submit.assert_called_once()

    def test_completed_journal_returns_original_receipt_without_submit(self):
        receipt = self.execute()
        self.capture.reset_mock()
        self.submit.reset_mock()
        self.assertEqual(self.execute(), receipt)
        self.submit.assert_not_called()
        self.capture.assert_not_called()

    def test_modified_frozen_body_is_rejected_before_identity(self):
        self.job["body"] += "changed"
        with self.assertRaises(bridge.BeforeSubmit):
            self.execute()
        self.identity.assert_not_called()

    def test_reply_target_id_must_match_actual_page_post(self):
        self.job.update(action="reply", target_id="90", media_id="90",
                        target_url="https://www.threads.com/@other/post/Target")
        self.capture.side_effect = [[self.post("91")]]
        self.assertEqual(self.execute(), {"status": "pre_send_failure",
                                          "phase": "read_before",
                                          "reason": "target_unavailable"})
        self.submit.assert_not_called()

    def test_read_target_confirms_exact_id_and_permalink(self):
        self.command = {"mode": "read", "account": "mine", "operation": "target",
                        "post_id": "123", "target_url": "https://www.threads.com/@mine/post/AbCd",
                        "limit": 1}
        self.capture.side_effect = [[self.post("123")], [self.post("91")]]
        found = self.execute()
        self.assertEqual(found["status"], "ok")
        self.assertEqual(found["result"]["post"]["id"], "123")
        self.assertEqual(self.execute(), {"status": "target_unavailable"})
        self.submit.assert_not_called()

    def test_quoted_post_is_not_extracted_as_an_independent_receipt(self):
        post = self.post("1")
        post["text_post_app_info"] = {"share_info": {"quoted_post": self.post("2")}}
        self.assertEqual([p["pk"] for p in bridge.extract_posts([{"items": [post]}])], ["1"])

    def test_search_reads_public_posts_through_verified_session(self):
        self.command = {"mode": "read", "account": "mine", "operation": "search",
                        "query": "local AI", "limit": 5}
        self.capture.side_effect = [[{
            "pk": "77", "code": "GreatPost", "user": {"username": "someoneelse"},
            "caption": {"text": "A public conversation"}, "like_count": 91}]]
        result = self.execute()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result"]["posts"][0]["username"], "someoneelse")
        self.assertEqual(result["result"]["posts"][0]["like_count"], 91)
        self.assertEqual(self.capture.call_args.args[1], "https://www.threads.com/search?q=local%20AI")
        self.submit.assert_not_called()

    def test_search_rejects_empty_or_control_character_query(self):
        self.command = {"mode": "read", "account": "mine", "operation": "search",
                        "query": "\n", "limit": 5}
        with self.assertRaises(bridge.BeforeSubmit):
            self.execute()
        self.identity.assert_not_called()

    def test_pre_send_composer_failure_returns_fixed_local_reason(self):
        self.prepare.side_effect = bridge.BeforeSubmit("composer_missing")
        result = self.execute()
        self.assertEqual(result, {"status": "pre_send_failure",
                                  "phase": "prepare_composer", "reason": "composer_missing"})
        self.assertFalse(bridge.attempt_path(self.account_dir, self.job).exists())

    def test_missing_composer_reopens_once_before_any_submit(self):
        self.page.context.pages = [self.page]
        self.prepare.side_effect = [bridge.BeforeSubmit("composer_missing"), self.submit]
        result = self.execute()
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.prepare.call_count, 2)
        self.submit.assert_called_once()

    def test_context_close_error_preserves_published_receipt(self):
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        session.fetch.side_effect = lambda url, **kwargs: kwargs["page_action"](self.page)
        factory.return_value.__exit__.side_effect = RuntimeError("close failed")
        expected = {"status": "published", "provider_id": "123", "permalink": "receipt"}
        with patch.dict(os.environ, {"SOCIAL_FARM_THREADS_SCRAPLING_STATE": str(self.root)}), \
                patch.object(bridge, "execute", return_value=expected):
            self.assertEqual(bridge.run(self.command, factory=factory), expected)
        self.assertEqual(factory.call_args.kwargs["retries"], 1)

    def test_adapter_auto_selection_uses_private_verified_profile(self):
        adapter = BrowserThreadsAdapter(registry=Registry(), state_dir=self.root / "old-browser",
                                        scrapling_state_dir=self.root)
        self.assertIsInstance(adapter._scrapling("mine"), ScraplingThreadsAdapter)
        (self.account_dir / "verified-cookies.json").chmod(0o644)
        self.assertIsNone(adapter._scrapling("mine"))
        self.assertFalse(verified_profile("..", self.root))

    def test_adapter_passes_exact_job_via_stdin_and_checks_receipt(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
            "status": "published", "provider_id": "123",
            "permalink": "https://www.threads.com/@mine/post/AbC"}), ""))
        python = self.root / "python"
        python.touch()
        adapter = ScraplingThreadsAdapter(registry=Registry(), runner=runner, state_dir=self.root, python=python)
        self.assertEqual(adapter.publish(self.job)["provider_id"], "123")
        args, kwargs = runner.call_args
        self.assertNotIn(self.job["body"], " ".join(args[0]))
        self.assertEqual(json.loads(kwargs["input"])["job"], self.job)
        runner.return_value.stdout = json.dumps({"status": "published", "provider_id": "123", "permalink": "bad"})
        with self.assertRaises(PublishUncertain):
            adapter.publish(self.job)

    def test_adapter_auth_and_uncertain_map_to_existing_semantics(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 2, '{"status":"auth_unavailable"}', ""))
        python = self.root / "python"
        python.touch()
        adapter = ScraplingThreadsAdapter(registry=Registry(), runner=runner, state_dir=self.root, python=python)
        with self.assertRaises(AuthUnavailable):
            adapter.publish(self.job)
        runner.return_value.stdout = '{"status":"uncertain"}'
        with self.assertRaises(PublishUncertain):
            adapter.publish(self.job)
        runner.side_effect = subprocess.TimeoutExpired([], 180)
        with self.assertRaises(PublishUncertain):
            adapter.publish(self.job)

    def test_adapter_reports_unavailable_target_without_exposing_page_data(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 2,
            '{"status":"target_unavailable"}', ""))
        python = self.root / "python"
        python.touch()
        adapter = ScraplingThreadsAdapter(registry=Registry(), runner=runner,
                                          state_dir=self.root, python=python,
                                          sleep=lambda _: None)
        self.assertEqual(adapter.read("mine", "target", post_id="123",
                                      target_url="https://www.threads.com/@mine/post/AbCd"),
                         {"target_unavailable": True})

    def test_read_staggers_fetch_and_retries_local_profile_contention(self):
        statuses = ('profile_busy', 'profile_busy', 'ok')
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0,
            json.dumps({"status": status, "result": {"posts": []}}), "") for status in statuses])
        python = self.root / "python"
        python.touch()
        delays = []
        adapter = ScraplingThreadsAdapter(registry=Registry(), runner=runner,
            state_dir=self.root, python=python, sleep=delays.append,
            jitter=lambda low, high: (low + high) / 2, max_busy_retries=3)
        self.assertEqual(adapter.read("mine", "search", query="local AI"), {"posts": []})
        self.assertEqual(delays, [2.5, 4.0, 4.0])
        self.assertEqual(runner.call_count, 3)

    def test_busy_profile_has_distinct_result_after_bounded_retries(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 2,
            '{"status":"profile_busy"}', ""))
        python = self.root / "python"
        python.touch()
        adapter = ScraplingThreadsAdapter(registry=Registry(), runner=runner,
            state_dir=self.root, python=python, sleep=lambda _: None,
            jitter=lambda low, high: low, max_busy_retries=1)
        with self.assertRaises(ProfileBusy):
            adapter.read("mine", "search", query="local AI")
        self.assertEqual(runner.call_count, 2)

    def test_post_composer_in_second_tab_uses_its_menu_post_button(self):
        original, composer = Mock(), Mock()
        original.context.pages = [original, composer]
        original.is_closed.return_value = False
        composer.is_closed.return_value = False
        original.locator.return_value.count.return_value = 0
        scope = Mock()
        scope.count.return_value = 1
        composer.locator.return_value = scope
        editor = Mock()
        editor.inner_text.side_effect = ["", self.job["body"]]
        locator = Mock()
        locator.count.return_value = 1
        locator.first = editor
        scope.locator.return_value = locator
        button = Mock()
        button.count.return_value = 1
        button.is_enabled.return_value = True
        scope.get_by_role.return_value = button
        with patch.object(login, "visible", return_value=Mock()), \
                patch.object(login, "trusted", return_value=True):
            submit = bridge.prepare_submit(original, self.job)
        submit()
        editor.fill.assert_called_once_with(self.job["body"])
        button.click.assert_called_once_with(timeout=10000, no_wait_after=True)
        self.assertIs(bridge.current_page(original), composer)

    def test_reply_composer_uses_dialog_post_button_in_second_tab(self):
        self.job.update(action="reply", target_url="https://www.threads.com/@mine/post/AbCd")
        original, composer = Mock(), Mock()
        original.context.pages = [original, composer]
        original.is_closed.return_value = False
        composer.is_closed.return_value = False
        inline, expand = Mock(), Mock()
        inline.count.return_value = 1
        expand.count.return_value = 1
        expand.is_visible.return_value = True
        original.locator.side_effect = lambda selector: (
            inline if selector.startswith('[contenteditable="true"][aria-placeholder=') else
            expand if selector == '[aria-label="Expand composer"]' else Mock(count=Mock(return_value=0)))
        dialog = Mock()
        dialog.count.return_value = 1
        composer.locator.return_value = dialog
        editor = Mock()
        editor.inner_text.side_effect = ["", self.job["body"]]
        locator = Mock()
        locator.count.return_value = 1
        locator.first = editor
        dialog.locator.return_value = locator
        button = Mock()
        button.count.return_value = 1
        button.is_enabled.return_value = True
        dialog.get_by_role.return_value = button
        with patch.object(login, "trusted", return_value=True):
            submit = bridge.prepare_submit(original, self.job)
        submit()
        expand.click.assert_called_once_with(timeout=10000, no_wait_after=True)
        editor.fill.assert_called_once_with(self.job["body"])
        button.click.assert_called_once_with(timeout=10000, no_wait_after=True)



class ScraplingDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        account_dir = login.private_dir(self.root / "mine")
        login.private_dir(account_dir / "profile")
        login.save_json(account_dir / "verified-cookies.json", {
            "version": 1, "account": "mine", "cookies": []})
        body = "Exact approved text"
        self.job = {"id": "exact-job", "platform": "threads", "account": "mine",
                    "account_key": "meta:mine", "action": "post", "body": body,
                    "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                    "media_path": None, "media_sha256": None}
        self.python = self.root / "python"
        self.python.touch()

    def adapter(self, stdout, python=None):
        runner = Mock(return_value=subprocess.CompletedProcess([], 2, stdout, ""))
        return ScraplingThreadsAdapter(registry=Registry(), runner=runner, state_dir=self.root,
                                       python=python or self.python,
                                       sleep=lambda seconds: None), runner

    def test_bridge_dependency_report_is_a_dependency_error_not_uncertain(self):
        adapter, _ = self.adapter('{"status":"dependency_unavailable","dependency":"scrapling"}')
        with self.assertRaises(ThreadsDependencyUnavailable) as raised:
            adapter.publish(self.job)
        self.assertEqual(raised.exception.dependency, "scrapling")
        self.assertIn(str(self.python), raised.exception.install)
        self.assertIn("[browser]", raised.exception.install)
        self.assertIn("scrapling install", raised.exception.install)

    def test_reads_keep_the_dependency_error_instead_of_a_generic_read_failure(self):
        adapter, _ = self.adapter('{"status":"dependency_unavailable","dependency":"scrapling"}')
        with self.assertRaises(ThreadsDependencyUnavailable):
            adapter.read("mine", "own")

    def test_missing_runtime_python_is_named_without_running_anything(self):
        adapter, runner = self.adapter("{}", python=self.root / "missing-python")
        with self.assertRaises(ThreadsDependencyUnavailable) as raised:
            adapter.publish(self.job)
        self.assertEqual(raised.exception.dependency, "python")
        runner.assert_not_called()

    def test_bridge_reports_missing_scrapling_before_any_browser(self):
        command = {"mode": "read", "account": "mine", "operation": "own", "limit": 1,
                   "post_id": "", "target_url": ""}
        with patch.dict(os.environ, {"SOCIAL_FARM_THREADS_SCRAPLING_STATE": str(self.root)}), \
                patch.dict("sys.modules", {"scrapling": None, "scrapling.fetchers": None}):
            result = bridge.run(command)
        self.assertEqual(result, {"status": "dependency_unavailable", "dependency": "scrapling"})

    def test_login_reports_missing_scrapling_with_install_command(self):
        args = Mock(mode="diagnose", account="mine", headed=False, screenshot=False)
        with patch.dict("sys.modules", {"scrapling": None, "scrapling.fetchers": None}):
            result = login.browser_run(args, self.root / "mine" / "profile", self.root / "mine")
        self.assertEqual((result["status"], result["dependency"]),
                         ("dependency_unavailable", "scrapling"))
        self.assertIn("scrapling install", result["install"])


if __name__ == "__main__":
    unittest.main()
