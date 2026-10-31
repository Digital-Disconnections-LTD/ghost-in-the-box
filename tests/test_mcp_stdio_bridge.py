import io
import json
from pathlib import Path
import subprocess
import sys
import unittest

from scripts.mcp_stdio_bridge import MAX_MESSAGE_BYTES, read_message, write_message


class MCPBridgeTests(unittest.TestCase):
    def test_framed_unicode_and_pretty_json_become_one_line(self):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "value": "🌱 café\nhello"},
                             indent=2, ensure_ascii=False).encode()
        stream = io.BytesIO(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
        result, mode = read_message(stream)
        self.assertEqual(mode, "framed")
        self.assertNotIn(b"\n", result)
        self.assertEqual(json.loads(result)["value"], "🌱 café\nhello")

    def test_invalid_and_incomplete_lengths_are_rejected(self):
        for source in (b"Content-Length: -1\r\n\r\n", b"Content-Length: 6\r\n\r\n{}",
                       f"Content-Length: {MAX_MESSAGE_BYTES+1}\r\n\r\n".encode()):
            with self.assertRaises(ValueError):
                read_message(io.BytesIO(source))

    def test_real_child_round_trip_for_both_client_framings(self):
        child = "import sys,json; [print(json.dumps({'jsonrpc':'2.0','id':json.loads(line)['id'],'result':{'text':'garden 🌱'}},ensure_ascii=False),flush=True) for line in sys.stdin]"
        bridge = Path(__file__).resolve().parents[1] / "scripts/mcp_stdio_bridge.py"
        for mode in ("framed", "jsonl"):
            with self.subTest(mode=mode):
                incoming = io.BytesIO()
                write_message(incoming, b'{"jsonrpc":"2.0","id":7,"method":"ping"}', mode)
                result = subprocess.run([sys.executable, str(bridge), "--", sys.executable, "-c", child],
                                        input=incoming.getvalue(), capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                payload, actual_mode = read_message(io.BytesIO(result.stdout))
                self.assertEqual(actual_mode, mode)
                self.assertEqual(json.loads(payload)["result"]["text"], "garden 🌱")


if __name__ == "__main__":
    unittest.main()
