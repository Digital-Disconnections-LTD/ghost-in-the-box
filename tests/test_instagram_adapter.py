from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from farm.accounts import AccountRegistry
from farm.adapters.instagram import InstagramAdapter, PreSendFailure, PublishUncertain
from farm.outbox import Outbox
from scripts.instagram_carousel import create_draft as create_carousel_draft


class FakeClient:
    def __init__(self, *, account="mine", send_error=False):
        self.account = account
        self.send_error = send_error
        self.calls = []

    def load_settings(self, path):
        self.calls.append(("load_settings", str(path)))

    def account_info(self):
        self.calls.append(("account_info",))
        return SimpleNamespace(username=self.account)

    def media_comment(self, media_id, text, replied_to_comment_id):
        self.calls.append(("media_comment", media_id, text, replied_to_comment_id))
        if self.send_error:
            raise TimeoutError("ambiguous provider response")
        return SimpleNamespace(pk="reply-55")

    def photo_upload(self, path, caption, share_to_threads):
        self.calls.append(("photo_upload", str(path), caption, share_to_threads))
        if self.send_error:
            raise TimeoutError("ambiguous provider response")
        return SimpleNamespace(pk="photo-55", code="abc123")


class InstagramAdapterTests(unittest.TestCase):
    def setUp(self):
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
