#!/usr/bin/env python3
"""Print every Ghost in the Box MCP server's tool catalog, offline.

  ghost-in-the-box mcp-tools                 JSON summary of every server's tools
  ghost-in-the-box mcp-tools --format md     the Markdown tables docs/mcp.md is generated from
  ghost-in-the-box mcp-tools --format mcp    full tool definitions with input schemas

Needs no account, session or network. The Substack server's own tools are read
from its source, so the substack extra is not needed either.
"""

import argparse
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GENERATED_BY = "ghost-in-the-box mcp-tools"


def catalog(substack_path=None):
    """Instagram, LinkedIn, then Substack (when its source is present)."""
    from farm import instagram_mcp, linkedin_mcp, mcp_server
    instagram = (instagram_mcp.catalog() if substack_path is None
                 else instagram_mcp.catalog(substack_path))["servers"]
    return mcp_server.catalog_document(GENERATED_BY, [instagram[0], linkedin_mcp.server_entry(), *instagram[1:]])


def definitions():
    from farm import instagram_mcp, linkedin_mcp
    return {"servers": [{"server": instagram_mcp.SERVER_NAME, "tools": instagram_mcp.TOOLS},
                        {"server": linkedin_mcp.SERVER_NAME, "tools": linkedin_mcp.TOOLS}]}


def main(argv=None):
    from farm import mcp_server
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--format", choices=("json", "md", "mcp"), default="json",
                        help="json summary (default), md tables, or full mcp definitions")
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    if args.format == "md":
        print(mcp_server.catalog_markdown(catalog()))
    elif args.format == "mcp":
        print(json.dumps(definitions(), ensure_ascii=False))
    else:
        print(json.dumps(catalog(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
