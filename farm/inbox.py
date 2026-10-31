"""Private local Instagram inbox. Network access lives only in the sync CLI."""

from farm.config import STATE_ROOT
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3

DEFAULT_DB = STATE_ROOT / 'inbox.sqlite3'


class Inbox:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_DB).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS comments (
            account TEXT, comment_id TEXT, media_id TEXT NOT NULL,
            parent_id TEXT, url TEXT NOT NULL, media_url TEXT NOT NULL,
            author TEXT, body TEXT, timestamp TEXT, seen INTEGER DEFAULT 0,
            PRIMARY KEY(account,comment_id));
          CREATE TABLE IF NOT EXISTS feed (
            account TEXT, media_id TEXT, url TEXT, author TEXT, body TEXT, timestamp TEXT,
            PRIMARY KEY(account,media_id));
          CREATE TABLE IF NOT EXISTS cursors (
            account TEXT, stream TEXT, cursor TEXT, PRIMARY KEY(account,stream));
          CREATE TABLE IF NOT EXISTS media_scan (
            account TEXT NOT NULL, media_id TEXT NOT NULL, code TEXT NOT NULL,
            last_scanned_at TEXT, PRIMARY KEY(account,media_id));
          CREATE TABLE IF NOT EXISTS notification_batches (
            id TEXT PRIMARY KEY, account TEXT NOT NULL, recipient TEXT NOT NULL,
            comment_ids TEXT NOT NULL, content TEXT NOT NULL,
            created_at TEXT NOT NULL, sent_at TEXT);
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(comments)')}
        if 'response_state' not in columns:
            self.db.execute("ALTER TABLE comments ADD COLUMN response_state TEXT DEFAULT 'unreviewed'")
        if 'read_at' not in columns:
            self.db.execute('ALTER TABLE comments ADD COLUMN read_at TEXT')
        if 'claimed_by' not in columns:
            self.db.execute('ALTER TABLE comments ADD COLUMN claimed_by TEXT')
        if 'claim_until' not in columns:
            self.db.execute('ALTER TABLE comments ADD COLUMN claim_until TEXT')
        if 'draft_job_id' not in columns:
            self.db.execute('ALTER TABLE comments ADD COLUMN draft_job_id TEXT')
        if 'notified_at' not in columns:
            self.db.execute('ALTER TABLE comments ADD COLUMN notified_at TEXT')

    def close(self):
        self.db.close()

    def cursor(self, account, stream):
        row = self.db.execute('SELECT cursor FROM cursors WHERE account=? AND stream=?',
                              (account, stream)).fetchone()
        return row[0] if row else ''

    def save_cursor(self, account, stream, cursor):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?,?)',
                            (account, stream, cursor or ''))

    def save_backfill_cursor(self, account, stream, cursor, *, older=False):
        """Recent refreshes must not reset a deeper historical cursor."""
        if older or not self.cursor(account,stream):
            self.save_cursor(account,stream,cursor)

    def queue_media_page(self, account, media, next_cursor, *, older=False):
        """Keep every media ID before acknowledging its server page cursor."""
        stream = 'own-posts-backfill'
        with self.db:
            for post in media:
                self.db.execute('''INSERT INTO media_scan(account,media_id,code)
                    VALUES (?,?,?) ON CONFLICT(account,media_id)
                    DO UPDATE SET code=excluded.code''',
                    (account,str(post.id),str(post.code)))
            if older or not self.cursor(account,stream):
                self.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?,?)',
                                (account,stream,next_cursor or ''))

    def media_for_scan(self, account, limit):
        return [dict(row) for row in self.db.execute('''SELECT media_id,code
            FROM media_scan WHERE account=?
            ORDER BY last_scanned_at IS NOT NULL, last_scanned_at, media_id DESC
            LIMIT ?''',(account,limit))]

    def mark_media_scanned(self, account, media_id):
        with self.db:
            self.db.execute('UPDATE media_scan SET last_scanned_at=? WHERE account=? AND media_id=?',
                            (datetime.now(timezone.utc).isoformat(),account,media_id))

    def save_comment(self, account, media_id, code, comment, parent_id=None):
        cid = str(comment.pk)
        url = f'https://www.instagram.com/p/{code}/'
        stamp = comment.created_at_utc
        stamp = stamp.isoformat() if hasattr(stamp, 'isoformat') else str(stamp)
        self.save_external_comment(account=account,comment_id=cid,media_id=str(media_id),
            parent_id=str(parent_id) if parent_id else None,
            url=f'{url}?comment_id={cid}',media_url=url,
            author=comment.user.username,body=comment.text,timestamp=stamp)

    def save_external_comment(self, *, account, comment_id, media_id, parent_id,
                              url, media_url, author, body, timestamp):
        """Upsert a source-linked comment without reopening a consumed notice."""
        if not all((account,comment_id,media_id,url,media_url)):
            raise ValueError('comment needs account, provider IDs and source URLs')
        with self.db:
            self.db.execute('''INSERT INTO comments
              (account,comment_id,media_id,parent_id,url,media_url,author,body,timestamp)
              VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(account,comment_id) DO UPDATE SET
              body=excluded.body,author=excluded.author,timestamp=excluded.timestamp''',
              (account,str(comment_id),str(media_id),str(parent_id) if parent_id else None,
               url,media_url,author,body,timestamp))

    def save_feed(self, account, item):
        media_id = str(item.get('pk') or item.get('id') or '')
        code = item.get('code')
        if not media_id or not code:
            return False
        caption = item.get('caption') or {}
        stamp = item.get('taken_at')
        if isinstance(stamp, (int,float)):
            stamp = datetime.fromtimestamp(stamp,timezone.utc).isoformat()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO feed VALUES (?,?,?,?,?,?)',
                (account,media_id,f'https://www.instagram.com/p/{code}/',
                 (item.get('user') or {}).get('username'),caption.get('text',''),stamp))
        return True

    def feed(self, account, limit=20):
        if not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM feed WHERE account=? ORDER BY timestamp DESC LIMIT ?', (account,limit))]

    def unseen(self, account, limit=20, *, worker='operator', lease_seconds=3600):
        """Claim a bounded notification batch; expired reviews return to the queue."""
        if not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        if not worker.strip() or lease_seconds < 60:
            raise ValueError('worker and lease of at least one minute are required')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            now = datetime.now(timezone.utc)
            rows = [dict(row) for row in self.db.execute(
                """SELECT * FROM comments WHERE account=? AND
                   (seen=0 OR (response_state='reviewing' AND claim_until<?))
                   ORDER BY timestamp,comment_id LIMIT ?""",
                (account,now.isoformat(),limit))]
            read_at = now.isoformat()
            until = (now + timedelta(seconds=lease_seconds)).isoformat()
            self.db.executemany('''UPDATE comments SET seen=1,response_state='reviewing',
                                  read_at=?,claimed_by=?,claim_until=?
                                  WHERE account=? AND comment_id=?''',
                                ((read_at,worker,until,account,row['comment_id']) for row in rows))
            self.db.execute('COMMIT')
            for row in rows:
                row.update(seen=1,response_state='reviewing',read_at=read_at,
                           claimed_by=worker,claim_until=until)
            return rows
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def peek(self, account, limit=20):
        if not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        return [dict(row) for row in self.db.execute(
            """SELECT * FROM comments WHERE account=? AND
               (seen=0 OR (response_state='reviewing' AND claim_until<?))
               ORDER BY timestamp,comment_id LIMIT ?""",
            (account,datetime.now(timezone.utc).isoformat(),limit))]

    def notification_batch(self, account, recipient='operator', limit=20):
        """Return one durable pending notice, or freeze a new bounded batch."""
        if not 1 <= limit <= 25 or not recipient or not account.startswith('threads:'):
            raise ValueError('Threads account, recipient and limit 1..25 required')
        with self.db:
            row = self.db.execute('''SELECT * FROM notification_batches
                WHERE account=? AND recipient=? AND sent_at IS NULL
                ORDER BY created_at,id LIMIT 1''', (account, recipient)).fetchone()
            if row:
                return dict(row)
            ids = [row['comment_id'] for row in self.db.execute('''SELECT comment_id
                FROM comments WHERE account=? AND seen=0 AND notified_at IS NULL
                ORDER BY timestamp,comment_id LIMIT ?''', (account,limit))]
            if not ids:
                return None
            digest = hashlib.sha256(json.dumps([account,recipient,ids]).encode()).hexdigest()[:32]
            batch_id = 'threadsc_' + digest
            handle = account.removeprefix('threads:')
            command = f'ghost-in-the-box threads-notifications --account {shlex.quote(handle)}'
            worker = shlex.quote(recipient)
            content = (f'Threads has {len(ids)} comment updates for @{handle}. '
                       f'Run `{command} pending --worker {worker} --limit 10`, then '
                       f'`{command} unseen --worker {worker} --limit 10`. Choose replies or run '
                       f'`{command} skip --comment-id ID --worker {worker}` for the rest. '
                       f'The inbox preserves each comment until handled.')
            created = datetime.now(timezone.utc).isoformat()
            self.db.execute('''INSERT INTO notification_batches
                (id,account,recipient,comment_ids,content,created_at)
                VALUES (?,?,?,?,?,?)''',
                (batch_id,account,recipient,json.dumps(ids),content,created))
            return {'id':batch_id,'account':account,'recipient':recipient,
                    'comment_ids':json.dumps(ids),'content':content,
                    'created_at':created,'sent_at':None}

    def mark_notification_sent(self, batch_id):
        """Acknowledge only after the worker inbox accepted the durable message."""
        with self.db:
            row = self.db.execute('SELECT * FROM notification_batches WHERE id=?',
                                  (batch_id,)).fetchone()
            if row is None:
                raise ValueError('notification batch not found')
            if row['sent_at']:
                return False
            ids = json.loads(row['comment_ids'])
            now = datetime.now(timezone.utc).isoformat()
            self.db.executemany('''UPDATE comments SET notified_at=COALESCE(notified_at,?)
                WHERE account=? AND comment_id=?''',
                ((now,row['account'],comment_id) for comment_id in ids))
            self.db.execute('UPDATE notification_batches SET sent_at=? WHERE id=?',
                            (now,batch_id))
            return True

    def pending(self, account, limit=20, *, worker=None):
        if not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        where = "account=? AND response_state='reviewing'"
        params = [account]
        if worker:
            where += ' AND claimed_by=?'
            params.append(worker)
        return [dict(row) for row in self.db.execute(
            f'SELECT * FROM comments WHERE {where} ORDER BY read_at,comment_id LIMIT ?',
            (*params,limit))]

    def skip(self, account, comment_id, *, worker=None):
        with self.db:
            where = "account=? AND comment_id=? AND response_state IN ('unreviewed','reviewing')"
            params = [account,str(comment_id)]
            if worker:
                where += " AND claimed_by=? AND response_state='reviewing'"
                params.append(worker)
            cursor = self.db.execute('''UPDATE comments SET seen=1,response_state='skipped',
                read_at=COALESCE(read_at,?),claim_until=NULL WHERE ''' + where,
                (datetime.now(timezone.utc).isoformat(),*params))
            if cursor.rowcount != 1:
                raise ValueError('comment not held by this worker')

    def mark_seen(self, account, comment_id):
        with self.db:
            now = datetime.now(timezone.utc)
            self.db.execute("""UPDATE comments SET seen=1,response_state='reviewing',
                read_at=?,claimed_by='operator',claim_until=? WHERE account=? AND comment_id=?""",
                (now.isoformat(),(now+timedelta(hours=1)).isoformat(),account,str(comment_id)))

    def draft_reply(self, outbox, account, comment_id, body, *, worker=None,
                    platform='instagram', profile_account=None):
        if platform not in ('instagram','threads'):
            raise ValueError('this inbox supports Instagram and Threads replies')
        provider_account = profile_account or account
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT * FROM comments WHERE account=? AND comment_id=?',
                                  (account,str(comment_id))).fetchone()
            if row is None:
                raise ValueError('comment not found in this account inbox')
            if row['response_state'] == 'skipped':
                raise ValueError('comment was marked as needing no reply')
            if row['draft_job_id']:
                if worker and row['claimed_by'] != worker:
                    raise ValueError('comment not held by this worker')
                existing = outbox.get(row['draft_job_id'])
                if existing and existing['body'] == body:
                    self.db.execute('COMMIT')
                    return existing
                raise ValueError('this comment already has a different reply draft')
            if worker and (row['claimed_by'] != worker or row['response_state'] != 'reviewing'):
                raise ValueError('comment not held by this worker')
            job = outbox.create_draft(platform=platform, account=provider_account, action='reply',
                body=body,media_id=row['media_id'] if platform == 'instagram' else row['comment_id'],
                target_id=row['comment_id'],target_url=row['url'],
                source_urls=[row['url'],row['media_url']],
                idempotency_key=f'{platform}-inbox:{provider_account}:{comment_id}:reply')
            self.db.execute("""UPDATE comments SET seen=1,response_state='drafted',
                draft_job_id=?,claim_until=NULL WHERE account=? AND comment_id=?""",
                (job['id'],account,str(comment_id)))
            self.db.execute('COMMIT')
            return job
        except Exception:
            self.db.execute('ROLLBACK')
            raise


def sync_comments(client, inbox, account, *, posts=3, pages=1, older=False, threads=0, feed_pages=0):
    """At most one media page, posts*pages comment pages and posts*pages*threads reply pages.

    Fresh runs revisit recent comments. --older resumes saved server cursors.
    Each complete server page is retained so saving its cursor cannot skip records.
    """
    if not 1 <= posts <= 10 or not 1 <= pages <= 3 or not 0 <= threads <= 5 or not 0 <= feed_pages <= 3:
        raise ValueError('posts=1..10, pages=1..3, threads=0..5, feed_pages=0..3 required')
    # The installed helper slices its returned list to `amount` but advances to
    # the provider's whole-page cursor. Ask for the whole page, then durably
    # queue every media ID before moving the cursor or scanning a bounded subset.
    media, cursor = client.user_medias_paginated_v1(client.user_id, amount=33,
        end_cursor=inbox.cursor(account,'own-posts-backfill') if older else '')
    if len(media) > 33:
        raise ValueError('media page exceeded expected bound')
    for post in media:
        if str(post.user.pk) != str(client.user_id):
            raise ValueError('refusing comments on media not owned by connected account')
    inbox.queue_media_page(account,media,cursor,older=older)
    count = 0
    selected = inbox.media_for_scan(account,posts)
    for post in selected:
        stream = f'comments:{post["media_id"]}'
        next_id = inbox.cursor(account,stream) if older else ''
        visited = set()
        for _ in range(pages):
            comments, _, new_id = client.media_comments_v1_chunk(post['media_id'],max_id=next_id)
            for comment in comments:
                inbox.save_comment(account,post['media_id'],post['code'],comment)
                count += 1
            inbox.save_backfill_cursor(account,stream,new_id,older=older)
            for comment in comments[:threads]:
                reply_stream = f'replies:{post["media_id"]}:{comment.pk}'
                replies, reply_cursor = client.media_comment_replies_chunk(post['media_id'],str(comment.pk),
                    max_amount=100,min_id=inbox.cursor(account,reply_stream) if older else None)
                for reply in replies:
                    inbox.save_comment(account,post['media_id'],post['code'],reply,parent_id=comment.pk)
                    count += 1
                inbox.save_backfill_cursor(account,reply_stream,reply_cursor,older=older)
            if not new_id or new_id in visited:
                break
            visited.add(new_id)
            next_id = new_id
        inbox.mark_media_scanned(account,post['media_id'])
    feed_count = 0
    cursor = inbox.cursor(account,'timeline') if older else ''
    visited = set()
    for _ in range(feed_pages):
        result = client.get_timeline_feed(max_id=cursor or None,seen_posts=[],feed_view_info=[])
        items = list(result.get('items') or [])
        items.extend(entry['media_or_ad'] for entry in result.get('feed_items',[])
                     if isinstance(entry,dict) and isinstance(entry.get('media_or_ad'),dict))
        for item in items:
            if isinstance(item,dict):
                feed_count += int(inbox.save_feed(account,item))
        new_cursor = result.get('next_max_id') or ''
        inbox.save_backfill_cursor(account,'timeline',new_cursor,older=older)
        if not new_cursor or new_cursor in visited:
            break
        visited.add(new_cursor)
        cursor = new_cursor
    return {'comments_observed':count,'posts_observed':len(selected),
            'media_page_items':len(media),'feed_observed':feed_count}
