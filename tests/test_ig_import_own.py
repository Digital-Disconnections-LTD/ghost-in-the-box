from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("ig_import_own", Path(__file__).parents[1] / "scripts/ig_import_own.py")
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


def media(pk, caption="First line\n猫 café 🦇", *, author="123"):
    return SimpleNamespace(pk=str(pk), id=f"{pk}_{author}", code=f"Post_{pk}",
        caption_text=caption, taken_at=datetime(2020, 1, 2, tzinfo=timezone.utc),
        user=SimpleNamespace(pk=author, username="writer" if author=="123" else "another_writer"),
        media_type=1, product_type="feed")


class Client:
    user_id = 123

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def account_info(self):
        return SimpleNamespace(pk="123", username="writer")

    def user_medias_paginated_v1(self, user_id, amount, end_cursor):
        self.calls.append((user_id, amount, end_cursor))
        response = self.pages[end_cursor]
        if isinstance(response, Exception):
            raise response
        records, cursor = response
        self.last_json = {"status":"ok", "items":[{} for _ in records]}
        return records, cursor


class ImportTests(unittest.TestCase):
    def test_exact_unicode_source_anchors_permissions_and_idempotence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/"corpus"
            client=Client({"":([media(1),media(2)],"")})
            with importer.corpus_lock(root):
                first=importer.import_posts(client,root,"writer")
            self.assertEqual((first["new"],first["pages"]),(2,1))
            contents={p.name:p.read_bytes() for p in (root/"posts").glob("*.json")}
            with importer.corpus_lock(root):
                again=importer.import_posts(client,root,"writer")
            self.assertEqual((again["new"],again["updated"],again["unchanged"]),(0,0,2))
            self.assertEqual(contents,{p.name:p.read_bytes() for p in (root/"posts").glob("*.json")})
            for path in root.rglob("*"):
                self.assertEqual(path.stat().st_mode & 0o777,0o700 if path.is_dir() else 0o600)
            row=json.loads(contents["1.json"])
            self.assertEqual(row["caption"],"First line\n猫 café 🦇")
            self.assertEqual(row["source_uri"],"https://www.instagram.com/p/Post_1/")
            self.assertEqual(row["source_anchor"],"instagram:media:1:caption")
            self.assertEqual(row["source_date"],"2020-01-02T00:00:00+00:00")
            self.assertNotEqual(row["source_date"],row["captured_at"])
            self.assertEqual(importer.corpus_status(root)["nonempty_captions"],2)

    def test_recent_refresh_does_not_lose_older_cursor_and_caption_edits_update_one_post(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            client=Client({"":([media(3)],"older1"),"older1":([media(2)],"older2"),"older2":([media(1)],"")})
            importer.import_posts(client,root,"writer")
            original=json.loads((root/"posts/3.json").read_text())
            client.pages[""]=([media(4),media(3,"Edited caption")],"new-head-cursor")
            refreshed=importer.import_posts(client,root,"writer")
            self.assertEqual((refreshed["new"],refreshed["updated"]),(1,1))
            edited=json.loads((root/"posts/3.json").read_text())
            self.assertEqual(edited["first_captured_at"],original["first_captured_at"])
            self.assertEqual(edited["source_anchor"],original["source_anchor"])
            self.assertEqual(json.loads((root/"state.json").read_text())["backfill_cursor"],"older1")
            completed=importer.import_posts(client,root,"writer",older=True,pages=2)
            self.assertTrue(completed["backfill_complete"])
            self.assertEqual([c[2] for c in client.calls],["","","older1","older2"])
            self.assertEqual(importer.corpus_status(root)["posts"],4)

    def test_cursor_commits_only_after_post_files_and_replay_recovers_failed_state_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            client=Client({"":([media(1)],"next")})
            original=importer.atomic_json
            def fail_state(path,value):
                if path.name=="state.json":
                    raise OSError("private path should not be echoed")
                return original(path,value)
            with patch.object(importer,"atomic_json",fail_state):
                failed=importer.import_posts(client,root,"writer")
            self.assertFalse(failed["ok"])
            self.assertFalse((root/"state.json").exists())
            self.assertTrue((root/"posts/1.json").exists())
            self.assertNotIn("private path",json.dumps(failed))
            recovered=importer.import_posts(client,root,"writer")
            self.assertEqual((recovered["new"],recovered["unchanged"]),(0,1))
            self.assertEqual(json.loads((root/"state.json").read_text())["backfill_cursor"],"next")

    def test_other_authors_are_not_included_and_swallowed_failures_do_not_finish_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            client=Client({"":([media(1,author="999"),media(2)],"next")})
            report=importer.import_posts(client,root,"writer")
            self.assertEqual((report["new"],report["other_author_skipped"]),(1,1))
            self.assertFalse((root/"posts/1.json").exists())
            client.user_medias_paginated_v1=lambda *args,**kwargs:([],None)
            failed=importer.import_posts(client,root,"writer",older=True)
            self.assertEqual(failed["error"],"page_not_acknowledged")
            self.assertFalse(failed["backfill_complete"])
            self.assertEqual(json.loads((root/"state.json").read_text())["backfill_cursor"],"next")

    def test_import_cannot_overlap_or_accept_the_wrong_saved_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with importer.corpus_lock(root):
                with self.assertRaises(BlockingIOError):
                    with importer.corpus_lock(root):
                        pass
            client=Client({"":([],"")})
            client.account_info=lambda:SimpleNamespace(pk="123",username="wrong")
            with self.assertRaisesRegex(importer.ImportFailure,"account_mismatch"):
                importer.import_posts(client,root,"writer")
            self.assertEqual(client.calls,[])

    def test_installed_client_keeps_session_bytes_and_sends_only_direct_instagram_gets(self):
        import requests
        with tempfile.TemporaryDirectory() as temporary:
            session=Path(temporary)/"session.json"
            session.write_text(json.dumps({"cookies":{"ds_user_id":"123","sessionid":"fixture-only"},
                "authorization_data":{"ds_user_id":"123","sessionid":"fixture-only"}}))
            session.chmod(0o600)
            before=session.read_bytes()
            sent=[]
            def send(_session,request,**kwargs):
                sent.append((request.method,request.url,kwargs))
                return requests.Response()
            with patch.object(requests.Session,"send",send):
                client,audit=importer.make_client(session)
                self.assertEqual(sent,[]) # load_settings/init perform no network request
                client.private.send(requests.Request("GET","https://i.instagram.com/api/v1/feed/user/123/").prepare())
                for method,url in [("POST","https://i.instagram.com/api/v1/feed/user/123/"),
                    ("GET","https://collector.example/test"),("GET","https://i.instagram.com/api/v1/feed/user/999/")]:
                    with self.assertRaises(importer.ImportFailure):
                        client.private.send(requests.Request(method,url).prepare())
                self.assertFalse(client.with_challenge_flow)
                self.assertFalse(client.private.trust_env)
                self.assertEqual(sent[0][2]["proxies"],{})
                self.assertFalse(sent[0][2]["allow_redirects"])
                self.assertEqual(audit,{"hosts":["i.instagram.com"],"methods":["GET"],"requests":1})
                client.private.close();client.public.close()
            self.assertEqual(session.read_bytes(),before)


if __name__=="__main__":
    unittest.main()
