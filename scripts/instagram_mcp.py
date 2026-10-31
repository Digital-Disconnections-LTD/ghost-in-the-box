#!/usr/bin/env python3
"""Discoverable Instagram MCP server (Content-Length or newline JSON stdio)."""

import argparse
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    from farm.instagram_mcp import CAROUSEL_ROOT, DEFAULT_ACCOUNT, MEME_ROOT, REFERENCE_ROOT, TOOLS, InstagramTools, serve
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default=os.environ.get("SOCIAL_FARM_IG_ACCOUNT", DEFAULT_ACCOUNT))
    parser.add_argument("--outbox-db")
    parser.add_argument("--inbox-db")
    parser.add_argument("--carousel-root", type=Path, default=CAROUSEL_ROOT)
    parser.add_argument("--reference-root", type=Path, default=REFERENCE_ROOT,
                        help="private research records and image downloads root")
    parser.add_argument("--meme-root", type=Path, default=MEME_ROOT,
                        help="categorized local meme library root")
    parser.add_argument("--list-tools", action="store_true", help="print the catalog without connecting to Instagram")
    args = parser.parse_args(argv)
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    if args.list_tools:
        print(json.dumps({"tools": TOOLS}, ensure_ascii=False))
        return 0
    try:
        service = InstagramTools(account=args.account, outbox_path=args.outbox_db,
            inbox_path=args.inbox_db, carousel_root=args.carousel_root, reference_root=args.reference_root,
            meme_root=args.meme_root)
        return serve(service)
    except Exception as error:
        # Startup failures must not dump local inputs or provider responses.
        print(f"Instagram MCP unavailable ({type(error).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if importlib.util.find_spec("instagrapi") is None:
        installed = Path(os.environ.get("SOCIAL_FARM_IG_PYTHON") or Path(sys.executable))
        if installed.is_file() and Path(sys.executable).absolute() != installed.absolute():
            os.execv(str(installed), [str(installed), __file__, *sys.argv[1:]])
    raise SystemExit(main())
