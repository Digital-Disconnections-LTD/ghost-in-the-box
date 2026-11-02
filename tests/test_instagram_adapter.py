from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import importlib.util

import farm.adapters as shared
from farm.accounts import AccountRegistry
from farm.config import install_command
from farm.adapters.instagram import (
    AuthUnavailable, DependencyUnavailable, InstagramAdapter, PreSendFailure, PublishRejected,
    PublishUncertain,
)
from farm.outbox import Outbox
from scripts.instagram_carousel import create_draft as create_carousel_draft
from _env import IsolatedStateTestCase


class ProviderError(Exception):
    pass


class FakeClient:
    """Mimics the instagrapi 2.18.20 members the adapter relies on."""

    session_retry_total = 3
    private_requests_count = 0

    def __init__(self, *, account="mine", send_error=None):
        self.account = account
        # True or "timeout": no answer. An int: that HTTP status.
        self.send_error = send_error
        self.calls = []
        self.writes = []
        self.retry_total_at_configure = None
        self.last_response = None

    def load_settings(self, path):
        self.calls.append(("load_settings", str(path)))

    def account_info(self):
        self.calls.append(("account_info",))
        return SimpleNamespace(username=self.account)

    def _configure_private_session_retry(self):
        self.retry_total_at_configure = self.session_retry_total

    def _send_private_request(self, endpoint, data=None, **kwargs):
        self.last_response = None
        self.writes.append(endpoint)
        if self.send_error in (True, "timeout"):
            raise TimeoutError("ambiguous provider response")
        if isinstance(self.send_error, int):
            self.last_response = SimpleNamespace(status_code=self.send_error)
            raise ProviderError(f"HTTP {self.send_error}")
        return {"status": "ok"}

    def private_request(self, endpoint, data=None, **kwargs):
        return self._send_private_request(endpoint, data=data, **kwargs)

    def media_comment(self, media_id, text, replied_to_comment_id):
        self.calls.append(("media_comment", media_id, text, replied_to_comment_id))
        self.private_request(f"media/{media_id}/comment/", data={"comment_text": text})
        return SimpleNamespace(pk="reply-55")

    def photo_upload(self, path, caption, share_to_threads):
        self.calls.append(("photo_upload", str(path), caption, share_to_threads))
        self.private_request("media/configure/", data={"caption": caption})
        return SimpleNamespace(pk="photo-55", code="abc123")


class InstagramAdapterTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = self.root / "session.json"
        self.session.write_text("{}")
        self.session.chmod(0o600)
        self.registry = AccountRegistry(self.root / "accounts.json")
        self.registry.register(platform="instagram", account="mine",
                               kind="instagrapi_settings", session_file=self.session)
        self.box = Outbox(self.root / "outbox.sqlite3")

    def tearDown(self):
        self.box.close()
        self.temp.cleanup()

    def _claim(self, **kwargs):
        draft = self.box.create_draft(platform="instagram", account="mine", **kwargs)
        self.box.queue(draft["id"])
        return self.box.claim_due(platform="instagram")

    def test_reply_uses_exact_parent_comment_and_media(self):
        job = self._claim(
            body="A reply", action="reply", media_id="media-7",
            target_id="1234", target_url="https://www.instagram.com/p/abc123/?comment_id=1234",
        )
        client = FakeClient()
        result = InstagramAdapter(self.registry, client_factory=lambda: client).publish(job)
        self.assertEqual(client.calls[-1], ("media_comment", "media-7", "A reply", 1234))
        self.assertEqual(result["provider_id"], "reply-55")
        self.assertEqual(result["permalink"], "https://www.instagram.com/p/abc123/?comment_id=reply-55")

    def test_photo_can_request_threads_crosspost(self):
        image = self.root / "image.jpg"
        image.write_bytes(b"JPG fixture")
        job = self._claim(body="A post", media_path=str(image), crosspost_threads=True)
        client = FakeClient()
        result = InstagramAdapter(self.registry, client_factory=lambda: client).publish(job)
        self.assertEqual(client.calls[-1][-1], True)
        self.assertEqual(result["permalink"], "https://www.instagram.com/p/abc123/")

    def test_changed_media_is_rejected_before_network_send(self):
        image = self.root / "image.jpg"
        image.write_bytes(b"first")
        job = self._claim(body="A post", media_path=str(image))
        image.write_bytes(b"second")
        client = FakeClient()
        with self.assertRaisesRegex(PreSendFailure, "changed"):
            InstagramAdapter(self.registry, client_factory=lambda: client).publish(job)
        self.assertEqual(client.calls, [])

    def test_upload_uses_verified_snapshot_if_source_changes_during_account_check(self):
        image = self.root / "image.jpg"
        image.write_bytes(b"queued bytes")
        job = self._claim(body="A post", media_path=str(image))

        class ChangingClient(FakeClient):
            def account_info(self):
                image.write_bytes(b"changed after account validation began")
                return super().account_info()

            def photo_upload(self, path, caption, share_to_threads):
                self.uploaded_bytes = Path(path).read_bytes()
                return super().photo_upload(path, caption, share_to_threads)

        client = ChangingClient()
        # The original path changes before the network call. If validation
        # rejects it, no call is made; if it sends, only queued bytes may go.
        try:
            InstagramAdapter(self.registry, client_factory=lambda: client).publish(job)
        except PreSendFailure:
            self.assertFalse(any(call[0] == "photo_upload" for call in client.calls))
        else:
            self.assertEqual(client.uploaded_bytes, b"queued bytes")

    def test_provider_timeout_is_uncertain_and_wrong_account_is_pre_send(self):
        job = self._claim(
            body="A reply", action="reply", media_id="media-7",
            target_id="1234", target_url="https://www.instagram.com/p/abc123/",
        )
        with self.assertRaisesRegex(PreSendFailure, "different account"):
            InstagramAdapter(self.registry, client_factory=lambda: FakeClient(account="other")).publish(job)
        with self.assertRaises(PublishUncertain):
            InstagramAdapter(self.registry, client_factory=lambda: FakeClient(send_error=True)).publish(job)

    def test_ambiguous_comment_timeout_has_one_write_attempt(self):
        job = self._claim(body="A reply", action="reply", media_id="media-7",
                          target_id="1234", target_url="https://www.instagram.com/p/abc123/")

        class RetryLikeClient(FakeClient):
            authorization = "test-auth"
            private_requests_count = 0
            sends = 0
            def _send_private_request(self, endpoint, **kwargs):
                self.sends += 1
                raise TimeoutError("ambiguous write")
            def private_request(self, endpoint, data=None, **kwargs):
                try:
                    return self._send_private_request(endpoint, data=data, **kwargs)
                except TimeoutError:
                    return self._send_private_request(endpoint, data=data, **kwargs)
            def media_comment(self, media_id, text, replied_to_comment_id):
                return self.private_request('media/comment/', data={'text':text})

        client = RetryLikeClient()
        with self.assertRaises(PublishUncertain):
            InstagramAdapter(self.registry, client_factory=lambda: client).publish(job)
        self.assertEqual(client.sends,1)

    def test_old_client_does_not_overwrite_a_newer_shared_login(self):
        job = self._claim(body="A reply", action="reply", media_id="media-7",
                          target_id="1234", target_url="https://www.instagram.com/p/abc123/")
        class SavingClient(FakeClient):
            def dump_settings(self, path):
                Path(path).write_text('{"session":"older"}')
                return True
        adapter = InstagramAdapter(self.registry, client_factory=SavingClient)
        adapter._prepare(job)
        self.session.write_text('{"session":"newer"}')
        adapter.save_session()
        self.assertEqual(self.session.read_text(), '{"session":"newer"}')

    def test_rejected_saved_login_recovers_before_single_reply_send(self):
        job = self._claim(body="A reply", action="reply", media_id="media-7",
                          target_id="1234", target_url="https://www.instagram.com/p/abc123/")
        class LoginRequired(Exception):
            pass
        class ExpiredClient(FakeClient):
            def account_info(self):
                raise LoginRequired("private provider body")
        fresh = FakeClient()
        adapter = InstagramAdapter(self.registry, client_factory=ExpiredClient)
        with patch("farm.adapters.instagram.read_with_session", return_value=fresh) as recover:
            receipt = adapter.publish(job)
        self.assertEqual(receipt["provider_id"], "reply-55")
        recover.assert_called_once()
        self.assertEqual([call[0] for call in fresh.calls], ["media_comment"])

    def test_carousel_snapshots_ordered_slides_and_sends_one_album(self):
        first = self.root / "one.jpg"
        second = self.root / "two.png"
        first.write_bytes(b"first image")
        second.write_bytes(b"second image")
        draft = create_carousel_draft(account="mine", body="Two slides",
            images=[first, second], root=self.root / "carousels",
            box_factory=lambda: Outbox(self.root / "outbox.sqlite3"))
        self.box.queue(draft["id"])
        job = self.box.claim_due(platform="instagram")
        class AlbumClient(FakeClient):
            def album_upload(self, paths, caption, share_to_threads):
                self.slides = [Path(path).read_bytes() for path in paths]
                self.calls.append(("album_upload", caption, share_to_threads))
                return SimpleNamespace(pk="album-55", code="Album55")
        client = AlbumClient()
        result = InstagramAdapter(self.registry, client_factory=lambda: client).publish(job)
        self.assertEqual(client.slides, [b"first image", b"second image"])
        self.assertEqual(client.calls[-1], ("album_upload", "Two slides", False))
        self.assertEqual(result["permalink"], "https://www.instagram.com/p/Album55/")

    def _reply_job(self):
        return self._claim(body="A reply", action="reply", media_id="media-7",
                           target_id="1234", target_url="https://www.instagram.com/p/abc123/")

    def test_exceptions_are_the_shared_adapter_contract(self):
        self.assertIs(PreSendFailure, shared.PreSendFailure)
        self.assertIs(AuthUnavailable, shared.AuthUnavailable)
        self.assertIs(PublishUncertain, shared.PublishUncertain)
        self.assertTrue(issubclass(DependencyUnavailable, PreSendFailure))
        self.assertTrue(issubclass(PublishRejected, PreSendFailure))
        self.assertIsInstance(InstagramAdapter(self.registry), shared.PublishingAdapter)

    def test_missing_retry_guard_members_refuse_before_any_write(self):
        job = self._reply_job()

        members = {key: value for key, value in vars(FakeClient).items()
                   if key not in ("_send_private_request", "__dict__", "__weakref__")}
        client = type("OldClient", (), members)()
        with self.assertRaisesRegex(PreSendFailure, "_send_private_request"):
            InstagramAdapter(self.registry, client_factory=lambda: client).publish(job)
        self.assertFalse(any(call[0] == "media_comment" for call in client.calls))

    def test_guard_disables_session_retries_before_sending(self):
        job = self._reply_job()
        client = FakeClient()
        InstagramAdapter(self.registry, client_factory=lambda: client).publish(job)
        self.assertEqual(client.retry_total_at_configure, 0)
        self.assertEqual(client.writes, ["media/media-7/comment/"])

    def test_definite_client_error_on_the_write_is_rejected_not_uncertain(self):
        job = self._reply_job()
        with self.assertRaises(PublishRejected) as caught:
            InstagramAdapter(self.registry, client_factory=lambda: FakeClient(send_error=400)).publish(job)
        self.assertEqual(caught.exception.status_code, 400)

    def test_server_error_conflict_and_timeout_stay_uncertain(self):
        for error in (500, 408, 409, "timeout"):
            with self.subTest(error=error):
                job = self._reply_job()
                with self.assertRaises(PublishUncertain):
                    InstagramAdapter(self.registry,
                                     client_factory=lambda: FakeClient(send_error=error)).publish(job)
                self.box.finish(job["id"], job["lease_token"], outcome="uncertain")
                self.box.resolve_uncertain(job["id"], outcome="failed", reason="test cleanup")

    def test_local_error_before_any_write_is_pre_send(self):
        image = self.root / "image.jpg"
        image.write_bytes(b"not really a jpeg")
        job = self._claim(body="A post", media_path=str(image))

        class LocalFailure(FakeClient):
            def photo_upload(self, path, caption, share_to_threads):
                raise ValueError("Invalid file format")

        client = LocalFailure()
        with self.assertRaisesRegex(PreSendFailure, "before any public write") as caught:
            InstagramAdapter(self.registry, client_factory=lambda: client).publish(job)
        self.assertNotIsInstance(caught.exception, PublishRejected)
        self.assertEqual(client.writes, [])

    def test_auth_refusal_before_any_write_keeps_the_job_queued(self):
        job = self._reply_job()

        class LoginRequired(Exception):
            pass

        class RefusedRead(FakeClient):
            def media_comment(self, media_id, text, replied_to_comment_id):
                raise LoginRequired("private body")

        with self.assertRaises(AuthUnavailable):
            InstagramAdapter(self.registry, client_factory=RefusedRead).publish(job)

    def test_unreadable_answer_after_a_completed_write_is_uncertain(self):
        job = self._reply_job()

        class NoReceipt(FakeClient):
            def media_comment(self, media_id, text, replied_to_comment_id):
                self.private_request("media/comment/", data={"comment_text": text})
                raise KeyError("media")

        with self.assertRaises(PublishUncertain):
            InstagramAdapter(self.registry, client_factory=NoReceipt).publish(job)

    def test_before_send_runs_after_checks_and_can_stop_the_write(self):
        job = self._reply_job()
        client = FakeClient()

        def stop():
            self.assertIn(("account_info",), client.calls)
            raise PreSendFailure("stopped at the boundary")

        with self.assertRaisesRegex(PreSendFailure, "boundary"):
            InstagramAdapter(self.registry, client_factory=lambda: client).publish(job, before_send=stop)
        self.assertEqual(client.writes, [])

    def test_missing_instagrapi_is_a_dependency_problem_with_install_hint(self):
        job = self._reply_job()

        def missing():
            raise ImportError("instagrapi")

        with self.assertRaises(DependencyUnavailable) as caught:
            InstagramAdapter(self.registry, client_factory=missing).publish(job)
        self.assertEqual(caught.exception.install_hint, install_command("instagram"))
        self.assertIn("[instagram]", caught.exception.install_hint)

    @unittest.skipUnless(importlib.util.find_spec("instagrapi"), "instagrapi not installed")
    def test_pinned_instagrapi_client_has_the_single_write_guard_members(self):
        from instagrapi import Client
        client = Client()
        for name in ("_configure_private_session_retry", "_send_private_request",
                     "private_request", "session_retry_total"):
            self.assertTrue(hasattr(client, name), name)
        adapter = InstagramAdapter(self.registry)
        adapter.client = client
        adapter._single_write_attempt()
        self.assertEqual(client.session_retry_total, 0)
        self.assertEqual(client.private_request.__name__, "once")

    def test_carousel_changed_slide_fails_before_any_provider_send(self):
        first = self.root / "one.jpg"
        second = self.root / "two.jpg"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        draft = create_carousel_draft(account="mine", body="Two slides",
            images=[first, second], root=self.root / "carousels",
            box_factory=lambda: Outbox(self.root / "outbox.sqlite3"))
        self.box.queue(draft["id"])
        job = self.box.claim_due(platform="instagram")
        manifest = Path(job["media_path"])
        (manifest.parent / "slide-02.jpg").write_bytes(b"changed")
        client = FakeClient()
        with self.assertRaisesRegex(PreSendFailure, "carousel snapshot invalid"):
            InstagramAdapter(self.registry, client_factory=lambda: client).publish(job)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
