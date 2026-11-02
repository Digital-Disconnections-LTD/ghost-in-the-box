from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import zlib
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import os
from unittest.mock import Mock, patch

import requests
from requests.structures import CaseInsensitiveDict
from urllib3.exceptions import NewConnectionError

import farm.adapters as shared
from farm import config
from farm.accounts import AccountRegistry
from farm.adapters.linkedin import (ALT_TEXT_LIMIT, MAX_IMAGE_BYTES, SHARE_QUERY_ENV, SHARE_QUERY_ID,
                                    SINGLE_IMAGE_ONLY, AuthUnavailable, LinkedInAdapter,
                                    LinkedInReadFailure, LinkedInUploadFailure, PreSendFailure,
                                    PublishRejected, PublishUncertain, identity, little_text, read_image)
from farm.login import save_login
from farm.outbox import Outbox
from farm.session_files import read_session
from scripts.social_publish_job import run
from test_login import session


def response(data=None, status=200, headers=None, text=None):
    return SimpleNamespace(status_code=status, json=lambda: data or {}, headers=headers or {},
                           text=json.dumps(data or {}) if text is None else text)


def unreadable(status=200, text="<html>gateway</html>"):
    def fail():
        raise ValueError("not JSON")
    return SimpleNamespace(status_code=status, json=fail, headers={}, text=text)


def png_bytes(width=2, height=2):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def jpeg_bytes():
    try:
        from PIL import Image
    except ImportError:
        return None
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (255, 0, 0)).save(output, "JPEG")
    return output.getvalue()


def pillow_available():
    return importlib.util.find_spec("PIL") is not None


ME = {"miniProfile": {"publicIdentifier": "mine", "entityUrn": "urn:li:fs_miniProfile:ABC123"}}
SHARED = {"data": {"data": {"createContentcreationDashShares": {
    "resourceKey": "urn:li:fsd_share:urn:li:share:123", "*entity": "urn:li:fsd_share:urn:li:share:123"}}},
    "included": []}


class LinkedInTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = AccountRegistry(self.root / "accounts.json")
        self.entry = save_login("linkedin", "mine", session(), registry=self.registry, state_root=self.root)
        self.http = Mock(cookies=requests.cookies.RequestsCookieJar())
        self.http.get.return_value = response(ME)
        self.http.post.return_value = response(SHARED)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(SHARE_QUERY_ENV, None)
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
        self.assertEqual(result["permalink"], "https://www.linkedin.com/feed/update/urn:li:share:123/")
        self.assertEqual(self.http.post.call_count, 1)
        boundary.assert_called_once()
        call = self.http.post.call_args
        self.assertEqual(call.args[0], "https://www.linkedin.com/voyager/api/graphql")
        self.assertEqual(call.kwargs["params"], {"action": "execute", "queryId": SHARE_QUERY_ID})
        self.assertFalse(call.kwargs["allow_redirects"])
        payload = call.kwargs["json"]
        self.assertEqual(payload["queryId"], SHARE_QUERY_ID)
        self.assertTrue(payload["includeWebMetadata"])
        self.assertEqual(payload["variables"]["post"], {
            "allowedCommentersScope": "ALL", "intendedShareLifeCycleState": "PUBLISHED", "origin": "FEED",
            "visibilityDataUnion": {"visibilityType": "ANYONE"},
            "commentary": {"text": "Exact #text [test]", "attributesV2": []}})

    def test_share_headers_carry_csrf_from_jsessionid_and_graphql_client(self):
        self.adapter.publish(self.job)
        headers = CaseInsensitiveDict(self.http.post.call_args.kwargs["headers"])
        self.assertEqual(headers["csrf-token"], "ajax:123")
        self.assertEqual(headers["x-li-graphql-pegasus-client"], "true")
        self.assertEqual(headers["x-restli-protocol-version"], "2.0.0")
        self.assertEqual(headers["x-li-lang"], "en_US")
        self.assertEqual(headers["content-type"], "application/json; charset=UTF-8")
        self.assertEqual(headers["accept"], "application/vnd.linkedin.normalized+json+2.1")
        self.assertEqual(headers["x-li-pem-metadata"], "Voyager - Sharing - CreateShare=sharing-create-content")
        self.assertEqual(headers["origin"], "https://www.linkedin.com")
        self.assertEqual(headers["referer"], "https://www.linkedin.com/feed/")
        self.assertEqual(headers["user-agent"], "TEST_UA")

    def test_before_send_runs_once_after_identity_and_right_before_the_post(self):
        order = Mock()
        self.http.get.side_effect = lambda *a, **k: (order.identity(), response(ME))[1]
        self.http.post.side_effect = lambda *a, **k: (order.post(), response(SHARED))[1]
        self.adapter.publish(self.job, before_send=order.before_send)
        self.assertEqual([c[0] for c in order.mock_calls], ["identity", "before_send", "post"])

    def test_query_id_environment_override_is_used(self):
        digest = "0123456789abcdef0123456789abcdef"
        for value in (digest, "voyagerContentcreationDashShares." + digest):
            with self.subTest(value=value):
                os.environ[SHARE_QUERY_ENV] = value
                self.adapter.publish(self.job)
                call = self.http.post.call_args
                self.assertEqual(call.kwargs["params"]["queryId"], "voyagerContentcreationDashShares." + digest)
                self.assertEqual(call.kwargs["json"]["queryId"], "voyagerContentcreationDashShares." + digest)

    def test_malformed_query_id_override_fails_before_any_request(self):
        boundary = Mock()
        for value in ("voyagerSomethingElse.0123456789abcdef0123456789abcdef", "abc", "x" * 40):
            with self.subTest(value=value):
                os.environ[SHARE_QUERY_ENV] = value
                with self.assertRaisesRegex(PreSendFailure, SHARE_QUERY_ENV) as caught:
                    self.adapter.publish(self.job, before_send=boundary)
                self.assertNotIsInstance(caught.exception, PublishRejected)
        self.http.post.assert_not_called()
        self.http.get.assert_not_called()
        boundary.assert_not_called()

    def test_default_query_id_is_the_documented_setting(self):
        row = next(r for r in config.ENVIRONMENT if r[0] == SHARE_QUERY_ENV)
        self.assertEqual(row[2], SHARE_QUERY_ID)
        self.assertIn(SHARE_QUERY_ENV, config.KNOWN_ENVIRONMENT)

    def test_ok_answer_with_graphql_errors_is_rejected_with_the_message(self):
        self.http.post.return_value = response({"data": {"data": {"createContentcreationDashShares": None},
                                                         "errors": [{"message": "Invalid share"}]}})
        with self.assertRaisesRegex(PublishRejected, "Invalid share") as caught:
            self.adapter.publish(self.job)
        self.assertEqual(caught.exception.status_code, 200)
        self.assertEqual(self.http.post.call_count, 1)
        self.http.post.return_value = response({"errors": [{"message": "You can't post right now"}]})
        with self.assertRaisesRegex(PublishRejected, "You can't post right now"):
            self.adapter.publish(self.job)

    def test_ok_answer_without_share_urn_or_errors_is_uncertain(self):
        for data in ({"data": {"data": {"createContentcreationDashShares": {}}}},
                     {"data": {"data": {"createContentcreationDashShares": {"resourceKey": "urn:li:share:abc"}}}},
                     {"data": {"data": None}}):
            with self.subTest(data=data):
                self.http.post.return_value = response(data)
                with self.assertRaises(PublishUncertain):
                    self.adapter.publish(self.job)
        self.http.post.return_value = unreadable()
        with self.assertRaisesRegex(PublishUncertain, "unreadable"):
            self.adapter.publish(self.job)

    def test_client_error_keeps_status_and_sanitised_body_excerpt(self):
        body = json.dumps({"message": "Invalid share request", "status": 400, "serviceErrorCode": 65600,
                           "echo": 'li_at=TEST_COOKIE; JSESSIONID="ajax:123"',
                           "trace": "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3ODk="}) + "x" * 1000
        self.http.post.return_value = response(status=400, text=body)
        with self.assertRaises(PublishRejected) as caught:
            self.adapter.publish(self.job)
        message = str(caught.exception)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("HTTP 400", message)
        self.assertIn("Invalid share request", message)
        self.assertIn("65600", message)
        for secret in ("TEST_COOKIE", "ajax:123", "QUJDREVG", '"', "{", "="):
            self.assertNotIn(secret, message)
        self.assertLessEqual(len(caught.exception.excerpt), shared.EXCERPT_LIMIT)
        self.assertEqual(shared.safe_reason(caught.exception, limit=shared.REASON_LIMIT + shared.EXCERPT_LIMIT + 2),
                         message)

    def test_refused_session_is_auth_unavailable_after_one_post(self):
        for status, headers in ((401, {}), (403, {}),
                                (302, {"Location": "https://www.linkedin.com/login?session_redirect=x"}),
                                (303, {"Location": "https://www.linkedin.com/checkpoint/challenge/abc"})):
            with self.subTest(status=status):
                self.http.post.reset_mock()
                self.http.post.return_value = response(status=status, headers=headers, text="TEST_COOKIE")
                with self.assertRaises(AuthUnavailable) as caught:
                    self.adapter.publish(self.job)
                self.assertNotIn("TEST_COOKIE", str(caught.exception))
                self.assertEqual(self.http.post.call_count, 1)

    def test_other_redirect_is_uncertain(self):
        self.http.post.return_value = response(status=302, headers={"Location": "https://www.linkedin.com/feed/"})
        with self.assertRaises(PublishUncertain):
            self.adapter.publish(self.job)

    def test_router_stores_the_rejection_excerpt_in_last_error(self):
        db = self.root / "outbox.sqlite3"
        with patch("farm.outbox.AccountRegistry", return_value=self.registry), Outbox(db) as box:
            draft = box.create_draft(platform="linkedin", account="mine", body="exact text")
            job = box.queue(draft["id"])
        self.http.post.return_value = response(status=400, text='{"message":"Invalid share","status":400}')
        with redirect_stdout(io.StringIO()) as output:
            code = run(job["id"], box_factory=lambda: Outbox(db), linkedin_factory=lambda: self.adapter)
        self.assertEqual((code, json.loads(output.getvalue())["status"]), (1, "rejected"))
        with Outbox(db) as box:
            stored = box.get(job["id"])
        self.assertEqual(stored["failure_kind"], "rejected")
        self.assertEqual(stored["last_error"], "LinkedIn rejected the post with HTTP 400: message : Invalid share , status :400")

    def test_router_keeps_the_excerpt_for_an_uncertain_server_error(self):
        db = self.root / "outbox.sqlite3"
        with patch("farm.outbox.AccountRegistry", return_value=self.registry), Outbox(db) as box:
            draft = box.create_draft(platform="linkedin", account="mine", body="exact text")
            job = box.queue(draft["id"])
        self.http.post.return_value = response(status=502, text="<html>Bad gateway</html>")
        with redirect_stdout(io.StringIO()):
            code = run(job["id"], box_factory=lambda: Outbox(db), linkedin_factory=lambda: self.adapter)
        self.assertEqual(code, 3)
        with Outbox(db) as box:
            self.assertEqual(box.get(job["id"])["last_error"],
                             "provider call uncertain: LinkedIn answered the post with HTTP 502: html Bad gateway /html")

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

    def test_safe_excerpt_drops_secrets_markup_and_length(self):
        text = '{"error": "bad", "li_at": "S3CRET_VALUE", "token": "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3ODk="}\n'
        excerpt = shared.safe_excerpt(text + "word " * 200, secrets=["S3CRET_VALUE"])
        self.assertTrue(excerpt.startswith("error : bad , li_at :"))
        self.assertIn("redacted", excerpt)
        for leaked in ("S3CRET_VALUE", "QUJDREVG", '"', "{", "=", "\n"):
            self.assertNotIn(leaked, excerpt)
        self.assertLessEqual(len(excerpt), shared.EXCERPT_LIMIT)
        self.assertEqual(shared.safe_excerpt(None), "")
        self.assertEqual(str(PublishRejected("refused", excerpt="  ")), "refused")
        self.assertIsNone(PublishUncertain("unknown").excerpt)

    def test_little_text_keeps_hashtags_and_escapes_url_fragments(self):
        self.assertEqual(little_text("#code https://example.com/#anchor #1"), "#code https://example.com/\\#anchor \\#1")


REGISTER_URL = "https://www.linkedin.com/voyager/api/voyagerVideoDashMediaUploadMetadata"
GRAPHQL_URL = "https://www.linkedin.com/voyager/api/graphql"
UPLOAD_URL = ("https://www.linkedin.com/dms-uploads/sp/v2/D4E10AQH0example/uploaded-image/0"
              "?ca=vector_feedshare&cn=uploads")
ASSET = "urn:li:digitalmediaAsset:D4E10AQH0example"


def registered(**value):
    return {"data": {"value": {"urn": ASSET, "singleUploadUrl": UPLOAD_URL,
                               "singleUploadHeaders": {"media-type-family": "STILLIMAGE"},
                               "type": "SINGLE", **value}}, "included": []}


class LinkedInImageTests(unittest.TestCase):
    def setUp(self):
        LinkedInTests.setUp(self)
        self.image = self.root / "photo.png"
        self.image.write_bytes(png_bytes())
        self.job.update(media_path=str(self.image), media_sha256=hashlib.sha256(self.image.read_bytes()).hexdigest(),
                        media_alt="A red square")
        self.order = Mock()
        self.register_answer = response(registered())
        self.share_answer = response(SHARED)

        def post(url, *args, **kwargs):
            if url == REGISTER_URL:
                self.order.register()
                return self.register_answer
            self.order.share()
            return self.share_answer

        def put(*args, **kwargs):
            self.order.put()
            return response(status=201, text="")

        self.http.post.side_effect = post
        self.http.put.side_effect = put
        self.boundary = Mock(side_effect=self.order.before_send)

    def calls(self, url):
        return [c for c in self.http.post.call_args_list if c.args[0] == url]

    def assertNothingPosted(self):
        self.assertEqual(self.calls(GRAPHQL_URL), [])
        self.boundary.assert_not_called()

    def test_image_post_registers_uploads_then_shares_with_one_boundary(self):
        result = self.adapter.publish(self.job, before_send=self.boundary)
        self.assertEqual(result["provider_id"], "urn:li:share:123")
        self.assertEqual([c[0] for c in self.order.mock_calls], ["register", "put", "before_send", "share"])
        self.boundary.assert_called_once()

        register = self.calls(REGISTER_URL)[0]
        self.assertEqual(register.kwargs["params"], {"action": "upload"})
        self.assertEqual(register.kwargs["json"], {"mediaUploadType": "IMAGE_SHARING",
                                                   "fileSize": len(png_bytes()), "filename": "photo.png"})
        self.assertFalse(register.kwargs["allow_redirects"])
        headers = CaseInsensitiveDict(register.kwargs["headers"])
        self.assertEqual(headers["csrf-token"], "ajax:123")
        self.assertEqual(headers["accept"], "application/vnd.linkedin.normalized+json+2.1")
        self.assertEqual(headers["x-restli-protocol-version"], "2.0.0")
        self.assertEqual(headers["x-li-lang"], "en_US")
        self.assertEqual(headers["content-type"], "application/json")

        upload = self.http.put.call_args
        self.assertEqual(self.http.put.call_count, 1)
        self.assertEqual(upload.args[0], UPLOAD_URL)
        self.assertEqual(upload.kwargs["data"], png_bytes())
        self.assertFalse(upload.kwargs["allow_redirects"])
        put_headers = CaseInsensitiveDict(upload.kwargs["headers"])
        self.assertEqual(put_headers["media-type-family"], "STILLIMAGE")
        self.assertEqual(put_headers["content-type"], "image/png")
        self.assertEqual(put_headers["user-agent"], "TEST_UA")
        self.assertNotIn("csrf-token", put_headers)

        share = self.calls(GRAPHQL_URL)[0]
        self.assertEqual(share.kwargs["params"], {"action": "execute", "queryId": SHARE_QUERY_ID})
        post = share.kwargs["json"]["variables"]["post"]
        self.assertEqual(post["commentary"], {"text": "Exact #text [test]", "attributesV2": []})
        self.assertEqual(post["media"], {"category": "IMAGE", "mediaUrn": ASSET, "tapTargets": [],
                                         "altText": "A red square"})

    def test_missing_alt_text_is_sent_as_empty(self):
        self.job["media_alt"] = None
        self.adapter.publish(self.job)
        self.assertEqual(self.calls(GRAPHQL_URL)[0].kwargs["json"]["variables"]["post"]["media"]["altText"], "")

    def test_text_only_share_body_is_unchanged(self):
        self.job.update(media_path=None, media_sha256=None, media_alt=None)
        self.adapter.publish(self.job, before_send=self.boundary)
        self.assertEqual([c[0] for c in self.order.mock_calls], ["before_send", "share"])
        self.http.put.assert_not_called()
        self.assertEqual(self.calls(REGISTER_URL), [])
        self.assertEqual(json.dumps(self.calls(GRAPHQL_URL)[0].kwargs["json"]), (
            '{"variables": {"post": {"allowedCommentersScope": "ALL", "intendedShareLifeCycleState": '
            '"PUBLISHED", "origin": "FEED", "visibilityDataUnion": {"visibilityType": "ANYONE"}, '
            '"commentary": {"text": "Exact #text [test]", "attributesV2": []}}}, '
            '"queryId": "voyagerContentcreationDashShares.279996efa5064c01775d5aff003d9377", '
            '"includeWebMetadata": true}'))

    def test_register_refusing_the_session_is_auth_unavailable(self):
        for status, headers in ((401, {}), (403, {}),
                                (302, {"Location": "https://www.linkedin.com/uas/login?x=1"})):
            with self.subTest(status=status):
                self.register_answer = response(status=status, headers=headers, text="TEST_COOKIE")
                with self.assertRaises(AuthUnavailable) as caught:
                    self.adapter.publish(self.job, before_send=self.boundary)
                self.assertNotIn("TEST_COOKIE", str(caught.exception))
                self.http.put.assert_not_called()
                self.assertNothingPosted()

    def test_register_client_error_is_pre_send_with_a_sanitised_excerpt(self):
        self.register_answer = response(status=400, text=json.dumps(
            {"message": "Invalid upload request", "status": 400, "echo": 'JSESSIONID="ajax:123"; li_at=TEST_COOKIE'}))
        with self.assertRaises(LinkedInUploadFailure) as caught:
            self.adapter.publish(self.job, before_send=self.boundary)
        error = caught.exception
        self.assertIsInstance(error, PreSendFailure)
        self.assertNotIsInstance(error, (PublishRejected, AuthUnavailable))
        self.assertEqual(error.status_code, 400)
        self.assertIn("HTTP 400", str(error))
        self.assertIn("Invalid upload request", error.excerpt)
        for secret in ("TEST_COOKIE", "ajax:123", '"', "{"):
            self.assertNotIn(secret, str(error))
        self.http.put.assert_not_called()
        self.assertNothingPosted()

    def test_register_server_error_or_timeout_is_pre_send_not_uncertain(self):
        self.register_answer = response(status=503, text="<html>Service unavailable</html>")
        with self.assertRaisesRegex(LinkedInUploadFailure, "Service unavailable"):
            self.adapter.publish(self.job, before_send=self.boundary)
        self.http.post.side_effect = requests.Timeout("PRIVATE_RESPONSE")
        with self.assertRaises(PreSendFailure) as caught:
            self.adapter.publish(self.job, before_send=self.boundary)
        self.assertNotIn("PRIVATE_RESPONSE", str(caught.exception))
        self.assertNothingPosted()

    def test_upload_server_error_or_timeout_is_pre_send(self):
        self.http.put.side_effect = lambda *a, **k: response(status=500, text="upload broke")
        with self.assertRaisesRegex(LinkedInUploadFailure, "image upload failed with HTTP 500: upload broke"):
            self.adapter.publish(self.job, before_send=self.boundary)
        self.http.put.side_effect = requests.ConnectionError("PRIVATE")
        with self.assertRaises(PreSendFailure) as caught:
            self.adapter.publish(self.job, before_send=self.boundary)
        self.assertNotIsInstance(caught.exception, PublishRejected)
        self.assertNothingPosted()

    def test_upload_refusing_the_session_is_auth_unavailable(self):
        self.http.put.side_effect = lambda *a, **k: response(status=403, text="")
        with self.assertRaises(AuthUnavailable):
            self.adapter.publish(self.job, before_send=self.boundary)
        self.assertNothingPosted()

    def test_multipart_upload_is_refused_before_any_upload(self):
        self.register_answer = response(registered(type="MULTIPART"))
        with self.assertRaisesRegex(PreSendFailure, "multipart upload not supported"):
            self.adapter.publish(self.job, before_send=self.boundary)
        self.http.put.assert_not_called()
        self.assertNothingPosted()

    def test_unexpected_registration_answers_are_refused_before_any_upload(self):
        for answer in (registered(urn="urn:li:share:1"),
                       registered(singleUploadUrl="https://uploads.example.com/x"),
                       registered(singleUploadUrl="http://www.linkedin.com/dms-uploads/x"),
                       registered(singleUploadHeaders={"bad header": "x"}),
                       registered(singleUploadHeaders={"x-ok": "a\r\nInjected: 1"}),
                       {"data": {}}):
            with self.subTest(answer=answer):
                self.register_answer = response(answer)
                with self.assertRaises(LinkedInUploadFailure):
                    self.adapter.publish(self.job, before_send=self.boundary)
        self.register_answer = unreadable(status=200, text="<html>not json</html>")
        with self.assertRaisesRegex(LinkedInUploadFailure, "unreadable"):
            self.adapter.publish(self.job, before_send=self.boundary)
        self.http.put.assert_not_called()
        self.assertNothingPosted()

    def test_a_carousel_manifest_is_refused_as_more_than_one_image(self):
        manifest = self.root / "carousel.json"
        manifest.write_text('{"items": [1, 2]}', encoding="utf-8")
        self.job.update(media_path=str(manifest), media_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
        with self.assertRaisesRegex(PreSendFailure, SINGLE_IMAGE_ONLY):
            self.adapter.publish(self.job, before_send=self.boundary)
        self.http.get.assert_not_called()
        self.http.post.assert_not_called()

    def test_image_changed_after_queue_is_refused_before_any_request(self):
        self.image.write_bytes(png_bytes(3, 3))
        with self.assertRaisesRegex(PreSendFailure, "changed before send"):
            self.adapter.publish(self.job, before_send=self.boundary)
        self.http.post.assert_not_called()

    def test_alt_text_limits_are_checked_before_any_request(self):
        for alt in ("x" * (ALT_TEXT_LIMIT + 1), "two\nlines"):
            with self.subTest(alt=alt[:10]):
                self.job["media_alt"] = alt
                with self.assertRaisesRegex(PreSendFailure, "alt text"):
                    self.adapter.publish(self.job, before_send=self.boundary)
        self.job.update(media_path=None, media_sha256=None, media_alt="orphan")
        with self.assertRaisesRegex(PreSendFailure, "needs an image"):
            self.adapter.publish(self.job)
        self.http.post.assert_not_called()

    def test_mime_comes_from_magic_bytes_not_the_extension(self):
        named_jpg = self.root / "really-png.jpg"
        named_jpg.write_bytes(png_bytes())
        self.assertEqual(read_image(named_jpg)[1], "image/png")
        self.assertEqual(shared.image_mime(b"GIF89a" + b"\x00" * 8), "image/gif")
        self.assertEqual(shared.image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        fake = self.root / "notes.jpg"
        fake.write_text("just some text, not a photo", encoding="utf-8")
        with self.assertRaisesRegex(PreSendFailure, "JPEG, PNG, GIF or WebP"):
            read_image(fake)

    def test_jpeg_is_detected_and_uploaded_with_its_type(self):
        data = jpeg_bytes()
        if data is None:
            self.skipTest("Pillow is not installed")
        photo = self.root / "photo.jpeg"
        photo.write_bytes(data)
        self.assertEqual(read_image(photo), (data, "image/jpeg"))
        self.job.update(media_path=str(photo), media_sha256=hashlib.sha256(data).hexdigest())
        self.adapter.publish(self.job)
        self.assertEqual(CaseInsensitiveDict(self.http.put.call_args.kwargs["headers"])["content-type"], "image/jpeg")

    def test_without_pillow_magic_bytes_decide(self):
        photo = self.root / "photo.jpg"
        photo.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
        with patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
            self.assertEqual(read_image(photo)[1], "image/jpeg")
        if pillow_available():
            with self.assertRaisesRegex(PreSendFailure, "could not be decoded as image/jpeg"):
                read_image(photo)

    def test_pillow_refuses_a_broken_image_behind_a_valid_signature(self):
        if not pillow_available():
            self.skipTest("Pillow is not installed")
        broken = self.root / "broken.png"
        broken.write_bytes(png_bytes()[:40])
        with self.assertRaisesRegex(PreSendFailure, "could not be decoded as image/png"):
            read_image(broken)

    def test_size_cap_is_enforced_before_reading(self):
        large = self.root / "large.png"
        with large.open("wb") as handle:
            handle.write(png_bytes())
            handle.truncate(MAX_IMAGE_BYTES + 1)
        with self.assertRaisesRegex(PreSendFailure, "larger than 8 MiB"):
            read_image(large)
        self.job.update(media_path=str(large), media_sha256=None)
        with self.assertRaises(PreSendFailure):
            self.adapter.publish(self.job)
        self.http.post.assert_not_called()

    def queued_image_job(self, db):
        with patch("farm.outbox.AccountRegistry", return_value=self.registry), Outbox(db) as box:
            draft = box.create_draft(platform="linkedin", account="mine", body="exact text",
                                     media_path=str(self.image), media_alt="A red square")
            return box.queue(draft["id"])

    def test_router_publishes_an_image_job_from_the_outbox(self):
        db = self.root / "outbox.sqlite3"
        job = self.queued_image_job(db)
        with redirect_stdout(io.StringIO()) as output:
            code = run(job["id"], box_factory=lambda: Outbox(db), linkedin_factory=lambda: self.adapter)
        self.assertEqual((code, json.loads(output.getvalue())["status"]), (0, "published"))
        self.assertEqual([c[0] for c in self.order.mock_calls], ["register", "put", "share"])
        media = self.calls(GRAPHQL_URL)[0].kwargs["json"]["variables"]["post"]["media"]
        self.assertEqual(media["altText"], "A red square")

    def test_router_marks_a_failed_upload_as_failed_before_send_with_the_excerpt(self):
        db = self.root / "outbox.sqlite3"
        job = self.queued_image_job(db)
        self.register_answer = response(status=400, text='{"message":"File too large"}')
        with redirect_stdout(io.StringIO()) as output:
            code = run(job["id"], box_factory=lambda: Outbox(db), linkedin_factory=lambda: self.adapter)
        self.assertEqual((code, json.loads(output.getvalue())["status"]), (1, "failed_before_send"))
        with Outbox(db) as box:
            stored = box.get(job["id"])
        self.assertEqual((stored["status"], stored["failure_kind"]), ("failed", "pre_send"))
        self.assertEqual(stored["last_error"],
                         "LinkedIn image registration failed with HTTP 400: message : File too large")
        self.assertEqual(self.calls(GRAPHQL_URL), [])


if __name__ == "__main__":
    unittest.main()
