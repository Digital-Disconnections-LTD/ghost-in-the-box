from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import NewConnectionError

import farm.adapters as shared
from farm.accounts import AccountRegistry
from farm.adapters.linkedin import (AuthUnavailable, LinkedInAdapter, LinkedInReadFailure,
                                    PreSendFailure, PublishRejected, PublishUncertain, identity,
                                    little_text)
from farm.login import save_login
from farm.outbox import Outbox
from farm.session_files import read_session
from scripts.social_publish_job import run
from test_login import session


def response(data=None, status=200, headers=None):
    return SimpleNamespace(status_code=status, json=lambda: data or {}, headers=headers or {})


ME = {"miniProfile": {"publicIdentifier": "mine", "entityUrn": "urn:li:fs_miniProfile:ABC123"}}


class LinkedInTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = AccountRegistry(self.root / "accounts.json")
        self.entry = save_login("linkedin", "mine", session(), registry=self.registry, state_root=self.root)
        self.http = Mock(cookies=requests.cookies.RequestsCookieJar())
        self.http.get.return_value = response(ME)
        self.http.post.return_value = response(status=201, headers={"x-restli-id": "urn:li:share:123"})
        self.adapter = LinkedInAdapter(self.registry, self.http)
        self.job = {"platform": "linkedin", "account": "mine", "account_key": "linkedin:mine", "action": "post",
                    "body": "Exact #text [test]", "body_sha256": hashlib.sha256(b"Exact #text [test]").hexdigest(),
                    "media_path": None, "media_sha256": None}

    def test_identity_headers_csrf_quotes_and_no_redirects(self):
        self.assertEqual(self.adapter.me("mine")["author_urn"], "urn:li:person:ABC123")
        headers = self.http.get.call_args.kwargs["headers"]
        self.assertEqual(headers["Csrf-Token"], "ajax:123")
        self.assertFalse(self.http.get.call_args.kwargs["allow_redirects"])
        self.adapter.me("mine")
        self.assertEqual(headers["X-Li-Page-Instance"], self.http.get.call_args.kwargs["headers"]["X-Li-Page-Instance"])

    def test_normalized_identity_requires_referenced_profile(self):
        data = {"data": {"*miniProfile": "urn:li:fs_miniProfile:ABC123"}, "included": [ME["miniProfile"]]}
        self.assertEqual(identity(data)["username"], "mine")
        data["data"]["*miniProfile"] = "wrong"
        with self.assertRaises(LinkedInReadFailure):
            identity(data)

    def test_bounded_reads_use_reference_routes_and_encoded_ids(self):
        self.adapter.conversations("mine", limit=5, start=10)
        self.assertEqual(self.http.get.call_args.kwargs["params"], {"keyVersion": "LEGACY_INBOX", "count": 5, "start": 10})
        with self.assertRaises(ValueError):
            self.adapter.own_posts("mine", limit=26)
        self.adapter.messages("mine", "a/b?c")
        self.assertIn("a%2Fb%3Fc/events", self.http.get.call_args.args[0])

    def test_text_publish_exactly_once_with_identity_and_receipt(self):
        boundary = Mock()
        result = self.adapter.publish(self.job, before_send=boundary)
        self.assertEqual(result["provider_id"], "urn:li:share:123")
        self.assertEqual(self.http.post.call_count, 1)
        boundary.assert_called_once()
        payload = self.http.post.call_args.kwargs["json"]
        self.assertEqual(payload["author"], "urn:li:person:ABC123")
        self.assertEqual(payload["commentary"], "Exact #text \\[test\\]")

    def test_wrong_identity_and_changed_job_never_write(self):
        self.http.get.return_value = response({"miniProfile": {"publicIdentifier": "other", "entityUrn": "urn:li:fs_miniProfile:OTHER"}})
        with self.assertRaises(PreSendFailure):
            self.adapter.publish(self.job)
        self.http.post.assert_not_called()
        self.job["body"] = "changed"
        with self.assertRaises(PreSendFailure):
            self.adapter.publish(self.job)
        self.http.post.assert_not_called()

    def test_auth_redirect_does_not_follow_or_send(self):
        self.http.get.return_value = response(status=302, headers={"Location": "https://example.invalid"})
        with self.assertRaises(AuthUnavailable):
            self.adapter.publish(self.job)
        self.http.post.assert_not_called()

    def test_missing_receipt_or_timeout_is_uncertain_without_retry(self):
        self.http.post.return_value = response(status=201)
        with self.assertRaises(PublishUncertain):
            self.adapter.publish(self.job)
        self.assertEqual(self.http.post.call_count, 1)
        self.http.post.side_effect = requests.Timeout("PRIVATE_RESPONSE")
        with self.assertRaisesRegex(PublishUncertain, "reconciliation") as error:
            self.adapter.publish(self.job)
        self.assertNotIn("PRIVATE_RESPONSE", str(error.exception))

    def test_rotating_cookies_persist_between_processes(self):
        def rotate(*args, **kwargs):
            self.http.cookies.set("li_at", "ROTATED_TEST_COOKIE", domain=".linkedin.com", path="/")
            return response(ME)
        self.http.get.side_effect = rotate
        self.adapter.me("mine")
        saved = read_session(Path(self.entry["session_file"]))
        self.assertEqual(next(c["value"] for c in saved["cookies"] if c["name"] == "li_at"), "ROTATED_TEST_COOKIE")

    def test_confirmed_receipt_survives_cookie_save_failure(self):
        with patch("farm.adapters.linkedin.write_session", side_effect=OSError("disk full")):
            receipt = self.adapter.publish(self.job)
        self.assertEqual(receipt["provider_id"], "urn:li:share:123")
        self.assertFalse(receipt["session_saved"])

    def test_outbox_uncertain_send_cannot_be_reclaimed(self):
        db = self.root / "outbox.sqlite3"
        with patch("farm.outbox.AccountRegistry", return_value=self.registry), Outbox(db) as box:
            draft = box.create_draft(platform="linkedin", account="mine", body="exact text")
            job = box.queue(draft["id"])
        self.http.post.side_effect = requests.Timeout()
        with redirect_stdout(io.StringIO()) as output:
            code = run(job["id"], box_factory=lambda: Outbox(db), linkedin_factory=lambda: self.adapter)
        self.assertEqual((code, json.loads(output.getvalue())["status"]), (3, "uncertain"))
        with Outbox(db) as box:
            self.assertEqual(box.get(job["id"])["status"], "uncertain")
            self.assertIsNone(box.claim_due(platform="linkedin"))

    def test_exceptions_are_the_shared_adapter_contract(self):
        self.assertIs(PreSendFailure, shared.PreSendFailure)
        self.assertIs(AuthUnavailable, shared.AuthUnavailable)
        self.assertIs(PublishUncertain, shared.PublishUncertain)
        self.assertTrue(issubclass(LinkedInReadFailure, shared.ReadFailure))
        self.assertIsInstance(self.adapter, shared.IdentityAdapter)

    def test_definite_client_error_is_rejected_and_server_error_uncertain(self):
        self.http.post.return_value = response(status=422)
        with self.assertRaises(PublishRejected) as caught:
            self.adapter.publish(self.job)
        self.assertEqual(caught.exception.status_code, 422)
        for status in (500, 408, 409):
            with self.subTest(status=status):
                self.http.post.return_value = response(status=status)
                with self.assertRaises(PublishUncertain):
                    self.adapter.publish(self.job)

    def test_unregistered_account_keeps_the_job_queued_for_login(self):
        self.job["account"] = "nobody"
        with self.assertRaisesRegex(AuthUnavailable, "login is required"):
            self.adapter.publish(self.job)
        self.job["account"], self.job["account_key"] = "mine", "linkedin:other"
        with self.assertRaises(PreSendFailure) as caught:
            self.adapter.publish(self.job)
        self.assertNotIsInstance(caught.exception, AuthUnavailable)
        self.http.post.assert_not_called()

    def test_connection_never_opened_is_pre_send_not_uncertain(self):
        self.http.post.side_effect = requests.ConnectionError(NewConnectionError(None, "refused"))
        with self.assertRaisesRegex(PreSendFailure, "never left") as caught:
            self.adapter.publish(self.job)
        self.assertNotIsInstance(caught.exception, PublishRejected)

    def test_little_text_keeps_hashtags_and_escapes_url_fragments(self):
        self.assertEqual(little_text("#code https://example.com/#anchor #1"), "#code https://example.com/\\#anchor \\#1")


if __name__ == "__main__":
    unittest.main()
