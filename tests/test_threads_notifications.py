from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from farm.inbox import Inbox
from farm.outbox import Outbox
from farm.threads_notifications import namespace, sync_threads
from scripts.threads_notifications import notify_agent, quiet_window


class ThreadsNotificationTests(unittest.TestCase):
    def test_eastern_quiet_window_skips_browser_refresh(self):
        self.assertEqual(quiet_window(datetime(2026,10,26,9,59,tzinfo=timezone.utc))
                         ["status"],"quiet_hours")
        self.assertIsNone(quiet_window(datetime(2026,10,26,10,0,tzinfo=timezone.utc)))
        self.assertEqual(quiet_window(datetime(2026,10,27,3,55,tzinfo=timezone.utc))
                         ["status"],"quiet_hours")

    def test_replies_continue_from_saved_page_cursor(self):
        with TemporaryDirectory() as root:
            inbox = Inbox(Path(root)/"inbox.sqlite3")
            class Adapter:
                calls = []
                def own_posts(self, account, limit):
                    return {"posts":[{"id":"1","username":account,
                                      "permalink":f"https://www.threads.com/@{account}/post/P1"}]}
                def replies(self, account, post_id, limit, cursor=""):
                    self.calls.append(cursor)
                    page = {"": ("101", "older-1"),
                            "older-1": ("102", "older-2"),
                            "older-2": ("103", "")}[cursor]
                    return {"posts":[{"id":page[0],"username":"reader",
                        "permalink":f"https://www.threads.com/@reader/post/R{page[0]}",
                        "body":"a comment"}],"next_cursor":page[1]}
            adapter = Adapter()
            for _ in range(4):
                sync_threads(adapter,inbox,"mine",posts=1)
            self.assertEqual(adapter.calls,["","","older-1","","older-2",""])
            self.assertEqual(inbox.cursor(namespace("mine"),"replies-backfill:1"),"-")
            self.assertEqual(len(inbox.peek(namespace("mine"),limit=10)),3)
            inbox.close()

    def test_rotating_posts_and_selective_reply_draft(self):
        with TemporaryDirectory() as root:
            inbox = Inbox(Path(root)/"inbox.sqlite3")
            class Adapter:
                scanned = []
                def own_posts(self, account, limit):
                    return {"posts":[{"id":str(i),"username":account,
                        "permalink":f"https://www.threads.com/@{account}/post/P{i}"}
                        for i in (1,2,3,4)]}
                def replies(self, account, post_id, limit):
                    self.scanned.append(post_id)
                    return {"posts":[{"id":str(int(post_id)+100),"username":"reader",
                        "permalink":f"https://www.threads.com/@reader/post/R{post_id}",
                        "body":"a comment","timestamp":datetime.now(timezone.utc).isoformat()}]}
            adapter=Adapter()
            sync_threads(adapter,inbox,"mine",posts=2)
            sync_threads(adapter,inbox,"mine",posts=2)
            sync_threads(adapter,inbox,"mine",posts=2)
            self.assertEqual(set(adapter.scanned),{"1","2","3","4"})
            self.assertEqual(adapter.scanned.count("1"),3)
            key=namespace("mine")
            claimed=inbox.unseen(key,limit=2,worker="operator")
            self.assertEqual(len(claimed),2)
            inbox.skip(key,claimed[0]["comment_id"],worker="operator")
            with Outbox(Path(root)/"outbox.sqlite3") as outbox:
                job=inbox.draft_reply(outbox,key,claimed[1]["comment_id"],
                    "A thoughtful reply",worker="operator",platform="threads",
                    profile_account="mine")
                self.assertEqual(job["platform"],"threads")
                self.assertEqual(job["account_key"],"meta:mine")
                self.assertEqual(job["target_id"],job["media_id"])
                self.assertEqual(job["source_urls"][0],claimed[1]["url"])
                self.assertEqual(outbox.queue(job["id"])["status"],"queued")
                resumed=inbox.draft_reply(outbox,key,claimed[1]["comment_id"],
                    "A thoughtful reply",worker="operator",platform="threads",
                    profile_account="mine")
                self.assertEqual(resumed["id"],job["id"])
            self.assertEqual(inbox.pending(key,worker="operator"),[])
            inbox.close()

    @patch('scripts.threads_notifications.quiet_window', return_value=None)
    def test_durable_comment_notice_retries_same_batch_and_consumes_once(self, _quiet):
        with TemporaryDirectory() as root:
            inbox = Inbox(Path(root)/"inbox.sqlite3")
            account = namespace("mine")
            for comment_id in ("101", "102"):
                inbox.save_external_comment(account=account,comment_id=comment_id,
                    media_id="1",parent_id="1",
                    url=f"https://www.threads.com/@reader/post/{comment_id}",
                    media_url="https://www.threads.com/@mine/post/Parent",
                    author="reader",body="comment",timestamp="2026-10-26T19:00:00Z")
            frozen = inbox.notification_batch(account)
            self.assertEqual(json.loads(frozen["comment_ids"]),["101","102"])
            self.assertEqual(inbox.notification_batch(account)["id"],frozen["id"])
            failed = Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))
            with self.assertRaises(RuntimeError):
                notify_agent(inbox,"mine","operator",runner=failed,
                             chat_path=Path(root)/"engine_chat.py")
            self.assertEqual(inbox.notification_batch(account)["id"],frozen["id"])
            accepted = Mock(return_value=subprocess.CompletedProcess([], 0,
                json.dumps({"id":frozen["id"],"durable":True,"status":"queued"}),""))
            with patch('scripts.threads_notifications.quiet_window',
                       return_value={"status":"quiet_hours"}):
                self.assertEqual(notify_agent(inbox,"mine","operator",runner=accepted,
                                              chat_path=Path(root)/"engine_chat.py"),
                                 {"status":"quiet_hours"})
            accepted.assert_not_called()
            result = notify_agent(inbox,"mine","operator",runner=accepted,
                                  chat_path=Path(root)/"engine_chat.py")
            self.assertEqual(result["count"],2)
            self.assertEqual(notify_agent(inbox,"mine","operator",runner=accepted,
                                          chat_path=Path(root)/"engine_chat.py"),
                             {"status":"none","count":0})
            self.assertEqual(accepted.call_count,1)
            self.assertEqual(len(inbox.peek(account,limit=10)),2)
            inbox.close()

    @patch('scripts.threads_notifications.quiet_window', return_value=None)
    def test_unconfigured_hook_retains_pending_batch_for_selected_worker(self, _quiet):
        with TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            inbox = Inbox(Path(root)/"inbox.sqlite3")
            self.addCleanup(inbox.close)
            inbox.save_external_comment(account=namespace("mine"),comment_id="101",
                media_id="1",parent_id="1",url="https://www.threads.com/@reader/post/101",
                media_url="https://www.threads.com/@mine/post/Parent",
                author="reader",body="comment",timestamp="2026-10-26T19:00:00Z")
            runner = Mock(side_effect=AssertionError("unconfigured hook launched"))
            result = notify_agent(inbox,"mine","community-helper",runner=runner)
            self.assertEqual(result["status"], "pending")
            self.assertIn("--worker community-helper", result["content"])
            batch = inbox.notification_batch(namespace("mine"), recipient="community-helper")
            self.assertEqual(result["message_id"], batch["id"])
            self.assertIsNone(batch["sent_at"])
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
