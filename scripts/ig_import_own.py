#!/usr/bin/env python3
"""Import the authenticated account's captions into a private local corpus.

Uses instagrapi's documented account_info/user_medias_paginated_v1 interfaces.
No login, session save, upload, interaction, image download, or hosted service.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from farm import locking as fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import re
import signal
import tempfile
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.cli_output import (
    EXIT_AUTH, EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, check_account, emit, error_object, exception_failure,
    failure, guarded,
)
from farm.config import STATE_ROOT, install_command


DEFAULT_ROOT = STATE_ROOT


class ImportFailure(ValueError):
    pass


class DeadlineExceeded(BaseException):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def atomic_json(path, value):
    """Publish bytes before any cursor that acknowledges them; 0600 + fsync."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".import-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def corpus_lock(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def make_client(session_file, *, client_factory=None):
    """Load settings without rewriting them; network boundary contains no secrets."""
    if not session_file.is_file():
        raise ImportFailure("saved_session_missing")
    if session_file.stat().st_mode & 0o077:
        raise ImportFailure("saved_session_not_private")
    if client_factory is None:
        from instagrapi import Client
        client_factory = Client
    logger = logging.getLogger("social_farm.own_post_import")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    client = client_factory(logger=logger)
    client.load_settings(session_file)
    client.with_challenge_flow = False
    client.set_retry_config(session_retry_total=0, public_request_retries_count=0,
                            request_timeout=1, private_transport="requests")
    client.private_request_logger = logger
    client.delay_range = [1, 2]
    client.private.trust_env = False
    client.private.proxies.clear()
    client.private.verify = True
    if not client.user_id:
        raise ImportFailure("saved_session_has_no_account")
    audit = {"hosts": [], "methods": [], "requests": 0}
    original_send = client.private.send
    owner_path = f"/api/v1/feed/user/{client.user_id}/"

    def send(request, **kwargs):
        url = urlsplit(request.url)
        if (request.method != "GET" or url.scheme != "https" or url.hostname != "i.instagram.com"
                or url.path not in (owner_path, "/api/v1/accounts/current_user/")):
            raise ImportFailure("unexpected_import_endpoint")
        # No proxy, redirect, credential URL, or collector outside Instagram.
        if url.username or url.password or url.port not in (None, 443):
            raise ImportFailure("unexpected_import_endpoint")
        kwargs.update(timeout=(10, 20), allow_redirects=False, proxies={})
        audit["requests"] += 1
        if url.hostname not in audit["hosts"]:
            audit["hosts"].append(url.hostname)
        if request.method not in audit["methods"]:
            audit["methods"].append(request.method)
        return original_send(request, **kwargs)

    client.private.send = send
    return client, audit


def post_record(media, username, user_id, captured_at):
    user = getattr(media, "user", None)
    # Own-feed responses can contain collaborations/reposts. Do not attribute
    # another account's caption to this writer merely because it is in the feed.
    if str(getattr(user, "pk", "")) != user_id:
        return None
    if getattr(user, "username", "").casefold() != username.casefold():
        raise ImportFailure("post_owner_username_changed")
    pk, code = str(media.pk), str(media.code)
    if not pk.isdecimal() or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", code):
        raise ImportFailure("invalid_post_identity")
    caption = media.caption_text
    if not isinstance(caption, str) or len(caption.encode("utf-8")) > 256 * 1024:
        raise ImportFailure("invalid_caption")
    taken = media.taken_at
    if not isinstance(taken, datetime) or taken.tzinfo is None:
        raise ImportFailure("post_time_missing_timezone")
    return {"version": 1, "platform": "instagram", "media_pk": pk, "media_id": str(media.id),
        "shortcode": code, "source_uri": f"https://www.instagram.com/p/{code}/",
        "source_anchor": f"instagram:media:{pk}:caption", "source_author": username,
        "author_user_id": user_id, "source_date": taken.astimezone(timezone.utc).isoformat(),
        "caption": caption, "caption_sha256": hashlib.sha256(caption.encode()).hexdigest(),
        "media_type": media.media_type, "product_type": getattr(media, "product_type", ""),
        "captured_at": captured_at}


def import_posts(client, directory, username, *, pages=1, page_size=12, older=False, transport=None):
    if not 1 <= pages <= 5 or not 1 <= page_size <= 33:
        raise ImportFailure("invalid_import_bounds")
    state_file = directory / "state.json"
    state = read_json(state_file, {"version": 1, "username": username, "backfill_started": False,
                                   "backfill_complete": False, "backfill_cursor": ""})
    account = client.account_info()
    user_id = str(client.user_id)
    if (str(account.pk) != user_id or account.username.casefold() != username.casefold()
            or state.get("username") != username or state.get("user_id", user_id) != user_id):
        raise ImportFailure("saved_session_account_mismatch")
    state["user_id"] = user_id
    report = {"ok": True, "username": username, "mode": "older" if older else "recent",
              "started_at": now(), "pages": 0, "new": 0, "updated": 0, "unchanged": 0,
              "other_author_skipped": 0, "page_size": page_size, "page_limit": pages}
    cursor = state.get("backfill_cursor", "") if older else ""
    advance_backfill = older or not state["backfill_started"]
    seen_cursors = set()
    try:
        for _ in range(0 if older and state["backfill_complete"] else pages):
            if cursor in seen_cursors:
                raise ImportFailure("pagination_cursor_repeated")
            seen_cursors.add(cursor)
            client.last_json = {}
            medias, next_cursor = client.user_medias_paginated_v1(user_id, amount=page_size, end_cursor=cursor)
            # v3.2.1 can swallow a non-PrivateError and return [],None. Do not
            # falsely mark history complete or advance a cursor on that path.
            response = client.last_json
            if not isinstance(response, dict) or response.get("status") != "ok" or not isinstance(response.get("items"), list):
                raise ImportFailure("page_not_acknowledged")
            if not isinstance(medias, list) or len(medias) > page_size:
                raise ImportFailure("invalid_page_size")
            if len(response["items"]) != len(medias):
                raise ImportFailure("page_truncated_by_library")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise ImportFailure("invalid_page_cursor")
            if next_cursor and next_cursor == cursor:
                raise ImportFailure("pagination_cursor_repeated")
            captured = now()
            for media in medias:
                record = post_record(media, username, user_id, captured)
                if record is None:
                    report["other_author_skipped"] += 1
                    continue
                path = directory / "posts" / (record["media_pk"] + ".json")
                existing = read_json(path)
                comparable = {k: v for k, v in record.items() if k != "captured_at"}
                if existing and comparable == {k: v for k, v in existing.items() if k not in ("captured_at", "first_captured_at")}:
                    report["unchanged"] += 1
                    continue
                record["first_captured_at"] = existing.get("first_captured_at", existing["captured_at"]) if existing else captured
                atomic_json(path, record)
                report["updated" if existing else "new"] += 1
            # Every post above is durable before this page's progress commits.
            if advance_backfill:
                state.update(backfill_started=True, backfill_cursor=next_cursor or "", backfill_complete=not bool(next_cursor))
            state.update(last_import_at=captured, last_mode=report["mode"])
            atomic_json(state_file, state)
            report["pages"] += 1
            if not next_cursor:
                break
            cursor = next_cursor
    except (Exception, DeadlineExceeded) as error:
        report.update(ok=False, error_type=type(error).__name__)
        if isinstance(error, ImportFailure):
            report["error"] = str(error)
        # No raw provider errors, response objects, cookies, or request headers.
    persisted = read_json(state_file, {"backfill_complete": False, "backfill_cursor": ""})
    report.update(finished_at=now(), backfill_complete=persisted["backfill_complete"],
                  more_older_available=bool(persisted.get("backfill_cursor")), transport=transport or {})
    atomic_json(directory / "status.json", report)
    return report


def corpus_status(directory):
    paths = list((directory / "posts").glob("*.json")) if (directory / "posts").exists() else []
    records = [read_json(path) for path in paths]
    return {"corpus": str(directory), "posts": len(records),
            "nonempty_captions": sum(bool(r["caption"].strip()) for r in records),
            "last_import": read_json(directory / "status.json"),
            "backfill_complete": (read_json(directory / "state.json", {}) or {}).get("backfill_complete", False)}


COMMAND = "instagram-import"
HELP = {
    "import": "fetch pages of your own captions into the private corpus",
    "status": "show corpus size and the last import receipt",
    "list": "show the newest imported captions",
    "show": "show one imported post by --media-pk",
}
# Fixed import refusals and the step that clears each one.
SESSION_FAILURES = {
    "saved_session_missing": "session_unavailable",
    "saved_session_not_private": "session_unavailable",
    "saved_session_has_no_account": "session_unavailable",
    "saved_session_account_mismatch": "account_mismatch",
}
IMPORT_HINTS = {
    "post_not_imported": "list imported posts: ghost-in-the-box instagram-import list --account {account}",
    "show_requires_numeric_media_pk": "ghost-in-the-box instagram-import show --account {account} --media-pk MEDIA_PK",
    "invalid_import_bounds": "ghost-in-the-box instagram-import --help",
}
RETRY_HINT = ("nothing past the last saved page was committed; retry later: "
              "ghost-in-the-box instagram-import import --account {account}")


def _bounded(low: int, high: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected a whole number {low}..{high}") from None
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be {low}..{high}")
        return number
    return parse


def _import_error(code: str, account: str, session_file: Path) -> dict:
    session_code = SESSION_FAILURES.get(code)
    if session_code:
        from farm.instagram_session import InstagramSessionError
        details = {
            "saved_session_missing": f"No saved Instagram session at {session_file}.",
            "saved_session_not_private": f"The Instagram session file is readable by other users: {session_file}",
            "saved_session_has_no_account": f"The Instagram session at {session_file} has no signed-in account.",
            "saved_session_account_mismatch": f"The saved Instagram session is not signed in as {account}.",
        }
        hint = f"chmod 600 '{session_file}'" if code == "saved_session_not_private" else None
        error = InstagramSessionError(session_code, message=details[code], hint=hint, account=account).to_error()
        error["code"] = code
        return error
    hint = IMPORT_HINTS.get(code, RETRY_HINT).format(account=account)
    return error_object(code, f"The import stopped: {code.replace('_', ' ')}.", hint)


def _parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example: ghost-in-the-box instagram-import import --account YOUR_HANDLE --pages 1")
    parser.add_argument("command", choices=tuple(HELP), metavar="COMMAND",
                        help="; ".join(f"{name}: {text}" for name, text in HELP.items()))
    parser.add_argument("--account", dest="account", metavar="HANDLE", help="your Instagram handle (required)")
    # The old spelling keeps working without being advertised.
    parser.add_argument("--username", dest="account", help=argparse.SUPPRESS)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_ROOT, metavar="DIR",
                        help="state directory holding ig/HANDLE.json (default: see ghost-in-the-box config)")
    parser.add_argument("--pages", type=_bounded(1, 5), default=1, metavar="N", help="pages to fetch, 1..5 (default 1)")
    parser.add_argument("--page-size", type=_bounded(1, 33), default=12, metavar="N",
                        help="posts per page, 1..33 (default 12)")
    parser.add_argument("--older", action="store_true", help="continue saved historical pagination")
    parser.add_argument("--deadline", type=_bounded(30, 600), default=120, metavar="SECONDS",
                        help="stop an import after this long, 30..600 (default 120)")
    parser.add_argument("--limit", type=_bounded(1, 20), default=5, metavar="N", help="posts for list, 1..20 (default 5)")
    parser.add_argument("--media-pk", metavar="MEDIA_PK", help="numeric post ID for show")
    return parser


@guarded("instagram-import")
def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.account:
        parser.error("--account HANDLE is required, e.g. instagram-import status --account YOUR_HANDLE")
    account = check_account(parser, args.account)
    os.umask(0o077)
    directory = args.state_root.expanduser() / "ig-corpus" / account
    session_file = args.state_root.expanduser() / "ig" / (account + ".json")
    client = None
    locked = False
    try:
        if args.command == "import":
            with corpus_lock(directory):
                locked = True
                def expired(*_):
                    raise DeadlineExceeded()
                signal.signal(signal.SIGALRM, expired)
                signal.alarm(args.deadline)
                client, audit = make_client(session_file)
                result = import_posts(client, directory, account, pages=args.pages,
                                      page_size=args.page_size, older=args.older, transport=audit)
                signal.alarm(0)
            if not result.get("ok", True):
                code = result.get("error") or ("deadline_exceeded" if result.get("error_type") == "DeadlineExceeded"
                                               else "import_failed")
                result["error"] = _import_error(code, account, session_file)
                emit(result)
                return EXIT_RUNTIME
        elif args.command == "status":
            result = corpus_status(directory)
        elif args.command == "show":
            if not args.media_pk or not args.media_pk.isdecimal():
                raise ImportFailure("show_requires_numeric_media_pk")
            result = read_json(directory / "posts" / (args.media_pk + ".json"))
            if result is None:
                raise ImportFailure("post_not_imported")
        else:
            records = [read_json(p) for p in (directory / "posts").glob("*.json")]
            records.sort(key=lambda row: (row["source_date"], row["media_pk"]), reverse=True)
            result = {"posts": [{k: row[k] for k in ("media_pk", "source_uri", "source_anchor", "source_author", "source_date")} |
                      {"caption": row["caption"][:400], "caption_truncated": len(row["caption"]) > 400}
                      for row in records[:args.limit]]}
        emit(result)
        return EXIT_OK
    except (Exception, DeadlineExceeded) as error:
        if isinstance(error, ImportFailure):
            code = str(error)
            exit_code = EXIT_AUTH if code in SESSION_FAILURES else (
                EXIT_USAGE if code in IMPORT_HINTS else EXIT_RUNTIME)
            result = {"ok": False, "error_type": type(error).__name__,
                      "error": _import_error(code, account, session_file)}
        elif isinstance(error, DeadlineExceeded):
            exit_code = EXIT_RUNTIME
            result = {"ok": False, "error_type": "DeadlineExceeded",
                      "error": _import_error("deadline_exceeded", account, session_file)}
        elif isinstance(error, BlockingIOError):
            exit_code = EXIT_RUNTIME
            result = failure("import_running", f"Another import for {account} is running.",
                             f"wait for it, then check: ghost-in-the-box instagram-import status --account {account}",
                             error_type=type(error).__name__)
        elif isinstance(error, ModuleNotFoundError):
            exit_code = EXIT_AUTH
            result = failure("dependency_unavailable", "The Instagram client library is not installed.",
                             install_command("instagram"), error_type=type(error).__name__)
        else:
            exit_code, result = exception_failure(error, command=COMMAND, reveal=False)
        if args.command == "import" and locked:
            try:
                atomic_json(directory / "status.json", result | {"finished_at": now(), "username": account})
            except OSError:
                result["receipt_written"] = False
        emit(result)
        return exit_code
    finally:
        signal.alarm(0)
        if client is not None:
            client.private.close()
            client.public.close()

if __name__ == "__main__":
    raise SystemExit(main())
