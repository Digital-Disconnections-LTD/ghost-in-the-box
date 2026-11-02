import json
from pathlib import Path
from tempfile import TemporaryDirectory
import tomllib
import unittest

from farm import linkedin_mcp
from farm.accounts import AccountRegistry
from farm.instagram_mcp import AccountSelectionError, resolve_account

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
STANDARD_CLIENTS = ("claude-desktop.mcp.json", "claude-code.mcp.json")
SERVERS = {"ghost-in-the-box-instagram", "ghost-in-the-box-linkedin", "ghost-in-the-box-substack"}
RESOLVERS = {"ghost-in-the-box-instagram": resolve_account,
             "ghost-in-the-box-linkedin": linkedin_mcp.resolve_account}


def servers(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))["mcpServers"]


def codex_servers():
    with (EXAMPLES / "codex.config.toml").open("rb") as handle:
        return tomllib.load(handle)["mcp_servers"]


class MCPExampleConfigTests(unittest.TestCase):
    def test_every_example_is_valid_json(self):
        for path in EXAMPLES.glob("*.json"):
            with self.subTest(path=path.name):
                json.loads(path.read_text(encoding="utf-8"))

    def test_standard_clients_start_every_server_directly(self):
        for name in STANDARD_CLIENTS:
            with self.subTest(name=name):
                entries = servers(name)
                self.assertEqual(set(entries), SERVERS)
                for entry in entries.values():
                    self.assertTrue(entry["command"].startswith("/"))
                    self.assertNotIn("mcp-bridge", entry["args"])
                self.assertEqual(entries["ghost-in-the-box-instagram"]["args"][:2], ["instagram-mcp", "--account"])
                self.assertEqual(entries["ghost-in-the-box-linkedin"]["args"][:2], ["linkedin-mcp", "--account"])
                self.assertEqual(entries["ghost-in-the-box-substack"]["args"][0], "substack-mcp")
        self.assertTrue(all(entry["type"] == "stdio" for entry in servers("claude-code.mcp.json").values()))

    def test_batbox_bridges_only_the_newline_only_substack_server(self):
        entries = servers("batbox.mcp.json")
        self.assertEqual(set(entries), SERVERS)
        self.assertNotIn("mcp-bridge", entries["ghost-in-the-box-instagram"]["args"])
        self.assertNotIn("mcp-bridge", entries["ghost-in-the-box-linkedin"]["args"])
        self.assertEqual(entries["ghost-in-the-box-substack"]["args"][:2], ["mcp-bridge", "--"])

    def test_codex_config_is_valid_toml_with_every_server_started_directly(self):
        entries = codex_servers()
        self.assertEqual(set(entries), SERVERS)
        for name, entry in entries.items():
            with self.subTest(name=name):
                self.assertEqual(set(entry) - {"env"}, {"command", "args"})
                self.assertTrue(entry["command"].startswith("/"))
                self.assertTrue(all(isinstance(arg, str) for arg in entry["args"]))
                self.assertNotIn("mcp-bridge", entry["args"])
        self.assertEqual(entries["ghost-in-the-box-linkedin"]["args"], ["linkedin-mcp", "--account", "YOUR_HANDLE"])
        self.assertEqual(entries, {name: {key: value for key, value in entry.items() if key != "type"}
                                   for name, entry in servers("claude-code.mcp.json").items()})

    def test_unedited_example_handle_is_refused(self):
        with TemporaryDirectory() as directory:
            registry = AccountRegistry(Path(directory) / "accounts.json")
            files = {name: servers(name) for name in (*STANDARD_CLIENTS, "batbox.mcp.json")}
            files["codex.config.toml"] = codex_servers()
            for name, entries in files.items():
                for server, resolve in RESOLVERS.items():
                    args = entries[server]["args"]
                    handle = args[args.index("--account") + 1]
                    with self.subTest(name=name, server=server), \
                            self.assertRaisesRegex(AccountSelectionError, "placeholder"):
                        resolve(handle, environ={}, registry_factory=lambda: registry)

    def test_readme_names_every_example_file(self):
        readme = (EXAMPLES / "README.md").read_text(encoding="utf-8")
        for path in [*EXAMPLES.glob("*.json"), *EXAMPLES.glob("*.toml")]:
            self.assertIn(f"`{path.name}`", readme)

    def test_source_distribution_ships_the_example_readme_and_not_private_notes(self):
        manifest = (EXAMPLES.parent / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        self.assertIn("recursive-include examples *.json *.md *.toml", manifest)
        self.assertFalse([line for line in manifest if "REFERENCE.md" in line])


if __name__ == "__main__":
    unittest.main()
