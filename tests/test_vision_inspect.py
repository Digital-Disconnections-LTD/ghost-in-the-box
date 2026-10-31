from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from scripts.vision_inspect import inspect, media_type


class VisionInspectTests(unittest.TestCase):
    def test_local_png_is_sent_as_one_typed_image_part(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "meme.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"sample-image-data")
            response = Mock()
            response.json.return_value = {"choices": [{"message": {"content": "A short joke."},
                                                          "finish_reason": "stop"}]}
            client = Mock()
            client.post.return_value = response
            result = inspect(path, "Explain the joke", session=client)
            payload = client.post.call_args.kwargs["json"]
            parts = payload["messages"][0]["content"]
            self.assertEqual(parts[0]["text"], "Explain the joke")
            self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
            self.assertEqual(len(parts), 2)
            self.assertEqual(result["answer"], "A short joke.")
            self.assertNotIn("sample-image-data", str(result))

    def test_unsupported_bytes_are_rejected(self):
        with self.assertRaises(ValueError):
            media_type(b"not an image")


if __name__ == "__main__":
    unittest.main()
