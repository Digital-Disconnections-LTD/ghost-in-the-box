#!/usr/bin/env python3
"""Publish at most one due Instagram job from the local outbox.

The systemd timer runs this file. It is `ghost-in-the-box publish --due
--platform instagram --limit 1`: auth or dependency problems release the job
back to the queue, and only a possibly sent request becomes uncertain.
"""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.cli_output import guarded
from farm.adapters.instagram import InstagramAdapter
from farm.outbox import Outbox
from scripts.social_publish_job import run_due


def run(*, box_factory=Outbox, adapter_factory=InstagramAdapter) -> int:
    return run_due(box_factory=box_factory, platform="instagram", limit=1,
                   instagram_factory=adapter_factory)


@guarded("publish")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", metavar="PATH", help="private outbox SQLite path (default: see ghost-in-the-box config)")
    args = parser.parse_args(argv)
    return run(box_factory=lambda: Outbox(args.db))


if __name__ == "__main__":
    raise SystemExit(main())
