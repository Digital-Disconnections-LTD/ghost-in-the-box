from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from farm.accounts import AccountRegistry


class AccountRegistryTests(unittest.TestCase):
    def test_local_session_reference_is_private_and_idempotent(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "session.json"
            session.write_text('{"opaque":"do-not-copy"}')
            session.chmod(0o600)
            registry = AccountRegistry(root / "private/accounts.json")
            first = registry.register(
                platform="instagram", account="mine", kind="instagrapi_settings",
                session_file=session, account_key="meta:mine",
            )
            second = registry.register(
                platform="instagram", account="mine", kind="instagrapi_settings",
                session_file=session, account_key="meta:mine",
            )
            self.assertEqual(first["created_at"], second["created_at"])
            self.assertEqual(first["account_key"], "meta:mine")
            self.assertTrue(second["session_private"])
            self.assertEqual(registry.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(registry.lock_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("do-not-copy", registry.path.read_text())
            self.assertEqual(len(registry.list()), 1)

    def test_missing_session_is_rejected(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            registry = AccountRegistry(root / "private/accounts.json")
            with self.assertRaisesRegex(ValueError, "does not exist"):
                registry.register(
                    platform="instagram", account="mine",
                    kind="instagrapi_settings", session_file=root / "missing",
                )


if __name__ == "__main__":
    unittest.main()
