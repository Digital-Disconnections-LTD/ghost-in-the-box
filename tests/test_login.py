from contextlib import redirect_stdout
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from farm.accounts import AccountRegistry
from farm.login import (export_accounts, import_accounts, provider_cookies, save_login,
                        validate_session, verify_browser)
from farm.session_files import account_name, read_session, write_session
from scripts import social_login


def session(platform="linkedin", account="mine"):
    names = {"linkedin": {"li_at": "TEST_COOKIE", "JSESSIONID": '"ajax:123"'},
             "threads": {"sessionid": "TEST_COOKIE", "ds_user_id": "123"},
             "substack": {"substack.sid": "TEST_COOKIE"}}[platform]
    result = {"schema": 1, "platform": platform, "account": account, "user_agent": "TEST_UA",
              "cookies": [{"name": k, "value": v, "domain": "." + platform + ".com",
                           "path": "/", "secure": True, "expires": -1} for k, v in names.items()]}
    if platform == "substack":
        result["publication_url"] = "https://example.substack.com"
    return result


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.registry = AccountRegistry(self.root / "local/accounts.json")
        # These tests mock browser_login; a real browser must never start.
        guard = patch("farm.login.LoginWindow.open", side_effect=AssertionError("a real browser would open"))
        guard.start()
        self.addCleanup(guard.stop)

    def save(self, platform="linkedin", account="mine", value=None):
        return save_login(platform, account, value or session(platform, account),
                          registry=self.registry, state_root=self.root / "local")

    def test_only_selected_accounts_export_and_paths_rebase_on_import(self):
        self.save()
        self.save("threads")
        bundle = self.root / "selected.json"
        export_accounts(["linkedin:mine"], bundle, registry=self.registry)
        raw = read_session(bundle)
        self.assertEqual([e["platform"] for e in raw["accounts"]], ["linkedin"])
        self.assertNotIn(str(self.root), bundle.read_text())
        remote = AccountRegistry(self.root / "server/accounts.json")
        with patch("requests.Session.request", side_effect=AssertionError("network")):
            result = import_accounts(bundle, registry=remote, state_root=self.root / "server")
        self.assertEqual(result["network_requests"], 0)
        entry = remote.get("linkedin", "mine")
        self.assertTrue(Path(entry["session_file"]).is_relative_to(self.root / "server"))
        self.assertEqual(read_session(Path(entry["session_file"]))["cookies"], session()["cookies"])
        self.assertIsNone(remote.get("threads", "mine"))

    def test_meta_account_key_is_preserved(self):
        settings = {"authorization_data": {"sessionid": "TEST_COOKIE", "ds_user_id": "123"}}
        entry = self.save("instagram", value=settings)
        self.registry.register(platform="instagram", account="mine", kind=entry["kind"],
                               session_file=entry["session_file"], account_key="meta:custom")
        bundle = self.root / "sessions.json"
        export_accounts(["instagram:mine"], bundle, registry=self.registry)
        remote = AccountRegistry(self.root / "server/accounts.json")
        import_accounts(bundle, registry=remote, state_root=self.root / "server")
        self.assertEqual(remote.get("instagram", "mine")["account_key"], "meta:custom")

    def test_substack_config_and_cookies_rebase_together(self):
        self.save("substack")
        bundle = self.root / "sessions.json"
        export_accounts(["substack:mine"], bundle, registry=self.registry)
        remote = AccountRegistry(self.root / "server/accounts.json")
        import_accounts(bundle, registry=remote, state_root=self.root / "server")
        config = read_session(Path(remote.get("substack", "mine")["session_file"]))
        self.assertTrue(Path(config["cookies_path"]).is_relative_to(self.root / "server"))
        self.assertEqual(read_session(Path(config["cookies_path"])), {"substack.sid": "TEST_COOKIE"})

    def test_import_rejects_conflicts_before_any_account_is_written(self):
        self.save()
        self.save("threads")
        bundle = self.root / "sessions.json"
        export_accounts(["threads:mine", "linkedin:mine"], bundle, registry=self.registry)
        remote = AccountRegistry(self.root / "server/accounts.json")
        save_login("linkedin", "mine", session(), registry=remote, state_root=self.root / "server")
        with self.assertRaisesRegex(ValueError, "already registered"):
            import_accounts(bundle, registry=remote, state_root=self.root / "server")
        self.assertIsNone(remote.get("threads", "mine"))

    def test_import_rejects_traversal_and_invalid_later_entry_before_writes(self):
        bundle = self.root / "sessions.json"
        write_session(bundle, {"schema": 1, "format": "ghost-in-the-box-sessions", "accounts": [
            {"platform": "linkedin", "account": "mine", "session": session(), "account_key": "linkedin:mine"},
            {"platform": "threads", "account": "../outside", "session": {}, "account_key": "bad"}]})
        with self.assertRaises(ValueError):
            import_accounts(bundle, registry=self.registry, state_root=self.root / "server")
        self.assertIsNone(self.registry.get("linkedin", "mine"))

    def test_browser_cookies_exclude_sso_and_other_provider_secrets(self):
        cookies = session()["cookies"] + [{"name": "sid", "value": "SECRET", "domain": ".google.com"},
                                          {"name": "sid", "value": "SECRET", "domain": ".evil-linkedin.com"}]
        self.assertEqual(provider_cookies("linkedin", cookies), session()["cookies"])
        bad = session()
        bad["cookies"] = cookies
        with self.assertRaises(ValueError):
            validate_session("linkedin", "mine", bad)

    def test_export_cannot_overwrite_source_or_existing_destination(self):
        entry = self.save()
        with self.assertRaises(ValueError):
            export_accounts(["linkedin:mine"], Path(entry["session_file"]), registry=self.registry, replace=True)
        path = self.root / "bundle.json"
        write_session(path, {"existing": True})
        with self.assertRaises(ValueError):
            export_accounts(["linkedin:mine"], path, registry=self.registry)
        self.assertEqual(read_session(path), {"existing": True})

    def test_paths_and_cookie_identity_are_checked(self):
        for name in ("../outside", ".", "..", "a/b", "a\\b", "NUL", "CON.json", "mine."):
            with self.subTest(name=name), self.assertRaises(ValueError):
                account_name(name)
        for field, value in (("account", "somebody-else"), ("platform", "threads")):
            bad = session()
            bad[field] = value
            with self.assertRaises(ValueError):
                self.save(value=bad)

    @unittest.skipIf(os.name == "nt", "POSIX mode checks; Windows writer uses a user ACL")
    def test_private_modes_and_public_bundle_rejected(self):
        entry = self.save()
        self.assertEqual(Path(entry["session_file"]).stat().st_mode & 0o777, 0o600)
        bundle = self.root / "public.json"
        bundle.write_text("{}")
        bundle.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            import_accounts(bundle, registry=self.registry)

    def test_wizard_continues_selected_providers_and_never_prints_cookies(self):
        with patch.object(social_login.sys.stdin, "isatty", return_value=True), \
                patch.object(social_login, "browser_login", side_effect=[RuntimeError("SECRET"), ("mine", session("threads"))]), \
                patch.object(social_login, "save_login", return_value={"session_file": "private.json"}) as save, \
                patch("builtins.input", return_value="mine"), redirect_stdout(io.StringIO()) as output:
            code = social_login.main(["start", "--platform", "linkedin", "--platform", "threads"])
        self.assertEqual(code, 4)
        self.assertEqual(save.call_args.args[:2], ("threads", "mine"))
        self.assertNotIn("SECRET", output.getvalue())
        self.assertNotIn("TEST_COOKIE", output.getvalue())

    def test_linkedin_browser_verification_requires_live_identity(self):
        context = SimpleNamespace(cookies=lambda: session()["cookies"],
                                  pages=[SimpleNamespace(evaluate=lambda _: "TEST_UA")])
        with patch("farm.adapters.linkedin.LinkedInAdapter") as cls:
            cls.return_value.me.side_effect = RuntimeError("wrong account")
            with self.assertRaises(RuntimeError):
                verify_browser("linkedin", "mine", context)
            cls.return_value.me.assert_called_once_with("mine")

    def test_windows_writer_sets_acl_before_credentials_are_written(self):
        from farm import session_files
        path = self.root / "session.json"
        with patch.object(session_files, "protect") as protect:
            write_session(path, {"cookie": "TEST_COOKIE"})
        protect.assert_called_once()
        self.assertEqual(read_session(path), {"cookie": "TEST_COOKIE"})

    def test_windows_acl_command_removes_inherited_access(self):
        from farm import session_files
        path = self.root / "session.json"
        with patch.object(session_files, "os", SimpleNamespace(name="nt")), \
                patch.object(session_files.subprocess, "run", side_effect=[
                    SimpleNamespace(stdout="DESKTOP\\owner\n"), SimpleNamespace()]) as run:
            session_files.protect(path)
        self.assertEqual(run.call_args.args[0], ["icacls", str(path), "/inheritance:r", "/grant:r", "DESKTOP\\owner:(F)"])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_failed_permission_setup_preserves_existing_session(self):
        from farm import session_files
        path = self.root / "session.json"
        write_session(path, {"existing": True})
        with patch.object(session_files, "protect", side_effect=OSError("ACL setup failed")):
            with self.assertRaises(OSError):
                write_session(path, {"cookie": "TEST_COOKIE"})
        self.assertEqual(read_session(path), {"existing": True})

    def test_threads_and_instagram_logins_share_one_meta_key(self):
        settings = {"authorization_data": {"sessionid": "TEST_COOKIE", "ds_user_id": "123"}}
        self.assertEqual(self.save("instagram", value=settings)["account_key"], "meta:mine")
        self.assertEqual(self.save("threads")["account_key"], "meta:mine")
        self.assertEqual(self.save("linkedin")["account_key"], "linkedin:mine")

    def test_relogin_keeps_a_chosen_shared_key(self):
        entry = self.save("threads")
        self.registry.register(platform="threads", account="mine", kind=entry["kind"],
                               session_file=entry["session_file"], account_key="meta:team")
        self.assertEqual(self.save("threads")["account_key"], "meta:team")

    def test_one_persons_instagram_and_threads_jobs_never_run_concurrently(self):
        from farm.outbox import Outbox
        settings = {"authorization_data": {"sessionid": "TEST_COOKIE", "ds_user_id": "123"}}
        self.save("instagram", value=settings)
        self.save("threads")
        with patch.dict(os.environ, {"SOCIAL_FARM_ACCOUNTS_FILE": str(self.registry.path)}), \
                Outbox(self.root / "outbox.sqlite3") as box:
            image = self.root / "image.jpg"
            image.write_bytes(b"\xff\xd8\xff")
            ig = box.create_draft(platform="instagram", account="mine", body="photo", media_path=str(image))
            threads = box.create_draft(platform="threads", account="mine", body="text")
            self.assertEqual(ig["account_key"], threads["account_key"])
            box.queue(ig["id"])
            box.queue(threads["id"])
            first = box.claim_due()
            self.assertIsNotNone(first)
            self.assertIsNone(box.claim_due())
            other = threads if first["id"] == ig["id"] else ig
            self.assertIsNone(box.claim_job(other["id"]))
            box.release(first["id"], first["lease_token"])
            self.assertIsNotNone(box.claim_job(other["id"]))

    def test_legacy_threads_key_in_a_bundle_imports_as_meta(self):
        bundle = self.root / "sessions.json"
        write_session(bundle, {"schema": 1, "format": "ghost-in-the-box-sessions", "accounts": [
            {"platform": "threads", "account": "mine", "session": session("threads"), "account_key": "threads:mine"}]})
        import_accounts(bundle, registry=self.registry, state_root=self.root / "server")
        entry = self.registry.get("threads", "mine")
        self.assertEqual(entry["account_key"], "meta:mine")
        self.assertEqual(entry["legacy_account_keys"], ["threads:mine"])

    def bundle_of_three(self):
        self.save()
        self.save("threads")
        self.save("substack")
        bundle = self.root / "sessions.json"
        export_accounts(["linkedin:mine", "threads:mine", "substack:mine"], bundle, registry=self.registry)
        return bundle

    def test_import_writes_the_registry_once(self):
        bundle = self.bundle_of_three()
        remote = AccountRegistry(self.root / "server/accounts.json")
        with patch.object(remote, "_write", wraps=remote._write) as write, \
                patch.object(remote, "register", side_effect=AssertionError("per-entry register")):
            result = import_accounts(bundle, registry=remote, state_root=self.root / "server")
        write.assert_called_once()
        self.assertEqual(result["accounts"], ["linkedin:mine", "threads:mine", "substack:mine"])
        self.assertEqual(len(remote.list()), 3)
        self.assertFalse([p for p in (self.root / "server").rglob(".*") if p.name != "accounts.json.lock"])

    def test_failed_registry_write_rolls_back_every_new_session(self):
        bundle = self.bundle_of_three()
        server = self.root / "server"
        remote = AccountRegistry(server / "accounts.json")
        with patch.object(remote, "_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                import_accounts(bundle, registry=remote, state_root=server)
        self.assertEqual(remote.list(), [])
        self.assertEqual([p for p in server.rglob("*") if p.is_file() and p.name != "accounts.json.lock"], [])

    def test_failed_replace_restores_previous_sessions(self):
        bundle = self.bundle_of_three()
        server = self.root / "server"
        remote = AccountRegistry(server / "accounts.json")
        old = session()
        old["cookies"][0]["value"] = "OLD_COOKIE"
        save_login("linkedin", "mine", old, registry=remote, state_root=server)
        before = remote.get("linkedin", "mine")
        real_replace = os.replace
        calls = []
        def flaky_replace(source, target):
            calls.append(target)
            # Fail while publishing the third staged file, after earlier targets moved.
            if len([c for c in calls if not str(Path(c).name).startswith(".")]) == 3:
                raise OSError("replace failed")
            return real_replace(source, target)
        with patch("farm.login.os.replace", side_effect=flaky_replace):
            with self.assertRaises(OSError):
                import_accounts(bundle, registry=remote, state_root=server, replace=True)
        self.assertEqual(remote.get("linkedin", "mine"), before)
        restored = read_session(Path(before["session_file"]))
        self.assertEqual(restored["cookies"][0]["value"], "OLD_COOKIE")
        self.assertIsNone(remote.get("threads", "mine"))
        self.assertFalse((server / "threads" / "mine.json").exists())
        self.assertFalse([p for p in server.rglob(".*") if p.name != "accounts.json.lock"])

    def test_missing_bundle_names_the_path_and_next_command(self):
        from farm.login import LoginError
        with self.assertRaises(LoginError) as error:
            import_accounts(self.root / "nope.json", registry=self.registry)
        self.assertEqual(error.exception.code, "bundle_missing")
        self.assertIn(str(self.root / "nope.json"), error.exception.message)
        self.assertIn("login export", error.exception.hint)

    def test_windows_descriptor_protection_uses_the_acl_helper(self):
        from farm import session_files
        path = self.root / "secret.json"
        path.write_text("{}")
        with patch.object(session_files, "os", SimpleNamespace(name="nt")), \
                patch.object(session_files, "protect") as protect:
            session_files.protect_descriptor(3, path)
            self.assertTrue(session_files.is_private(path))
        protect.assert_called_once_with(path)
        with patch.object(session_files, "os", SimpleNamespace(name="posix")), \
                patch.object(session_files, "protect") as protect:
            session_files.protect_descriptor(3, path)
        protect.assert_called_once_with(path)

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_posix_descriptor_protection_and_privacy_check(self):
        from farm import session_files
        path = self.root / "secret.json"
        with open(path, "w") as stream:
            os.chmod(path, 0o644)
            self.assertFalse(session_files.is_private(path))
            session_files.protect_descriptor(stream.fileno(), path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(session_files.is_private(path))
        self.assertEqual(session_files.O_NOFOLLOW, os.O_NOFOLLOW)


if __name__ == "__main__":
    unittest.main()
