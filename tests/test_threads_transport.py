import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from farm.accounts import AccountRegistry
from farm.adapters import DependencyUnavailable
from farm.adapters.threads import ThreadsReadFailure
from farm.login import save_login
from farm.threads_auth import ThreadsToken, save_token
from farm.threads_transport import (LOGIN_INSTALL, ThreadsDependencyUnavailable,
                                    module_available, resolve)
from test_login import session as wizard_session


TOKEN = "IGT:2:" + "A" * 40


class TransportResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.registry = AccountRegistry(self.root / "accounts.json")
        self.scrapling = self.root / "scrapling"
        self.browser = self.root / "browser"
        self.auth = self.root / "threads-auth"
        self.installed = {"playwright", "scrapling"}
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("SOCIAL_FARM_NODE", "SOCIAL_FARM_THREADS_MCP_SOURCE", "DISPLAY", "WAYLAND_DISPLAY"):
            os.environ.pop(name, None)

    def resolve(self, account, purpose="agent", which=lambda name: None):
        return resolve(account, registry=self.registry, purpose=purpose,
                       scrapling_state_dir=self.scrapling, browser_state_dir=self.browser,
                       auth_dir=self.auth, python=sys.executable,
                       available=lambda name, python=None: name in self.installed, which=which)

    def linked(self, account="mine"):
        session = self.root / "ig" / f"{account}.json"
        session.parent.mkdir(exist_ok=True)
        session.write_text(json.dumps({"authorization_data": {"ds_user_id": "12345"}}))
        session.chmod(0o600)
        for platform in ("instagram", "threads"):
            self.registry.register(platform=platform, account=account, kind="instagrapi_settings",
                                   session_file=session, account_key=f"meta:{account}")
        return session

    def profile(self, account="mine"):
        directory = self.scrapling / account
        (directory / "profile").mkdir(parents=True, exist_ok=True)
        marker = directory / "verified-cookies.json"
        marker.write_text(json.dumps({"version": 1, "account": account}))
        marker.chmod(0o600)

    def test_missing_registration_points_to_the_login_wizard(self):
        transport = self.resolve("nobody")
        self.assertEqual((transport.name, transport.ready), ("none", False))
        self.assertIn("login start --platform threads --account nobody", transport.next_step)

    def test_wizard_cookie_jar_selects_portable_and_checks_playwright(self):
        save_login("threads", "mine", wizard_session("threads"), registry=self.registry,
                   state_root=self.root / "state")
        for purpose in ("agent", "publish_job", "read"):
            transport = self.resolve("mine", purpose)
            self.assertEqual((transport.name, transport.ready, transport.registration_kind),
                             ("portable", True, "cookie_jar"))
        self.installed = set()
        missing = self.resolve("mine")
        self.assertEqual((missing.ready, missing.dependency, missing.install),
                         (False, "playwright", LOGIN_INSTALL))
        Path(self.registry.get("threads", "mine")["session_file"]).chmod(0o644)
        exposed = self.resolve("mine")
        self.assertFalse(exposed.ready)
        self.assertIsNone(exposed.dependency)
        self.assertIn("login start", exposed.next_step)

    def test_unsupported_kind_is_named(self):
        folder = self.root / "browser-profile"
        folder.mkdir()
        self.registry.register(platform="threads", account="mine", kind="browser_profile",
                               session_file=folder)
        transport = self.resolve("mine")
        self.assertEqual(transport.name, "none")
        self.assertIn("'browser_profile'", transport.reason)

    def test_unlinked_instagrapi_registration_is_explained(self):
        session = self.root / "lonely.json"
        session.write_text("{}")
        session.chmod(0o600)
        self.registry.register(platform="threads", account="mine", kind="instagrapi_settings",
                               session_file=session)
        transport = self.resolve("mine")
        self.assertEqual(transport.name, "none")
        self.assertIn("not linked", transport.reason)
        self.assertIn("connect", transport.next_step)

    def test_agent_uses_verified_scrapling_profile_and_names_missing_pieces(self):
        self.linked()
        missing_profile = self.resolve("mine")
        self.assertEqual((missing_profile.name, missing_profile.ready), ("scrapling", False))
        self.assertIn("threads --account mine connect", missing_profile.next_step)
        self.profile()
        self.assertTrue(self.resolve("mine").ready)
        self.installed = {"playwright"}
        no_module = self.resolve("mine")
        self.assertEqual(no_module.dependency, "scrapling")
        self.assertIn("scrapling install", no_module.install)
        no_python = resolve("mine", registry=self.registry, scrapling_state_dir=self.scrapling,
                            python=self.root / "missing-python",
                            available=lambda name, python=None: True)
        self.assertEqual(no_python.dependency, "python")

    def test_publish_job_prefers_mobile_bearer_and_flags_missing_live_identity_check(self):
        self.linked()
        save_token(ThreadsToken(TOKEN, "12345", "android-0123456789abc", "mine",
                                "2026-10-26T00:00:00+00:00"), self.auth)
        transport = self.resolve("mine", "publish_job")
        self.assertEqual((transport.name, transport.ready), ("mobile", True))
        self.assertTrue(transport.live_identity_check)
        self.assertNotIn("not asked who the bearer belongs to", transport.reason)
        with patch("farm.adapters.threads.ThreadsAdapter.LIVE_IDENTITY_CHECK", False):
            unchecked = self.resolve("mine", "publish_job")
            self.assertFalse(unchecked.live_identity_check)
            self.assertIn("not asked who the bearer belongs to", unchecked.reason)

    def test_publish_job_falls_back_to_browsers_then_explains_every_gap(self):
        self.linked()
        self.profile()
        self.assertEqual(self.resolve("mine", "publish_job").name, "scrapling")
        (self.scrapling / "mine" / "verified-cookies.json").unlink()
        nothing = self.resolve("mine", "publish_job")
        self.assertEqual(nothing.name, "none")
        for part in ("mobile:", "scrapling:", "browser_node:"):
            self.assertIn(part, nothing.reason)
        self.assertEqual(nothing.dependency, "node")
        (self.browser / "source/build/browser").mkdir(parents=True)
        (self.browser / "source/build/browser/session.js").touch()
        node = self.root / "node"
        node.touch()
        os.environ["SOCIAL_FARM_NODE"] = str(node)
        with patch("farm.threads_transport.sys.platform", "linux"):
            headless = self.resolve("mine", "publish_job")
            self.assertEqual(headless.dependency, "xvfb-run")
            ready = self.resolve("mine", "publish_job",
                                 which=lambda name: "/usr/bin/xvfb-run" if name == "xvfb-run" else None)
        self.assertEqual((ready.name, ready.ready), ("browser_node", True))

    def test_reads_use_the_linked_session_before_any_browser(self):
        self.linked()
        transport = self.resolve("mine", "read")
        self.assertEqual((transport.name, transport.ready), ("mobile", True))

    def test_unknown_purpose_is_rejected(self):
        with self.assertRaises(ValueError):
            self.resolve("mine", "everything")


class DependencyProbeTests(unittest.TestCase):
    def test_same_interpreter_uses_find_spec(self):
        self.assertTrue(module_available("json"))
        self.assertFalse(module_available("ghost_in_the_box_missing_module"))
        self.assertTrue(module_available("json", sys.executable))

    def test_missing_interpreter_is_not_available(self):
        self.assertFalse(module_available("json", "/nonexistent/python3"))

    def test_other_interpreter_is_probed_without_importing(self):
        with TemporaryDirectory() as folder:
            other = Path(folder) / "python"
            other.symlink_to(sys.executable)
            self.assertTrue(module_available("json", other))
            self.assertFalse(module_available("ghost_in_the_box_missing_module", other))

    def test_dependency_error_releases_jobs_and_keeps_read_handling(self):
        error = ThreadsDependencyUnavailable("playwright", LOGIN_INSTALL)
        self.assertIsInstance(error, DependencyUnavailable)
        self.assertIsInstance(error, ThreadsReadFailure)
        self.assertEqual(error.install_hint, LOGIN_INSTALL)
        self.assertEqual(str(error), "dependency_unavailable")


if __name__ == "__main__":
    unittest.main()
