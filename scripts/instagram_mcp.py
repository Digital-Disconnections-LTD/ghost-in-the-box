#!/usr/bin/env python3
"""Instagram MCP server over stdio (standard newline JSON, or Content-Length frames)."""

import argparse
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.cli_output import DOCTOR, EXIT_RUNTIME, EXIT_USAGE, emit, failure


def _startup_failure(code, message, hint, exit_code, **extra):
    # stdout belongs to the MCP protocol, and clients often hide stderr, so say it
    # plainly first and then give the same envelope every command prints.
    print(f"instagram-mcp: {message}\nhint: {hint}", file=sys.stderr)
    emit(failure(code, message, hint, **extra), stream=sys.stderr)
    return exit_code


def main(argv=None):
    from farm.instagram_mcp import (
        CAROUSEL_ROOT, MEME_ROOT, REFERENCE_ROOT, TOOLS, AccountSelectionError, InstagramTools,
        catalog, catalog_markdown, resolve_account, serve,
    )
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Without --account the server uses SOCIAL_FARM_IG_ACCOUNT, or the only registered "
               "Instagram account. List the tools offline with --list-tools [--format md].")
    parser.add_argument("--account", metavar="HANDLE", help="registered Instagram handle to serve")
    parser.add_argument("--outbox-db", metavar="PATH", help="outbox SQLite path (default: see ghost-in-the-box config)")
    parser.add_argument("--inbox-db", metavar="PATH", help="inbox SQLite path (default: see ghost-in-the-box config)")
    parser.add_argument("--carousel-root", type=Path, default=CAROUSEL_ROOT, metavar="DIR",
                        help="where carousel drafts snapshot their slides")
    parser.add_argument("--reference-root", type=Path, default=REFERENCE_ROOT, metavar="DIR",
                        help="private research records and image downloads root")
    parser.add_argument("--meme-root", type=Path, default=MEME_ROOT, metavar="DIR",
                        help="categorized local meme library root")
    parser.add_argument("--list-tools", action="store_true",
                        help="print the tool catalog and exit, without an account or Instagram")
    parser.add_argument("--format", choices=("json", "md", "mcp"), default="json",
                        help="--list-tools output: json summary (default), md table, or full mcp definitions")
    args = parser.parse_args(argv)
    if args.format != "json" and not args.list_tools:
        parser.error("--format applies only with --list-tools")
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    if args.list_tools:
        if args.format == "md":
            print(catalog_markdown(catalog()))
        elif args.format == "mcp":
            print(json.dumps({"tools": TOOLS}, ensure_ascii=False))
        else:
            print(json.dumps(catalog(), ensure_ascii=False, indent=2))
        return 0
    try:
        account = resolve_account(args.account)
    except AccountSelectionError as error:
        # Exit before any handshake: a server for the wrong account must never start.
        return _startup_failure("account_unresolved", str(error), error.hint, EXIT_USAGE)
    try:
        service = InstagramTools(account=account, outbox_path=args.outbox_db,
            inbox_path=args.inbox_db, carousel_root=args.carousel_root, reference_root=args.reference_root,
            meme_root=args.meme_root)
        return serve(service)
    except Exception as error:
        # Startup failures must not dump local inputs or provider responses.
        return _startup_failure("server_unavailable", f"Instagram MCP unavailable ({type(error).__name__})",
                                DOCTOR, EXIT_RUNTIME, error_type=type(error).__name__)


if __name__ == "__main__":
    if importlib.util.find_spec("instagrapi") is None:
        installed = Path(os.environ.get("SOCIAL_FARM_IG_PYTHON") or Path(sys.executable))
        if installed.is_file() and Path(sys.executable).absolute() != installed.absolute():
            os.execv(str(installed), [str(installed), __file__, *sys.argv[1:]])
    raise SystemExit(main())
