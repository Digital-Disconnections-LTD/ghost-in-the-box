import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from farm import locking
from farm.accounts import AccountRegistry, RegistryError, account_key_matches, default_account_key


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

    def test_meta_platforms_share_one_default_key(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "session.json"
            session.write_text("{}")
            session.chmod(0o600)
            registry = AccountRegistry(root / "accounts.json")
            for platform in ("instagram", "threads", "linkedin"):
                registry.register(platform=platform, account="mine", kind="cookie_jar", session_file=session)
            self.assertEqual(registry.get("instagram", "mine")["account_key"], "meta:mine")
            self.assertEqual(registry.get("threads", "mine")["account_key"], "meta:mine")
            self.assertEqual(registry.get("linkedin", "mine")["account_key"], "linkedin:mine")
            self.assertEqual(default_account_key("threads", "x"), "meta:x")

    def test_legacy_threads_key_reads_as_meta_and_is_rewritten(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "session.json"
            session.write_text("{}")
            session.chmod(0o600)
            path = root / "accounts.json"
            legacy = {"platform": "threads", "account": "mine", "account_key": "threads:mine", "kind": "cookie_jar",
                      "session_file": str(session), "created_at": "2025-01-01T00:00:00+00:00",
                      "updated_at": "2025-01-01T00:00:00+00:00"}
            custom = dict(legacy, platform="threads", account="other", account_key="threads:custom-team")
            path.write_text(json.dumps({"schema": 1, "accounts": {"threads:mine": legacy, "threads:other": custom}}))
            registry = AccountRegistry(path)
            entry = registry.get("threads", "mine")
            self.assertEqual(entry["account_key"], "meta:mine")
            self.assertTrue(account_key_matches(entry, "threads:mine"))
            self.assertTrue(account_key_matches(entry, "meta:mine"))
            self.assertFalse(account_key_matches(entry, "meta:other"))
            self.assertEqual(registry.get("threads", "other")["account_key"], "threads:custom-team")
            registry.register(platform="linkedin", account="mine", kind="cookie_jar", session_file=session)
            stored = json.loads(path.read_text())["accounts"]
            self.assertEqual(stored["threads:mine"]["account_key"], "meta:mine")
            self.assertEqual(stored["threads:mine"]["legacy_account_keys"], ["threads:mine"])
            self.assertEqual(stored["threads:mine"]["created_at"], "2025-01-01T00:00:00+00:00")
            registry.register(platform="threads", account="mine", kind="cookie_jar", session_file=session)
            self.assertEqual(registry.get("threads", "mine")["legacy_account_keys"], ["threads:mine"])

    def test_register_many_validates_everything_then_writes_once(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "session.json"
            session.write_text("{}")
            session.chmod(0o600)
            registry = AccountRegistry(root / "accounts.json")
            good = {"platform": "linkedin", "account": "a", "kind": "cookie_jar", "session_file": session}
            bad = dict(good, account="b", session_file=root / "missing.json")
            with patch.object(registry, "_write", wraps=registry._write) as write:
                with self.assertRaises(RegistryError) as error:
                    registry.register_many([good, bad])
                self.assertEqual(error.exception.code, "session_missing")
                self.assertIn("login start --platform linkedin --account b", error.exception.hint)
                write.assert_not_called()
                result = registry.register_many([good, dict(good, account="c")])
                write.assert_called_once()
            self.assertEqual([r["account"] for r in result], ["a", "c"])
            self.assertTrue(all(r["session_exists"] and r["session_private"] for r in result))

    def test_unreadable_registry_has_a_code_and_hint(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "accounts.json"
            path.write_text("{not json")
            with self.assertRaises(RegistryError) as error:
                AccountRegistry(path).list()
            self.assertEqual(error.exception.code, "registry_unreadable")
            self.assertIn(str(path), error.exception.hint)
            self.assertIsInstance(error.exception, ValueError)

    def test_failed_lock_acquisition_never_unlocks(self):
        with TemporaryDirectory() as temp:
            registry = AccountRegistry(Path(temp) / "accounts.json")
            calls = []
            def flock(fd, operation, timeout=None):
                calls.append(operation)
                raise TimeoutError("busy")
            with patch.object(locking, "flock", side_effect=flock):
                with self.assertRaises(TimeoutError):
                    registry.list()
            self.assertEqual(calls, [locking.LOCK_EX])


if __name__ == "__main__":
    unittest.main()
