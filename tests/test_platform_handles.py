"""Each platform's --account follows that platform's public handle shape everywhere it is checked."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import Mock, patch

from farm import login
from farm.accounts import AccountRegistry, check_handle
from scripts import ig_sync_inbox, linkedin, social_accounts, social_login, social_outbox
from _env import IsolatedStateTestCase

LINKEDIN = "your-handle-123456789"


def invoke(main, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as stop:
            code = stop.code
    return code, out.getvalue(), err.getvalue()


class HandleRuleTests(unittest.TestCase):
    def test_meta_platforms_keep_thirty_letters_digits_dots_and_underscores(self):
        for platform in ("instagram", "threads"):
            with self.subTest(platform=platform):
                self.assertEqual(check_handle(platform, "my.handle_1"), "my.handle_1")
                self.assertEqual(check_handle(platform, "a" * 30), "a" * 30)
                for bad in (LINKEDIN, "a" * 31, "", "with space", "@mine"):
                    with self.assertRaisesRegex(ValueError, "1-30 letters"):
                        check_handle(platform, bad)

    def test_linkedin_allows_hyphens_and_up_to_one_hundred_characters(self):
        for good in (LINKEDIN, "first.last_2", "a" * 100, "x"):
            self.assertEqual(check_handle("linkedin", good), good)
        for bad in ("a" * 101, "-leading", ".leading", "has space", "a/b", "../x", ""):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "LinkedIn handle of 1-100"):
                check_handle("linkedin", bad)

    def test_substack_allows_subdomain_style_handles(self):
        for good in ("my-letter", "my_letter2", "a" * 63):
            self.assertEqual(check_handle("substack", good), good)
        for bad in ("my.letter", "a" * 64, "-leading", ""):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "Substack handle"):
                check_handle("substack", bad)


class LinkedInCommandHandleTests(IsolatedStateTestCase):
    def test_hyphenated_handle_passes_validation_and_reaches_the_session_check(self):
        for argv in (["--account", LINKEDIN, "me"], ["posts", "--account", f"@{LINKEDIN}", "--limit", "3"]):
            with self.subTest(argv=argv):
                code, out, err = invoke(linkedin.main, argv)
                self.assertEqual(code, 4, err)
                result = json.loads(out)
                self.assertEqual(result["error"]["code"], "auth_unavailable")
                self.assertEqual(result["error"]["hint"],
                                 f"ghost-in-the-box login start --platform linkedin --account {LINKEDIN}")

    def test_invalid_linkedin_handle_is_a_usage_error_naming_the_linkedin_rule(self):
        code, out, err = invoke(linkedin.main, ["--account", "a" * 101, "me"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--account must be a LinkedIn handle of 1-100 letters", err)

    def test_instagram_commands_still_refuse_hyphenated_handles(self):
        code, out, err = invoke(ig_sync_inbox.main, ["--db", str(self.isolated_state / "inbox.sqlite3"),
                                                     "--account", LINKEDIN, "peek"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--account must be an Instagram handle of 1-30 letters", err)


class AccountsRegisterHandleTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.registry = self.isolated_state / "accounts.json"
        self.session = self.isolated_state / "session.json"
        self.session.write_text("{}")
        self.session.chmod(0o600)

    def register(self, platform, account):
        code, out, err = invoke(social_accounts.main, [
            "--registry", str(self.registry), "register", "--platform", platform, "--account", account,
            "--kind", "cookie_jar", "--session-file", str(self.session)])
        return code, (json.loads(out) if out.strip() else None), err

    def test_hyphenated_linkedin_handle_registers(self):
        code, result, err = self.register("linkedin", LINKEDIN)
        self.assertEqual(code, 0, err)
        self.assertEqual(result["result"]["account"], LINKEDIN)
        self.assertEqual(result["result"]["account_key"], f"linkedin:{LINKEDIN}")
        self.assertIsNotNone(AccountRegistry(self.registry).get("linkedin", LINKEDIN))

    def test_hyphenated_substack_handle_registers(self):
        code, result, err = self.register("substack", "my-letter")
        self.assertEqual(code, 0, err)
        self.assertEqual(result["result"]["account"], "my-letter")

    def test_same_handle_is_refused_for_instagram_and_threads_without_writing(self):
        for platform in ("instagram", "threads"):
            with self.subTest(platform=platform):
                code, result, _ = self.register(platform, LINKEDIN)
                self.assertEqual(code, 2)
                self.assertEqual(result["error"]["code"], "invalid_account")
                self.assertIn("1-30 letters", result["error"]["message"])
                self.assertTrue(result["error"]["hint"])
        self.assertFalse(self.registry.exists())

    def test_at_sign_is_dropped_before_registering(self):
        code, result, err = self.register("linkedin", f"@{LINKEDIN}")
        self.assertEqual(code, 0, err)
        self.assertEqual(result["result"]["account"], LINKEDIN)


class OutboxDraftHandleTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.db = self.isolated_state / "outbox.sqlite3"
        self.body = self.isolated_state / "body.txt"
        self.body.write_text("Exact text", encoding="utf-8")

    def draft(self, platform, account):
        code, out, err = invoke(social_outbox.main, [
            "--db", str(self.db), "draft", "--platform", platform, "--account", account,
            "--body-file", str(self.body)])
        return code, json.loads(out), err

    def test_hyphenated_linkedin_handle_drafts(self):
        code, result, err = self.draft("linkedin", LINKEDIN)
        self.assertEqual(code, 0, err)
        self.assertEqual(result["result"]["account"], LINKEDIN)
        self.assertEqual(result["result"]["status"], "draft")

    def test_same_handle_is_refused_for_instagram_and_threads_before_the_outbox_opens(self):
        for platform in ("instagram", "threads"):
            with self.subTest(platform=platform):
                code, result, _ = self.draft(platform, LINKEDIN)
                self.assertEqual(code, 2)
                self.assertEqual(result["error"]["code"], "invalid_account")
                self.assertIn("1-30 letters", result["error"]["message"])
        self.assertFalse(self.db.exists())

    def test_too_long_linkedin_handle_is_refused(self):
        code, result, _ = self.draft("linkedin", "a" * 101)
        self.assertEqual(code, 2)
        self.assertIn("LinkedIn handle of 1-100", result["error"]["message"])


class LoginHandleTests(IsolatedStateTestCase):
    def test_typed_handle_follows_the_platform_rule(self):
        answers = iter([LINKEDIN])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(login.choose_handle("linkedin", None, lambda _: next(answers)), LINKEDIN)
        answers = iter([LINKEDIN, "my.handle"])
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(login.choose_handle("instagram", None, lambda _: next(answers)), "my.handle")
        self.assertIn("Instagram handle of 1-30 letters", err.getvalue())

    def test_detected_handle_follows_the_platform_rule(self):
        with patch.object(login, "_detect_raw_handle", return_value=LINKEDIN):
            self.assertEqual(login.detect_handle("linkedin", object()), LINKEDIN)
            self.assertIsNone(login.detect_handle("instagram", object()))

    def test_browser_login_refuses_a_handle_the_platform_does_not_allow_before_opening(self):
        window = Mock()
        with self.assertRaisesRegex(ValueError, "Instagram handle"):
            login.browser_login("instagram", LINKEDIN, window=window)
        window.open.assert_not_called()

    def test_login_start_checks_account_against_the_selected_platform(self):
        code, out, err = invoke(social_login.main,
                                ["start", "--platform", "instagram", "--account", LINKEDIN])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--account must be an Instagram handle of 1-30 letters", err)
        with patch.object(social_login, "_start", return_value=0) as start:
            code, _, _ = invoke(social_login.main, ["start", "--platform", "linkedin", "--account", f"@{LINKEDIN}"])
        self.assertEqual(code, 0)
        self.assertEqual(start.call_args.args[0].account, LINKEDIN)

    def test_saved_session_identity_follows_the_platform_rule(self):
        with self.assertRaisesRegex(ValueError, "Threads handle"):
            login.validate_session("threads", LINKEDIN, {})


if __name__ == "__main__":
    unittest.main()
