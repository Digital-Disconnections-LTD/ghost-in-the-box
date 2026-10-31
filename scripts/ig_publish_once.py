#!/usr/bin/env python3
"""Publish at most one queued Instagram job from the local outbox."""

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.adapters.instagram import InstagramAdapter, PreSendFailure, PublishUncertain
from farm.outbox import Outbox


def main() -> int:
    os.umask(0o077)
    with Outbox() as box:
        job = box.claim_due(platform="instagram")
        if job is None:
            print(json.dumps({"ok": True, "status": "idle"}))
            return 0
        adapter = InstagramAdapter()
        try:
            receipt = adapter.publish(job)
        except PreSendFailure as exc:
            failed = box.finish(
                job["id"], job["lease_token"], outcome="failed", error=str(exc)
            )
            print(json.dumps({"ok": False, "status": "failed_before_send",
                              "job_id": failed["id"], "reason": str(exc)}))
            return 2
        except PublishUncertain as exc:
            uncertain = box.finish(
                job["id"], job["lease_token"], outcome="uncertain",
                error=f"provider call uncertain: {type(exc.__cause__).__name__}",
            )
            print(json.dumps({"ok": False, "status": "uncertain",
                              "job_id": uncertain["id"]}))
            return 3

        published = box.finish(
            job["id"], job["lease_token"], outcome="published",
            provider_id=receipt["provider_id"], permalink=receipt["permalink"],
        )
        session_saved = True
        try:
            adapter.save_session()
        except Exception:
            # The provider receipt is already durable; a session-save failure
            # must never make this post eligible for another send.
            session_saved = False
        print(json.dumps({
            "ok": True, "status": "published", "job_id": published["id"],
            "provider_id": published["provider_id"],
            "permalink": published["permalink"],
            "session_saved": session_saved,
        }))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
