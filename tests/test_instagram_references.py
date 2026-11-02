from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from farm import instagram_references, session_files
from farm.instagram_references import ReferenceError, canonical_post_url, fetch_image


class FakeResponse:
    status_code = 200

    def __init__(self, chunks, headers=None):
        self.chunks = chunks
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def iter_content(self, chunk_size):
        yield from self.chunks


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


class ImageDownloadTests(unittest.TestCase):
    def test_stream_is_private_bounded_and_does_not_follow_redirects(self):
        data = b"\x89PNG\r\n\x1a\n" + b"valid fixture prefix"
        client = FakeSession(FakeResponse([data[:10], data[10:]], {"Content-Length": str(len(data))}))
        with TemporaryDirectory() as root:
            path = Path(root) / "image.part"
            receipt = fetch_image("https://scontent.cdninstagram.com/image.png", path, 100,
                                  session_factory=lambda: client)
            self.assertEqual((receipt["size_bytes"], receipt["suffix"]), (len(data), ".png"))
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertFalse(client.trust_env)
        self.assertFalse(client.calls[0][1]["allow_redirects"])
        self.assertTrue(client.closed)

    def test_download_protection_has_no_posix_only_calls(self):
        self.assertNotIn("os.fchmod", Path(instagram_references.__file__).read_text())
        data = b"\xff\xd8\xff" + b"jpeg fixture"
        client = FakeSession(FakeResponse([data]))
        with TemporaryDirectory() as root, \
                patch.object(session_files, "os", SimpleNamespace(name="nt")), \
                patch.object(session_files, "protect") as protect:
            path = Path(root) / "image.part"
            fetch_image("https://scontent.cdninstagram.com/image.jpg", path, 100, session_factory=lambda: client)
            self.assertEqual(path.read_bytes(), data)
        protect.assert_called_once_with(path)

    def test_oversize_truncated_nonimage_and_timeout_leave_no_partial_file(self):
        responses = [
            FakeResponse([b"\x89PNG\r\n\x1a\n", b"too much data"]),
            FakeResponse([b"\x89PNG\r\n\x1a\n"], {"Content-Length": "9"}),
            FakeResponse([b"not an image"]),
            FakeResponse([], {"Content-Length": "100000000"}),
        ]
        with TemporaryDirectory() as root:
            path = Path(root) / "image.part"
            for response in responses:
                client = FakeSession(response)
                with self.assertRaises(ReferenceError):
                    fetch_image("https://scontent.fbcdn.net/image.jpg", path, 12, session_factory=lambda: client)
                self.assertFalse(path.exists())
                self.assertTrue(client.closed)
            client = FakeSession(FakeResponse([]))
            with self.assertRaisesRegex(ReferenceError, "time budget"):
                fetch_image("https://scontent.fbcdn.net/image.jpg", path, 12,
                            deadline=time.monotonic() - 1, session_factory=lambda: client)
            self.assertEqual(client.calls, [])

    def test_arbitrary_hosts_credentials_and_share_redirects_are_rejected(self):
        with TemporaryDirectory() as root:
            for url in ("http://scontent.fbcdn.net/image.jpg", "https://127.0.0.1/image.jpg",
                        "https://fbcdn.net.attacker.test/image.jpg", "https://user:pass@fbcdn.net/image.jpg"):
                with self.assertRaises(ReferenceError):
                    fetch_image(url, Path(root) / "image.part", 100,
                                session_factory=lambda: self.fail("must reject before HTTP"))
        self.assertEqual(canonical_post_url("https://instagram.com/p/AbC_12/?igsh=tracking"),
                         "https://www.instagram.com/p/AbC_12/")
        for url in ("https://instagram.com/share/p/code/", "https://evil.test/p/code/", "http://instagram.com/p/code/"):
            with self.assertRaises(ReferenceError):
                canonical_post_url(url)


if __name__ == "__main__":
    unittest.main()
