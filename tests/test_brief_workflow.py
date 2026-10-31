import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from buzz.buzz_brief import _parse_source, render
from farm.outbox import Outbox


class BriefWorkflowTests(unittest.TestCase):
    def test_labeled_source_preserves_its_url(self):
        self.assertEqual(_parse_source("Mission=https://example.org/mission"),
                         {"label": "Mission", "url": "https://example.org/mission"})

    def test_platform_cap_counts_characters(self):
        _, receipt, _ = render({"title": "A thought", "why": ["An example"],
            "angle": ["Keep it clear"], "lanes": ["threads"],
            "drafts": {"threads": "x" * 501}})
        self.assertTrue(receipt["lanes"][0]["over_cap"])

    def run_cli(self, *args, env):
        return subprocess.run([sys.executable, "-m", "farm.cli", *args],
                              capture_output=True, text=True, env=env, check=True)

    def test_emitted_scaffold_matches_the_brief_receipt(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            env = dict(os.environ, SOCIAL_FARM_STATE_DIR=str(root / "state"))
            self.run_cli("brief", "--title", "A new thought", "--why", "A local example",
                         "--angle", "A clear idea", "--lane", "threads", "--emit",
                         "--out", str(root / "briefs"), env=env)
            body = next((root / "briefs").glob("*.threads.body")).read_text().rstrip("\n")
            receipt = json.loads(next((root / "briefs").glob("*.receipt.json")).read_text())
            self.assertTrue(body.strip())
            self.assertEqual(hashlib.sha256(body.encode()).hexdigest(),
                             receipt["lanes"][0]["body_sha256"])

    def test_reviewed_brief_becomes_one_frozen_job_with_sources(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            env = dict(os.environ, SOCIAL_FARM_STATE_DIR=str(root / "state"))
            example = Path(__file__).resolve().parents[1] / "examples/topic-pack.json"
            result = self.run_cli("brief", "--json", str(example), "--out",
                                  str(root / "briefs"), "--emit", env=env)
            recipe = next(line.strip() for line in result.stdout.splitlines()
                          if line.strip().startswith("ghost-in-the-box outbox draft --platform threads"))
            arguments = shlex.split(recipe)[1:]
            arguments[arguments.index("YOUR_HANDLE")] = "example_creator"
            drafted = json.loads(self.run_cli(*arguments, env=env).stdout)["result"]
            queued = json.loads(self.run_cli("outbox", "queue", drafted["id"], env=env).stdout)["result"]
            self.assertEqual(queued["status"], "queued")
            with Outbox(root / "state/outbox.sqlite3") as box:
                job = box.get(drafted["id"])
                self.assertIn("https://digitaldisconnections.com/our-story.html", job["source_urls"])
                self.assertEqual(job["body"].strip(), json.loads(example.read_text())["drafts"]["threads"])
                self.assertEqual(len(box.list(status="all")), 1)
                self.assertIsNone(job["provider_id"])


if __name__ == "__main__":
    unittest.main()
