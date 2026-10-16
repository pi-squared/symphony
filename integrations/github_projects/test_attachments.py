import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import attachments as a

class AttachmentTests(unittest.TestCase):
    def test_retry_reuses_verified_upload_and_existing_comment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "capture.webm"
            content = b"unchanged capture"
            video.write_bytes(content)
            capture = {"video": {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)},
                "video_path": str(video), "scenario": "test-flow", "id": "a"*32,
                "results": [{"seconds": 1, "name": "Full app", "result": "passed"}],
                "workspace": "/local/GH-4", "source": {"sha256": "source"}, "head": "head",
                "build": {"sha256": "build"}}
            task = SimpleNamespace(repository="owner/repo", number=4)
            publisher = a.Attachments("gh", root / "uploads", task.repository)
            comments, uploads = [], []
            url = "https://github.com/user-attachments/assets/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            def api(endpoint, *, payload=None, **kwargs):
                if endpoint.startswith("https://uploads."):
                    uploads.append(endpoint)
                    return json.dumps({"url": url}).encode()
                if endpoint == "repos/owner/repo": return b'{"id": 123, "full_name": "owner/repo"}'
                if endpoint == "markdown": return f'<video src="{url}"></video>'.encode()
                if "comments?" in endpoint: return json.dumps(comments).encode()
                if endpoint.endswith("/comments"):
                    comment = {"id": len(comments)+1, "body": payload["body"], "html_url": "https://github.com/owner/repo/issues/4#issuecomment-"+str(len(comments)+1)}
                    comments.append(comment)
                    return json.dumps(comment).encode()
                if "/issues/comments/" in endpoint:
                    comment = comments[int(endpoint.rsplit("/",1)[1])-1]
                    return json.dumps({**comment, "body_html": f'<video src="{url}"></video>'}).encode()
                raise AssertionError(endpoint)
            with patch.object(publisher, "api", side_effect=api), patch.object(publisher, "verify", return_value={
                    "video_player_rendered": True, "download_sha256_verified": True}), patch.object(
                    a.subprocess, "check_output", return_value="gh version 2.102.0 (fixture)\n"):
                first = publisher.publish(task, capture)
                second = publisher.publish(task, capture)
                third = publisher.publish(task, {**capture, "id": "b"*32, "source": {"sha256": "new-source"}})
            self.assertEqual(first["comment_id"], second["comment_id"])
            self.assertEqual(len(uploads), 1)
            self.assertEqual(len(comments), 2)
            self.assertNotEqual(third["comment_id"], first["comment_id"])
            self.assertIn("source `new-source`", comments[1]["body"])

    def test_rendered_download_must_match_original_hash_and_size(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, maximum): return b"incorrect video"
        with patch.object(a, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "hash or byte count"):
                a.Attachments("gh", "/tmp", "owner/repo").verify(
                    '<video src="https://github.com/user-attachments/assets/fixture"></video>',
                    {"bytes": 15, "sha256": "0"*64})
        with self.assertRaisesRegex(ValueError, "exactly one"):
            a.Attachments("gh", "/tmp", "owner/repo").verify('<a href="video.webm">download</a>', {})

    def test_pr_body_rejects_native_urls_and_video_embeds(self):
        for body in ['<video src="x">', 'https://github.com/user-attachments/assets/123',
                     '[video](https://example.invalid/capture.webm)']:
            with self.assertRaises(ValueError):
                a.reject_pr_videos(body)
        a.reject_pr_videos("See https://github.com/owner/repo/issues/4#issuecomment-123")


if __name__ == "__main__":
    unittest.main()
