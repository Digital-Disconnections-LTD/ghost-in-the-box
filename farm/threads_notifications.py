"""Store bounded Threads replies in the same consumable inbox as Instagram."""

from __future__ import annotations

from farm.inbox import Inbox


def namespace(account: str) -> str:
    return f"threads:{account}"


def sync_threads(adapter, inbox: Inbox, account: str, *, posts: int = 3,
                 replies_per_post: int = 25) -> dict:
    if not 1 <= posts <= 10 or not 1 <= replies_per_post <= 25:
        raise ValueError("posts must be 1..10 and replies_per_post 1..25")
    key = namespace(account)
    page = adapter.own_posts(account, limit=25)
    own = [row for row in page["posts"] if row.get("id") and row.get("permalink")
           and str(row.get("username") or "").casefold() == account.casefold()]
    if not own:
        return {"own_posts_available": 0, "posts_scanned": 0, "replies_observed": 0}
    # Scan the newest post on every run; rotate the remaining slots through
    # older posts so a busy new conversation reaches the inbox promptly.
    older = own[1:]
    position = int(inbox.cursor(key, "post-rotation") or "0") % max(1, len(older))
    rotating = min(max(0, posts - 1), len(older))
    selected = [own[0]] + [older[(position + offset) % len(older)]
                           for offset in range(rotating)]
    observed = 0
    for post in selected:
        stream = f"replies-backfill:{post['id']}"
        older_cursor = inbox.cursor(key, stream)
        # Always refresh the newest page. Advance at most one historical page
        # per selected post and run, so an active post cannot starve the inbox.
        pages = [adapter.replies(account, post["id"], limit=replies_per_post)]
        if older_cursor and older_cursor != "-":
            pages.append(adapter.replies(account, post["id"],
                                         limit=replies_per_post, cursor=older_cursor))
        for response in pages:
            for reply in response["posts"]:
                if (not reply.get("id") or not reply.get("permalink") or
                        str(reply.get("username") or "").casefold() == account.casefold()):
                    continue
                inbox.save_external_comment(
                    account=key, comment_id=str(reply["id"]), media_id=str(post["id"]),
                    parent_id=str(post["id"]), url=reply["permalink"],
                    media_url=post["permalink"], author=reply.get("username"),
                    body=reply.get("body"), timestamp=reply.get("timestamp"),
                )
                observed += 1
        if not older_cursor or older_cursor != "-":
            inbox.save_cursor(key, stream, pages[-1].get("next_cursor") or "-")
    inbox.save_cursor(key,"post-rotation",str(position + rotating))
    return {"own_posts_available": len(own), "posts_scanned": len(selected),
            "replies_observed": observed}
