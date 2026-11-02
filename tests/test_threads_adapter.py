import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs

import requests
from urllib3.exceptions import NewConnectionError

import farm.adapters as shared
from farm.accounts import AccountRegistry
from farm.adapters.threads import (
    AuthUnavailable, PreSendFailure, PublishRejected, PublishUncertain, ThreadsAdapter,
    ThreadsReadFailure,
)
from farm.outbox import Outbox
from farm.threads_auth import ThreadsToken, save_token
from _env import IsolatedStateTestCase


class Response:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self.data = data or {}

    def json(self):
        return self.data


class FakeHTTP:
    def __init__(self, *, mobile_status=200, post_status=200):
        self.mobile_status = mobile_status
        self.post_status = post_status
        self.posts = []
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append(url)
        if url.endswith("/accounts/current_user/"):
            return Response(data={"status": "ok", "user": {
                "pk": 123, "username": "mine"}})
        if url.endswith("/profile/"):
            return Response(data={"status": "ok", "threads": [{"thread_items": [{"post": {
                "pk": 456, "code": "ABC", "caption": {"text": "Hello"},
                "user": {"username": "mine"}, "taken_at": 1700000000,
            }}]}], "next_max_id": "older"})
        if url.endswith("/replies/"):
            return Response(data={"status": "ok", "reply_threads": [{"thread_items": [{"post": {
                "pk": 789, "code": "DEF", "caption": {"text": "Reply"},
                "user": {"username": "someone"},
            }}]}], "paging_tokens": {"downwards": "more"}})
        if url.endswith("/text_post_app_timeline/"):
            return Response(status_code=self.mobile_status,
                            data={"status": "ok" if self.mobile_status == 200 else "fail"})
        raise AssertionError(url)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response(status_code=self.post_status, data={
            "status": "ok", "media": {"pk": "999", "code": "XYZ",
                                     "user": {"username": "mine"}},
        })


class FakeClient:
    authorization = "Bearer IGT:2:fake"
    android_device_id = "android-aaaaaaaaaaaaaaaa"
    user_id = "123"

    def load_settings(self, path):
        self.path = path

    def account_info(self):
        return SimpleNamespace(username="mine")

    def media_share_to_threads_config(self):
        return {"status": "ok"}

    def media_share_to_threads_destination(self, config):
        return {"destination_id": "123", "username": "mine"}


class ThreadsAdapterTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = self.root / "session.json"
        self.session.write_text(json.dumps({"cookies": {
            "sessionid": "fake", "csrftoken": "fake", "ds_user_id": "123"},
            "mid": "fake", "uuids": {"uuid": "fake",
                                      "android_device_id": FakeClient.android_device_id}}))
        self.session.chmod(0o600)
        self.registry = AccountRegistry(self.root / "accounts.json")
        for platform in ("instagram", "threads"):
            self.registry.register(platform=platform, account="mine",
                                   account_key="meta:mine", kind="instagrapi_settings",
                                   session_file=self.session)
        self.box = Outbox(self.root / "outbox.sqlite3")
        self.auth_dir = self.root / "threads-auth"
        save_token(ThreadsToken("IGT:2:" + "a" * 30, "123",
                                FakeClient.android_device_id, "mine", "2026-10-26T00:00:00Z"),
                   self.auth_dir)

    def tearDown(self):
        self.box.close()
        self.temp.cleanup()

    def adapter(self, http):
        return ThreadsAdapter(self.registry, http=http, client_factory=FakeClient,
                              auth_dir=self.auth_dir)

    def job(self, *, action="post", body="Hello Threads"):
        kwargs = {"platform": "threads", "account": "mine", "account_key": "meta:mine",
                  "body": body, "action": action}
        if action == "reply":
            kwargs.update(media_id="456", target_id="456")
        draft = self.box.create_draft(**kwargs)
        self.box.queue(draft["id"])
        return self.box.claim_due(platform="threads")

    def test_bounded_reads(self):
        adapter = self.adapter(FakeHTTP())
        self.assertEqual(adapter.me("mine"), {"id": "123", "username": "mine"})
        own = adapter.own_posts("mine", limit=2)
        self.assertEqual(own["posts"][0]["permalink"],
                         "https://www.threads.com/@mine/post/ABC")
        self.assertEqual(own["next_cursor"], "older")
        replies = adapter.replies("mine", "456", limit=2)
        self.assertEqual(replies["posts"][0]["id"], "789")
        self.assertEqual(replies["next_cursor"], "more")
        with self.assertRaises(ValueError):
            adapter.own_posts("mine", limit=26)

    def test_nested_thread_items_are_all_kept_before_page_cursor(self):
        class MultiPostHTTP(FakeHTTP):
            def get(self, url, **kwargs):
                if url.endswith("/profile/"):
                    return Response(data={"status":"ok","threads":[{"thread_items":[
                        {"post":{"pk":1,"code":"ONE","caption":{"text":"First"},
                                 "user":{"username":"mine"}}},
                        {"post":{"pk":2,"code":"TWO","caption":{"text":"Second"},
                                 "user":{"username":"mine"}}},
                    ]}],"next_max_id":"after-both"})
                return super().get(url,**kwargs)
        page = self.adapter(MultiPostHTTP()).own_posts("mine",limit=1)
        self.assertEqual([post["id"] for post in page["posts"]],["1","2"])
        self.assertEqual(page["next_cursor"],"after-both")

    def test_fresh_instagrapi_settings_use_authorization_data(self):
        self.session.write_text(json.dumps({
            "authorization_data": {"sessionid": "fresh-session", "ds_user_id": "123"},
            "cookies": {"rur": "unrelated"}, "mid": "device-mid",
            "uuids": {"uuid": "device-uuid",
                      "android_device_id": FakeClient.android_device_id},
        }))
        http = FakeHTTP()
        self.assertEqual(self.adapter(http).me("mine")["username"], "mine")

    def test_refreshed_authorization_cookie_precedes_stale_cookie_jar(self):
        self.session.write_text(json.dumps({
            "authorization_data": {"sessionid": "fresh-auth-session", "ds_user_id": "123"},
            "cookies": {"sessionid": "stale-cookie-session", "ds_user_id": "123"},
            "mid": "device-mid", "uuids": {"uuid": "device-uuid",
                                          "android_device_id": FakeClient.android_device_id},
        }))
        settings = self.adapter(FakeHTTP())._settings("mine")
        self.assertEqual(settings["_threads_cookies"]["sessionid"], "fresh-auth-session")

    def test_mobile_preflight_failure_never_starts_write(self):
        http = FakeHTTP(mobile_status=403)
        with self.assertRaisesRegex(AuthUnavailable, "preflight returned HTTP 403"):
            self.adapter(http).publish(self.job())
        self.assertEqual(http.posts, [])
        self.assertEqual(len(http.gets), 1)
        self.assertTrue(http.gets[0].endswith("/text_post_app_timeline/"))

    def test_publish_asks_threads_who_the_bearer_belongs_to_before_writing(self):
        self.assertIs(ThreadsAdapter.LIVE_IDENTITY_CHECK, True)
        identity = "https://i.instagram.com/api/v1/accounts/current_user/"

        class OrderedHTTP(FakeHTTP):
            def post(self, url, **kwargs):
                if identity not in self.gets:
                    raise AssertionError("wrote before the identity check")
                return super().post(url, **kwargs)
        http = OrderedHTTP()
        self.adapter(http).publish(self.job())
        self.assertEqual(len(http.posts), 1)

    def test_bearer_for_another_account_is_a_pre_send_failure(self):
        class OtherAccountHTTP(FakeHTTP):
            def get(self, url, **kwargs):
                if url.startswith("https://i.instagram.com/") and url.endswith("/accounts/current_user/"):
                    self.gets.append(url)
                    return Response(data={"status": "ok", "user": {"pk": 999, "username": "someone_else"}})
                return super().get(url, **kwargs)
        http = OtherAccountHTTP()
        with self.assertRaisesRegex(PreSendFailure, "different account") as caught:
            self.adapter(http).publish(self.job())
        self.assertNotIsInstance(caught.exception, AuthUnavailable)
        self.assertEqual(http.posts, [])

    def test_rejected_or_throttled_identity_check_keeps_the_job_queued(self):
        for status, reason in ((401, "bearer_rejected"), (429, "HTTP 429")):
            with self.subTest(status=status):
                class RefusingHTTP(FakeHTTP):
                    def get(self, url, **kwargs):
                        if url.startswith("https://i.instagram.com/") and url.endswith("/accounts/current_user/"):
                            self.gets.append(url)
                            return Response(status_code=status)
                        return super().get(url, **kwargs)
                http = RefusingHTTP()
                job = self.job()
                with self.assertRaisesRegex(AuthUnavailable, reason):
                    self.adapter(http).publish(job)
                self.assertEqual(http.posts, [])
                self.box.release(job["id"], job["lease_token"])

    def test_login_required_payload_never_starts_write(self):
        class LoggedOutHTTP(FakeHTTP):
            def get(self, url, **kwargs):
                if url.endswith("/text_post_app_timeline/"):
                    return Response(data={"status": "fail", "message": "login_required",
                                          "logout_reason": 8})
                return super().get(url, **kwargs)
        http = LoggedOutHTTP()
        with self.assertRaises(AuthUnavailable):
            self.adapter(http).publish(self.job())
        self.assertEqual(http.posts, [])

    def test_before_send_can_stop_public_write_after_preflight(self):
        http = FakeHTTP()
        def stop():
            raise PreSendFailure("quiet hours began")
        with self.assertRaisesRegex(PreSendFailure, "quiet hours began"):
            self.adapter(http).publish(self.job(), before_send=stop)
        self.assertEqual(http.posts, [])

    def test_post_and_reply_use_single_configure_call(self):
        for action in ("post", "reply"):
            with self.subTest(action=action):
                http = FakeHTTP()
                job = self.job(action=action)
                result = self.adapter(http).publish(job)
                self.box.finish(job["id"], job["lease_token"], outcome="published",
                                provider_id=result["provider_id"])
                self.assertEqual(result["provider_id"], "999")
                self.assertEqual(len(http.posts), 1)
                wire = http.posts[0][1]["data"]["signed_body"]
                payload = json.loads(wire.removeprefix("SIGNATURE."))
                encoded = requests.Request("POST", "https://i.instagram.com/",
                                           data={"signed_body": wire}).prepare().body
                self.assertEqual(parse_qs(encoded)["signed_body"], [wire])
                self.assertEqual(payload["_uid"], "123")
                self.assertEqual(payload["caption"], "Hello Threads")
                self.assertEqual(payload["text_post_app_info"].get("reply_id"),
                                 "456" if action == "reply" else None)

    def test_failed_configure_is_uncertain(self):
        http = FakeHTTP(post_status=503)
        with self.assertRaises(PublishUncertain):
            self.adapter(http).publish(self.job())
        self.assertEqual(len(http.posts), 1)

    def test_exceptions_are_the_shared_adapter_contract(self):
        self.assertIs(PreSendFailure, shared.PreSendFailure)
        self.assertIs(AuthUnavailable, shared.AuthUnavailable)
        self.assertIs(PublishUncertain, shared.PublishUncertain)
        self.assertTrue(issubclass(ThreadsReadFailure, shared.ReadFailure))
        self.assertIsInstance(self.adapter(FakeHTTP()), shared.PublishingAdapter)
        self.assertIsInstance(self.adapter(FakeHTTP()), shared.IdentityAdapter)

    def test_definite_client_error_from_configure_is_rejected(self):
        for status in (400, 403, 422, 429):
            with self.subTest(status=status):
                with self.assertRaises(PublishRejected) as caught:
                    self.adapter(FakeHTTP(post_status=status)).publish(self.job())
                self.assertEqual(caught.exception.status_code, status)
                self.box.db.execute("UPDATE jobs SET status='failed' WHERE status='in_flight'")

    def test_timeout_conflict_and_unreadable_answers_stay_uncertain(self):
        class Unreadable(FakeHTTP):
            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                response = Response()
                response.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
                return response

        class Slow(FakeHTTP):
            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                raise requests.ReadTimeout("PRIVATE")

        for http in (FakeHTTP(post_status=408), FakeHTTP(post_status=409), Unreadable(), Slow()):
            with self.subTest(http=type(http).__name__, status=http.post_status):
                with self.assertRaises(PublishUncertain) as caught:
                    self.adapter(http).publish(self.job())
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.box.db.execute("UPDATE jobs SET status='failed' WHERE status='in_flight'")

    def test_job_queued_under_a_legacy_threads_key_still_publishes(self):
        self.registry.register(platform="threads", account="mine", account_key="threads:mine",
                               kind="instagrapi_settings", session_file=self.session)
        job = self.job()
        self.box.db.execute("UPDATE jobs SET account_key='threads:mine' WHERE id=?", (job["id"],))
        job["account_key"] = "threads:mine"
        http = FakeHTTP()
        self.assertEqual(self.adapter(http).publish(job)["provider_id"], "999")
        job["account_key"] = "threads:someone-else"
        with self.assertRaisesRegex(PreSendFailure, "account key"):
            self.adapter(FakeHTTP()).publish(job)

    def test_connection_never_opened_is_pre_send(self):
        class Offline(FakeHTTP):
            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                raise requests.ConnectionError(NewConnectionError(None, "connection refused"))

        with self.assertRaisesRegex(PreSendFailure, "never left") as caught:
            self.adapter(Offline()).publish(self.job())
        self.assertNotIsInstance(caught.exception, PublishRejected)


if __name__ == "__main__":
    unittest.main()
