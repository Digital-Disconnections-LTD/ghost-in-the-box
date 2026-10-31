import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import meme_library as library

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipUnless(Image, "Pillow runtime required (~/.venvs/iglib/bin/python)")
class MemeLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "images").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def row(self, title="Debugging in production", color="red", size=(400, 200), format="PNG", number=1):
        blob = io.BytesIO()
        Image.new("RGB", size, color).save(blob, format)
        body = blob.getvalue()
        digest = hashlib.sha256(body).hexdigest()
        metadata = library.inspect_image(body)
        suffix = metadata.pop("suffix")
        path = self.root / "images" / (digest + suffix)
        path.write_bytes(body)
        return {"title": title, "sha256": digest, "bytes": len(body), **metadata,
                "local_path": str(path.relative_to(self.root)), "post_url": f"https://example.test/post/{number}",
                "image_url": f"https://example.test/image/{number}.png", "score": 200,
                "comments": 12, "community": "https://programming.dev/c/programmer_humor", "ocr_text": title}

    def save(self, rows):
        (self.root / "manifest.jsonl").write_text(library.jsonl(rows))

    def test_collect_appends_keeps_old_rows_and_merges_repost_provenance(self):
        original = self.row()
        self.save([original])
        new = self.row("AI assistants and vibe coding", "blue", number=2)
        repost = {**new, "post_url": "https://example.test/post/3", "image_url": "https://example.test/image/3.png"}
        with patch.object(library, "fetch_candidates", return_value=([new, repost], [])), \
                patch.object(library, "download", side_effect=lambda c, _: ({**c, "_new_file": False}, None)):
            report = library.collect(self.root, 3, 1, 0, None)
        rows = library.load_rows(self.root)
        self.assertEqual(report["added_images"], 1)
        self.assertEqual(report["duplicates_merged"], 1)
        self.assertEqual(report["previously_unindexed_cached_files"], 1)
        self.assertEqual(len(rows), 2)
        by_hash = {r["sha256"]: r for r in rows}
        self.assertIn(original["sha256"], by_hash)
        self.assertEqual(len(by_hash[new["sha256"]]["sources"]), 2)
        with patch.object(library, "fetch_candidates", return_value=([new, repost], [])), \
                patch.object(library, "download") as download:
            repeated = library.collect(self.root, 1, 1, 0, None)
        self.assertEqual(repeated["images"], 2)
        self.assertEqual(repeated["added_images"], 0)
        download.assert_not_called()

    def test_same_pixels_are_one_cached_image_with_both_sources(self):
        old = self.row()
        alternate = self.row(format="WEBP", number=2)
        # Lossless PNG vs another PNG encoding is a reliable pixel-equivalent fixture.
        picture = Image.open(self.root / old["local_path"])
        blob = io.BytesIO()
        picture.save(blob, "PNG", compress_level=0)
        digest = hashlib.sha256(blob.getvalue()).hexdigest()
        alternate.update(sha256=digest, local_path=f"images/{digest}.png", pixel_sha256=old["pixel_sha256"])
        (self.root / alternate["local_path"]).write_bytes(blob.getvalue())
        self.save([old])
        with patch.object(library, "fetch_candidates", return_value=([alternate], [])), \
                patch.object(library, "download", return_value=({**alternate, "_new_file": True}, None)):
            report = library.collect(self.root, 2, 1, 0, None)
        self.assertEqual(report["images"], 1)
        self.assertEqual(report["new_files_downloaded"], 0)
        self.assertEqual(len(library.load_rows(self.root)[0]["sources"]), 2)
        self.assertFalse((self.root / alternate["local_path"]).exists())

    def test_topic_links_preserve_original_and_unrelated_user_files(self):
        row = self.row("ChatGPT writes code; subscription privacy")
        self.save([row])
        original = (self.root / row["local_path"]).read_bytes()
        library.organize(self.root)
        for topic in ("ai", "software-dev", "ownership-privacy"):
            links = [p for p in (self.root / "categories" / topic).iterdir() if p.is_symlink()]
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0].read_bytes(), original)
            gallery = (self.root / "categories" / topic / "index.html").read_text()
            self.assertIn("../../" + row["local_path"], gallery)
            self.assertIn(f"{topic} memes", gallery)
        user_file = self.root / "categories/ai/my-note.txt"
        user_file.write_text("keep")
        library.organize(self.root)
        self.assertEqual(user_file.read_text(), "keep")
        self.assertEqual((self.root / row["local_path"]).read_bytes(), original)

    def test_search_filters_ocr_category_and_engagement(self):
        ai = self.row("Plain title", "blue")
        ai["ocr_text"] = "ChatGPT hallucinating production code"
        other = self.row("Debugging a race condition", "green", number=2)
        other["score"] = 5
        self.save([other, ai])
        library.organize(self.root)
        result = library.search(self.root, "hallucinating", "ai", 10, 100)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], ai["sha256"][:12])
        self.assertTrue(Path(result[0]["image"]).is_absolute())
        self.assertEqual(result[0]["source_urls"], [ai["post_url"]])
        self.assertNotIn("ai", library.categories_for({"title": "rain and train schedules"}))
        self.assertNotIn("ai", library.categories_for({"title": "Windows search", "ocr_text": "Command Prompt\n\nai\n"}))
        self.assertNotIn("ai", library.categories_for({"title": "Thank you", "body": "The template is AI generated.", "ocr_text": "A thank you card\n\naI\n"}))
        self.assertIn("ai", library.categories_for({"title": "Office software", "ocr_text": "Al assistants writing code"}))
        self.assertNotIn("ownership-privacy", library.categories_for({"title": "Private school tuition"}))
        self.assertEqual(library.categories_for({"title": "Private school tuition"}), ["review-needed"])

    def test_prepare_mixed_ratios_webp_order_padding_sources_and_originals(self):
        wide = self.row("Wide", "red", (600, 200), format="WEBP")
        tall = self.row("Tall", "blue", (200, 600), number=2)
        self.save([wide, tall])
        originals = [(self.root / row["local_path"]).read_bytes() for row in (wide, tall)]
        output = self.root / "prepared"
        result = library.prepare_carousel(self.root, [tall["sha256"][:12], wide["local_path"]], output)
        self.assertEqual(result["source_urls"], [tall["post_url"], wide["post_url"]])
        self.assertEqual([Path(p).name for p in result["images"]], ["slide-01.jpg", "slide-02.jpg"])
        with Image.open(result["images"][0]) as slide:
            self.assertEqual(slide.size, (1080, 1350))
            self.assertEqual(slide.format, "JPEG")
            self.assertGreater(slide.getpixel((540, 675))[2], 240)
            self.assertLess(max(slide.getpixel((0, 675))), 30)
        with Image.open(result["images"][1]) as slide:
            self.assertGreater(slide.getpixel((540, 675))[0], 240)
            self.assertLess(max(slide.getpixel((540, 0))), 30)
        self.assertEqual(originals, [(self.root / row["local_path"]).read_bytes() for row in (wide, tall)])
        metadata = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(metadata["slides"][0]["id"], tall["sha256"][:12])
        with self.assertRaisesRegex(ValueError, "new or empty"):
            library.prepare_carousel(self.root, [wide["sha256"], tall["sha256"]], output)

    def test_prepare_rejects_ambiguous_missing_or_duplicate_selection(self):
        rows = [self.row("One", "red"), self.row("Two", "blue", number=2)]
        self.save(rows)
        with self.assertRaisesRegex(ValueError, "identify one"):
            library.prepare_carousel(self.root, ["missing", rows[0]["sha256"]], self.root / "bad")
        with self.assertRaisesRegex(ValueError, "different"):
            library.prepare_carousel(self.root, [rows[0]["sha256"]] * 2, self.root / "bad")
        self.assertFalse((self.root / "bad").exists())

    def test_non_images_and_small_images_are_rejected(self):
        with self.assertRaises(Exception):
            library.inspect_image(b"<html>not a meme</html>")
        blob = io.BytesIO()
        Image.new("RGB", (40, 40)).save(blob, "PNG")
        with self.assertRaisesRegex(ValueError, "too small"):
            library.inspect_image(blob.getvalue())
        self.assertFalse(library.image_url_ok("file:///etc/passwd.png"))
        self.assertTrue(library.image_url_ok("https://example.test/api/v3/image_proxy?url=https%3A%2F%2Fexample.test%2Fa.png"))

    def test_import_download_records_source_dedupes_and_can_be_prepared(self):
        downloaded = self.root / "instagram-download.webp"
        Image.new("RGB", (300, 500), "yellow").save(downloaded, "WEBP")
        original = downloaded.read_bytes()
        metadata = {"source_url": "https://www.instagram.com/p/Example/", "author": "example",
                    "caption": "AI assistants debugging production code", "likes": 1234, "comments": 24}
        with patch.object(library, "ocr_image", return_value="An AI debugger joke"):
            result = library.import_local(self.root, downloaded, metadata)
        self.assertTrue(result["added"])
        self.assertEqual(result["library_images"], 1)
        self.assertIn("ai", result["categories"])
        self.assertEqual(Path(result["image"]).read_bytes(), original)
        stored = library.load_rows(self.root)[0]
        self.assertEqual(stored["likes"], 1234)
        self.assertEqual(library.search(self.root, "", "ai", 1, 0)[0]["score_kind"], "likes")
        self.assertEqual(stored["sources"][0]["creator"], "example")
        self.assertEqual(stored["sources"][0]["caption"], metadata["caption"])
        repeated = library.import_local(self.root, downloaded, {**metadata, "source_url": "https://www.instagram.com/p/Repost/"})
        self.assertFalse(repeated["added"])
        self.assertEqual(len(repeated["source_urls"]), 2)
        self.assertEqual(len(list((self.root / "images").iterdir())), 1)
        second = self.row("Software deployment", "purple", number=2)
        rows = library.load_rows(self.root)
        self.save(rows + [second])
        prepared = library.prepare_carousel(self.root, [result["id"], second["sha256"]], self.root / "prepared")
        self.assertEqual(prepared["source_urls"][:2], repeated["source_urls"])
        self.assertEqual(downloaded.read_bytes(), original)

    def test_import_rejects_missing_provenance_and_invalid_counts(self):
        row = self.row()
        image = self.root / row["local_path"]
        with self.assertRaisesRegex(ValueError, "HTTPS post URL"):
            library.import_local(self.root, image, {})
        with self.assertRaisesRegex(ValueError, "nonnegative integers"):
            library.import_local(self.root, image, {"source_url": "https://instagram.com/p/x/", "likes": -1})
        self.assertFalse((self.root / "manifest.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
