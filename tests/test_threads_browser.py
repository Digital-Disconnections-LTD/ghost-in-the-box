import hashlib
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from farm.adapters.threads import AuthUnavailable, PublishUncertain, ThreadsReadFailure
from farm.threads_browser import BrowserThreadsAdapter
from farm.threads_reader import ThreadsReader


class Registry:
    def __init__(self, session):
        self.session = session

    def status(self, platform, account):
        if account != "mine":
            return None
        return {"kind": "instagrapi_settings", "account_key": "meta:mine",
                "session_file": str(self.session), "session_exists": True,
                "session_private": True}


class BrowserAdapterTests(unittest.TestCase):
    def setUp(self):
        self.display = patch("farm.threads_browser.shutil.which",
                             side_effect=lambda name: "/usr/bin/xvfb-run" if name == "xvfb-run" else None)
        self.display.start()
        self.addCleanup(self.display.stop)
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = self.root / "ig.json"
        self.session.write_text(json.dumps({"authorization_data": {
            "sessionid": "private-session-value", "ds_user_id": "12345"}}))
        os.chmod(self.session, 0o600)
        self.state = self.root / "browser"
        (self.state / "source/build/browser").mkdir(parents=True)
        (self.state / "source/build/browser/session.js").touch()
        (self.state / "node-v24.22.1-linux-x64/bin").mkdir(parents=True)
        (self.state / "node-v24.22.1-linux-x64/bin/node").touch()
        body = "Exact approved text"
        self.job = {"platform": "threads", "account": "mine", "account_key": "meta:mine",
                    "action": "post", "body": body, "body_sha256": hashlib.sha256(
                        body.encode()).hexdigest(), "media_path": None, "media_sha256": None}

    def tearDown(self):
        self.temp.cleanup()

    def adapter(self, status, **data):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, json.dumps({"status": status, **data}) + "\n", "")

        return BrowserThreadsAdapter(registry=Registry(self.session), runner=runner,
                                     state_dir=self.state), calls

    def test_receipt_is_verified_and_secrets_use_stdin_only(self):
        adapter, calls = self.adapter("published", provider_id="123456",
                                      permalink="https://www.threads.com/@mine/post/AbCd")
        receipt = adapter.publish(self.job)
        self.assertEqual(receipt["provider_id"], "123456")
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertNotIn("private-session-value", " ".join(argv))
        self.assertEqual(json.loads(kwargs["input"])["body"], self.job["body"])
        self.assertEqual(kwargs["env"]["THREADS_PROFILE_DIR"], str(self.state / "profile/mine"))

    def test_macos_does_not_require_an_x11_wrapper(self):
        adapter, calls = self.adapter("published", provider_id="123456",
                                      permalink="https://www.threads.com/@mine/post/AbCd")
        with patch("farm.threads_browser.shutil.which", return_value=None), \
                patch("farm.threads_browser.sys.platform", "darwin"):
            self.assertEqual(adapter.publish(self.job)["provider_id"], "123456")
        self.assertNotIn("-a", calls[0][0])

    def test_auth_unavailable_requires_one_time_action(self):
        adapter, _ = self.adapter("auth_unavailable")
        with self.assertRaises(AuthUnavailable):
            adapter.publish(self.job)

    def test_refreshed_authorization_cookie_takes_precedence(self):
        self.session.write_text(json.dumps({
            "authorization_data": {"sessionid": "fresh-auth-session", "ds_user_id": "12345"},
            "cookies": {"sessionid": "stale-cookie-session", "ds_user_id": "12345"},
        }))
        adapter, _ = self.adapter("verified", account="mine")
        self.assertEqual(adapter._settings("mine"), ("fresh-auth-session", "12345"))
        self.session.write_text(json.dumps({
            "authorization_data": {"sessionid": "fresh-auth-session", "ds_user_id": "12345"},
            "cookies": {"sessionid": "stale-cookie-session", "ds_user_id": "99999"},
        }))
        with self.assertRaises(AuthUnavailable):
            adapter._settings("mine")

    def test_verify_mode_never_requests_a_publish(self):
        adapter, calls = self.adapter("verified", account="mine")
        self.assertEqual(adapter.verify("mine"), {"status": "verified", "account": "mine"})
        self.assertEqual(json.loads(calls[0][1]["input"])["mode"], "verify")

    def test_existing_exact_post_blocks_automatic_resend(self):
        adapter, _ = self.adapter("possible_duplicate")
        with self.assertRaises(PublishUncertain):
            adapter.publish(self.job)

    def test_incomplete_receipt_is_uncertain(self):
        adapter, _ = self.adapter("published", provider_id="123456",
                                  permalink="https://wrong.example/post/AbCd")
        with self.assertRaises(PublishUncertain):
            adapter.publish(self.job)

    def test_browser_reads_cache_own_post_url_for_replies(self):
        calls = []

        def runner(argv, **kwargs):
            payload = json.loads(kwargs["input"])
            calls.append(payload)
            if payload["operation"] == "own":
                result = {"posts": [{"id": "123", "username": "mine",
                                      "permalink": "https://www.threads.com/@mine/post/AbC",
                                      "body": "one"}], "next_cursor": ""}
            else:
                result = {"post_id": "123", "posts": [{"id": "456",
                          "username": "other", "body": "reply",
                          "permalink": "https://www.threads.com/@other/post/DeF"}],
                          "next_cursor": ""}
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "status": "ok", "result": result}) + "\n", "")

        adapter = BrowserThreadsAdapter(registry=Registry(self.session), runner=runner,
                                        state_dir=self.state)
        self.assertEqual(adapter.own_posts("mine", limit=3)["posts"][0]["id"], "123")
        self.assertEqual(adapter.replies("mine", "123", limit=2)["posts"][0]["id"], "456")
        self.assertEqual(calls[1]["target_url"], "https://www.threads.com/@mine/post/AbC")

    def test_reader_switches_to_browser_once_after_mobile_rejection(self):
        class Mobile:
            calls = 0

            def own_posts(self, *_args, **_kwargs):
                self.calls += 1
                raise ThreadsReadFailure("HTTP 403")

            def replies(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("mobile should not be called again")

        class Browser:
            def own_posts(self, *_args, **_kwargs):
                return {"posts": [], "next_cursor": ""}

            def replies(self, *_args, **_kwargs):
                return {"post_id": "123", "posts": [], "next_cursor": ""}

        mobile = Mobile()
        reader = ThreadsReader(mobile=mobile, browser=Browser())
        self.assertEqual(reader.own_posts("mine")["posts"], [])
        self.assertEqual(reader.replies("mine", "123")["posts"], [])
        self.assertEqual(mobile.calls, 1)


if __name__ == "__main__":
    unittest.main()
