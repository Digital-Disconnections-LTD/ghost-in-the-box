import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as S
from farm.inbox import Inbox, sync_comments
from farm.outbox import Outbox
from _env import IsolatedStateTestCase


class InboxTests(IsolatedStateTestCase):
    def test_dedup_sources_draft_and_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Inbox(Path(tmp)/'inbox.sqlite3')
            comment = S(pk=44,created_at_utc=datetime.now(timezone.utc),user=S(username='reader'),text='hello')
            post = S(pk=1,id='1_7',user=S(pk=7),code='ABC')
            class Client:
                user_id = 7
                def user_medias_paginated_v1(self,*args,**kwargs):
                    return [post], 'posts-next'
                def media_comments_v1_chunk(self,*args,**kwargs):
                    return [comment], '', 'comments-next'
                def get_timeline_feed(self,**kwargs):
                    return {'feed_items':[{'media_or_ad':{'pk':9,'code':'FEED','caption':{'text':'topic'},
                        'taken_at':1700000000,'user':{'username':'source'}}}],'next_max_id':'feed-next'}
            for _ in range(2):
                sync_comments(Client(),inbox,'owner',feed_pages=1)
            rows = inbox.unseen('owner')
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['url'],'https://www.instagram.com/p/ABC/?comment_id=44')
            self.assertEqual(inbox.cursor('owner','comments:1_7'),'comments-next')
            self.assertEqual(len(inbox.feed('owner')),1)
            self.assertEqual(inbox.feed('owner')[0]['url'],'https://www.instagram.com/p/FEED/')
            with Outbox(Path(tmp)/'outbox.sqlite3') as outbox:
                job = inbox.draft_reply(outbox,'owner','44','Thanks!')
                self.assertEqual(job['status'],'draft')
                self.assertEqual(job['target_id'],'44')
                self.assertEqual(job['media_id'],'1_7')
                self.assertEqual(job['account_key'],'meta:owner')
                self.assertEqual(job['source_urls'],[rows[0]['url'],rows[0]['media_url']])
                self.assertEqual(inbox.draft_reply(outbox,'owner','44','Thanks!')['id'],job['id'])
                with self.assertRaises(ValueError):
                    inbox.draft_reply(outbox,'other','44','Thanks!')
            inbox.mark_seen('owner','44')
            inbox.save_comment('owner','1_7','ABC',comment)
            self.assertEqual(inbox.unseen('owner'),[])
            self.assertEqual(inbox.path.stat().st_mode & 0o777,0o600)
            inbox.close()

    def test_read_notifications_once_and_skip_or_draft_selectively(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Inbox(Path(tmp)/'inbox.sqlite3')
            base = datetime.now(timezone.utc)
            for pk in (1,2):
                comment = S(pk=pk,created_at_utc=base,user=S(username='reader'),text=f'comment {pk}')
                inbox.save_comment('owner','1_7','ABC',comment)
            self.assertEqual(len(inbox.peek('owner')),2)
            first = inbox.unseen('owner',limit=1)
            self.assertEqual(len(first),1)
            self.assertEqual(len(inbox.peek('owner')),1)
            self.assertEqual(len(inbox.pending('owner')),1)
            inbox.skip('owner',first[0]['comment_id'])
            self.assertEqual(inbox.pending('owner'),[])
            second = inbox.unseen('owner',limit=1)[0]
            with Outbox(Path(tmp)/'outbox.sqlite3') as outbox:
                inbox.draft_reply(outbox,'owner',second['comment_id'],'Worth answering')
            self.assertEqual(inbox.pending('owner'),[])
            self.assertEqual(inbox.unseen('owner'),[])
            inbox.close()

    def test_workers_receive_disjoint_batches_and_expired_lease_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Inbox(Path(tmp)/'inbox.sqlite3')
            stamp = datetime.now(timezone.utc)
            for pk in (1,2):
                inbox.save_comment('owner','1_7','ABC',
                    S(pk=pk,created_at_utc=stamp,user=S(username='reader'),text='hello'))
            first = inbox.unseen('owner',limit=1,worker='operator')[0]
            second = inbox.unseen('owner',limit=1,worker='mnemo')[0]
            self.assertNotEqual(first['comment_id'],second['comment_id'])
            with self.assertRaisesRegex(ValueError,'not held'):
                inbox.skip('owner',first['comment_id'],worker='mnemo')
            with inbox.db:
                inbox.db.execute("UPDATE comments SET claim_until='2000-01-01T00:00:00+00:00' WHERE comment_id=?",
                                 (first['comment_id'],))
            recovered = inbox.unseen('owner',limit=1,worker='mnemo')[0]
            self.assertEqual(recovered['comment_id'],first['comment_id'])
            inbox.close()

    def test_full_media_page_is_queued_before_cursor_and_scanned_in_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Inbox(Path(tmp)/'inbox.sqlite3')
            posts = [S(pk=i,id=f'{i}_7',user=S(pk=7),code=f'CODE{i}') for i in range(1,34)]
            class Client:
                user_id = 7
                scanned = []
                def user_medias_paginated_v1(self,*args,**kwargs):
                    self.requested = kwargs['amount']
                    return posts, 'after-33'
                def media_comments_v1_chunk(self,media_id,**kwargs):
                    self.scanned.append(media_id)
                    return [],'',None
            client = Client()
            for _ in range(11):
                sync_comments(client,inbox,'owner',posts=3)
            self.assertEqual(client.requested,33)
            self.assertEqual(set(client.scanned),{post.id for post in posts})
            self.assertEqual(inbox.cursor('owner','own-posts-backfill'),'after-33')
            inbox.close()

class MarkSeenTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = Inbox(Path(self.tmp.name)/'inbox.sqlite3')
        self.addCleanup(self.inbox.close)
        stamp = datetime.now(timezone.utc)
        for pk in (1,2,3,4,5):
            self.inbox.save_comment('owner','1_7','ABC',
                S(pk=pk,created_at_utc=stamp,user=S(username='reader'),text=f'comment {pk}'))

    def state(self, comment_id):
        row = self.inbox.db.execute('SELECT response_state,claimed_by FROM comments WHERE comment_id=?',
                                    (comment_id,)).fetchone()
        return tuple(row)

    def test_marks_unreviewed_and_expired_reviews_only(self):
        self.assertTrue(self.inbox.mark_seen('owner','1'))
        self.assertEqual(self.state('1'),('reviewing','operator'))
        held = self.inbox.unseen('owner',limit=1,worker='mnemo')[0]['comment_id']
        self.assertFalse(self.inbox.mark_seen('owner',held))
        self.assertEqual(self.state(held),('reviewing','mnemo'))
        self.inbox.db.execute("UPDATE comments SET claim_until='2000-01-01T00:00:00+00:00' WHERE comment_id=?",
                              (held,))
        self.assertTrue(self.inbox.mark_seen('owner',held))
        self.assertEqual(self.state(held),('reviewing','operator'))

    def test_never_resurrects_skipped_or_detaches_drafted_comments(self):
        self.inbox.skip('owner','4')
        with Outbox(Path(self.tmp.name)/'outbox.sqlite3') as outbox:
            job = self.inbox.draft_reply(outbox,'owner','5','A reply')
        self.assertFalse(self.inbox.mark_seen('owner','4'))
        self.assertFalse(self.inbox.mark_seen('owner','5'))
        self.assertEqual(self.state('4')[0],'skipped')
        self.assertEqual(self.state('5')[0],'drafted')
        row = self.inbox.db.execute('SELECT draft_job_id FROM comments WHERE comment_id=?',('5',)).fetchone()
        self.assertEqual(row[0],job['id'])
        self.assertFalse(self.inbox.mark_seen('owner','missing'))


if __name__ == '__main__':
    unittest.main()
