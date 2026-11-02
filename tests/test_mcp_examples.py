import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from farm.accounts import AccountRegistry
from farm.instagram_mcp import AccountSelectionError, resolve_account

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
STANDARD_CLIENTS = ("claude-desktop.mcp.json", "claude-code.mcp.json")


def servers(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))["mcpServers"]


class MCPExampleConfigTests(unittest.TestCase):
    def test_every_example_is_valid_json(self):
        for path in EXAMPLES.glob("*.json"):
            with self.subTest(path=path.name):
                json.loads(path.read_text(encoding="utf-8"))

    def test_standard_clients_start_both_servers_directly(self):
        for name in STANDARD_CLIENTS:
            with self.subTest(name=name):
                entries = servers(name)
                self.assertEqual(set(entries), {"ghost-in-the-box-instagram", "ghost-in-the-box-substack"})
                for entry in entries.values():
                    self.assertTrue(entry["command"].startswith("/"))
                    self.assertNotIn("mcp-bridge", entry["args"])
                self.assertEqual(entries["ghost-in-the-box-instagram"]["args"][:2], ["instagram-mcp", "--account"])
                self.assertEqual(entries["ghost-in-the-box-substack"]["args"][0], "substack-mcp")
        self.assertTrue(all(entry["type"] == "stdio" for entry in servers("claude-code.mcp.json").values()))

    def test_batbox_bridges_only_the_newline_only_substack_server(self):
        entries = servers("batbox.mcp.json")
        self.assertNotIn("mcp-bridge", entries["ghost-in-the-box-instagram"]["args"])
        self.assertEqual(entries["ghost-in-the-box-substack"]["args"][:2], ["mcp-bridge", "--"])

    def test_unedited_example_handle_is_refused(self):
        with TemporaryDirectory() as directory:
            registry = AccountRegistry(Path(directory) / "accounts.json")
            for name in (*STANDARD_CLIENTS, "batbox.mcp.json"):
                args = servers(name)["ghost-in-the-box-instagram"]["args"]
                handle = args[args.index("--account") + 1]
                with self.subTest(name=name), self.assertRaisesRegex(AccountSelectionError, "placeholder"):
                    resolve_account(handle, environ={}, registry_factory=lambda: registry)

    def test_readme_names_every_example_file(self):
        readme = (EXAMPLES / "README.md").read_text(encoding="utf-8")
        for path in EXAMPLES.glob("*.json"):
            self.assertIn(f"`{path.name}`", readme)

    def test_source_distribution_ships_the_example_readme_and_not_private_notes(self):
        manifest = (EXAMPLES.parent / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        self.assertIn("recursive-include examples *.json *.md", manifest)
        self.assertFalse([line for line in manifest if "REFERENCE.md" in line])


if __name__ == "__main__":
    unittest.main()
