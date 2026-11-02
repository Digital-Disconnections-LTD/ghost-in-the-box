import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from farm.accounts import AccountRegistry
from farm.instagram_mcp import TOOLS, server_version
from farm.linkedin_mcp import TOOLS as LINKEDIN_TOOLS
from scripts.mcp_stdio_bridge import read_message as read_bridge_message, write_message as write_bridge_message

ROOT = Path(__file__).resolve().parents[1]
# The real `ghost-in-the-box instagram-mcp` entry point, with instagrapi made unimportable
# so the smoke proves the server needs no Instagram extra and cannot reach Instagram.
SERVER = ("import sys; sys.modules['instagrapi'] = None; sys.path.insert(0, sys.argv.pop(1)); "
          "from farm.cli import main; raise SystemExit(main(sys.argv[1:]))")
ACCOUNT = "smoke_dummy"


def requests_for(version):
    return [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": version, "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "instagram_status", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "instagram_own_posts", "arguments": {}}},
    ]


class InstagramMCPSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        base = Path(self.temp.name)
        self.state, self.home = base / "state", base / "home"
        self.home.mkdir()
        session = base / "dummy-session.json"
        session.write_text('{"dummy": "not a real session"}')
        session.chmod(0o600)
        AccountRegistry(self.state / "accounts.json").register(
            platform="instagram", account=ACCOUNT, kind="instagrapi_settings", session_file=session)
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("SOCIAL_FARM_")}
        self.environment.update(SOCIAL_FARM_STATE_DIR=str(self.state), HOME=str(self.home),
                                USERPROFILE=str(self.home), PYTHONIOENCODING="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_server(self, command, payload):
        process = subprocess.run(command, input=payload, capture_output=True, timeout=60,
                                 env=self.environment, cwd=self.temp.name)
        self.assertEqual(process.returncode, 0, process.stderr.decode(errors="replace"))
        self.assertEqual(process.stderr, b"")
        self.assertFalse((self.home / ".batbox").exists(), "server wrote outside the throwaway state")
        return process.stdout

    def check_replies(self, replies, version):
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4])
        initialized = replies[0]["result"]
        self.assertEqual(initialized["protocolVersion"], version)
        self.assertEqual(initialized["serverInfo"]["name"], "ghost-in-the-box-instagram")
        self.assertEqual(initialized["serverInfo"]["version"], server_version())
        self.assertIn("tools", initialized["capabilities"])
        tools = replies[1]["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], [tool["name"] for tool in TOOLS])
        self.assertTrue(all(tool["title"] and tool["inputSchema"]["type"] == "object" for tool in tools))
        status = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertFalse(replies[2]["result"]["isError"])
        self.assertEqual((status["account"], status["session_ready"]), (ACCOUNT, True))
        provider = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertEqual(provider["error"]["code"], "dependency_unavailable")

    def test_newline_json_with_the_only_registered_account(self):
        payload = "".join(json.dumps(message) + "\n" for message in requests_for("2025-06-18")).encode()
        stdout = self.run_server([sys.executable, "-c", SERVER, str(ROOT), "instagram-mcp"], payload)
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 4)
        self.check_replies([json.loads(line) for line in lines], "2025-06-18")

    def test_content_length_frames_through_the_stdio_bridge(self):
        incoming = io.BytesIO()
        for message in requests_for("2024-11-05"):
            write_bridge_message(incoming, json.dumps(message).encode(), "framed")
        stdout = self.run_server([sys.executable, str(ROOT / "scripts" / "mcp_stdio_bridge.py"), "--",
                                  sys.executable, "-c", SERVER, str(ROOT), "instagram-mcp", "--account", ACCOUNT],
                                 incoming.getvalue())
        stream, replies = io.BytesIO(stdout), []
        while (message := read_bridge_message(stream)) is not None:
            payload, mode = message
            self.assertEqual(mode, "framed")
            replies.append(json.loads(payload))
        self.check_replies(replies, "2024-11-05")


# The real `ghost-in-the-box linkedin-mcp` entry point with every HTTP send refused,
# so the smoke proves the server starts, answers and fails cleanly with no network.
LINKEDIN_SERVER = ("import sys, requests.adapters; "
                   "requests.adapters.HTTPAdapter.send = lambda *a, **k: (_ for _ in ()).throw("
                   "requests.exceptions.ConnectionError('network disabled for the smoke test')); "
                   "sys.path.insert(0, sys.argv.pop(1)); "
                   "from farm.cli import main; raise SystemExit(main(sys.argv[1:]))")
LINKEDIN_ACCOUNT = "smoke-dummy"


def linkedin_requests_for(version):
    return [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": version, "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "linkedin_status", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "linkedin_me", "arguments": {}}},
    ]


class LinkedInMCPSmokeTests(unittest.TestCase):
    run_server = InstagramMCPSmokeTests.run_server

    def setUp(self):
        self.temp = TemporaryDirectory()
        base = Path(self.temp.name)
        self.state, self.home = base / "state", base / "home"
        self.home.mkdir()
        session = base / "dummy-linkedin-session.json"
        session.write_text(json.dumps({
            "schema": 1, "platform": "linkedin", "account": LINKEDIN_ACCOUNT, "user_agent": "smoke",
            "cookies": [{"name": "li_at", "value": "dummy", "domain": ".linkedin.com", "path": "/"},
                        {"name": "JSESSIONID", "value": "\"ajax:dummy\"", "domain": ".linkedin.com",
                         "path": "/"}]}))
        session.chmod(0o600)
        AccountRegistry(self.state / "accounts.json").register(
            platform="linkedin", account=LINKEDIN_ACCOUNT, kind="cookie_jar", session_file=session)
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("SOCIAL_FARM_")}
        self.environment.update(SOCIAL_FARM_STATE_DIR=str(self.state), HOME=str(self.home),
                                USERPROFILE=str(self.home), PYTHONIOENCODING="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def check_replies(self, replies, version):
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4])
        initialized = replies[0]["result"]
        self.assertEqual(initialized["protocolVersion"], version)
        self.assertEqual(initialized["serverInfo"]["name"], "ghost-in-the-box-linkedin")
        self.assertEqual(initialized["serverInfo"]["version"], server_version())
        tools = replies[1]["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], [tool["name"] for tool in LINKEDIN_TOOLS])
        status = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertFalse(replies[2]["result"]["isError"])
        self.assertEqual((status["account"], status["transport"]["ready"]), (LINKEDIN_ACCOUNT, True))
        self.assertNotIn("dummy", json.dumps({key: value for key, value in status.items() if key != "account"}))
        provider = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertEqual(provider["error"]["code"], "read_unavailable")

    def test_newline_json_with_the_only_registered_account(self):
        payload = "".join(json.dumps(message) + "\n" for message in linkedin_requests_for("2025-06-18")).encode()
        stdout = self.run_server([sys.executable, "-c", LINKEDIN_SERVER, str(ROOT), "linkedin-mcp"], payload)
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 4)
        self.check_replies([json.loads(line) for line in lines], "2025-06-18")

    def test_content_length_frames_are_answered_without_the_bridge(self):
        # Framed clients such as Batbox need no bridge: the server answers frames in kind.
        incoming = io.BytesIO()
        for message in linkedin_requests_for("2024-11-05"):
            write_bridge_message(incoming, json.dumps(message).encode(), "framed")
        stdout = self.run_server([sys.executable, "-c", LINKEDIN_SERVER, str(ROOT), "linkedin-mcp",
                                  "--account", LINKEDIN_ACCOUNT], incoming.getvalue())
        stream, replies = io.BytesIO(stdout), []
        while (message := read_bridge_message(stream)) is not None:
            payload, mode = message
            self.assertEqual(mode, "framed")
            replies.append(json.loads(payload))
        self.check_replies(replies, "2024-11-05")


if __name__ == "__main__":
    unittest.main()
