import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from farm.substack_connection import SubstackConnection, private_json


class Jar:
    def __init__(self, value): self.value = value
    def get_dict(self): return dict(self.value)


class FakeApi:
    def __init__(self, **kwargs):
        self.options = kwargs
        self._session = SimpleNamespace(cookies=Jar(json.loads(Path(kwargs["cookies_path"]).read_text())))
        self._session.request = self.request
        self.ok = True

    def request(self, method, url, *args, **kwargs):
        self._session.cookies.value["substack.sid"] = "rotated-session"
        return SimpleNamespace(ok=self.ok, json=lambda: {"canonical_url": "https://test.substack.com/p/hello"})


class SubstackConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cookies = self.root / "cookies.json"
        self.config = self.root / "connection.json"
        private_json(self.cookies, {"substack.sid": "initial-session"})
        private_json(self.config, {"ready": True, "publication_url": "https://test.substack.com",
                                   "cookies_path": str(self.cookies), "account": "example_creator"})
        self.connection = SubstackConnection(self.config, api_factory=FakeApi)

    def test_cookie_rotation_survives_a_new_mcp_client(self):
        client = self.connection.get_api()
        client._session.request("GET", "https://test.substack.com/api/v1/drafts")
        self.assertEqual(json.loads(self.cookies.read_text())["substack.sid"], "rotated-session")
        next_client = self.connection.get_api()
        self.assertEqual(next_client._session.cookies.get_dict()["substack.sid"], "rotated-session")
        self.assertEqual(self.cookies.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "last-published.json").exists())

    def test_successful_publish_records_a_small_receipt(self):
        client = self.connection.get_api()
        client._session.request("POST", "https://test.substack.com/api/v1/drafts/42/publish")
        receipt = self.root / "last-published.json"
        value = json.loads(receipt.read_text())
        self.assertEqual(value["draft_id"], 42)
        self.assertEqual(value["url"], "https://test.substack.com/p/hello")
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("session", receipt.read_text())

    def test_rejected_publish_does_not_count_as_publication(self):
        client = self.connection.get_api(); client.ok = False
        client._session.request("POST", "https://test.substack.com/api/v1/drafts/42/publish")
        self.assertFalse((self.root / "last-published.json").exists())
        self.assertEqual(json.loads(self.cookies.read_text())["substack.sid"], "rotated-session")

    def test_unfinished_setup_does_not_call_the_provider(self):
        private_json(self.config, {"ready": False})
        with self.assertRaisesRegex(RuntimeError, "setup is pending"):
            self.connection.get_api()

    def test_saved_session_with_publication_pending_is_reported_clearly(self):
        private_json(self.config, {"ready": False, "cookies_path": str(self.cookies),
                                   "editorial_phase": "proposal"})
        result = self.connection.status()
        self.assertEqual(result["stage"], "publication_setup")
        self.assertTrue(result["session_saved"])
        self.assertFalse(result["configured"])
        self.assertEqual(result["editorial_phase"], "proposal")


if __name__ == "__main__": unittest.main()
