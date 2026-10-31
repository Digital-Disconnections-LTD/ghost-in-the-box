"""One entry point for Ghost in the Box's existing local tools."""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import runpy
import shutil
import sys

from farm.config import STATE_ROOT

COMMANDS = {
    "accounts": ("scripts.social_accounts", "Register and inspect your account sessions"),
    "outbox": ("scripts.social_outbox", "Draft, queue, inspect, and reconcile deliveries"),
    "publish": ("scripts.social_publish_job", "Deliver one explicitly selected outbox job"),
    "brief": ("buzz.buzz_brief", "Turn a source-linked topic pack into platform briefs"),
    "instagram-mcp": ("scripts.instagram_mcp", "Run Instagram tools over MCP stdio"),
    "instagram-inbox": ("scripts.ig_sync_inbox", "Read a bounded Instagram inbox"),
    "instagram-import": ("scripts.ig_import_own", "Import your own writing for voice context"),
    "instagram-login": ("scripts.ig_probe", "Save your own Instagram session"),
    "carousel": ("scripts.instagram_carousel", "Prepare an Instagram carousel draft"),
    "threads": ("scripts.threads_agent", "Inspect Threads state and run a bounded agent step"),
    "threads-setup": ("scripts.threads_browser_setup", "Set up the optional Threads browser transport"),
    "threads-notifications": ("scripts.threads_notifications", "Read and handle Threads inbox updates"),
    "memes": ("scripts.meme_library", "Manage your local visual reference library"),
    "vision": ("scripts.vision_inspect", "Inspect one image with your configured vision model"),
    "substack-mcp": ("scripts.substack_mcp_server", "Run the optional Substack MCP connector"),
    "mcp-bridge": ("scripts.mcp_stdio_bridge", "Connect framed MCP clients to JSONL servers"),
}


def doctor() -> dict:
    """Inspect local prerequisites without loading sessions or contacting providers."""
    return {
        "python": platform.python_version(),
        "platform": sys.platform,
        "state_directory": str(STATE_ROOT),
        "state_exists": STATE_ROOT.is_dir(),
        "optional_modules": {name: importlib.util.find_spec(name) is not None
                             for name in ("instagrapi", "PIL", "scrapling", "substack")},
        "optional_commands": {name: shutil.which(name) is not None
                              for name in ("node", "npm", "tesseract", "xvfb-run")},
        "network_requests": 0,
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="ghost-in-the-box", description="Grow a conversation. Your accounts, your voice, your tools.",
        epilog="Run ghost-in-the-box COMMAND --help for that tool's options.")
    parser.add_argument("command", nargs="?", choices=["doctor", *COMMANDS],
                        help="doctor checks local setup without connecting accounts")
    if not argv or argv[0] in {"-h", "--help"}:
        parser.print_help()
        for name, (_, description) in COMMANDS.items():
            print(f"  {name:23} {description}")
        return 0
    args = parser.parse_args(argv[:1])
    if args.command == "doctor":
        parser.parse_args(argv)
        print(json.dumps(doctor(), indent=2))
        return 0
    optional = {"instagram-login": ("instagrapi", "instagram"),
                "substack-mcp": ("substack_mcp", "substack")}
    if args.command in optional:
        module, extra = optional[args.command]
        if importlib.util.find_spec(module) is None:
            parser.error(f"install the optional dependency: python -m pip install -e '.[{extra}]'")
    previous = sys.argv
    try:
        sys.argv = ["ghost-in-the-box " + args.command, *argv[1:]]
        runpy.run_module(COMMANDS[args.command][0], run_name="__main__")
    finally:
        sys.argv = previous
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
