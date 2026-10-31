"""Instagram tools backed by the saved session, inbox and durable outbox.

The transport lives here too: Batbox currently speaks Content-Length stdio.
No provider objects, credentials, session files or raw provider errors cross it.
"""

from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import sys
import uuid

from farm.accounts import AccountRegistry
from farm.adapters.instagram import InstagramAdapter
from farm.inbox import Inbox, sync_comments
from farm.instagram_session import InstagramSessionError, read_with_session
from farm.instagram_references import (
    DEFAULT_ROOT as REFERENCE_ROOT, ReferenceError, ReferenceStore, canonical_post_url, fetch_image,
)
from farm.outbox import Outbox, utc_text, utc_now
from scripts.instagram_carousel import ROOT as CAROUSEL_ROOT, create_draft as carousel_draft
from scripts import meme_library
from scripts.social_publish_job import run as publish_exact_job


DEFAULT_ACCOUNT = "example_creator"
MEME_ROOT = meme_library.ROOT
PROTOCOL_VERSION = "2024-11-05"
MAX_MESSAGE_BYTES = 1024 * 1024
JOB_STATES = ["draft", "queued", "in_flight", "uncertain", "published", "failed", "superseded"]


class ToolInputError(ValueError):
    """An explicitly safe local validation message."""


def string(description, *, minimum=1, maximum=4096, pattern=None, default=None):
    result = {"type": "string", "description": description,
              "minLength": minimum, "maxLength": maximum}
    if pattern:
        result["pattern"] = pattern
    if default is not None:
        result["default"] = default
    return result


def integer(description, default, low, high):
    return {"type": "integer", "description": description, "default": default,
            "minimum": low, "maximum": high}


def boolean(description, default=False):
    return {"type": "boolean", "description": description, "default": default}


def choice(description, values, default=None):
    result = {"type": "string", "description": description, "enum": values}
    if default is not None:
        result["default"] = default
    return result


JOB_ID = string("Exact Instagram outbox job ID.", maximum=100)
MEDIA_ID = string("Instagram media PK or numeric composite media ID.", maximum=100,
                  pattern=r"^[0-9]+(?:_[0-9]+)?$")
POST_URL = string("Canonical HTTPS Instagram /p/, /reel/ or /tv/ URL; supply this or media_id.")
HASHTAG = string("One topic hashtag, for example artificialintelligence, softwareengineering or programming.",
                 maximum=81, pattern=r"^#?[A-Za-z0-9_]{1,80}$")
COMMENT_ID = string("Numeric Instagram comment ID.", maximum=100, pattern=r"^[0-9]+$")
WORKER = string("Your stable worker name; use the same name when drafting or skipping.", maximum=100)
CURSOR = string("Opaque cursor from the previous page; omit for the first page.", minimum=0)


def tool(name, description, properties=None, required=(), *, read_only=False, external=False):
    return {"name": f"instagram_{name}", "description": description,
            "inputSchema": {"type": "object", "properties": properties or {},
                            "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": read_only, "destructiveHint": False,
                            "openWorldHint": external}}


TOOLS = [
    tool("status", "Check configured Instagram account, local session readiness and job counts. "
         "live_check optionally validates the saved login; no credentials are returned.",
         {"live_check": boolean("Validate the saved session with Instagram.")}, read_only=True, external=True),
    tool("own_posts", "Read one complete page (up to 33) of the connected account's own Instagram posts. "
         "Returns captions, IDs, links and a continuation cursor.", {"cursor": CURSOR}, read_only=True, external=True),
    tool("media", "Read any accessible Instagram post's caption, author and slide URLs. "
         "Supply exactly one of media_id or url. Use download_media for local images and inspect_slide to see them.",
         {"media_id": MEDIA_ID, "url": POST_URL}, read_only=True, external=True),
    tool("discover", "Discover topic posts with one bounded Instagram hashtag page using the saved session. "
         "Try artificialintelligence, machinelearning, softwareengineering, programming or developerlife. "
         "Returns up to limit posts and saves their metadata for references/curate. Does not download images.",
         {"hashtag": HASHTAG, "sort": choice("Provider hashtag ranking.", ["top", "recent"], "top"),
          "limit": integer("Maximum posts returned and saved from one provider page.", 12, 1, 30)},
         ["hashtag"], external=True),
    tool("references", "Browse saved Instagram research candidates and downloaded image paths locally. "
         "Selections and notes survive later discovery refreshes. This store is separate from the meme library.",
         {"state": choice("Curation filter.", ["new", "selected", "skipped", "all"], "new"),
          "hashtag": HASHTAG, "query": string("Literal caption or author text filter.", maximum=200),
          "limit": integer("Maximum saved posts.", 20, 1, 50),
          "offset": integer("Continue at next_offset from the previous result.", 0, 0, 10000)}, read_only=True),
    tool("curate", "Mark a saved Instagram research candidate new, selected or skipped and retain a note. "
         "Changes only the private local reference store.",
         {"media_id": MEDIA_ID, "state": choice("Curation state.", ["new", "selected", "skipped"]),
          "note": string("Why this post is useful or should be skipped.", minimum=0, maximum=2000, default="")},
         ["media_id", "state"]),
    tool("download_media", "Download images from one Instagram post or carousel, including other authors' posts. "
         "Supply exactly one of media_id or url. Returns ordered absolute slide paths, source URL, author, "
         "caption and IDs. Video slides yield preview images. Private local snapshots are reused; images are "
         "limited to 8 MB each, 64 MB and at most 20 slides per call. Follow next_start_index for remaining slides.",
         {"media_id": MEDIA_ID, "url": POST_URL,
          "max_images": integer("Maximum images for this call.", 10, 1, 20),
          "start_index": integer("One-based slide index to start or resume.", 1, 1, 100)}, external=True),
    tool("inspect_slide", "See one downloaded Instagram image using the existing local vision model. "
         "Reads visible words and explains the visual in a concise answer. Accepts only an unchanged absolute "
         "image path returned by download_media; image bytes stay on this machine.",
         {"image_path": string("Absolute downloaded slide path."),
          "question": string("What to examine in the image.", maximum=2000,
             default="Describe what is visually happening, read the visible words, and explain the joke or argument in three concise sentences."),
          "max_tokens": integer("Maximum local-model output tokens.", 500, 64, 1000)},
         ["image_path"], read_only=True),
    tool("search_memes", "Search the cached technology meme library by topic and words in titles or images. "
         "Returns IDs, local image paths, source links, observed engagement and short OCR excerpts. "
         "Use IDs with prepare_meme_carousel; use next_offset for more matches. Reads only local files.",
         {"query": string("Words to find in titles and OCR text, for example Claude or debugging.", minimum=0, maximum=200, default=""),
          "category": choice("Optional topic folder.", list(meme_library.PRIMARY_ORDER)),
          "limit": integer("Maximum memes returned.", 8, 1, 20),
          "offset": integer("Continue at next_offset from a previous result.", 0, 0, 10000),
          "min_score": integer("Minimum recorded native engagement count; scores and likes are labeled.", 0, 0, 1000000000)},
         read_only=True),
    tool("import_meme", "Add one image already returned by download_media to the categorized meme library. "
         "Source URL, author, caption and engagement are read from its saved download record automatically. "
         "Deduplicates, OCRs and indexes the image, then returns its library ID and cached path. Local files only.",
         {"image_path": string("Unchanged absolute image path returned by download_media.")}, ["image_path"]),
    tool("prepare_meme_carousel", "Prepare 2–10 cached meme IDs or paths as ordered 1080×1350 JPEG slides. "
         "Pads without cropping and preserves originals. Returns images and source_urls to pass directly "
         "to draft_carousel with your caption and idempotency_key. Creates local files; does not draft or publish.",
         {"items": {"type": "array", "minItems": 2, "maxItems": 10,
                    "items": string("Meme ID or cached image path from search_memes or import_meme.")}}, ["items"]),
    tool("comments", "Read one complete comment page on your own Instagram media and retain it in the inbox. "
         "Supply comment_id to read that comment's replies. Returned cursor continues the same stream.",
         {"media_id": MEDIA_ID, "comment_id": COMMENT_ID, "cursor": CURSOR},
         ["media_id"], external=True),
    tool("sync_inbox", "Refresh a bounded selection of own-media comments and optional feed. "
         "Uses durable cursors; older continues historical scans. No comments are marked reviewed.",
         {"posts": integer("Own posts to scan.", 3, 1, 10),
          "pages": integer("Comment pages per post.", 1, 1, 3),
          "threads": integer("Parent comments whose reply pages to scan per page.", 0, 0, 5),
          "feed_pages": integer("Timeline pages to retain.", 0, 0, 3),
          "older": boolean("Continue saved historical cursors.")}, external=True),
    tool("inbox", "Read cached unread comments, pending reviews, or feed items without consuming them. "
         "Call sync_inbox for fresh provider data.",
         {"view": choice("Cached view.", ["unread", "pending", "feed"], "unread"),
          "limit": integer("Maximum records.", 20, 1, 100), "worker": WORKER}, read_only=True),
    tool("claim_comments", "Claim unread comments for review for one hour. "
         "Use the worker name for draft_reply and skip_comment; expired claims become available again.",
         {"worker": WORKER, "limit": integer("Maximum comments.", 10, 1, 100)}, ["worker"]),
    tool("skip_comment", "Finish a claimed comment review without replying.",
         {"comment_id": COMMENT_ID, "worker": WORKER}, ["comment_id", "worker"]),
    tool("draft_reply", "Save the exact reply text for a claimed inbox comment. "
         "Returns one durable draft per comment; repeat the same text to recover the same job. Does not publish.",
         {"comment_id": COMMENT_ID, "text": string("Exact reply text.", maximum=2200), "worker": WORKER},
         ["comment_id", "text", "worker"]),
    tool("draft_carousel", "Snapshot 2–10 ordered local images and an exact caption into one Instagram draft. "
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
    tool("queue_job", "Freeze and queue one exact Instagram draft. Does not send. "
         "Publishers are not started by this server; publish_job explicitly sends the selected job.",
         {"job_id": JOB_ID, "at": string("Optional ISO 8601 schedule with timezone; default now.", maximum=100)},
         ["job_id"]),
    tool("publish_job", "Publish exactly one queued Instagram job now, including a future-scheduled job. "
         "Repeated calls for a published job return its receipt. Ambiguous sends become uncertain and cannot "
         "be resent until reconciled. Auth failures preserve the queue. This makes a public post or reply.",
         {"job_id": JOB_ID}, ["job_id"], external=True),
    tool("jobs", "List bounded Instagram job summaries for this configured account.",
         {"status": choice("Job status filter.", ["active", "all", *JOB_STATES], "active"),
          "limit": integer("Maximum jobs.", 20, 1, 100)}, read_only=True),
    tool("job", "Inspect one Instagram job's exact text, frozen assets, target and durable outcome. "
         "Lease tokens and session settings are omitted.", {"job_id": JOB_ID}, ["job_id"], read_only=True),
    tool("reconcile_job", "Resolve an uncertain Instagram job using observed evidence. "
         "For published, supply the observed provider_id: the server verifies the account and exact text "
         "against Instagram before recording it. For queued or failed, reason must explain direct evidence "
         "that no public write happened; a missing item in a bounded read is not proof. "
         "Requeue only when that evidence rules out a duplicate. Does not send.",
         {"job_id": JOB_ID, "outcome": choice("Evidence-supported outcome.", ["published", "queued", "failed"]),
          "reason": string("Concrete reconciliation evidence.", maximum=4000), "provider_id": MEDIA_ID},
         ["job_id", "outcome", "reason"], external=True),
    tool("retry_job", "Requeue a failed-before-send Instagram job after correcting the failure. "
         "Requires unchanged frozen content and a reason. Does not send and cannot retry uncertain jobs.",
         {"job_id": JOB_ID, "reason": string("What corrected the pre-send failure.", maximum=4000)},
         ["job_id", "reason"]),
]
TOOL_BY_NAME = {item["name"]: item for item in TOOLS}


def validate(value, schema, field="arguments"):
    """Validate the small JSON Schema subset advertised above before side effects."""
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "string": isinstance(value, str),
             "array": isinstance(value, list), "integer": type(value) is int,
             "boolean": type(value) is bool}.get(kind, False)
    if not valid:
        raise ToolInputError(f"{field} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolInputError(f"{field} has an unsupported value")
    if kind == "object":
        properties = schema["properties"]
        if any(key not in properties for key in value):
            raise ToolInputError("arguments contains an unknown field")
        for required in schema.get("required", []):
            if required not in value:
                raise ToolInputError(f"{required} is required")
        for key, item in value.items():
            validate(item, properties[key], key)
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", MAX_MESSAGE_BYTES):
            raise ToolInputError(f"{field} has an invalid length")
        if schema.get("minLength", 0) and not value.strip():
            raise ToolInputError(f"{field} must not be blank")
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            raise ToolInputError(f"{field} has an invalid format")
    elif kind == "integer":
        if not schema["minimum"] <= value <= schema["maximum"]:
            raise ToolInputError(f"{field} is outside its allowed bounds")
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise ToolInputError(f"{field} has an invalid number of items")
        for item in value:
            validate(item, schema["items"], field)


def _new_client():
    from instagrapi import Client
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
    def __init__(self, *, account=DEFAULT_ACCOUNT, outbox_path=None, inbox_path=None,
                 carousel_root=CAROUSEL_ROOT, registry=None, client_factory=None,
                 reader=None, adapter_factory=None, reference_root=REFERENCE_ROOT,
                 image_fetcher=None, vision_inspector=None, meme_root=MEME_ROOT):
        if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", account):
            raise ToolInputError("invalid configured account")
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
            raise ToolInputError("Instagram job not found for the configured account")
        return job

    def call(self, name, arguments):
        if name not in TOOL_BY_NAME:
            raise ToolInputError("unknown Instagram tool")
        schema = TOOL_BY_NAME[name]["inputSchema"]
        validate(arguments, schema)
        args = {key: prop["default"] for key, prop in schema["properties"].items() if "default" in prop}
        args.update(arguments)
        return getattr(self, name.removeprefix("instagram_"))(**args)

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
                raise ToolInputError("Instagram media page was not complete; cursor was not advanced")
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
                raise ToolInputError("Instagram returned a different media ID")
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
                raise ToolInputError("Instagram did not acknowledge a bounded hashtag page")
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
            raise ToolInputError("downloaded image could not be imported; use a supported image within the library bounds") from None
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
                raise ToolInputError("comment inbox reads require the connected account's own media")
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
                raise ToolInputError("Instagram comment page was not complete; cursor was not advanced")
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
                    raise ToolInputError("observed post does not match the queued account and text")
                taken_at = getattr(item, "taken_at", None)
                created_at = datetime.fromisoformat(job["created_at"].replace("Z", "+00:00"))
                if not isinstance(taken_at, datetime) or taken_at.tzinfo is None or taken_at < created_at:
                    raise ToolInputError("observed post predates the draft or has no verifiable timestamp")
                if job.get("media_path") and Path(job["media_path"]).name == "carousel.json":
                    manifest = json.loads(Path(job["media_path"]).read_text(encoding="utf-8"))
                    if len(getattr(item, "resources", None) or []) != len(manifest["items"]):
                        raise ToolInputError("observed carousel has a different slide count")
                return {"provider_id": str(item.pk), "permalink": _media(item)["url"]}
            post = client.media_info_v1(job["media_id"].split("_")[0])
            if str(post.user.pk) != str(client.user_id):
                raise ToolInputError("reply reconciliation requires the connected account's own media")
            items, _ = client.media_comment_replies_chunk(str(post.id), str(job["target_id"]),
                                                          max_amount=100, min_id=None)
            for item in items:
                if str(item.pk) == provider_id:
                    if str(item.user.pk) != str(client.user_id) or item.text != job["body"]:
                        raise ToolInputError("observed reply does not match the queued account and text")
                    return {"provider_id": str(item.pk),
                            "permalink": f"{_media(post)['url']}?comment_id={item.pk}"}
            raise ToolInputError("provider reply was not verified on this bounded page; job remains uncertain")
        return self.read(operation)

    def reconcile_job(self, job_id, outcome, reason, provider_id=None):
        with self.box() as box:
            job = self.selected(box, job_id)
            # A server crash may leave the named job leased. Only an expired
            # lease can become uncertain; an active sender must keep ownership.
            if job["status"] == "in_flight" and job["lease_until"] and job["lease_until"] <= utc_text(utc_now()):
                box.db.execute("""UPDATE jobs SET status='uncertain',lease_token=NULL,lease_until=NULL,
                    last_error='lease expired before receipt',updated_at=?
                    WHERE id=? AND status='in_flight' AND lease_until<=?""",
                    (utc_text(utc_now()), job_id, utc_text(utc_now())))
                job = self.selected(box, job_id)
            if job["status"] != "uncertain":
                raise ToolInputError("only an uncertain job can be reconciled")
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


def _tool_result(service, name, arguments):
    try:
        # Third-party diagnostics must never corrupt framing or expose secrets.
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            result = service.call(name, arguments)
    except (ToolInputError, ReferenceError) as error:
        result = {"ok": False, "error": str(error)}
    except InstagramSessionError as error:
        status = str(error)
        result = {"ok": False, "error": status if re.fullmatch(r"[A-Za-z_]{1,80}", status) else "session_unavailable"}
    except Exception as error:
        result = {"ok": False, "error_type": type(error).__name__}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": not result.get("ok", False)}


def dispatch(service, message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    if "id" not in message:
        return None  # Notifications never execute tools and never receive replies.
    request_id = message["id"]
    method = message.get("method")
    params = message.get("params", {})
    response = {"jsonrpc": "2.0", "id": request_id}
    if not isinstance(params, dict) or not isinstance(method, str):
        response["error"] = {"code": -32600, "message": "Invalid request"}
    elif method == "initialize":
        response["result"] = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
            "serverInfo": {"name": "ghost-in-the-box-instagram", "version": "1.0.0"},
            "instructions": "Instagram uses the configured saved session and durable outbox. "
                "Use discover and references for topic research, download_media for local carousel images, "
                "and inspect_slide to see an image through the local vision model. "
                "Use status, own_posts, comments and inbox for account context. Draft replies or carousels, "
                "queue the exact job, then publish_job. Never create a second draft to retry an uncertain send."}
    elif method == "ping":
        response["result"] = {}
    elif method == "tools/list":
        response["result"] = {"tools": TOOLS}
    elif method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or name not in TOOL_BY_NAME:
            response["error"] = {"code": -32602, "message": "Unknown Instagram tool"}
        else:
            response["result"] = _tool_result(service, name, arguments)
    elif method in ("resources/list", "resources/templates/list", "prompts/list"):
        key = {"resources/list": "resources", "resources/templates/list": "resourceTemplates", "prompts/list": "prompts"}[method]
        response["result"] = {key: []}
    else:
        response["error"] = {"code": -32601, "message": "Method not found"}
    return response


def read_message(stream):
    """Accept Batbox framing and newline JSON; respond in the caller's format."""
    line = stream.readline(8193)
    if not line:
        return None, "framed"
    if line.lstrip().startswith(b"{"):
        if not line.endswith(b"\n"):
            tail = stream.readline(MAX_MESSAGE_BYTES - len(line) + 1)
            line += tail
        if len(line) > MAX_MESSAGE_BYTES:
            raise ValueError("message too large")
        return json.loads(line), "lines"
    total = len(line)
    length = None
    while line not in (b"\r\n", b"\n"):
        if not line or total > 8192:
            raise ValueError("invalid frame headers")
        key, separator, value = line.partition(b":")
        if not separator:
            raise ValueError("invalid frame header")
        if key.strip().lower() == b"content-length":
            if length is not None:
                raise ValueError("duplicate length")
            length = int(value.strip())
        line = stream.readline(8193)
        total += len(line)
    if length is None or not 0 < length <= MAX_MESSAGE_BYTES:
        raise ValueError("invalid frame length")
    chunks = bytearray()
    while len(chunks) < length:
        data = stream.read(length - len(chunks))
        if not data:
            raise ValueError("incomplete frame")
        chunks.extend(data)
    return json.loads(chunks), "framed"


def write_message(stream, message, framing="framed"):
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write((f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
                 if framing == "framed" else body + b"\n")
    stream.flush()


def serve(service, stdin=None, stdout=None):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    while True:
        try:
            message, framing = read_message(stdin)
        except (ValueError, UnicodeError):
            # Invalid framing cannot safely be resynchronized. No raw payloads.
            write_message(stdout, {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "Invalid JSON-RPC message"}})
            return 2
        if message is None:
            return 0
        if isinstance(message, dict) and message.get("method") == "notifications/exit" and "id" not in message:
            return 0
        response = dispatch(service, message)
        if response is not None:
            write_message(stdout, response, framing)
