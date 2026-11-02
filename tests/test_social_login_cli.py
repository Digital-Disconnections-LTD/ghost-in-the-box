from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from farm.accounts import AccountRegistry
from farm.login import import_accounts, save_login
from farm.session_files import write_session
from scripts import social_login
from test_login import session


def session_for(platform, account):
    if platform == "instagram":
        return {"authorization_data": {"sessionid": "TEST_COOKIE", "ds_user_id": "123"}}
    return session(platform, account)


class SocialLoginCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.registry_path = self.state / "accounts.json"
        env = patch.dict(os.environ, {"SOCIAL_FARM_ACCOUNTS_FILE": str(self.registry_path)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SOCIAL_FARM_LOGIN_BROWSER", None)
        # These tests mock browser_login; a real browser must never start.
        guard = patch("farm.login.LoginWindow.open", side_effect=AssertionError("a real browser would open"))
        guard.start()
        self.addCleanup(guard.stop)
        # import/export default to the import-time state root; keep them in the temp tree.
        rebased = patch.object(social_login, "import_accounts",
                               lambda bundle, replace=False: import_accounts(bundle, state_root=self.state,
                                                                             replace=replace))
        rebased.start()
        self.addCleanup(rebased.stop)

    def run_cli(self, *argv):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            try:
                code = social_login.main(list(argv))
            except SystemExit as exit:
                code = exit.code
        text = out.getvalue().strip()
        return code, (json.loads(text) if text else None), err.getvalue()

    def test_missing_dependency_exits_4_with_install_and_browser_commands(self):
        with patch.object(social_login.sys.stdin, "isatty", return_value=True), \
                patch.object(social_login.importlib.util, "find_spec", return_value=None), \
                patch.object(social_login, "browser_login", side_effect=AssertionError("browser opened")):
            code, result, err = self.run_cli("start", "--platform", "instagram", "--account", "mine")
        self.assertEqual(code, 4)
        self.assertFalse(result["ok"])
        entry = result["accounts"][0]
        self.assertEqual((entry["status"], entry["error"]["code"]), ("dependency_unavailable", "dependency_unavailable"))
        self.assertIn("[login,instagram]", entry["error"]["hint"])
        self.assertIn("playwright install chromium", entry["error"]["hint"])
        self.assertEqual(result["error"], entry["error"])
        self.assertIn("-m pip install", err)

    def test_successful_login_exits_0(self):
        with patch.object(social_login.sys.stdin, "isatty", return_value=True), \
                patch.object(social_login.importlib.util, "find_spec", return_value=object()), \
                patch.object(social_login, "browser_login", return_value=("mine", session())), \
                patch.object(social_login, "save_login", return_value={"session_file": "/private/s.json"}):
            code, result, _ = self.run_cli("start", "--platform", "linkedin", "--account", "mine")
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertNotIn("error", result)

    def test_rejected_login_exits_4_without_leaking_provider_text(self):
        with patch.object(social_login.sys.stdin, "isatty", return_value=True), \
                patch.object(social_login.importlib.util, "find_spec", return_value=object()), \
                patch.object(social_login, "browser_login", side_effect=RuntimeError("SECRET cookie=abc")):
            code, result, _ = self.run_cli("start", "--platform", "linkedin", "--account", "mine")
        self.assertEqual(code, 4)
        self.assertEqual(result["error"]["code"], "login_failed")
        self.assertIn("login start --platform linkedin --account mine", result["error"]["hint"])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_unwritable_session_is_a_runtime_failure(self):
        with patch.object(social_login.sys.stdin, "isatty", return_value=True), \
                patch.object(social_login.importlib.util, "find_spec", return_value=object()), \
                patch.object(social_login, "browser_login", return_value=("mine", session())), \
                patch.object(social_login, "save_login", side_effect=PermissionError(13, "Permission denied")):
            code, result, _ = self.run_cli("start", "--platform", "linkedin", "--account", "mine")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "local_state_unavailable")

    def test_wizard_refuses_when_not_interactive(self):
        with patch.object(social_login.sys.stdin, "isatty", return_value=False):
            code, result, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "not_interactive")

    def test_start_refuses_without_terminal_before_opening_a_browser(self):
        with patch.object(social_login.sys.stdin, "isatty", return_value=False), \
                patch.object(social_login.importlib.util, "find_spec", return_value=object()), \
                patch.object(social_login, "browser_login", side_effect=AssertionError("browser opened")) as launch, \
                patch.object(social_login, "save_login", side_effect=AssertionError("session saved")) as save, \
                patch("builtins.input", side_effect=AssertionError("prompted")):
            code, result, _ = self.run_cli("start", "--platform", "linkedin", "--account", "mine")
        launch.assert_not_called()
        save.assert_not_called()
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "not_interactive")
        self.assertEqual(result["error"]["hint"],
                         "run from an interactive terminal, or use `login import` for a saved bundle")

    def test_start_without_stdin_is_not_interactive(self):
        with patch.object(social_login.sys, "stdin", None), \
                patch.object(social_login, "browser_login", side_effect=AssertionError("browser opened")) as launch:
            code, result, _ = self.run_cli("start", "--platform", "linkedin", "--account", "mine")
        launch.assert_not_called()
        self.assertEqual((code, result["error"]["code"]), (2, "not_interactive"))

    def test_interactive_cancel_exits_130(self):
        with patch.object(social_login.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", side_effect=EOFError), redirect_stdout(io.StringIO()):
            code, result, _ = self.run_cli("start")
        self.assertEqual(code, 130)
        self.assertEqual((result["status"], result["error"]["code"]), ("cancelled", "cancelled"))

    def test_account_needs_exactly_one_platform(self):
        code, _, err = self.run_cli("start", "--account", "mine")
        self.assertEqual(code, 2)
        self.assertIn("exactly one --platform", err)

    def test_export_and_import_round_trip_and_explain_failures(self):
        registry = AccountRegistry(self.registry_path)
        save_login("linkedin", "mine", session(), registry=registry, state_root=self.state)
        bundle = self.root / "bundle.ghostbox-sessions.json"
        code, result, _ = self.run_cli("export", "--account", "linkedin:mine", "--output", str(bundle))
        self.assertEqual((code, result["result"]["accounts"]), (0, ["linkedin:mine"]))

        code, result, _ = self.run_cli("export", "--account", "linkedin:mine", "--output", str(bundle))
        self.assertEqual((code, result["error"]["code"]), (2, "export_exists"))
        self.assertIn("--replace", result["error"]["hint"])
        self.assertEqual(result["status"], "login_or_transfer_failed")

        code, result, _ = self.run_cli("export", "--account", "linkedin", "--output", str(self.root / "x.json"))
        self.assertEqual((code, result["error"]["code"]), (2, "invalid_selector"))

        code, result, _ = self.run_cli("import", str(bundle))
        self.assertEqual((code, result["error"]["code"]), (2, "already_registered"))
        code, result, _ = self.run_cli("import", str(bundle), "--replace")
        self.assertEqual((code, result["result"]["status"]), (0, "imported"))

        code, result, _ = self.run_cli("list")
        self.assertEqual((code, [e["account"] for e in result["result"]]), (0, ["mine"]))

    def test_missing_and_public_bundles_name_the_path(self):
        missing = self.root / "nope.json"
        code, result, _ = self.run_cli("import", str(missing))
        self.assertEqual((code, result["error"]["code"]), (2, "bundle_missing"))
        self.assertIn(str(missing), result["error"]["message"])
        self.assertEqual(result["error_type"], "LoginError")
        if os.name != "nt":
            public = self.root / "public.json"
            write_session(public, {"schema": 1})
            public.chmod(0o644)
            code, result, _ = self.run_cli("import", str(public))
            self.assertEqual((code, result["error"]["code"]), (2, "bundle_unreadable"))
            self.assertIn("chmod 600", result["error"]["hint"])

    def test_unrecognized_bundle_has_a_hint(self):
        bundle = self.root / "other.json"
        write_session(bundle, {"schema": 1, "format": "something-else"})
        code, result, _ = self.run_cli("import", str(bundle))
        self.assertEqual((code, result["error"]["code"]), (2, "bundle_unrecognized"))
        self.assertIn("login export", result["error"]["hint"])

    def test_corrupt_registry_is_a_runtime_failure(self):
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text("[]")
        code, result, _ = self.run_cli("list")
        self.assertEqual((code, result["error"]["code"]), (1, "registry_unreadable"))


    def menu(self, answers, browser=None, detected=None):
        """Run the wizard menu with scripted answers; the browser and session root stay mocked."""
        def signed_in(platform, account=None, **_):
            # The real browser_login reads the handle from the session when none is given.
            account = account or (detected or {}).get(platform, "mine")
            return account, session_for(platform, account)
        launch = Mock(side_effect=browser if browser is not None else signed_in)
        rebased = lambda platform, account, value: save_login(platform, account, value, state_root=self.state)
        with patch.object(social_login.sys.stdin, "isatty", return_value=True), \
                patch.object(social_login.importlib.util, "find_spec", return_value=object()), \
                patch.object(social_login, "browser_login", launch), \
                patch.object(social_login, "save_login", side_effect=rebased), \
                patch("builtins.input", side_effect=answers) as asked:
            code, result, err = self.run_cli("start")
        return code, result, err, launch, asked

    def register(self, platform, account):
        value = ({"authorization_data": {"sessionid": "TEST_COOKIE", "ds_user_id": "123"}}
                 if platform == "instagram" else session(platform, account))
        return save_login(platform, account, value, registry=AccountRegistry(self.registry_path),
                          state_root=self.state)

    @staticmethod
    def day(entry):
        return datetime.fromisoformat(entry["created_at"]).astimezone().date().isoformat()

    def test_menu_lists_every_platform_in_order_when_nothing_is_connected(self):
        code, result, err, launch, _ = self.menu(["d"])
        self.assertIn("Which account do you want to connect?\n"
                      "  1) Instagram      not connected\n"
                      "  2) Threads        not connected (same login as Instagram)\n"
                      "  3) Substack       not connected\n"
                      "  4) LinkedIn       not connected\n"
                      "  d) done\n"
                      "Pick a number (or d): ", err)
        launch.assert_not_called()
        self.assertEqual((code, result), (0, {"ok": True, "accounts": [], "registered": []}))

    def test_menu_shows_existing_handles_with_registration_dates(self):
        instagram = self.register("instagram", "handle")
        first = self.register("linkedin", "alpha")
        second = self.register("linkedin", "beta")
        _, _, err, _, _ = self.menu([""])
        self.assertIn(f"  1) Instagram      logged in as @handle ({self.day(instagram)})\n", err)
        self.assertIn("  2) Threads        not connected (same login as Instagram)\n", err)
        self.assertIn(f"  4) LinkedIn       logged in as @alpha ({self.day(first)}), "
                      f"@beta ({self.day(second)})\n", err)

    def test_menu_shows_handle_alone_when_the_registry_has_no_date(self):
        self.register("threads", "mine")
        data = json.loads(self.registry_path.read_text())
        del data["accounts"]["threads:mine"]["created_at"]
        self.registry_path.write_text(json.dumps(data))
        _, _, err, _, _ = self.menu(["d"])
        self.assertIn("  2) Threads        logged in as @mine\n", err)

    def test_menu_number_selects_that_platform(self):
        code, result, _, launch, _ = self.menu(["4", "d"])
        self.assertEqual(code, 0)
        self.assertEqual(launch.call_args.args, ("linkedin", None))
        self.assertEqual([(r["platform"], r["account"], r["ok"]) for r in result["accounts"]],
                         [("linkedin", "mine", True)])

    def test_menu_accepts_a_list_of_numbers_separated_by_spaces_or_commas(self):
        for answer in ("2 4", "2,4", "2, 4", "4 2 4"):
            with self.subTest(answer=answer):
                if self.registry_path.exists():
                    self.registry_path.unlink()
                order = ["threads", "linkedin"] if answer != "4 2 4" else ["linkedin", "threads"]
                handles = {"threads": "tmine", "linkedin": "lmine"}
                # "n" declines the Instagram offer after Threads connects.
                code, result, _, launch, _ = self.menu([answer, "n", "d"], detected=handles)
                self.assertEqual(code, 0)
                self.assertEqual([c.args for c in launch.call_args_list], [(p, None) for p in order])
                self.assertEqual([(r["platform"], r["account"]) for r in result["accounts"]],
                                 [(p, handles[p]) for p in order])

    def test_menu_accepts_platform_names_in_any_case(self):
        code, result, _, launch, _ = self.menu(["LinkedIn", "threads", "n", "d"],
                                               detected={"linkedin": "mine", "threads": "other"})
        self.assertEqual(code, 0)
        self.assertEqual([c.args for c in launch.call_args_list], [("linkedin", None), ("threads", None)])
        self.assertEqual(len(result["accounts"]), 2)

    def test_menu_substack_choice_still_asks_for_the_publication(self):
        code, result, err, launch, _ = self.menu(["3", "https://example.substack.com", "d"])
        self.assertEqual(code, 0)
        self.assertEqual(launch.call_args.args, ("substack", None))
        self.assertEqual(launch.call_args.kwargs["publication"], "https://example.substack.com")
        self.assertIn("Publication URL: ", err)
        self.assertEqual(result["accounts"][0]["platform"], "substack")

    def test_menu_done_answers_finish_without_a_browser(self):
        for answer in ("", "d", "D", "done", " Done "):
            with self.subTest(answer=answer):
                code, result, err, launch, _ = self.menu([answer])
                launch.assert_not_called()
                self.assertEqual((code, result["accounts"]), (0, []))
                self.assertEqual(err.count("Which account do you want to connect?"), 1)

    def test_menu_rejects_other_answers_with_one_line_and_asks_again(self):
        code, result, err, launch, asked = self.menu(["7", "0", "x", "1 9", "facebook", "1 d", "d"])
        launch.assert_not_called()
        self.assertEqual((code, result["accounts"]), (0, []))
        self.assertEqual(asked.call_count, 7)
        self.assertEqual(err.count("Pick a number (or d): "), 7)
        self.assertEqual(err.count("Which account do you want to connect?"), 1)
        rejections = [line for line in err.splitlines() if "Not a choice:" in line]
        self.assertEqual(len(rejections), 6)
        self.assertIn("Not a choice: 'facebook'. Enter 1-4, a platform name, or d.", err)

    def test_menu_returns_after_a_login_and_shows_the_new_registration(self):
        code, result, err, _, _ = self.menu(["4", "d"])
        self.assertEqual(code, 0)
        entry = AccountRegistry(self.registry_path).get("linkedin", "mine")
        menus = err.split("Which account do you want to connect?")[1:]
        self.assertEqual(len(menus), 2)
        self.assertIn("  4) LinkedIn       not connected\n", menus[0])
        self.assertIn("LinkedIn connected as @mine. Session saved.\n", menus[0])
        self.assertIn(f"  4) LinkedIn       logged in as @mine ({self.day(entry)})\n", menus[1])
        self.assertTrue(Path(entry["session_file"]).is_relative_to(self.state))

    def test_menu_returns_after_a_failed_login_and_keeps_going(self):
        browser = [RuntimeError("SECRET cookie=abc"), ("mine", session("linkedin", "mine"))]
        code, result, err, launch, _ = self.menu(["4", "4", "d"], browser=browser)
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(err.count("Which account do you want to connect?"), 3)
        self.assertIn("linkedin login was not saved. Confirm the handle", err)
        self.assertEqual([r["ok"] for r in result["accounts"]], [False, True])
        self.assertEqual(code, 4)
        self.assertNotIn("SECRET", json.dumps(result))

    def test_menu_missing_dependency_returns_to_the_menu(self):
        with patch.object(social_login.sys.stdin, "isatty", return_value=True), \
                patch.object(social_login.importlib.util, "find_spec", return_value=None), \
                patch.object(social_login, "browser_login", side_effect=AssertionError("browser opened")), \
                patch("builtins.input", side_effect=["1", "d"]):
            code, result, err = self.run_cli("start")
        self.assertEqual(code, 4)
        self.assertEqual(err.count("Which account do you want to connect?"), 2)
        self.assertEqual(result["accounts"][0]["status"], "dependency_unavailable")

    def test_reconnecting_asks_before_opening_a_browser(self):
        self.register("linkedin", "mine")
        prompt = "LinkedIn is already connected as @mine. Log in again? [y/N] "
        for answer in ("", "n", "no", "maybe"):
            with self.subTest(answer=answer):
                code, result, err, launch, _ = self.menu(["4", answer, "d"])
                launch.assert_not_called()
                self.assertIn(prompt, err)
                self.assertIn("Kept the existing LinkedIn login.", err)
                self.assertEqual((code, result["accounts"]), (0, []))
        for answer in ("y", "YES"):
            with self.subTest(answer=answer):
                code, result, err, launch, _ = self.menu(["4", answer, "d"])
                self.assertIn(prompt, err)
                self.assertEqual(launch.call_args.args, ("linkedin", None))
                self.assertEqual((code, result["accounts"][0]["ok"]), (0, True))

    def test_ctrl_c_mid_menu_keeps_earlier_logins(self):
        for interrupted in (["4", "2"], ["4", KeyboardInterrupt], ["4", EOFError]):
            with self.subTest(interrupted=interrupted[1:]):
                if self.registry_path.exists():
                    self.registry_path.unlink()
                browser = [("mine", session("linkedin", "mine")), KeyboardInterrupt()]
                code, result, _, _, _ = self.menu(interrupted, browser=browser)
                self.assertEqual((code, result["status"], result["error"]["code"]), (130, "cancelled", "cancelled"))
                entry = AccountRegistry(self.registry_path).get("linkedin", "mine")
                self.assertIsNotNone(entry)
                self.assertTrue(Path(entry["session_file"]).is_file())
                self.assertIsNone(AccountRegistry(self.registry_path).get("threads", "other"))

    def test_ctrl_c_in_the_browser_during_the_menu_exits_130(self):
        code, result, _, _, _ = self.menu(["4", "2", "d"],
                                          browser=[("mine", session("linkedin", "mine")), KeyboardInterrupt()])
        self.assertEqual(code, 130)
        self.assertEqual(result["error"]["code"], "cancelled")
        self.assertIsNotNone(AccountRegistry(self.registry_path).get("linkedin", "mine"))

    def test_menu_session_prints_one_json_document_with_outcomes_and_registrations(self):
        existing = self.register("instagram", "handle")
        browser = [("mine", session("linkedin", "mine")), RuntimeError("SECRET cookie=abc")]
        code, result, _, launch, _ = self.menu(["4 2", "d"], browser=browser)
        # Instagram is already connected, so Threads continues with its handle.
        self.assertEqual(launch.call_args.args, ("threads", "handle"))
        self.assertTrue(launch.call_args.kwargs["linked"])
        self.assertEqual(code, 4)
        self.assertEqual(set(result), {"ok", "accounts", "registered", "error"})
        self.assertFalse(result["ok"])
        saved, failed = result["accounts"]
        self.assertEqual((saved["platform"], saved["account"], saved["ok"]), ("linkedin", "mine", True))
        self.assertTrue(saved["session_file"].endswith("mine.json"))
        self.assertEqual((failed["platform"], failed["ok"], failed["status"]), ("threads", False, "login_failed"))
        self.assertEqual(result["error"], failed["error"])
        linkedin = AccountRegistry(self.registry_path).get("linkedin", "mine")
        self.assertEqual(result["registered"], [
            {"platform": "instagram", "account": "handle", "account_key": "meta:handle",
             "created_at": existing["created_at"], "updated_at": existing["updated_at"]},
            {"platform": "linkedin", "account": "mine", "account_key": "linkedin:mine",
             "created_at": linkedin["created_at"], "updated_at": linkedin["updated_at"]},
        ])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("TEST_COOKIE", json.dumps(result))

    def test_platform_flag_skips_the_menu(self):
        with patch.object(social_login.sys.stdin, "isatty", return_value=True), \
                patch.object(social_login.importlib.util, "find_spec", return_value=object()), \
                patch.object(social_login, "browser_login", return_value=("mine", session())) as launch, \
                patch.object(social_login, "save_login", return_value={"session_file": "/private/s.json"}), \
                patch("builtins.input", side_effect=AssertionError("prompted")):
            code, result, err = self.run_cli("start", "--platform", "linkedin", "--account", "mine")
        self.assertEqual(code, 0)
        self.assertNotIn("Which account do you want to connect?", err)
        self.assertNotIn("registered", result)
        self.assertEqual(launch.call_args.args, ("linkedin", "mine"))


if __name__ == "__main__":
    unittest.main()
