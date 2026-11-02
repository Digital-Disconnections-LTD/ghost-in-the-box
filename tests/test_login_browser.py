from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from farm import cli, config, login
from farm.accounts import AccountRegistry
from farm.login import LoginError, LoginWindow, browser_login, save_login
from scripts import social_login
from test_login import session

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ENDPOINT = {"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/abc"}
VOYAGER_ME = {"data": {"*miniProfile": "urn:li:fs_miniProfile:abc"},
              "included": [{"entityUrn": "urn:li:fs_miniProfile:abc", "publicIdentifier": "detected-me"}]}


def fake_page(url="https://www.linkedin.com/feed/", evaluate=None):
    page = Mock()
    page.is_closed.return_value = False
    page.url = url
    if evaluate is not None:
        page.evaluate.side_effect = evaluate
    return page


def fake_context(page, cookies=()):
    context = Mock()
    context.pages = [page]
    context.cookies.return_value = list(cookies)
    return context


class BrowserTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        for patcher in (patch.object(config, "STATE_ROOT", self.state),
                        patch.dict(os.environ, {"SOCIAL_FARM_ACCOUNTS_FILE": str(self.state / "accounts.json")})):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(login.BROWSER_ENV, None)

    def browser(self, *, installed=None, context=None, process=None, verify=None):
        """Patch everything below browser_login: no browser, process or network is ever started."""
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.process = process or Mock(**{"poll.return_value": None, "wait.return_value": 0})
        self.context = context or fake_context(fake_page())
        self.playwright = Mock()
        self.playwright.chromium.connect_over_cdp.return_value.contexts = [self.context]
        self.playwright.chromium.launch_persistent_context.return_value = self.context
        factory = stack.enter_context(patch("playwright.sync_api.sync_playwright"))
        factory.return_value.__enter__.return_value = self.playwright
        stack.enter_context(patch.object(login, "installed_browsers",
                                         return_value={"chrome": CHROME} if installed is None else installed))
        self.popen = stack.enter_context(patch.object(login.subprocess, "Popen", return_value=self.process))
        stack.enter_context(patch.object(login, "_free_port", return_value=9333))
        stack.enter_context(patch.object(login, "_devtools_version", return_value=ENDPOINT))
        self.verify = stack.enter_context(patch.object(
            login, "verify_browser", **(verify or {"side_effect": lambda platform, account, *_a, **_k:
                                                   session(platform, account)})))
        self.err = stack.enter_context(redirect_stderr(io.StringIO()))
        return stack


class ProfileAndChoiceTests(BrowserTestCase):
    def test_instagram_and_threads_share_the_meta_profile(self):
        base = self.state / "login-profiles"
        self.assertEqual(config.login_profile_dir("instagram"), base / "meta")
        self.assertEqual(config.login_profile_dir("threads"), base / "meta")
        self.assertEqual(config.login_profile_dir("linkedin"), base / "linkedin")
        self.assertEqual(config.login_profile_dir("substack"), base / "substack")
        for bad in ("../x", "Meta", "", "a/b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.login_profile_dir(bad)

    def test_login_browser_setting_is_listed_with_the_profile_path(self):
        self.assertIn(login.BROWSER_ENV, config.KNOWN_ENVIRONMENT)
        rows = {row["name"]: row for row in config.paths_report()}
        self.assertEqual(rows["login browser profiles"]["path"], str(self.state / "login-profiles"))

    def test_flag_beats_environment_beats_auto(self):
        self.assertEqual(login.login_browser_choice(), ("auto", "default"))
        with patch.dict(os.environ, {login.BROWSER_ENV: "msedge"}):
            self.assertEqual(login.login_browser_choice(), ("msedge", login.BROWSER_ENV))
            self.assertEqual(login.login_browser_choice("brave"), ("brave", "--browser"))
        with patch.dict(os.environ, {login.BROWSER_ENV: "firefox"}):
            with self.assertRaises(LoginError) as error:
                login.login_browser_choice()
        self.assertEqual(error.exception.code, "invalid_browser")

    def test_auto_prefers_chrome_then_edge_brave_chromium_then_bundled(self):
        everything = {"chrome": "/c", "msedge": "/e", "brave": "/b", "chromium": "/m"}
        self.assertEqual(login.choose_browser("auto", everything), ("chrome", "/c"))
        self.assertEqual(login.choose_browser("auto", {"brave": "/b", "msedge": "/e"}), ("msedge", "/e"))
        self.assertEqual(login.choose_browser("auto", {"chromium": "/m"}), ("chromium", "/m"))
        self.assertEqual(login.choose_browser("auto", {}), ("chromium", None))
        self.assertEqual(login.choose_browser("chromium", everything), ("chromium", "/m"))

    def test_an_explicit_browser_that_is_missing_does_not_switch(self):
        with self.assertRaises(LoginError) as error:
            login.choose_browser("chrome", {"msedge": "/e"})
        self.assertEqual(error.exception.code, "dependency_unavailable")
        self.assertIn("--browser auto", error.exception.hint)

    def test_browser_paths_per_os(self):
        self.assertIn(CHROME, login.browser_paths("chrome", system="darwin"))
        windows = login.browser_paths("msedge", system="win32", environ={"PROGRAMFILES": r"C:\Program Files"})
        self.assertEqual(windows, [r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"])
        with patch.object(login.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}" if name == "chromium" else None):
            self.assertEqual(login.browser_paths("chromium", system="linux")[0], "/usr/bin/chromium")


class AttachModeTests(BrowserTestCase):
    def test_installed_chrome_starts_as_a_plain_process_on_the_state_profile(self):
        self.browser()
        prompt = Mock(return_value="")
        account, saved = browser_login("linkedin", "mine", prompt=prompt)
        self.assertEqual((account, saved), ("mine", session()))
        command = self.popen.call_args.args[0]
        profile = self.state / "login-profiles" / "linkedin"
        self.assertEqual(command[0], CHROME)
        self.assertIn(f"--user-data-dir={profile}", command)
        self.assertIn("--remote-debugging-port=9333", command)
        self.assertIn("--no-first-run", command)
        self.assertIn("--no-default-browser-check", command)
        self.assertEqual(command[-1], "https://www.linkedin.com/login")
        for flag in command:
            self.assertFalse(flag.startswith(("--enable-automation", "--incognito", "--disable-blink-features")), flag)
        self.assertEqual(self.popen.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.playwright.chromium.connect_over_cdp.assert_called_once_with(ENDPOINT["webSocketDebuggerUrl"],
                                                                         timeout=30000)
        self.playwright.chromium.launch_persistent_context.assert_not_called()
        self.context.add_init_script.assert_not_called()
        self.context.pages[0].goto.assert_not_called()
        self.assertIn("Opening Google Chrome...\n", self.err.getvalue())
        self.process.terminate.assert_called_once()
        prompt.assert_called_once()

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_profile_directories_are_created_owner_only(self):
        self.browser()
        browser_login("linkedin", "mine", prompt=Mock(return_value=""))
        for path in (self.state / "login-profiles", self.state / "login-profiles" / "linkedin"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700, path)

    def test_chrome_is_stopped_when_verification_fails(self):
        self.browser(verify={"side_effect": RuntimeError("wrong account")})
        with self.assertRaises(RuntimeError):
            browser_login("linkedin", "mine", prompt=Mock(return_value=""))
        self.process.terminate.assert_called_once()

    def test_chrome_is_stopped_on_ctrl_c(self):
        self.browser()
        with self.assertRaises(KeyboardInterrupt):
            browser_login("linkedin", "mine", prompt=Mock(side_effect=KeyboardInterrupt))
        self.process.terminate.assert_called_once()
        self.playwright.chromium.connect_over_cdp.return_value.close.assert_called_once()

    def test_a_browser_that_ignores_sigterm_is_killed(self):
        process = Mock(**{"poll.return_value": None})
        process.wait.side_effect = [subprocess.TimeoutExpired("chrome", 5), 0]
        login._stop_process(process, grace=0)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_a_browser_that_exits_before_devtools_is_ready_is_explained(self):
        self.browser(process=Mock(**{"poll.return_value": 0}))
        with self.assertRaises(LoginError) as error:
            browser_login("linkedin", "mine", prompt=Mock())
        self.assertEqual(error.exception.code, "browser_start_failed")
        self.assertIn("forget-browser", error.exception.hint)
        self.playwright.chromium.connect_over_cdp.assert_not_called()

    def test_wait_for_devtools_rejects_a_foreign_endpoint_and_times_out(self):
        process = Mock(**{"poll.return_value": None})
        with patch.object(login, "_devtools_version", return_value={"webSocketDebuggerUrl": "ws://evil:1/x"}), \
                patch.object(login, "DEVTOOLS_WAIT_SECONDS", 0):
            with self.assertRaises(LoginError) as error:
                login._wait_for_devtools(9333, process, "Google Chrome", self.root)
        self.assertEqual(error.exception.code, "browser_start_failed")


class LaunchFallbackTests(BrowserTestCase):
    def test_without_a_system_browser_bundled_chromium_launches_with_a_persistent_profile(self):
        self.browser(installed={})
        account, _ = browser_login("linkedin", "mine", prompt=Mock(return_value=""))
        self.assertEqual(account, "mine")
        self.popen.assert_not_called()
        call = self.playwright.chromium.launch_persistent_context.call_args
        self.assertEqual(call.args, (str(self.state / "login-profiles" / "linkedin"),))
        self.assertFalse(call.kwargs["headless"])
        self.assertEqual(call.kwargs["ignore_default_args"], ["--enable-automation"])
        self.assertIn("--disable-blink-features=AutomationControlled", call.kwargs["args"])
        self.assertIn("--no-first-run", call.kwargs["args"])
        self.assertIn("--no-default-browser-check", call.kwargs["args"])
        self.assertNotIn("--incognito", call.kwargs["args"])
        self.context.add_init_script.assert_called_once_with(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.context.pages[0].goto.assert_called_once()
        self.assertIn(login.BUNDLED_NOTICE, self.err.getvalue())
        self.context.close.assert_called_once()

    def test_chrome_missing_falls_back_to_chromium_which_succeeds(self):
        self.browser(installed={"chrome": None, "msedge": None})
        self.playwright.chromium.launch_persistent_context.side_effect = None
        browser_login("linkedin", "mine", prompt=Mock(return_value=""))
        self.playwright.chromium.launch_persistent_context.assert_called_once()
        self.assertIn("Opening Playwright Chromium (Google sign-in may be blocked", self.err.getvalue())

    def test_no_browser_at_all_is_a_dependency_problem_with_install_hints(self):
        from playwright.sync_api import Error
        self.browser(installed={})
        self.playwright.chromium.launch_persistent_context.side_effect = Error(
            "Executable doesn't exist at /x/chrome\nplaywright install")
        with self.assertRaises(LoginError) as error:
            browser_login("linkedin", "mine", prompt=Mock())
        self.assertEqual(error.exception.code, "dependency_unavailable")
        self.assertIn("playwright install chromium", error.exception.hint)
        self.assertIn("Google Chrome", error.exception.hint)


class HandleDetectionTests(BrowserTestCase):
    def linkedin_context(self, evaluate):
        return fake_context(fake_page(evaluate=evaluate),
                            cookies=[{"name": "JSESSIONID", "value": '"ajax:1"', "domain": ".www.linkedin.com"}])

    def test_linkedin_handle_comes_from_the_voyager_me_call_in_the_page(self):
        context = self.linkedin_context(lambda *_: VOYAGER_ME)
        self.browser(context=context)
        prompt = Mock(return_value="")
        account, _ = browser_login("linkedin", prompt=prompt)
        self.assertEqual(account, "detected-me")
        self.verify.assert_called_once()
        self.assertEqual(self.verify.call_args.args[:2], ("linkedin", "detected-me"))
        script, request = context.pages[0].evaluate.call_args.args
        self.assertEqual(request["url"], "/voyager/api/me")
        self.assertEqual(request["headers"]["Csrf-Token"], "ajax:1")
        self.assertIn("Signed in as @detected-me", self.err.getvalue())
        self.assertEqual([c.args[0] for c in prompt.call_args_list][1], "Use this handle? [Y/n] ")

    def test_failed_detection_asks_for_the_handle(self):
        self.browser(context=self.linkedin_context(lambda *_: None))
        prompt = Mock(side_effect=["", "typed"])
        account, _ = browser_login("linkedin", prompt=prompt)
        self.assertEqual(account, "typed")
        self.assertIn("Could not read your handle from the session; type it", self.err.getvalue())
        self.assertEqual(prompt.call_args.args[0], "Your linkedin public handle (without @): ")

    def test_declining_the_detected_handle_lets_you_type_another(self):
        self.browser(context=self.linkedin_context(lambda *_: VOYAGER_ME))
        account, _ = browser_login("linkedin", prompt=Mock(side_effect=["", "n", "", "../bad", "other"]))
        self.assertEqual(account, "other")
        self.assertEqual(self.verify.call_args.args[:2], ("linkedin", "other"))

    def test_an_account_skips_detection(self):
        context = self.linkedin_context(AssertionError("detection ran"))
        self.browser(context=context)
        prompt = Mock(return_value="")
        self.assertEqual(browser_login("linkedin", "given", prompt=prompt)[0], "given")
        context.pages[0].evaluate.assert_not_called()
        prompt.assert_called_once()

    def test_instagram_handle_needs_the_matching_user_id(self):
        cookies = [{"name": "ds_user_id", "value": "123", "domain": ".instagram.com"}]
        page = fake_page("https://www.instagram.com/", lambda *_: {"user": {"username": "ig.me", "pk": 123}})
        self.assertEqual(login.detect_handle("instagram", fake_context(page, cookies)), "ig.me")
        self.assertEqual(page.evaluate.call_args.args[1]["url"], "/api/v1/accounts/current_user/?edit=true")
        page = fake_page("https://www.instagram.com/", lambda *_: {"user": {"username": "ig.me", "pk": 999}})
        self.assertIsNone(login.detect_handle("instagram", fake_context(page, cookies)))

    def test_threads_handle_comes_from_the_profile_link(self):
        from scripts.threads_scrapling_login import PROFILE_HANDLES
        page = fake_page("https://www.threads.com/", lambda *_: ["thr.me", "thr.me"])
        self.assertEqual(login.detect_handle("threads", fake_context(page)), "thr.me")
        self.assertEqual(page.evaluate.call_args.args, (PROFILE_HANDLES,))

    def test_substack_handle_comes_from_the_profile_endpoint(self):
        page = fake_page("https://substack.com/home", lambda *_: {"handle": "subme"})
        self.assertEqual(login.detect_handle("substack", fake_context(page)), "subme")
        self.assertEqual(page.evaluate.call_args.args[1]["url"], "/api/v1/user/profile/self")

    def test_detection_on_another_site_reads_nothing(self):
        page = fake_page("https://accounts.google.com/", AssertionError("read"))
        self.assertIsNone(login.detect_handle("linkedin", fake_context(page)))


class LinkedMetaLoginTests(BrowserTestCase):
    def meta_context(self, cookies=()):
        return fake_context(fake_page("https://www.instagram.com/"), cookies)

    def test_threads_continues_from_the_instagram_login_without_prompts(self):
        cookies = [{"name": "sessionid", "value": "TEST_COOKIE_LONG", "domain": ".threads.com"},
                   {"name": "ds_user_id", "value": "123", "domain": ".threads.com"}]
        self.browser(context=self.meta_context(cookies))
        prompt = Mock(side_effect=AssertionError("prompted"))
        account, _ = browser_login("threads", "mine", linked=True, prompt=prompt)
        self.assertEqual(account, "mine")
        self.assertIn("--user-data-dir=" + str(self.state / "login-profiles" / "meta"), self.popen.call_args.args[0])
        self.assertEqual(self.verify.call_args.args[:2], ("threads", "mine"))

    def test_timed_out_continue_falls_back_to_signing_in_in_the_same_window(self):
        window = LoginWindow()
        self.browser(context=self.meta_context())
        prompt = Mock(return_value="")
        with patch.object(login, "LINKED_WAIT_SECONDS", 0), patch.object(login, "_click_continue", return_value=False):
            window.open("instagram")
            window._at = None
            account, _ = browser_login("threads", "mine", linked=True, window=window, prompt=prompt)
        self.addCleanup(window.close)
        self.assertEqual(account, "mine")
        self.assertIn("Threads did not continue automatically. Finish the Threads sign-in in the same browser "
                      "window.", self.err.getvalue())
        prompt.assert_called_once()
        self.context.pages[0].goto.assert_called_once_with("https://www.threads.com/login",
                                                           wait_until="domcontentloaded", timeout=60000)
        self.assertEqual(self.popen.call_count, 1)

    def test_a_window_is_never_reused_across_profiles(self):
        window = LoginWindow()
        self.browser()
        window.open("linkedin")
        self.addCleanup(window.close)
        with self.assertRaises(ValueError):
            window.open("threads")


class WizardBase(BrowserTestCase):
    def run_cli(self, *argv, answers=(), interactive=True):
        with patch.object(social_login.sys.stdin, "isatty", return_value=interactive), \
                patch.object(social_login.importlib.util, "find_spec", return_value=object()), \
                patch("builtins.input", side_effect=list(answers)) as asked, \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            try:
                code = social_login.main(list(argv))
            except SystemExit as exit:
                code = exit.code
        text = out.getvalue().strip()
        return code, (json.loads(text) if text else None), err.getvalue(), asked

    def rebased_save(self):
        return patch.object(social_login, "save_login",
                            side_effect=lambda platform, account, value: save_login(platform, account, value,
                                                                                  state_root=self.state))


class WizardTests(WizardBase):
    def test_start_reports_browser_mode_and_profile_dir_from_a_real_window(self):
        context = fake_context(fake_page(evaluate=lambda *_: VOYAGER_ME),
                               cookies=[{"name": "JSESSIONID", "value": '"ajax:1"', "domain": ".linkedin.com"}])
        self.browser(context=context)
        with self.rebased_save():
            code, result, err, asked = self.run_cli("start", "--platform", "linkedin", answers=["", ""])
        self.assertEqual(code, 0, result)
        entry = result["accounts"][0]
        self.assertEqual((entry["account"], entry["browser"], entry["mode"]), ("detected-me", "chrome", "attach"))
        self.assertEqual(entry["profile_dir"], str(self.state / "login-profiles" / "linkedin"))
        self.assertFalse([c for c in asked.call_args_list if "public handle" in str(c)])
        self.assertIsNotNone(AccountRegistry().get("linkedin", "detected-me"))
        self.process.terminate.assert_called_once()

    def test_failure_results_still_name_the_browser_and_profile(self):
        def fails(platform, account=None, *, window, **_):
            window.browser, window.mode = "chromium", "launch"
            raise LoginError("dependency_unavailable", "Playwright Chromium is not installed", "install it")
        with patch.object(social_login, "browser_login", side_effect=fails):
            code, result, _, _ = self.run_cli("start", "--platform", "linkedin", "--account", "mine")
        self.assertEqual(code, 4)
        entry = result["accounts"][0]
        self.assertEqual((entry["status"], entry["browser"]), ("dependency_unavailable", "chromium"))
        self.assertEqual(entry["profile_dir"], str(self.state / "login-profiles" / "linkedin"))

    def test_browser_flag_beats_the_environment_in_the_wizard(self):
        launch = Mock(return_value=("mine", session()))
        with patch.dict(os.environ, {login.BROWSER_ENV: "msedge"}), \
                patch.object(social_login, "browser_login", launch), \
                patch.object(social_login, "save_login", return_value={"session_file": "s.json"}):
            self.run_cli("start", "--platform", "linkedin", "--account", "mine", "--browser", "brave")
            self.run_cli("start", "--platform", "linkedin", "--account", "mine")
        self.assertEqual(launch.call_args_list[0].kwargs["window"].choice, "brave")
        self.assertEqual(launch.call_args_list[0].kwargs["browser"], "brave")
        self.assertIsNone(launch.call_args_list[1].kwargs["browser"])
        with patch.dict(os.environ, {login.BROWSER_ENV: "msedge"}):
            self.assertEqual(login.login_browser_choice(launch.call_args_list[1].kwargs["browser"])[0], "msedge")

    def test_a_bad_browser_setting_stops_before_any_browser(self):
        with patch.dict(os.environ, {login.BROWSER_ENV: "netscape"}), \
                patch.object(social_login, "browser_login", side_effect=AssertionError("browser opened")):
            code, result, _, _ = self.run_cli("start", "--platform", "linkedin", "--account", "mine")
        self.assertEqual((code, result["error"]["code"]), (2, "invalid_browser"))

    def meta_login(self, answers, first="instagram"):
        def signed_in(platform, account=None, **kwargs):
            account = account or "mine"
            value = ({"authorization_data": {"sessionid": "TEST_COOKIE", "ds_user_id": "123"}}
                     if platform == "instagram" else session(platform, account))
            return account, value
        launch = Mock(side_effect=signed_in)
        with patch.object(social_login, "browser_login", launch), self.rebased_save():
            outcome = self.run_cli("start", "--platform", first, answers=answers)
        return (*outcome, launch)

    def test_instagram_login_offers_threads_and_yes_registers_it_without_a_handle_prompt(self):
        code, result, err, asked, launch = self.meta_login(["y"])
        self.assertEqual(code, 0)
        self.assertIn("Threads uses the same Instagram login. Connect Threads now with it? [Y/n] ", err)
        self.assertEqual(asked.call_count, 1)
        first, second = launch.call_args_list
        self.assertEqual(second.args, ("threads", "mine"))
        self.assertTrue(second.kwargs["linked"])
        self.assertIs(first.kwargs["window"], second.kwargs["window"])
        self.assertEqual([(r["platform"], r["account"], r["ok"]) for r in result["accounts"]],
                         [("instagram", "mine", True), ("threads", "mine", True)])
        self.assertEqual(AccountRegistry().get("threads", "mine")["account_key"], "meta:mine")

    def test_declining_the_threads_offer_keeps_only_instagram(self):
        code, result, err, _, launch = self.meta_login(["n"])
        self.assertEqual(code, 0)
        self.assertIn("Threads uses the same Instagram login. Connect Threads now with it? [Y/n] ", err)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual([r["platform"] for r in result["accounts"]], ["instagram"])
        self.assertIsNone(AccountRegistry().get("threads", "mine"))

    def test_threads_login_offers_instagram(self):
        code, result, err, _, launch = self.meta_login([""], first="threads")
        self.assertEqual(code, 0)
        self.assertIn("Instagram uses the same login as Threads. Connect Instagram now with it? [Y/n] ", err)
        self.assertEqual(launch.call_args_list[1].args, ("instagram", "mine"))

    def test_asking_for_both_meta_platforms_connects_the_second_without_asking(self):
        launch = Mock(side_effect=lambda platform, account=None, **_: (
            account or "mine", {"authorization_data": {"sessionid": "TEST_COOKIE", "ds_user_id": "123"}}
            if platform == "instagram" else session(platform, account or "mine")))
        with patch.object(social_login, "browser_login", launch), self.rebased_save():
            code, result, _, asked = self.run_cli("start", "--platform", "instagram", "--platform", "threads")
        self.assertEqual(code, 0)
        asked.assert_not_called()
        self.assertEqual([c.args for c in launch.call_args_list], [("instagram", None), ("threads", "mine")])

    def test_menu_threads_with_instagram_connected_continues_as_that_handle(self):
        save_login("instagram", "ighandle", {"authorization_data": {"sessionid": "TEST_COOKIE", "ds_user_id": "123"}},
                   state_root=self.state)
        launch = Mock(side_effect=lambda platform, account=None, **_: (account, session(platform, account)))
        with patch.object(social_login, "browser_login", launch), self.rebased_save():
            code, result, err, asked = self.run_cli("start", answers=["2", "d"])
        self.assertEqual(code, 0)
        self.assertEqual(launch.call_args.args, ("threads", "ighandle"))
        self.assertTrue(launch.call_args.kwargs["linked"])
        self.assertIn("Threads uses your Instagram login. Continuing as @ighandle.", err)
        self.assertEqual(asked.call_count, 2)
        self.assertEqual(result["accounts"][0]["account"], "ighandle")


class ForgetBrowserTests(WizardBase):
    def profiles(self):
        base = self.state / "login-profiles"
        for name in ("meta", "linkedin", "substack"):
            (base / name / "Default").mkdir(parents=True)
            (base / name / "Default" / "Cookies").write_text("x")
        return base

    def test_forget_deletes_only_that_platforms_profile(self):
        base = self.profiles()
        code, result, _, _ = self.run_cli("forget-browser", "--platform", "linkedin", "--yes", interactive=False)
        self.assertEqual(code, 0)
        self.assertEqual((result["result"]["deleted"], result["result"]["profile_dir"]),
                         (True, str(base / "linkedin")))
        self.assertFalse((base / "linkedin").exists())
        self.assertTrue((base / "meta" / "Default" / "Cookies").exists())
        self.assertTrue((base / "substack" / "Default" / "Cookies").exists())

    def test_forget_refuses_without_a_platform(self):
        base = self.profiles()
        code, result, _, _ = self.run_cli("forget-browser", "--yes")
        self.assertEqual((code, result["error"]["code"]), (2, "platform_required"))
        self.assertEqual(sorted(p.name for p in base.iterdir()), ["linkedin", "meta", "substack"])

    def test_forget_asks_first_and_keeps_the_profile_on_no(self):
        base = self.profiles()
        code, result, err, _ = self.run_cli("forget-browser", "--platform", "threads", answers=["n"])
        self.assertEqual((code, result["result"]["status"]), (0, "kept"))
        self.assertIn("Delete the Instagram and Threads login browser profile", err)
        self.assertTrue((base / "meta").exists())
        code, result, _, _ = self.run_cli("forget-browser", "--platform", "threads", answers=["y"])
        self.assertEqual(result["result"]["shared_with"], ["instagram", "threads"])
        self.assertFalse((base / "meta").exists())

    def test_forget_without_a_terminal_needs_yes(self):
        base = self.profiles()
        code, result, _, _ = self.run_cli("forget-browser", "--platform", "linkedin", interactive=False)
        self.assertEqual((code, result["error"]["code"]), (2, "not_interactive"))
        self.assertTrue((base / "linkedin").exists())

    def test_forget_refuses_a_symlinked_profile(self):
        target = self.root / "elsewhere"
        target.mkdir()
        (self.state / "login-profiles").mkdir(parents=True)
        (self.state / "login-profiles" / "linkedin").symlink_to(target)
        code, result, _, _ = self.run_cli("forget-browser", "--platform", "linkedin", "--yes")
        self.assertEqual((code, result["error"]["code"]), (2, "profile_not_directory"))
        self.assertTrue(target.exists())


class DoctorBrowserTests(BrowserTestCase):
    def test_doctor_names_the_attached_browser(self):
        with patch.object(cli, "bundled_chromium", return_value=self.root / "chromium-1248"), \
                patch.object(cli, "system_browsers", return_value=[
                    {"browser": "chrome", "label": "Google Chrome", "path": CHROME}]):
            finding, = cli._check_browser(True, {})
        self.assertEqual(finding["severity"], "ok")
        self.assertIn(f"login wizard opens Google Chrome at {CHROME}", finding["message"])

    def test_doctor_explains_the_bundled_fallback_and_a_missing_choice(self):
        with patch.object(cli, "bundled_chromium", return_value=self.root / "chromium-1248"), \
                patch.object(cli, "system_browsers", return_value=[]):
            finding, = cli._check_browser(True, {})
            self.assertEqual(finding["severity"], "info")
            self.assertIn("Playwright Chromium (chromium-1248)", finding["message"])
            self.assertIn("Google sign-in may be blocked", finding["message"])
            with patch.dict(os.environ, {login.BROWSER_ENV: "msedge"}):
                finding, = cli._check_browser(True, {})
        self.assertEqual(finding["severity"], "warn")
        self.assertIn("Microsoft Edge was not found", finding["message"])

    def test_doctor_lists_profiles_read_only(self):
        (self.state / "login-profiles" / "meta").mkdir(parents=True, mode=0o700)
        os.chmod(self.state / "login-profiles" / "meta", 0o700)
        finding, = cli._check_login_profiles()
        self.assertEqual(finding["message"],
                         "login browser profiles: linkedin none, meta (instagram, threads) present, substack none")
        self.assertFalse((self.state / "login-profiles" / "linkedin").exists())


if __name__ == "__main__":
    unittest.main()
