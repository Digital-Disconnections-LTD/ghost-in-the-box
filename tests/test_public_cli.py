import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from farm import cli


class PublicCLITests(unittest.TestCase):
    def test_installed_instagram_client_cannot_consume_protocol_input(self):
        from farm.instagram_mcp import _new_client
        from farm.instagram_session import InstagramSessionError
        client = _new_client()
        try:
            with self.assertRaises(InstagramSessionError):
                client.challenge_code_handler("example_creator", "email")
            with self.assertRaises(InstagramSessionError):
                client.change_password_handler("example_creator")
        finally:
            client.private.close()
            client.public.close()

    def test_help_and_doctor_do_not_run_a_provider(self):
        with patch.object(cli.runpy, "run_module", side_effect=AssertionError("provider launched")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["doctor"]), 0)
            self.assertEqual(json.loads(output.getvalue())["network_requests"], 0)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["--help"]), 0)
            self.assertIn("instagram-mcp", output.getvalue())
            self.assertIn("usage: ghost-in-the-box", output.getvalue())

    def test_existing_command_receives_arguments_and_exit_status(self):
        seen = []
        def run(module, *, run_name):
            seen.append((module, list(sys.argv), run_name))
            raise SystemExit(2)
        previous = sys.argv
        with patch.object(cli.runpy, "run_module", side_effect=run):
            with self.assertRaises(SystemExit) as error:
                cli.main(["outbox", "--db", "/tmp/example.sqlite3", "list"])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(seen[0][0], "scripts.social_outbox")
        self.assertEqual(seen[0][1][1:], ["--db", "/tmp/example.sqlite3", "list"])
        self.assertIs(sys.argv, previous)

    def test_state_override_connects_registry_inbox_and_outbox(self):
        with TemporaryDirectory() as folder:
            env = dict(os.environ, SOCIAL_FARM_STATE_DIR=folder)
            program = "from farm.config import STATE_ROOT; from farm.accounts import DEFAULT_REGISTRY; from farm.inbox import DEFAULT_DB as inbox; from farm.outbox import DEFAULT_DB as outbox; import json; print(json.dumps([str(x) for x in (STATE_ROOT,DEFAULT_REGISTRY,inbox,outbox)]))"
            result = subprocess.run([sys.executable, "-c", program], env=env, capture_output=True, text=True, check=True)
            paths = [Path(p) for p in json.loads(result.stdout)]
            self.assertEqual(paths[0], Path(folder))
            self.assertTrue(all(p.parent == Path(folder) for p in paths[1:]))


if __name__ == "__main__":
    unittest.main()
