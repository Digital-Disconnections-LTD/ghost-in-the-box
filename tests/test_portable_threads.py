import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from farm.accounts import AccountRegistry
from farm.adapters.threads import AuthUnavailable, ThreadsAdapter, PublishUncertain
from farm.login import save_login
from farm.threads_browser import BrowserThreadsAdapter
from farm.threads_portable import PortableThreadsAdapter
from farm.threads_transport import CHROMIUM_INSTALL, LOGIN_INSTALL, ThreadsDependencyUnavailable
from test_login import session


class PortableThreadsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.registry = AccountRegistry(self.root / "accounts.json")
        save_login("threads", "mine", session("threads"), registry=self.registry, state_root=self.root)
        self.adapter = PortableThreadsAdapter(registry=self.registry, state_dir=self.root / "portable")
        account_key = self.registry.get("threads", "mine")["account_key"]
        self.job = {"id": "test-job", "platform": "threads", "account": "mine", "account_key": account_key,
                    "body": "exact text", "body_sha256": hashlib.sha256(b"exact text").hexdigest(),
                    "action": "post", "media_path": None, "media_sha256": None}

    def test_selects_portable_transport_without_linked_instagram(self):
        browser = BrowserThreadsAdapter(registry=self.registry, state_dir=self.root / "browser")
        self.assertIsInstance(browser._scrapling("mine"), PortableThreadsAdapter)
        with self.assertRaises(AuthUnavailable):
            ThreadsAdapter(registry=self.registry)._preflight(self.job)

    def test_uses_existing_executor_and_only_injects_scoped_cookies(self):
        playwright = Mock()
        context = playwright.chromium.launch.return_value.new_context.return_value
        context.cookies.return_value = session("threads")["cookies"]
        result = {"status": "published", "provider_id": "123", "permalink": "https://www.threads.com/@mine/post/ABC"}
        with patch("playwright.sync_api.sync_playwright") as factory, \
                patch("scripts.threads_scrapling_bridge.execute", return_value=result) as execute:
            factory.return_value.__enter__.return_value = playwright
            receipt = self.adapter.publish(self.job)
        self.assertEqual(receipt["provider_id"], "123")
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(execute.call_args.args[1], {"mode": "publish", "job": self.job})
        context.add_cookies.assert_called_once_with(session("threads")["cookies"])

    def test_uncertain_send_is_not_retried(self):
        with patch.object(self.adapter, "_call", return_value={"status": "uncertain"}) as call:
            with self.assertRaises(PublishUncertain):
                self.adapter.publish(self.job)
            call.assert_called_once()

    def test_browser_startup_failure_keeps_job_available_for_setup(self):
        with patch("playwright.sync_api.sync_playwright", side_effect=RuntimeError("PRIVATE")):
            with self.assertRaisesRegex(AuthUnavailable, "unavailable") as error:
                self.adapter.publish(self.job)
        self.assertNotIn("PRIVATE", str(error.exception))

    def test_wrong_account_key_never_opens_browser(self):
        self.job["account_key"] = "other"
        from farm.adapters.threads import PreSendFailure
        with patch.object(self.adapter, "_call") as call, self.assertRaises(PreSendFailure):
            self.adapter.publish(self.job)
        call.assert_not_called()

    def test_missing_playwright_names_the_install_command(self):
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            with self.assertRaises(ThreadsDependencyUnavailable) as raised:
                self.adapter.publish(self.job)
        self.assertEqual((raised.exception.dependency, raised.exception.install),
                         ("playwright", LOGIN_INSTALL))

    def test_missing_chromium_build_names_the_browser_install(self):
        playwright = Mock()
        playwright.chromium.launch.side_effect = RuntimeError(
            "BrowserType.launch: Executable doesn't exist at /x/chrome; run playwright install")
        with patch("playwright.sync_api.sync_playwright") as factory:
            factory.return_value.__enter__.return_value = playwright
            with self.assertRaises(ThreadsDependencyUnavailable) as raised:
                self.adapter.publish(self.job)
        self.assertEqual((raised.exception.dependency, raised.exception.install),
                         ("chromium", CHROMIUM_INSTALL))

    def test_wizard_login_with_shared_account_key_reaches_the_executor(self):
        self.assertEqual(self.registry.get("threads", "mine")["account_key"], self.job["account_key"])
        with patch.object(self.adapter, "_call", return_value={
                "status": "published", "provider_id": "5",
                "permalink": "https://www.threads.com/@mine/post/Z"}) as call:
            self.assertEqual(self.adapter.publish(self.job)["provider_id"], "5")
        call.assert_called_once()


    def test_job_queued_under_the_pre_migration_threads_key_still_publishes(self):
        import json
        data = json.loads(self.registry.path.read_text())
        entry = data["accounts"]["threads:mine"]
        entry["account_key"] = "threads:mine"
        entry.pop("legacy_account_keys", None)
        self.registry.path.write_text(json.dumps(data))
        self.job["account_key"] = "threads:mine"
        with patch.object(self.adapter, "_call", return_value={
                "status": "published", "provider_id": "6",
                "permalink": "https://www.threads.com/@mine/post/Y"}) as call:
            self.assertEqual(self.adapter.publish(self.job)["provider_id"], "6")
        call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
