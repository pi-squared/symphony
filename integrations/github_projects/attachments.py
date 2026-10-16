"""Native GitHub attachment publication, with rendered-player byte verification.

Adapted from DocumentAnalysis request 6146672007/upload_experiment.py. Upload
receipts are durable before rendering/commenting so retries reuse the asset.
"""
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

from browser import write_json


class Players(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "video" and attrs.get("src"):
            self.sources.append(attrs["src"])


def reject_pr_videos(body):
    if re.search(r"<video\b|https://github\.com/user-attachments/assets/|https?://\S+\.(?:webm|mp4|mov)(?:[\s)\"?#]|$)", body, re.I):
        raise ValueError("PR descriptions must point to the issue discussion, without direct video links or embeds")


class Attachments:
    def __init__(self, gh, directory, repository, runtime=None):
        self.gh = str(gh)
        self.directory = Path(directory)
        self.repository = repository
        self.runtime = runtime

    def api(self, endpoint, *, payload=None, headers=(), input_path=None):
        args = [self.gh, "api", endpoint]
        for header in headers:
            args += ["--header", header]
        if payload is not None or input_path:
            args += ["--method", "POST", "--input", str(input_path) if input_path else "-"]
        result = subprocess.run(args, input=json.dumps(payload).encode() if payload is not None else None,
                                capture_output=True, timeout=180)
        if result.returncode:
            raise ValueError("GitHub attachment operation failed (private diagnostics required)")
        return result.stdout

    def verify(self, html, expected):
        players = Players()
        players.feed(html)
        if len(players.sources) != 1:
            raise ValueError("Attachment did not render as exactly one video player")
        url = players.sources[0]
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in {"github.com", "private-user-images.githubusercontent.com", "user-images.githubusercontent.com"}:
            raise ValueError("GitHub returned an unexpected media origin")
        with urlopen(url, timeout=90) as response:
            data = response.read(expected["bytes"] + 1)
        if len(data) != expected["bytes"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise ValueError("Rendered video download hash or byte count differs from the original capture")
        return {"video_player_rendered": True, "download_sha256_verified": True}

    def publish(self, task, capture):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache = self.directory / (capture["video"]["sha256"] + ".json")
        expected = capture["video"]
        video = Path(capture["video_path"])
        if len(video.read_bytes()) != expected["bytes"] or hashlib.sha256(video.read_bytes()).hexdigest() != expected["sha256"]:
            raise ValueError("Capture changed before native upload")
        if cache.exists():
            row = json.loads(cache.read_text())
            if row["repository"] != task.repository or row["issue"] != task.number:
                raise ValueError("Upload cache belongs to another destination")
        else:
            version = subprocess.check_output([self.gh, "--version"], text=True).splitlines()[0]
            if not version.startswith("gh version 2.102.0 "):
                raise ValueError("Native uploads require the verified GitHub CLI 2.102.0")
            repo = json.loads(self.api("repos/" + self.repository))
            if repo["full_name"] != task.repository:
                raise ValueError("Attachment destination does not match the bound issue repository")
            endpoint = "https://uploads.github.com/user-attachments/assets?" + urlencode({
                "name": f"issue-{task.number}-{capture['scenario']}.webm", "content_type": "video/webm",
                "repository_id": repo["id"]})
            result = json.loads(self.api(endpoint, input_path=video,
                headers=["Content-Type: application/octet-stream", "Accept: application/vnd.github+json"]))
            if not re.fullmatch(r"https://github\.com/user-attachments/assets/[0-9a-f-]+", result["url"]):
                raise ValueError("GitHub returned an invalid native attachment URL")
            row = {"url": result["url"], "repository": task.repository, "issue": task.number,
                   "sha256": expected["sha256"], "bytes": expected["bytes"],
                   "repository_id": repo["id"], "uploader": "gh 2.102.0",
                   "upload_endpoint": "https://uploads.github.com/user-attachments/assets"}
            write_json(cache, row)
        html = self.api("markdown", payload={"text": row["url"], "mode": "gfm", "context": task.repository}).decode()
        row.update(self.verify(html, expected))
        write_json(cache, row)
        legacy_marker = f"<!-- symphony-full-app:{task.number}:{capture['scenario']}:{expected['sha256']} -->"
        marker = f"<!-- symphony-full-app:{task.number}:{capture['scenario']}:{expected['sha256']}:{capture['source']['sha256']} -->"
        timestamps = "\n".join(f"- {r['seconds']:.1f}s: {r['name']} — passed" for r in capture["results"])
        body = (f"{marker}\nFull application verification.\n\n"
                f"{row['url']}\n\n{timestamps}\n\n"
                f"Tested workspace `{Path(capture['workspace']).name}`, source `{capture['source']['sha256']}`, "
                f"starting commit `{capture['head']}`; production build `{capture['build']['sha256']}`.\n\n"
                "Scope: application worker configured by the installation operator. "
                f"Scenario `{capture['scenario']}`. Video SHA-256 `{expected['sha256']}`.")
        comments = []
        page = 1
        while True:
            batch = json.loads(self.api(f"repos/{task.repository}/issues/{task.number}/comments?per_page=100&page={page}"))
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        matches = [c for c in comments if (c.get("body") or "").startswith(marker) or (
            (c.get("body") or "").startswith(legacy_marker)
            and f"source `{capture['source']['sha256']}`" in c["body"]
            and f"production build `{capture['build']['sha256']}`" in c["body"])]
        if len(matches) > 1:
            raise ValueError("Duplicate publication comments exist; preserve them for inspection")
        comment = matches[0] if matches else json.loads(self.api(
            f"repos/{task.repository}/issues/{task.number}/comments", payload={"body": body}))
        # Verify the actual persisted issue comment, not just a preview render.
        rendered = json.loads(self.api(f"repos/{task.repository}/issues/comments/{comment['id']}",
                                      headers=["Accept: application/vnd.github.full+json"]))
        if row["url"] not in rendered.get("body", "").split("\n\n"):
            raise ValueError("The persisted issue comment does not contain the standalone attachment")
        row.update(self.verify(rendered["body_html"], expected))
        if self.runtime:
            playback_input = self.directory / (expected["sha256"] + "-playback.json")
            write_json(playback_input, {"comment_html": rendered["body_html"],
                "duration": expected["duration"], "browser_cache": self.runtime["browser_cache"]})
            result = subprocess.run([self.runtime["python"], str(Path(__file__).with_name("browser_playback.py")),
                                     str(playback_input)], capture_output=True, timeout=90)
            if result.returncode:
                raise ValueError("The persisted issue comment player failed browser playback")
            row["playback"] = json.loads(result.stdout)
        row.update(comment_url=rendered["html_url"], comment_id=comment["id"], capture_id=capture["id"])
        write_json(cache, row)
        return row
