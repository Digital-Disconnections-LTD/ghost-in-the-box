"""Instagram tools backed by the saved session, inbox and durable outbox.

The stdio transport, protocol negotiation and catalog helpers are shared with the
other servers in farm.mcp_server. No provider objects, credentials, session files
or raw provider errors cross it.
"""

from __future__ import annotations

import ast
from contextlib import closing, redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import re
import uuid

from farm import config
from farm.accounts import AccountRegistry
from farm.adapters.instagram import InstagramAdapter
from farm.inbox import Inbox, sync_comments
from farm.instagram_session import InstagramSessionError, read_with_session
from farm.instagram_references import (
    DEFAULT_ROOT as REFERENCE_ROOT, ReferenceError, ReferenceStore, canonical_post_url, fetch_image,
)
from farm.outbox import JOB_STATES as OUTBOX_JOB_STATES, Outbox, utc_text, utc_now
from scripts.instagram_carousel import ROOT as CAROUSEL_ROOT, create_draft as carousel_draft
from scripts import meme_library
from scripts.social_publish_job import run as publish_exact_job
from farm import mcp_server
from farm.mcp_server import ToolInputError, boolean, choice, integer, string


MEME_ROOT = meme_library.ROOT
# The protocol and transport live in farm.mcp_server; these names stay importable from here.
SUPPORTED_PROTOCOL_VERSIONS = mcp_server.SUPPORTED_PROTOCOL_VERSIONS
LATEST_PROTOCOL_VERSION = mcp_server.LATEST_PROTOCOL_VERSION
BATCH_PROTOCOL_VERSIONS = mcp_server.BATCH_PROTOCOL_VERSIONS
MAX_MESSAGE_BYTES = mcp_server.MAX_MESSAGE_BYTES
AccountSelectionError = mcp_server.AccountSelectionError
negotiate_protocol = mcp_server.negotiate_protocol
server_version = mcp_server.server_version
validate = mcp_server.validate
read_message = mcp_server.read_message
write_message = mcp_server.write_message
catalog_markdown = mcp_server.catalog_markdown

SERVER_NAME = "ghost-in-the-box-instagram"
SERVER_TITLE = "Ghost in the Box: Instagram"
JOB_STATES = list(OUTBOX_JOB_STATES)
ACCOUNT_PATTERN = r"[A-Za-z0-9_.]{1,30}"
# Handles that appear in docs and examples; they are refused unless really registered.
PLACEHOLDER_ACCOUNTS = frozenset({"example_creator", "your_handle"})
SUBSTACK_SERVER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "substack_mcp_server.py"


def login_hint(account):
    return f"sign in with: ghost-in-the-box login start --platform instagram --account {account}"


INSTALL_HINT = "install the Instagram extra: " + config.install_command("instagram")


def is_placeholder(account):
    return account.casefold() in PLACEHOLDER_ACCOUNTS


def resolve_account(explicit=None, *, environ=None, registry_factory=AccountRegistry):
    """Pick the configured account: --account, SOCIAL_FARM_IG_ACCOUNT, else the only registered one."""
    return mcp_server.select_account(
        explicit, platform="instagram", label="Instagram", env_var="SOCIAL_FARM_IG_ACCOUNT",
        environ=environ, registry_factory=registry_factory,
        valid_handle=lambda handle: re.fullmatch(ACCOUNT_PATTERN, handle) is not None,
        invalid_message="--account must be an Instagram handle: 1-30 letters, digits, '.' or '_'",
        invalid_hint="pass the handle without '@', for example --account my_handle",
        placeholders=PLACEHOLDER_ACCOUNTS, login_hint=login_hint)


JOB_ID = string("Exact Instagram outbox job ID.", maximum=100)
MEDIA_ID = string("Instagram media PK or numeric composite media ID.", maximum=100,
                  pattern=r"^[0-9]+(?:_[0-9]+)?$")
POST_URL = string("Canonical HTTPS Instagram /p/, /reel/ or /tv/ URL; supply this or media_id.")
HASHTAG = string("One topic hashtag, for example artificialintelligence, softwareengineering or programming.",
                 maximum=81, pattern=r"^#?[A-Za-z0-9_]{1,80}$")
COMMENT_ID = string("Numeric Instagram comment ID.", maximum=100, pattern=r"^[0-9]+$")
WORKER = string("Your stable worker name; use the same name when drafting or skipping.", maximum=100)
CURSOR = string("Opaque cursor from the previous page; omit for the first page.", minimum=0)


def tool(name, title, description, properties=None, required=(), *, read_only=False, external=False,
         destructive=False):
    return mcp_server.make_tool("instagram", name, title, description, properties, required,
                                read_only=read_only, external=external, destructive=destructive)


TOOLS = [
    tool("status", "Instagram account status",
         "Check configured Instagram account, local session readiness and job counts. "
         "live_check optionally validates the saved login; no credentials are returned.",
         {"live_check": boolean("Validate the saved session with Instagram.")}, read_only=True, external=True),
    tool("own_posts", "Read own Instagram posts",
         "Read one complete page (up to 33) of the connected account's own Instagram posts. "
         "Returns captions, IDs, links and a continuation cursor.", {"cursor": CURSOR}, read_only=True, external=True),
    tool("media", "Read an Instagram post",
         "Read any accessible Instagram post's caption, author and slide URLs. "
         "Supply exactly one of media_id or url. Use download_media for local images and inspect_slide to see them.",
         {"media_id": MEDIA_ID, "url": POST_URL}, read_only=True, external=True),
    tool("discover", "Discover posts by hashtag",
         "Discover topic posts with one bounded Instagram hashtag page using the saved session. "
         "Try artificialintelligence, machinelearning, softwareengineering, programming or developerlife. "
         "Returns up to limit posts and saves their metadata for references/curate. Does not download images.",
         {"hashtag": HASHTAG, "sort": choice("Provider hashtag ranking.", ["top", "recent"], "top"),
          "limit": integer("Maximum posts returned and saved from one provider page.", 12, 1, 30)},
         ["hashtag"], external=True),
    tool("references", "Browse saved research posts",
         "Browse saved Instagram research candidates and downloaded image paths locally. "
         "Selections and notes survive later discovery refreshes. This store is separate from the meme library.",
         {"state": choice("Curation filter.", ["new", "selected", "skipped", "all"], "new"),
          "hashtag": HASHTAG, "query": string("Literal caption or author text filter.", maximum=200),
          "limit": integer("Maximum saved posts.", 20, 1, 50),
          "offset": integer("Continue at next_offset from the previous result.", 0, 0, 10000)}, read_only=True),
    tool("curate", "Curate a saved research post",
         "Mark a saved Instagram research candidate new, selected or skipped and retain a note. "
         "Changes only the private local reference store.",
         {"media_id": MEDIA_ID, "state": choice("Curation state.", ["new", "selected", "skipped"]),
          "note": string("Why this post is useful or should be skipped.", minimum=0, maximum=2000, default="")},
         ["media_id", "state"]),
    tool("download_media", "Download post images",
         "Download images from one Instagram post or carousel, including other authors' posts. "
         "Supply exactly one of media_id or url. Returns ordered absolute slide paths, source URL, author, "
         "caption and IDs. Video slides yield preview images. Private local snapshots are reused; images are "
         "limited to 8 MB each, 64 MB and at most 20 slides per call. Follow next_start_index for remaining slides.",
         {"media_id": MEDIA_ID, "url": POST_URL,
          "max_images": integer("Maximum images for this call.", 10, 1, 20),
          "start_index": integer("One-based slide index to start or resume.", 1, 1, 100)}, external=True),
    tool("inspect_slide", "Inspect a downloaded image",
         "See one downloaded Instagram image using the existing local vision model. "
         "Reads visible words and explains the visual in a concise answer. Accepts only an unchanged absolute "
         "image path returned by download_media; image bytes stay on this machine.",
         {"image_path": string("Absolute downloaded slide path."),
          "question": string("What to examine in the image.", maximum=2000,
             default="Describe what is visually happening, read the visible words, and explain the joke or argument in three concise sentences."),
          "max_tokens": integer("Maximum local-model output tokens.", 500, 64, 1000)},
         ["image_path"], read_only=True),
    tool("search_memes", "Search the meme library",
         "Search the cached technology meme library by topic and words in titles or images. "
         "Returns IDs, local image paths, source links, observed engagement and short OCR excerpts. "
         "Use IDs with prepare_meme_carousel; use next_offset for more matches. Reads only local files.",
         {"query": string("Words to find in titles and OCR text, for example Claude or debugging.", minimum=0, maximum=200, default=""),
          "category": choice("Optional topic folder.", list(meme_library.PRIMARY_ORDER)),
          "limit": integer("Maximum memes returned.", 8, 1, 20),
          "offset": integer("Continue at next_offset from a previous result.", 0, 0, 10000),
          "min_score": integer("Minimum recorded native engagement count; scores and likes are labeled.", 0, 0, 1000000000)},
         read_only=True),
    tool("import_meme", "Add an image to the meme library",
         "Add one image already returned by download_media to the categorized meme library. "
         "Source URL, author, caption and engagement are read from its saved download record automatically. "
         "Deduplicates, OCRs and indexes the image, then returns its library ID and cached path. Local files only.",
         {"image_path": string("Unchanged absolute image path returned by download_media.")}, ["image_path"]),
    tool("prepare_meme_carousel", "Prepare meme carousel slides",
         "Prepare 2–10 cached meme IDs or paths as ordered 1080×1350 JPEG slides. "
         "Pads without cropping and preserves originals. Returns images and source_urls to pass directly "
         "to draft_carousel with your caption and idempotency_key. Creates local files; does not draft or publish.",
         {"items": {"type": "array", "minItems": 2, "maxItems": 10,
                    "items": string("Meme ID or cached image path from search_memes or import_meme.")}}, ["items"]),
    tool("comments", "Read comments on own post",
         "Read one complete comment page on your own Instagram media and retain it in the inbox. "
         "Supply comment_id to read that comment's replies. Returned cursor continues the same stream.",
         {"media_id": MEDIA_ID, "comment_id": COMMENT_ID, "cursor": CURSOR},
         ["media_id"], external=True),
    tool("sync_inbox", "Refresh the comment inbox",
         "Refresh a bounded selection of own-media comments and optional feed. "
         "Uses durable cursors; older continues historical scans. No comments are marked reviewed.",
         {"posts": integer("Own posts to scan.", 3, 1, 10),
          "pages": integer("Comment pages per post.", 1, 1, 3),
          "threads": integer("Parent comments whose reply pages to scan per page.", 0, 0, 5),
          "feed_pages": integer("Timeline pages to retain.", 0, 0, 3),
          "older": boolean("Continue saved historical cursors.")}, external=True),
    tool("inbox", "Read the cached inbox",
         "Read cached unread comments, pending reviews, or feed items without consuming them. "
         "Call sync_inbox for fresh provider data.",
         {"view": choice("Cached view.", ["unread", "pending", "feed"], "unread"),
          "limit": integer("Maximum records.", 20, 1, 100), "worker": WORKER}, read_only=True),
    tool("claim_comments", "Claim comments for review",
         "Claim unread comments for review for one hour. "
         "Use the worker name for draft_reply and skip_comment; expired claims become available again.",
         {"worker": WORKER, "limit": integer("Maximum comments.", 10, 1, 100)}, ["worker"]),
    tool("skip_comment", "Skip a claimed comment",
         "Finish a comment review claimed with claim_comments without replying. "
         "Use the same worker name that claimed it.",
         {"comment_id": COMMENT_ID, "worker": WORKER}, ["comment_id", "worker"]),
    tool("draft_reply", "Draft a comment reply",
         "Save the exact reply text for a claimed inbox comment. "
         "Returns one durable draft per comment; repeat the same text to recover the same job. Does not publish.",
         {"comment_id": COMMENT_ID, "text": string("Exact reply text.", maximum=2200), "worker": WORKER},
         ["comment_id", "text", "worker"]),
    tool("draft_carousel", "Draft a carousel post",
         "Snapshot 2–10 ordered local images and an exact caption into one Instagram draft. "
         "Reuse the same idempotency_key for retries; changed content with that key is rejected. "
         "Then queue_job and publish_job using the returned job ID.",
         {"caption": string("Exact caption, including line breaks.", minimum=0, maximum=2200),
          "images": {"type": "array", "minItems": 2, "maxItems": 10,
                     "items": string("Absolute path to a local JPG, PNG or WebP slide.")},
          "idempotency_key": string("Stable unique key for this intended post.", maximum=200),
          "crosspost_threads": boolean("Request Instagram's linked Threads cross-post on this upload."),
          "source_urls": {"type": "array", "maxItems": 30,
                          "items": string("Source URL retained with the draft.")}},
         ["caption", "images", "idempotency_key"]),
    tool("queue_job", "Queue a draft job",
         "Freeze and queue one exact Instagram draft. Does not send. "
         "Publishers are not started by this server; publish_job explicitly sends the selected job.",
         {"job_id": JOB_ID, "at": string("Optional ISO 8601 schedule with timezone; default now.", maximum=100)},
         ["job_id"]),
    tool("publish_job", "Publish a queued job",
         "Publish exactly one queued Instagram job now, including a future-scheduled job. "
         "Repeated calls for a published job return its receipt. Ambiguous sends become uncertain and cannot "
         "be resent until reconciled. Auth failures preserve the queue. This makes a public post or reply.",
         {"job_id": JOB_ID}, ["job_id"], external=True, destructive=True),
    tool("jobs", "List Instagram jobs",
         "List bounded Instagram job summaries for this configured account.",
         {"status": choice("Job status filter.", ["active", "all", *JOB_STATES], "active"),
          "limit": integer("Maximum jobs.", 20, 1, 100)}, read_only=True),
    tool("job", "Inspect one Instagram job",
         "Inspect one Instagram job's exact text, frozen assets, target and durable outcome. "
         "Lease tokens and session settings are omitted.", {"job_id": JOB_ID}, ["job_id"], read_only=True),
    tool("reconcile_job", "Reconcile an uncertain job",
         "Resolve an uncertain Instagram job using observed evidence. "
         "For published, supply the observed provider_id: the server verifies the account and exact text "
         "against Instagram before recording it. For queued or failed, reason must explain direct evidence "
         "that no public write happened; a missing item in a bounded read is not proof. "
         "Requeue only when that evidence rules out a duplicate. Does not send.",
         {"job_id": JOB_ID, "outcome": choice("Evidence-supported outcome.", ["published", "queued", "failed"]),
          "reason": string("Concrete reconciliation evidence.", maximum=4000), "provider_id": MEDIA_ID},
         ["job_id", "outcome", "reason"], external=True),
    tool("retry_job", "Retry a failed job",
         "Requeue a failed-before-send Instagram job after correcting the failure. "
         "Requires unchanged frozen content and a reason. Does not send and cannot retry uncertain jobs.",
         {"job_id": JOB_ID, "reason": string("What corrected the pre-send failure.", maximum=4000)},
         ["job_id", "reason"]),
]
TOOL_BY_NAME = {item["name"]: item for item in TOOLS}


RETRY_PAGE_HINT = "nothing was saved and no cursor advanced; call again later with the same arguments"
RECEIPT_HINT = ("the job stays uncertain; confirm the provider_id on the account's own post or reply, "
                "then call instagram_reconcile_job again")


def _new_client():
    try:
        from instagrapi import Client
    except ImportError:
        raise InstagramSessionError("dependency_unavailable") from None
    client = Client()
    client.with_challenge_flow = False
    client.delay_range = [2, 5]
    # An MCP process must never consume protocol input as an interactive code.
    def no_prompt(*_args, **_kwargs):
        raise InstagramSessionError("checkpoint_required")
    client.challenge_code_handler = no_prompt
    client.change_password_handler = no_prompt
    return client


def _stamp(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _media(item):
    user = getattr(item, "user", None)
    code = str(item.code)
    surface = "reel" if getattr(item, "product_type", "") == "clips" else "p"
    return {"media_id": str(item.id), "provider_id": str(item.pk), "code": code,
            "url": f"https://www.instagram.com/{surface}/{code}/",
            "author": getattr(user, "username", ""), "author_id": str(getattr(user, "pk", "")),
            "caption": getattr(item, "caption_text", ""), "taken_at": _stamp(getattr(item, "taken_at", None)),
            "media_type": getattr(item, "media_type", None),
            "like_count": getattr(item, "like_count", None), "comment_count": getattr(item, "comment_count", None),
            "image_url": str(getattr(item, "thumbnail_url", "") or ""),
            "slides": [{"provider_id": str(getattr(slide, "pk", "")),
                        "media_type": getattr(slide, "media_type", None),
                        "image_url": str(getattr(slide, "thumbnail_url", "") or ""),
                        "video_url": str(getattr(slide, "video_url", "") or "")}
                       for slide in (getattr(item, "resources", None) or [])]}


def _job_summary(job, *, detail=False):
    fields = ["id", "platform", "account", "action", "status", "scheduled_at", "attempts",
              "provider_id", "permalink", "created_at", "updated_at"]
    if detail:
        fields += ["body", "media_path", "media_sha256", "body_sha256", "media_id", "target_id",
                   "target_url", "source_urls", "crosspost_threads", "lease_until"]
    return {key: job[key] for key in fields}


class InstagramTools:
    def __init__(self, *, account, outbox_path=None, inbox_path=None,
                 carousel_root=CAROUSEL_ROOT, registry=None, client_factory=None,
                 reader=None, adapter_factory=None, reference_root=REFERENCE_ROOT,
                 image_fetcher=None, vision_inspector=None, meme_root=MEME_ROOT):
        if not isinstance(account, str) or not re.fullmatch(ACCOUNT_PATTERN, account):
            raise ToolInputError("invalid configured account", code="invalid_account")
        self.account = account
        self.outbox_path = outbox_path
        self.inbox_path = inbox_path
        self.carousel_root = Path(carousel_root)
        self.reference_root = Path(reference_root)
        self.meme_root = Path(meme_root).expanduser().resolve()
        self.image_fetcher = image_fetcher or fetch_image
        self.vision_inspector = vision_inspector
        self.registry = registry or AccountRegistry()
        self.client_factory = client_factory or _new_client
        self.reader = reader or read_with_session
        self.adapter_factory = adapter_factory or (lambda: InstagramAdapter(
            self.registry, client_factory=self.client_factory, session_refresh=self._refresh))

    def box(self):
        return Outbox(self.outbox_path)

    def _inbox(self):
        return closing(Inbox(self.inbox_path))

    def _refresh(self):
        from farm import threads_auth
        inputs = threads_auth.load_inputs()
        entry = self.registry.get("instagram", self.account)
        if (not entry or inputs.username.casefold() != self.account.casefold() or
                inputs.session_path.resolve() != Path(entry["session_file"]).resolve()):
            raise InstagramSessionError("account_mismatch")
        threads_auth.login(inputs, force_refresh=True, client_factory=self.client_factory)

    def read(self, operation):
        return self.reader(self.account, operation, registry=self.registry,
                           client_factory=self.client_factory, refresh=self._refresh)

    def selected(self, box, job_id):
        job = box.get(job_id)
        if not job or job["platform"] != "instagram" or job["account"] != self.account:
            raise ToolInputError("Instagram job not found for the configured account", code="job_not_found",
                                 hint="list this account's job IDs with instagram_jobs")
        return job

    def call(self, name, arguments):
        if name not in TOOL_BY_NAME:
            raise ToolInputError("unknown Instagram tool", code="unknown_tool",
                                 hint="call tools/list for the available names")
        self.require_real_account()
        args = mcp_server.prepare_call(TOOL_BY_NAME, name, arguments, unknown="unknown Instagram tool")
        return getattr(self, name.removeprefix("instagram_"))(**args)

    def require_real_account(self):
        # A copied example config must not quietly read and write an empty fake account.
        if is_placeholder(self.account) and self.registry.get("instagram", self.account) is None:
            raise ToolInputError(f"'{self.account}' is the documentation placeholder, not a registered account",
                code="placeholder_account",
                hint="restart the server with --account set to your own registered Instagram handle")

    def status(self, live_check=False):
        entry = self.registry.status("instagram", self.account)
        available = bool(entry and entry["kind"] == "instagrapi_settings" and
                         entry["session_exists"] and entry["session_private"] and
                         not Path(entry["session_file"]).is_symlink())
        with self.box() as box:
            counts = {row["status"]: row["count"] for row in box.db.execute(
                "SELECT status,COUNT(*) AS count FROM jobs WHERE platform='instagram' AND account=? GROUP BY status",
                (self.account,))}
        result = {"ok": True, "account": self.account, "session_ready": available,
                  "live_checked": False, "jobs": counts,
                  "publishing": "explicit queued job IDs only"}
        if not available:
            result["hint"] = login_hint(self.account)
        if live_check:
            result["user_id"] = self.read(lambda client: str(client.user_id))
            result["live_checked"] = True
        return result

    def own_posts(self, cursor=""):
        def operation(client):
            client.last_json = {}
            items, next_cursor = client.user_medias_paginated_v1(client.user_id, amount=33, end_cursor=cursor)
            response = client.last_json
            if (not isinstance(response, dict) or response.get("status") != "ok" or
                    not isinstance(response.get("items"), list) or len(items) > 33 or
                    len(response["items"]) != len(items)):
                raise ToolInputError("Instagram media page was not complete; cursor was not advanced",
                                     code="provider_incomplete", hint=RETRY_PAGE_HINT)
            own = [item for item in items if str(item.user.pk) == str(client.user_id)]
            return {"ok": True, "account": self.account, "posts": [_media(item) for item in own],
                    "other_authors_skipped": len(items) - len(own), "next_cursor": next_cursor or ""}
        return self.read(operation)

    def _read_media(self, media_id=None, url=None):
        if bool(media_id) == bool(url):
            raise ToolInputError("supply exactly one of media_id or url")
        canonical = canonical_post_url(url) if url else None
        def operation(client):
            pk = client.media_pk_from_url(canonical) if canonical else media_id.split("_")[0]
            item = client.media_info_v1(pk)
            if str(item.pk) != str(pk):
                raise ToolInputError("Instagram returned a different media ID", code="provider_mismatch",
                                     hint="check the media_id or URL and call again")
            return _media(item)
        return self.read(operation)

    def media(self, media_id=None, url=None):
        return {"ok": True, "media": self._read_media(media_id, url)}

    def discover(self, hashtag, sort="top", limit=12):
        tag = hashtag.lstrip("#").lower()
        def operation(client):
            client.last_json = {}
            items, _ = client.hashtag_medias_v1_chunk(tag, max_amount=limit, tab_key=sort, max_id=None)
            response = client.last_json
            if (not isinstance(response, dict) or response.get("status") != "ok" or
                    not isinstance(response.get("sections"), list) or len(items) > limit):
                raise ToolInputError("Instagram did not acknowledge a bounded hashtag page", code="provider_incomplete", hint=RETRY_PAGE_HINT)
            return [_media(item) for item in items]
        records = self.read(operation)
        with ReferenceStore(self.account, self.reference_root) as store:
            saved = [store.remember(record, hashtag=tag, mode=sort) for record in records]
        return {"ok": True, "hashtag": tag, "sort": sort, "posts": saved,
                "saved_count": len(saved), "coverage": "bounded first provider page"}

    def references(self, state="new", hashtag=None, query=None, limit=20, offset=0):
        with ReferenceStore(self.account, self.reference_root) as store:
            result = store.list(state=state, hashtag=hashtag.lstrip("#").lower() if hashtag else None,
                                query=query, limit=limit, offset=offset)
        return {"ok": True, **result}

    def curate(self, media_id, state, note=""):
        with ReferenceStore(self.account, self.reference_root) as store:
            post = store.curate(media_id.split("_")[0], state, note)
        return {"ok": True, "post": post}

    def download_media(self, media_id=None, url=None, max_images=10, start_index=1):
        record = self._read_media(media_id, url)
        with ReferenceStore(self.account, self.reference_root) as store:
            return store.download(record, max_images=max_images, start_index=start_index, fetcher=self.image_fetcher)

    def inspect_slide(self, image_path, question, max_tokens=500):
        with ReferenceStore(self.account, self.reference_root) as store:
            image = store.verified_image(image_path)
            post = store.get(image["media_pk"])
        inspector = self.vision_inspector
        if inspector is None:
            import requests
            from scripts.vision_inspect import inspect
            with requests.Session() as session:
                session.trust_env = False
                result = inspect(Path(image["path"]), question, max_tokens=max_tokens, session=session)
        else:
            result = inspector(Path(image["path"]), question, max_tokens=max_tokens)
        return {**result, "source_url": post["url"], "author": post["author"],
                "provider_id": image["media_pk"], "slide_index": image["slide_index"],
                "slide_id": image["slide_id"]}

    def search_memes(self, query="", category=None, limit=8, offset=0, min_score=0):
        matches = meme_library.search(self.meme_root, query, category, offset + limit + 1, min_score)
        items = []
        for row in matches[offset:offset + limit]:
            item = {key: row[key] for key in ("id", "categories", "score", "score_kind", "likes", "comments", "image")}
            item.update(title=row["title"][:300], ocr_excerpt=row.get("ocr_text", "")[:600],
                        source_urls=row["source_urls"][:3], source_count=len(row["source_urls"]))
            items.append(item)
        catalog = self.meme_root / "catalog.json"
        summary = json.loads(catalog.read_text(encoding="utf-8")) if catalog.is_file() else {}
        return {"ok": True, "memes": items, "library_images": summary.get("images", 0),
                "category_counts": {name: summary.get("category_counts", {}).get(name, 0)
                                    for name in meme_library.PRIMARY_ORDER},
                "next_offset": offset + len(items) if len(matches) > offset + limit else None}

    def import_meme(self, image_path):
        with ReferenceStore(self.account, self.reference_root) as store:
            image = store.verified_image(image_path)
            post = store.get(image["media_pk"])
        if post["media_type"] == 8:
            original_url = next((slide.get("image_url") for slide in post["slides"]
                                 if str(slide.get("provider_id")) == image["slide_id"]), None)
        else:
            original_url = post.get("image_url")
        def count_or_unknown(value):
            return value if type(value) is int and value >= 0 else None
        metadata = {"source_url": canonical_post_url(post["url"]), "image_url": original_url,
                    "author": post["author"], "caption": post["caption"], "published": post.get("taken_at"),
                    "likes": count_or_unknown(post.get("like_count")),
                    "comments": count_or_unknown(post.get("comment_count")), "platform": "instagram"}
        try:
            result = meme_library.import_local(self.meme_root, Path(image["path"]), metadata)
        except ValueError:
            raise ToolInputError("downloaded image could not be imported; use a supported image within the library bounds",
                                 code="import_rejected") from None
        urls = result["source_urls"]
        return {"ok": True, **result, "source_urls": urls[:30], "source_count": len(urls),
                "provider_id": image["media_pk"], "slide_index": image["slide_index"]}

    def prepare_meme_carousel(self, items):
        destination = self.carousel_root / "prepared" / self.account / uuid.uuid4().hex
        try:
            result = meme_library.prepare_carousel(self.meme_root, items, destination)
        except ValueError:
            raise ToolInputError("select 2–10 different cached meme IDs or paths returned by search_memes or import_meme") from None
        urls = result["source_urls"]
        return {"ok": True, **result, "source_urls": urls[:30], "source_count": len(urls),
                "source_urls_truncated": len(urls) > 30}

    def comments(self, media_id, comment_id=None, cursor=""):
        def operation(client):
            post = client.media_info_v1(media_id.split("_")[0])
            if str(post.user.pk) != str(client.user_id):
                raise ToolInputError("comment inbox reads require the connected account's own media",
                                     code="not_own_media", hint="pick a media_id from instagram_own_posts")
            client.last_json = {}
            if comment_id:
                items, next_cursor = client.media_comment_replies_chunk(str(post.id), comment_id,
                    max_amount=100, min_id=cursor or None)
                field = "child_comments"
            else:
                items, _, next_cursor = client.media_comments_v1_chunk(str(post.id), max_id=cursor)
                field = "comments"
            response = client.last_json
            if (not isinstance(response, dict) or response.get("status") != "ok" or
                    not isinstance(response.get(field), list) or len(items) > 100 or
                    len(response[field]) != len(items)):
                raise ToolInputError("Instagram comment page was not complete; cursor was not advanced",
                                     code="provider_incomplete", hint=RETRY_PAGE_HINT)
            with self._inbox() as inbox:
                for item in items:
                    inbox.save_comment(self.account, str(post.id), str(post.code), item, parent_id=comment_id)
            base = _media(post)["url"]
            return {"ok": True, "media_id": str(post.id), "parent_comment_id": comment_id,
                    "comments": [{"comment_id": str(item.pk), "author": item.user.username,
                                  "body": item.text, "timestamp": _stamp(item.created_at_utc),
                                  "url": f"{base}?comment_id={item.pk}"} for item in items],
                    "next_cursor": next_cursor or ""}
        return self.read(operation)

    def sync_inbox(self, posts=3, pages=1, threads=0, feed_pages=0, older=False):
        with self._inbox() as inbox:
            result = self.read(lambda client: sync_comments(client, inbox, self.account,
                posts=posts, pages=pages, threads=threads, feed_pages=feed_pages, older=older))
        return {"ok": True, **result}

    def inbox(self, view="unread", limit=20, worker=None):
        with self._inbox() as inbox:
            if view == "pending":
                rows = inbox.pending(self.account, limit, worker=worker)
            elif view == "feed":
                rows = inbox.feed(self.account, limit)
            else:
                rows = inbox.peek(self.account, limit)
        return {"ok": True, "view": view, "items": rows}

    def claim_comments(self, worker, limit=10):
        with self._inbox() as inbox:
            rows = inbox.unseen(self.account, limit, worker=worker)
        return {"ok": True, "comments": rows}

    def skip_comment(self, comment_id, worker):
        with self._inbox() as inbox:
            inbox.skip(self.account, comment_id, worker=worker)
        return {"ok": True, "comment_id": comment_id, "status": "skipped"}

    def draft_reply(self, comment_id, text, worker):
        with self._inbox() as inbox, self.box() as box:
            job = inbox.draft_reply(box, self.account, comment_id, text, worker=worker)
        return {"ok": True, "job": _job_summary(job, detail=True)}

    def draft_carousel(self, caption, images, idempotency_key, crosspost_threads=False, source_urls=None):
        if any(not Path(path).is_absolute() for path in images):
            raise ToolInputError("carousel images must use absolute local paths")
        job = carousel_draft(account=self.account, body=caption, images=[Path(path) for path in images],
            crosspost_threads=crosspost_threads, source_urls=source_urls,
            root=self.carousel_root, box_factory=self.box,
            idempotency_key=f"instagram-mcp:carousel:{self.account}:{idempotency_key}")
        return {"ok": True, "job": _job_summary(job, detail=True), "slides": len(images)}

    def queue_job(self, job_id, at=None):
        with self.box() as box:
            self.selected(box, job_id)
            job = box.queue(job_id, at=at)
        return {"ok": True, "job": _job_summary(job, detail=True)}

    def publish_job(self, job_id):
        with self.box() as box:
            job = self.selected(box, job_id)
            if job["status"] == "published":
                return {"ok": True, "status": "published", "already_published": True,
                        "job_id": job_id, "provider_id": job["provider_id"], "permalink": job["permalink"]}
        # Reuse the exact-job publisher: it owns claims, ambiguity and receipts.
        # Its final line is bounded JSON; ignore any accidental provider prints.
        captured = io.StringIO()
        with redirect_stdout(captured):
            publish_exact_job(job_id, box_factory=self.box, instagram_factory=self.adapter_factory)
        return json.loads(captured.getvalue().splitlines()[-1])

    def jobs(self, status="active", limit=20):
        where = "platform='instagram' AND account=?"
        params = [self.account]
        if status == "active":
            where += " AND status IN ('draft','queued','in_flight','uncertain')"
        elif status != "all":
            where += " AND status=?"
            params.append(status)
        with self.box() as box:
            rows = box.db.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ?",
                                  (*params, limit))
            result = [_job_summary(dict(row)) for row in rows]
        return {"ok": True, "jobs": result}

    def job(self, job_id):
        with self.box() as box:
            job = self.selected(box, job_id)
        return {"ok": True, "job": _job_summary(job, detail=True)}

    def _verify_receipt(self, job, provider_id):
        def operation(client):
            if job["action"] == "post":
                item = client.media_info_v1(provider_id.split("_")[0])
                if (str(item.pk) != provider_id.split("_")[0] or
                        str(item.user.pk) != str(client.user_id) or item.caption_text != job["body"]):
                    raise ToolInputError("observed post does not match the queued account and text",
                                         code="receipt_mismatch", hint=RECEIPT_HINT)
                taken_at = getattr(item, "taken_at", None)
                created_at = datetime.fromisoformat(job["created_at"].replace("Z", "+00:00"))
                if not isinstance(taken_at, datetime) or taken_at.tzinfo is None or taken_at < created_at:
                    raise ToolInputError("observed post predates the draft or has no verifiable timestamp",
                                         code="receipt_mismatch", hint=RECEIPT_HINT)
                if job.get("media_path") and Path(job["media_path"]).name == "carousel.json":
                    manifest = json.loads(Path(job["media_path"]).read_text(encoding="utf-8"))
                    if len(getattr(item, "resources", None) or []) != len(manifest["items"]):
                        raise ToolInputError("observed carousel has a different slide count",
                                             code="receipt_mismatch", hint=RECEIPT_HINT)
                return {"provider_id": str(item.pk), "permalink": _media(item)["url"]}
            post = client.media_info_v1(job["media_id"].split("_")[0])
            if str(post.user.pk) != str(client.user_id):
                raise ToolInputError("reply reconciliation requires the connected account's own media",
                                     code="not_own_media", hint=RECEIPT_HINT)
            items, _ = client.media_comment_replies_chunk(str(post.id), str(job["target_id"]),
                                                          max_amount=100, min_id=None)
            for item in items:
                if str(item.pk) == provider_id:
                    if str(item.user.pk) != str(client.user_id) or item.text != job["body"]:
                        raise ToolInputError("observed reply does not match the queued account and text",
                                             code="receipt_mismatch", hint=RECEIPT_HINT)
                    return {"provider_id": str(item.pk),
                            "permalink": f"{_media(post)['url']}?comment_id={item.pk}"}
            raise ToolInputError("provider reply was not verified on this bounded page; job remains uncertain",
                                 code="receipt_unverified", hint=RECEIPT_HINT)
        return self.read(operation)

    def reconcile_job(self, job_id, outcome, reason, provider_id=None):
        with self.box() as box:
            job = self.selected(box, job_id)
            # A server crash may leave the named job leased. Only an expired
            # lease can become uncertain; an active sender must keep ownership.
            # The outbox keeps the expired token, so the original sender can still record a late receipt.
            if job["status"] == "in_flight" and job["lease_until"] and job["lease_until"] <= utc_text(utc_now()):
                box.expire_leases()
                job = self.selected(box, job_id)
            if job["status"] != "uncertain":
                raise ToolInputError("only an uncertain job can be reconciled", code="not_uncertain",
                                     hint="check the job's status with instagram_job")
        receipt = {}
        if outcome == "published":
            if not provider_id:
                raise ToolInputError("published reconciliation requires an observed provider_id")
            receipt = self._verify_receipt(job, provider_id)
        elif provider_id:
            raise ToolInputError("provider_id applies only to a published resolution")
        with self.box() as box:
            job = box.resolve_uncertain(job_id, outcome=outcome, reason=reason, **receipt)
        return {"ok": True, "job": _job_summary(job, detail=True)}

    def retry_job(self, job_id, reason):
        with self.box() as box:
            self.selected(box, job_id)
            job = box.retry_failed(job_id, reason=reason)
        return {"ok": True, "job": _job_summary(job, detail=True)}


SESSION_ERRORS = {
    "session_unavailable": "no private Instagram session is registered for this account",
    "account_mismatch": "the saved session belongs to a different Instagram account",
    "auth_cooldown": "a recent automatic re-login failed, so Instagram reads are paused for 30 minutes",
    "checkpoint_required": "Instagram asked for a verification step this server cannot answer",
    "dependency_unavailable": "the Instagram client library is not installed",
    "session_save_failed": "the refreshed Instagram session could not be saved privately",
}
# Publisher statuses, mapped to next steps an MCP client can take with these tools.
UNCERTAIN_HINT = ("do not draft it again; check the account for the post, then call instagram_reconcile_job "
                  "with that evidence")
PUBLISH_ERRORS = {
    "not_found": ("Instagram job not found", "list job IDs with instagram_jobs"),
    "not_queued": ("only a queued job can be published",
                   "check its status with instagram_job; queue a draft with instagram_queue_job, "
                   "or resolve an uncertain job with instagram_reconcile_job"),
    "not_claimable": ("another job for this account is in flight or uncertain",
                      "resolve that job first (instagram_jobs, then instagram_reconcile_job), then publish again"),
    "auth_unavailable": ("the saved Instagram session could not be used; the job stays queued", None),
    "dependency_unavailable": ("the Instagram client library is not installed; the job stays queued", INSTALL_HINT),
    "adapter_unavailable": ("the Instagram publisher could not start; the job stays queued",
                            INSTALL_HINT + ", then run ghost-in-the-box doctor"),
    "rejected": ("Instagram refused the job before publishing it",
                 "inspect it with instagram_job, change what was refused, then call instagram_retry_job"),
    "failed_before_send": ("the job failed before anything was sent",
                           "inspect it with instagram_job, correct the cause, then call instagram_retry_job"),
    "uncertain": ("the send started but its outcome is unknown, so the job is now uncertain", UNCERTAIN_HINT),
    "lease_lost": ("the publisher lost its claim, so the outcome was not recorded", UNCERTAIN_HINT),
    "receipt_not_recorded": ("the post may be live but its receipt was not recorded", UNCERTAIN_HINT),
    "outbox_refused": ("the outbox refused this job", "inspect it with instagram_job"),
    "outbox_unavailable": ("the local outbox database could not be used",
                           "call again shortly; if it persists, run ghost-in-the-box doctor"),
    "unsupported_platform": ("this job is not an Instagram job", "list Instagram job IDs with instagram_jobs"),
}
# Outbox refusals whose own hints name CLI commands; MCP clients get the matching tool.
OUTBOX_HINTS = {
    "not_found": "list this account's job IDs with instagram_jobs",
    "not_draft": "check its status with instagram_job; only a draft can be queued",
    "not_uncertain": "check its status with instagram_job",
    "not_failed": "check its status with instagram_job; only a failed job can be retried",
    "idempotency_conflict": "use a new idempotency_key, or inspect the existing job with instagram_jobs",
}
NOT_QUEUED_HINTS = {
    "draft": "queue it with instagram_queue_job, then call instagram_publish_job",
    "uncertain": "resolve it with instagram_reconcile_job before anything else",
    "failed": "if nothing was sent, requeue it with instagram_retry_job",
    "published": "it is already published; instagram_job shows the receipt",
    "in_flight": "another publisher holds it; check again shortly with instagram_job",
    "cancelled": "it was cancelled; draft the post again with instagram_draft_carousel or instagram_draft_reply",
    "superseded": "it was retired; draft the post again with instagram_draft_carousel or instagram_draft_reply",
}
LOCAL_REJECT_HINT = ("the local outbox, inbox or library refused this change; check the current state with "
                     "instagram_job, instagram_inbox or instagram_jobs and adjust the arguments")
INTERNAL_HINT = "run ghost-in-the-box doctor; for a job, inspect it with instagram_job before trying again"
# These tools never call Instagram or a model endpoint, so their local refusals are safe to show.
LOCAL_DETAIL_TOOLS = frozenset(item["name"] for item in TOOLS
                               if not item["annotations"]["openWorldHint"]) - {"instagram_inspect_slide"}


def tool_error(code, message, hint):
    return mcp_server.tool_error(code, message, hint, fallback_hint=INTERNAL_HINT)


def _session_failure(account, status):
    code = status if re.fullmatch(r"[A-Za-z_]{1,80}", status) else "session_unavailable"
    hint = INSTALL_HINT if code == "dependency_unavailable" else (
        "wait 30 minutes, or " + login_hint(account) if code == "auth_cooldown" else
        "run ghost-in-the-box doctor and check the state directory permissions"
        if code == "session_save_failed" else login_hint(account))
    return tool_error(code, SESSION_ERRORS.get(code, "the saved Instagram session could not be used"), hint)


def _structured_failure(result, account):
    """Give every unsuccessful tool result the same error object, keeping its other fields."""
    if result.get("ok") is not False:
        return result
    failure = result.get("failure")
    if isinstance(failure, dict) and not isinstance(result.get("error"), dict):
        resume = result.get("next_start_index")
        return {**result, "error": tool_error(
            "download_incomplete", str(failure.get("error") or "image download unavailable"),
            "completed slides are kept; call instagram_download_media again"
            + (f" with start_index={resume}" if resume else ""))}
    status = result.get("status")
    status = status if isinstance(status, str) and re.fullmatch(r"[a-z_]{1,80}", status) else "tool_failed"
    message, hint = PUBLISH_ERRORS.get(status, (f"the tool stopped with status {status}",
                                                "inspect the job with instagram_job"))
    if status == "auth_unavailable":
        hint = login_hint(account)
    elif status == "not_queued":
        hint = NOT_QUEUED_HINTS.get(result.get("job_status"), hint)
    existing = result.get("error")
    if isinstance(existing, dict):
        # The publisher's own code and message stay; its hints name CLI commands, so
        # an MCP client gets the matching tool instead when one exists.
        code = existing.get("code") if isinstance(existing.get("code"), str) else status
        message = existing.get("message") if isinstance(existing.get("message"), str) else message
        if status not in PUBLISH_ERRORS and isinstance(existing.get("hint"), str):
            hint = existing["hint"]
        return {**result, "error": tool_error(code, message, hint)}
    return {**result, "error": tool_error(status, message, hint)}


def _tool_result(service, name, arguments):
    try:
        # Third-party diagnostics must never corrupt framing or expose secrets.
        result = mcp_server.quiet_call(service.call, name, arguments)
        if not isinstance(result, dict):
            raise TypeError("tool returned a non-object result")
        result = _structured_failure(result, service.account)
    except ToolInputError as error:
        result = {"ok": False, "error": tool_error(error.code, str(error), error.hint)}
    except ReferenceError as error:
        result = {"ok": False, "error": tool_error(
            "reference_rejected", str(error),
            "use an exact media_id, URL or downloaded image path returned by an earlier Instagram tool")}
    except InstagramSessionError as error:
        result = {"ok": False, "error": _session_failure(service.account, str(error))}
    except ValueError as error:
        code = getattr(error, "code", None)
        if type(error).__module__.startswith("farm.") and isinstance(code, str) and str(error):
            # Our own coded refusals (for example the outbox's) are written to be shown.
            hint = OUTBOX_HINTS.get(code, getattr(error, "hint", None))
            failure = tool_error(code, str(error)[:500], hint if isinstance(hint, str) else LOCAL_REJECT_HINT)
        elif name in LOCAL_DETAIL_TOOLS and str(error):
            failure = tool_error("rejected", str(error)[:500], LOCAL_REJECT_HINT)
        else:
            failure = tool_error("internal_error", "the tool failed; details are withheld so session data "
                                 "and provider responses cannot leak", INTERNAL_HINT)
        result = {"ok": False, "error": failure, "error_type": type(error).__name__}
    except Exception as error:
        result = {"ok": False, "error": tool_error(
            "internal_error", "the tool failed; details are withheld so session data and provider responses "
            "cannot leak", INTERNAL_HINT), "error_type": type(error).__name__}
    return mcp_server.tool_content(result)


INSTRUCTIONS = ("Instagram uses the configured saved session and durable outbox. "
    "Call instagram_status first to confirm the account and session. "
    "Use discover and references for topic research, download_media for local carousel images, "
    "and inspect_slide to see an image through the local vision model. "
    "Use status, own_posts, comments and inbox for account context. Draft replies or carousels, "
    "queue the exact job, then publish_job. Never create a second draft to retry an uncertain send. "
    "Failed calls return error.code, error.message and error.hint; follow the hint.")


SERVER = mcp_server.Server(name=SERVER_NAME, title=SERVER_TITLE, instructions=INSTRUCTIONS, tools=TOOLS,
                           call=_tool_result, unknown_tool="Unknown Instagram tool")


def dispatch(service, message, session=None):
    """Answer one JSON-RPC message; session remembers the negotiated protocol version."""
    return SERVER.dispatch(service, message, session)


def handle(service, message, session):
    """Answer one message, or a JSON-RPC batch when 2025-03-26 was negotiated."""
    return SERVER.handle(service, message, session)


_first_sentence = mcp_server.first_sentence


def tool_catalog(tools=None):
    """One-line summaries of the Instagram tools, for docs and quick inspection."""
    return mcp_server.tool_catalog(TOOLS if tools is None else tools)


def substack_catalog(path=SUBSTACK_SERVER_SCRIPT):
    """Read the tools this repo adds to the Substack server without importing its optional extra."""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError, ValueError):
        return []
    entries = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr == "tool"
                   for item in node.decorator_list):
            continue
        positional = [*node.args.posonlyargs, *node.args.args]
        defaulted = len(node.args.defaults)
        required = [arg.arg for arg in positional[:len(positional) - defaulted]]
        required += [arg.arg for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is None]
        optional = [arg.arg for arg in [*positional, *node.args.kwonlyargs] if arg.arg not in required]
        entries.append({"name": node.name, "title": node.name.replace("_", " ").capitalize(),
                        "description": _first_sentence(ast.get_docstring(node) or ""),
                        "required": required, "optional": optional,
                        "read_only": None, "contacts_provider": None})
    return entries


def catalog(substack_path=SUBSTACK_SERVER_SCRIPT):
    servers = [{"server": SERVER_NAME, "command": "ghost-in-the-box instagram-mcp --account HANDLE",
                "tools": tool_catalog()}]
    substack = substack_catalog(substack_path)
    if substack:
        servers.append({"server": "ghost-in-the-box-substack",
                        "command": "ghost-in-the-box substack-mcp --account HANDLE",
                        "note": "Needs the substack extra. Also exposes the upstream python-substack tools, "
                                "which your MCP client lists after connecting.",
                        "tools": substack})
    return mcp_server.catalog_document("ghost-in-the-box instagram-mcp --list-tools", servers)


def serve(service, stdin=None, stdout=None):
    return SERVER.serve(service, stdin, stdout)
