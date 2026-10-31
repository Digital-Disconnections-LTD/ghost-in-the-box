from contextlib import redirect_stdout
import argparse
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, Mock, patch

from scripts import threads_scrapling_login as login


class ScraplingLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = self.root / "state"
        self.account = login.private_dir(login.private_dir(self.base) / "mine")
        self.profile = login.private_dir(self.account / "profile")
        self.creds = self.root / "ig.json"
        self.creds.write_text(json.dumps({"username": "mine", "password": "secret-password"}))
        self.creds.chmod(0o600)
        self.args = argparse.Namespace(account="mine", mode="login", headed=False,
                                       screenshot=False, verification_code_stdin=False)
        self.page = Mock()
        self.factory = MagicMock()
        session = self.factory.return_value.__enter__.return_value
        session.fetch.side_effect = lambda url, **kw: kw["page_action"](self.page)
        self.state = {"site": "threads", "form_stage": "two_factor", "fields": {"code": True}}

    def tearDown(self):
        self.temp.cleanup()

    def run_browser(self, state=None):
        with patch.object(login, "CREDS", self.creds), \
                patch.object(login, "settled", return_value=(self.page, state or self.state)):
            return login.browser_run(self.args, self.profile, self.account, factory=self.factory)

    def test_code_is_requested_only_after_challenge(self):
        self.args.verification_code_stdin = True
        self.args.mode = "diagnose"
        with patch.object(login, "read_code") as read:
            result = self.run_browser()
        self.assertEqual(result["status"], "diagnostic")
        read.assert_not_called()
        self.page.locator.assert_not_called()

    def test_missing_code_retains_challenge_profile_without_submission(self):
        (self.profile / "challenge-marker").touch()
        with patch.object(login, "read_code") as read, patch.object(login, "submit") as submit:
            result = self.run_browser()
        self.assertEqual(result["status"], "two_factor_required")
        self.assertTrue(result["needs_code"])
        self.assertTrue((self.profile / "challenge-marker").exists())
        read.assert_not_called()
        submit.assert_not_called()
        self.assertEqual(self.factory.call_args.kwargs["retries"], 1)
        self.assertEqual(self.factory.call_args.kwargs["user_data_dir"], str(self.profile))

    def test_unverified_identity_never_exports_cookies(self):
        stale = self.account / "verified-cookies.json"
        login.save_json(stale, {"cookies": ["old-secret"]})
        state = {"site": "threads", "form_stage": "page", "fields": {}}
        with patch.object(login, "verify_identity", return_value=(False, [{"value": "secret"}])), \
                patch.object(login, "inspect_page", return_value=state):
            result = self.run_browser(state)
        self.assertEqual(result["status"], "identity_unverified")
        self.assertFalse(stale.exists())
        self.assertNotIn("secret", json.dumps(result))

    def test_verified_export_is_private_and_result_sanitized(self):
        state = {"site": "threads", "form_stage": "page", "fields": {}}
        with patch.object(login, "verify_identity", return_value=(True, [{"value": "cookie-secret"}])):
            result = self.run_browser(state)
        path = self.account / "verified-cookies.json"
        self.assertEqual(result["status"], "saved")
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(login.private_json(path)["cookies"], [{"value": "cookie-secret"}])
        self.assertNotIn("cookie-secret", json.dumps(result))

    def test_wrong_saved_account_stops_before_browser_launch(self):
        self.args.account = "other"
        with self.assertRaises(login.LocalInputsInvalid):
            self.run_browser()
        self.factory.assert_not_called()

    def test_otp_submits_once_and_does_not_repeat_credentials(self):
        self.args.verification_code_stdin = True
        after = {"site": "threads", "form_stage": "page", "fields": {}}
        field = Mock()
        with patch.object(login, "CREDS", self.creds), \
                patch.object(login, "settled", side_effect=[(self.page, self.state), (self.page, after)]), \
                patch.object(login, "read_code", return_value="123456") as read, \
                patch.object(login, "visible", return_value=field), \
                patch.object(login, "trusted", return_value=True), \
                patch.object(login, "submit", return_value=True) as submit, \
                patch.object(login, "verify_identity", return_value=(True, [])):
            result = login.browser_run(self.args, self.profile, self.account, factory=self.factory)
        self.assertEqual(result["status"], "saved")
        read.assert_called_once()
        submit.assert_called_once_with(self.page, code=True)
        field.fill.assert_called_once_with("123456")
        self.assertNotIn("123456", json.dumps(result))

    def test_diagnostic_uses_copy_and_does_not_read_credentials(self):
        (self.profile / "marker").write_text("original")
        missing_creds = self.root / "missing.json"
        selected = []
        def run(args, profile, account_dir):
            selected.append(profile)
            self.assertEqual((profile / "marker").read_text(), "original")
            (profile / "marker").write_text("diagnostic")
            return {"ok": True, "status": "diagnostic", "needs_code": False}
        with patch.object(login, "BASE", self.base), patch.object(login, "CREDS", missing_creds), \
                patch.object(login, "browser_run", side_effect=run), redirect_stdout(io.StringIO()):
            self.assertEqual(login.main(["--account", "mine"]), 0)
        self.assertEqual((self.profile / "marker").read_text(), "original")
        self.assertNotEqual(selected[0], self.profile)
        self.assertFalse(selected[0].exists())

    def test_symlink_and_public_credentials_are_rejected(self):
        self.creds.chmod(0o644)
        with self.assertRaises(login.LocalInputsInvalid):
            login.private_json(self.creds)
        self.creds.chmod(0o600)
        link = self.root / "link.json"
        link.symlink_to(self.creds)
        with self.assertRaises(login.LocalInputsInvalid):
            login.private_json(link)

    def test_post_submit_wait_ignores_transient_visible_credentials(self):
        page = Mock()
        page.is_closed.return_value = False
        page.context.pages = [page]
        credentials = {"site": "instagram", "form_stage": "credentials", "fields": {}}
        challenge = {"site": "instagram", "form_stage": "two_factor", "fields": {}}
        with patch.object(login, "inspect_page", side_effect=[credentials, credentials, challenge]):
            current, state = login.settled(page, submitted_site="instagram", timeout_ms=20000)
        self.assertIs(current, page)
        self.assertEqual(state["form_stage"], "two_factor")
        self.assertEqual(page.wait_for_timeout.call_count, 2)

    def test_oauth_handoff_submits_once_per_provider_site(self):
        threads = {"site": "threads", "form_stage": "credentials", "fields": {}}
        instagram = {"site": "instagram", "form_stage": "credentials", "fields": {}}
        done = {"site": "threads", "form_stage": "page", "fields": {}}
        with patch.object(login, "CREDS", self.creds), \
                patch.object(login, "settled", side_effect=[(self.page, threads), (self.page, instagram), (self.page, done)]), \
                patch.object(login, "visible", return_value=Mock()), \
                patch.object(login, "trusted", return_value=True), \
                patch.object(login, "submit", return_value=True) as submit, \
                patch.object(login, "verify_identity", return_value=(True, [])):
            result = login.browser_run(self.args, self.profile, self.account, factory=self.factory)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(submit.call_count, 2)

    def test_same_provider_credential_form_never_resubmits(self):
        state = {"site": "threads", "form_stage": "credentials", "fields": {}}
        with patch.object(login, "visible", return_value=Mock()), \
                patch.object(login, "trusted", return_value=True), \
                patch.object(login, "submit", return_value=True) as submit:
            result = self.run_browser(state)
        self.assertEqual(result["status"], "login_rejected")
        submit.assert_called_once()

    def test_nested_accessible_login_control_has_bounded_text_fallback(self):
        page = Mock()
        control = Mock()
        def visible(_page, selectors):
            self.assertIn('[role="button"]:has-text("Log in")', selectors)
            self.assertIn(':text-is("Log in")', selectors)
            return control
        with patch.object(login, "visible", side_effect=visible), \
                patch.object(login, "trusted", return_value=True):
            self.assertTrue(login.submit(page))
        control.click.assert_called_once_with(timeout=10000)

    def test_transfer_requires_fresh_matching_verification(self):
        login.save_json(self.account / "verified-cookies.json", {
            "account": "mine", "verified_at": "2020-01-01T00:00:00+00:00", "cookies": []})
        self.assertEqual(login.transfer("mine", self.account)["status"], "verification_expired")
        with self.assertRaises(login.LocalInputsInvalid):
            login.transfer("other", self.account)

    def test_linked_mode_uses_existing_instagram_session_without_password(self):
        self.args.mode = "linked"
        self.page.get_by_text.return_value.count.return_value = 1
        self.page.get_by_text.return_value.is_visible.return_value = True
        state = {"site": "threads", "form_stage": "page", "fields": {}}
        with patch.object(login, "linked_instagram_cookies", return_value=[{
                "name": "sessionid", "value": "private-token", "domain": ".instagram.com",
                "path": "/", "secure": True}]), \
                patch.object(login, "inspect_page", return_value=state), \
                patch.object(login, "settled", return_value=(self.page, state)), \
                patch.object(login, "trusted", return_value=True), \
                patch.object(login, "verify_identity", return_value=(True, [])), \
                patch.object(login, "submit") as submit, \
                patch.object(login, "CREDS", self.root / "missing-password.json"):
            result = login.browser_run(self.args, self.profile, self.account,
                                       factory=self.factory)
        self.assertEqual(result["status"], "saved")
        self.assertTrue((self.account / "verified-cookies.json").is_file())
        self.assertNotIn("private-token", json.dumps(result))
        submit.assert_not_called()
        self.page.get_by_text.return_value.click.assert_called_once_with(timeout=10000)

    def test_linked_cookie_import_requires_private_complete_session(self):
        directory = login.private_dir(self.root / "ig-sessions")
        settings = directory / "mine.json"
        settings.write_text(json.dumps({"authorization_data": {
            "sessionid": "private-session", "ds_user_id": "12345"},
            "mid": "mid-value", "uuids": {"uuid": "device-value"}}))
        settings.chmod(0o600)
        with patch.object(login, "IG_SESSIONS", directory):
            cookies = login.linked_instagram_cookies("mine")
            self.assertEqual({c["name"] for c in cookies},
                             {"sessionid", "ds_user_id", "mid", "ig_did"})
            settings.chmod(0o644)
            with self.assertRaises(login.LocalInputsInvalid):
                login.linked_instagram_cookies("mine")

    def test_linked_login_replaces_stale_profile_after_verification(self):
        (self.profile / "stale").write_text("old")
        def run(args, selected, account_dir):
            self.assertEqual(args.mode, "linked")
            self.assertNotEqual(selected, self.profile)
            self.assertFalse((selected / "stale").exists())
            (selected / "fresh").write_text("new")
            login.save_json(account_dir / "verified-cookies.json", {"account": "mine"})
            return {"ok": True, "status": "saved", "needs_code": False}
        with patch.object(login, "BASE", self.base), \
                patch.object(login, "browser_run", side_effect=run), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(login.main(["--account", "mine", "--mode", "linked"]), 0)
        self.assertTrue((self.profile / "fresh").is_file())
        self.assertFalse((self.profile / "stale").exists())


if __name__ == "__main__":
    unittest.main()
