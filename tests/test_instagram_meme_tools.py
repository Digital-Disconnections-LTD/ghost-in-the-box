import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from farm.accounts import AccountRegistry
from farm.instagram_mcp import InstagramTools, ToolInputError, dispatch
from farm.instagram_references import ReferenceError
from farm.outbox import Outbox
from scripts import meme_library
from _env import IsolatedStateTestCase

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipUnless(Image, "Pillow runtime required (~/.venvs/iglib/bin/python)")
class InstagramMemeToolTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.provider_reads = []
        self.fetches = []
        self.record = SimpleNamespace(id="42_7", pk="42", code="Example", media_type=8,
            user=SimpleNamespace(pk="7", username="meme_author"),
            caption_text="AI assistants debug production code " + "caption " * 80,
            taken_at="2026-10-26T12:00:00Z", like_count=1234, comment_count=27,
            resources=[SimpleNamespace(pk=str(i), media_type=1, thumbnail_url=f"https://cdn.example/{i}.jpg")
                       for i in range(1, 4)])
        client = SimpleNamespace(media_info_v1=lambda _pk: self.record)
        def reader(account, operation, **_kwargs):
            self.provider_reads.append(account)
            return operation(client)
        self.service = InstagramTools(account="mine", reference_root=self.root / "references",
            meme_root=self.library, carousel_root=self.root / "carousels", outbox_path=self.root / "outbox.sqlite3",
            registry=AccountRegistry(self.root / "accounts.json"), reader=reader,
            image_fetcher=self.fetch_image, adapter_factory=lambda: self.fail("local tools must not publish"))
        self.ocr = patch.object(meme_library, "ocr_image", return_value="Claude debugging software " * 100)
        self.ocr.start()

    def tearDown(self):
        self.ocr.stop()
        self.temporary.cleanup()

    def fetch_image(self, url, destination, byte_limit, **_kwargs):
        self.fetches.append(url)
        index = int(url.rsplit("/", 1)[-1].split(".")[0])
        picture = Image.new("RGB", (600, 200) if index == 1 else (200, 600),
                            {1: "red", 2: "blue", 3: "green"}[index])
        body = io.BytesIO()
        picture.save(body, "WEBP" if index == 1 else "PNG")
        destination.write_bytes(body.getvalue())
        return {"size_bytes": len(body.getvalue()), "sha256": hashlib.sha256(body.getvalue()).hexdigest()}

    def call(self, name, **arguments):
        return self.service.call("instagram_" + name, arguments)

    def downloaded(self, amount=2):
        return self.call("download_media", media_id="42", max_images=amount)["image_paths"]

    def seed(self, amount=2):
        return [self.call("import_meme", image_path=path) for path in self.downloaded(amount)]

    def test_import_uses_registered_metadata_and_retries_without_duplicates_or_reads(self):
        path = self.downloaded(1)[0]
        original = Path(path).read_bytes()
        imported = self.call("import_meme", image_path=path)
        self.assertTrue(imported["added"])
        self.assertEqual(imported["library_images"], 1)
        self.assertEqual(imported["source_urls"], ["https://www.instagram.com/p/Example/"])
        self.assertEqual((imported["provider_id"], imported["slide_index"]), ("42", 1))
        row = meme_library.load_rows(self.library)[0]
        self.assertEqual(row["creator"], "meme_author")
        self.assertEqual(row["caption"], self.record.caption_text)
        self.assertEqual((row["likes"], row["comments"]), (1234, 27))
        self.assertEqual(row["image_url"], "https://cdn.example/1.jpg")
        self.assertEqual(Path(imported["image"]).read_bytes(), original)
        repeated = self.call("import_meme", image_path=path)
        self.assertFalse(repeated["added"])
        self.assertEqual(repeated["id"], imported["id"])
        self.assertEqual(self.provider_reads, ["mine"])
        self.assertEqual(len(self.fetches), 1)

    def test_search_is_paginated_and_text_and_sources_are_bounded(self):
        self.seed(3)
        rows = meme_library.load_rows(self.library)
        for index in range(6):
            meme_library.add_source(rows[0], {**rows[0], "post_url": f"https://example.test/post/{index}"})
        meme_library.atomic_write(self.library / "manifest.jsonl", meme_library.jsonl(rows))
        first = self.call("search_memes", query="Claude", category="ai", limit=1)
        second = self.call("search_memes", query="Claude", category="ai", limit=1, offset=first["next_offset"])
        last = self.call("search_memes", query="Claude", category="ai", limit=1, offset=2)
        self.assertEqual(first["library_images"], 3)
        self.assertEqual(first["category_counts"]["ai"], 3)
        self.assertNotEqual(first["memes"][0]["id"], second["memes"][0]["id"])
        self.assertIsNone(last["next_offset"])
        for result in (first, second, last):
            row = result["memes"][0]
            self.assertLessEqual(len(row["ocr_excerpt"]), 600)
            self.assertLessEqual(len(row["title"]), 300)
            self.assertLessEqual(len(row["source_urls"]), 3)
            self.assertEqual(row["score_kind"], "likes")
        self.assertEqual(first["memes"][0]["source_count"], 7)
        self.assertEqual(self.call("search_memes", query="not-found")["memes"], [])
        self.assertEqual(self.provider_reads, ["mine"])

    def test_prepare_preserves_order_and_originals_and_feeds_the_existing_draft_tool(self):
        seeded = self.seed(2)
        originals = [Path(row["image"]).read_bytes() for row in seeded]
        prepared = self.call("prepare_meme_carousel", items=[seeded[1]["id"], seeded[0]["image"]])
        self.assertEqual(len(prepared["images"]), 2)
        self.assertFalse(prepared["source_urls_truncated"])
        for index, path in enumerate(prepared["images"]):
            self.assertTrue(Path(path).is_relative_to(self.root / "carousels/prepared/mine"))
            with Image.open(path) as picture:
                self.assertEqual(picture.size, (1080, 1350))
                self.assertEqual(picture.format, "JPEG")
                self.assertGreater(picture.getpixel((540, 675))[2 if index == 0 else 0], 240)
        self.assertEqual(originals, [Path(row["image"]).read_bytes() for row in seeded])
        drafted = self.call("draft_carousel", caption="A developer's day", images=prepared["images"],
                            source_urls=prepared["source_urls"], idempotency_key="meme-test")
        self.assertEqual(drafted["job"]["status"], "draft")
        self.assertEqual(drafted["job"]["source_urls"], prepared["source_urls"])
        with Outbox(self.root / "outbox.sqlite3") as box:
            self.assertEqual(box.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(self.provider_reads, ["mine"])

    def test_prepare_caps_returned_sources_and_keeps_all_in_its_manifest(self):
        seeded = self.seed(2)
        rows = meme_library.load_rows(self.library)
        for index in range(40):
            meme_library.add_source(rows[0], {**rows[0], "post_url": f"https://example.test/post/{index}"})
        meme_library.atomic_write(self.library / "manifest.jsonl", meme_library.jsonl(rows))
        result = self.call("prepare_meme_carousel", items=[row["id"] for row in seeded])
        self.assertEqual(len(result["source_urls"]), 30)
        self.assertTrue(result["source_urls_truncated"])
        complete = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(len(complete["source_urls"]), result["source_count"])
        self.assertGreater(result["source_count"], 30)

    def test_import_rejects_arbitrary_or_changed_files_before_library_mutation(self):
        outsider = self.root / "not-a-download.png"
        outsider.write_bytes(b"image")
        with self.assertRaises(ReferenceError):
            self.call("import_meme", image_path=str(outsider))
        path = Path(self.downloaded(1)[0])
        path.write_bytes(path.read_bytes() + b"tampered")
        with self.assertRaises(ReferenceError):
            self.call("import_meme", image_path=str(path))
        self.assertFalse((self.library / "manifest.jsonl").exists())

    def test_schema_and_selection_failures_do_not_call_collection_or_prepare_invalid_input(self):
        with patch.object(meme_library, "collect") as collect, patch.object(meme_library, "prepare_carousel") as prepare:
            for name, args in (("search_memes", {"limit": 21}), ("search_memes", {"offset": 10001}),
                               ("search_memes", {"category": "unknown"}),
                               ("prepare_meme_carousel", {"items": ["only-one"]})):
                with self.assertRaises(ToolInputError):
                    self.call(name, **args)
            collect.assert_not_called()
            prepare.assert_not_called()
        seeded = self.seed(2)
        for selections in ([seeded[0]["id"]] * 2, ["missing-id", seeded[1]["id"]]):
            reply = dispatch(self.service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "instagram_prepare_meme_carousel", "arguments": {"items": selections}}})
            self.assertTrue(reply["result"]["isError"])
        self.assertFalse((self.root / "carousels").exists())

    def test_existing_server_discovers_new_tools_and_searches_isolated_library(self):
        self.seed(1)
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "instagram_search_memes", "arguments": {"category": "ai", "limit": 1}}},
        ]
        process = subprocess.run([sys.executable, "scripts/instagram_mcp.py", "--account", "mine",
            "--meme-root", str(self.library), "--reference-root", str(self.root / "references"),
            "--carousel-root", str(self.root / "carousels"), "--outbox-db", str(self.root / "outbox.sqlite3")],
            input="".join(json.dumps(message) + "\n" for message in messages).encode(),
            capture_output=True, timeout=15, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        tools = {tool["name"]: tool for tool in replies[0]["result"]["tools"]}
        for name in ("instagram_search_memes", "instagram_import_meme", "instagram_prepare_meme_carousel"):
            self.assertIn(name, tools)
            self.assertFalse(tools[name]["annotations"]["openWorldHint"])
        result = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertEqual(result["library_images"], 1)
        self.assertEqual(len(result["memes"]), 1)
        self.assertEqual(process.stderr, b"")


if __name__ == "__main__":
    unittest.main()
