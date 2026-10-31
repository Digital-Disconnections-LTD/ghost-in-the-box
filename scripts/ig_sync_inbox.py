#!/usr/bin/env python3
"""Read own Instagram comment pages, list unseen comments, or create local reply drafts."""
import argparse
import json
import logging
import os
from pathlib import Path
import re
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from farm.inbox import Inbox, sync_comments
from farm.outbox import Outbox
from farm.accounts import AccountRegistry
from farm.instagram_session import read_with_session


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--account', default='example_creator')
    p.add_argument('--db')
    sub = p.add_subparsers(dest='command',required=True)
    sync = sub.add_parser('sync')
    sync.add_argument('--posts',type=int,default=3)
    sync.add_argument('--pages',type=int,default=1)
    sync.add_argument('--threads',type=int,default=0)
    sync.add_argument('--older',action='store_true')
    sync.add_argument('--feed-pages',type=int,default=0)
    listing = sub.add_parser('unseen',help='consume notifications into the review queue')
    listing.add_argument('--limit',type=int,default=20)
    listing.add_argument('--worker',default='operator')
    peek = sub.add_parser('peek',help='inspect unread count/content without consuming')
    peek.add_argument('--limit',type=int,default=20)
    pending = sub.add_parser('pending',help='comments read but still awaiting a decision')
    pending.add_argument('--limit',type=int,default=20)
    pending.add_argument('--worker')
    feed = sub.add_parser('feed')
    feed.add_argument('--limit',type=int,default=20)
    draft = sub.add_parser('draft-reply')
    draft.add_argument('--comment-id',required=True)
    draft.add_argument('--body',required=True)
    draft.add_argument('--worker')
    seen = sub.add_parser('seen')
    seen.add_argument('--comment-id',required=True)
    skip = sub.add_parser('skip',help='mark a comment as needing no response')
    skip.add_argument('--comment-id',required=True)
    skip.add_argument('--worker')
    args = p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.]{1,30}',args.account):
        p.error('invalid account')
    os.umask(0o077)
    inbox = Inbox(args.db)
    try:
        if args.command == 'sync':
            # Do not log provider exception text, headers, settings or credentials.
            logging.disable(logging.CRITICAL)
            result = read_with_session(args.account,
                lambda client: sync_comments(client,inbox,args.account,posts=args.posts,
                    pages=args.pages,older=args.older,threads=args.threads,
                    feed_pages=args.feed_pages), registry=AccountRegistry())
        elif args.command == 'unseen':
            result = inbox.unseen(args.account,args.limit,worker=args.worker)
        elif args.command == 'peek':
            result = inbox.peek(args.account,args.limit)
        elif args.command == 'pending':
            result = inbox.pending(args.account,args.limit,worker=args.worker)
        elif args.command == 'feed':
            result = inbox.feed(args.account,args.limit)
        elif args.command == 'seen':
            inbox.mark_seen(args.account,args.comment_id)
            result = {'ok':True}
        elif args.command == 'skip':
            inbox.skip(args.account,args.comment_id,worker=args.worker)
            result = {'ok':True}
        else:
            with Outbox() as outbox:
                result = inbox.draft_reply(outbox,args.account,args.comment_id,args.body,
                                           worker=args.worker)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'ok':False,'error_type':type(exc).__name__}))
        return 1
    finally:
        inbox.close()

if __name__ == '__main__':
    raise SystemExit(main())
