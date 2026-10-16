"""Run an operator-configured application worker outside the controller lock."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from browser import inventory, write_json


def invoke(mode, path, session):
    runtime = session["runtime"]
    worker = Path(runtime["worker"])
    if hashlib.sha256(worker.read_bytes()).hexdigest() != runtime["worker_sha256"]:
        raise ValueError("The configured application worker changed during this session")
    subprocess.run([runtime["python"], str(worker), mode, str(path)], check=True,
                   timeout=runtime.get("timeout_seconds", 600), cwd=path.parent)


def video_metadata(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 50*1024*1024:
        raise ValueError("Recording must be a regular WebM no larger than 50 MiB")
    data = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        stderr=subprocess.PIPE, timeout=30))
    stream = next(row for row in data["streams"] if row["codec_type"] == "video")
    if stream["width"] != 1440 or stream["height"] != 900:
        raise ValueError("Recordings must be 1440 by 900 pixels")
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
                   check=True, capture_output=True, timeout=60)
    content = path.read_bytes()
    if not content.startswith(b"\x1aE\xdf\xa3"):
        raise ValueError("Recording is not a WebM")
    return {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
            "width": stream["width"], "height": stream["height"],
            "duration": float(data["format"]["duration"]), "decoding_verified": True}


def run(mode, path):
    original = json.loads(path.read_text())
    session_path = path if mode != "record" else path.parent.parent / "session.json"
    session = json.loads(session_path.read_text())
    original["worker_pid"] = os.getpid()
    write_json(path, original)
    if mode != "record":
        write_json(path.parent.parent / "session.json", original)
    try:
        if mode != "stop" and (session_path.parent / "stop.json").exists():
            raise ValueError("Browser session was stopped")
        if mode != "stop" and session["source"] != inventory(session["workspace"]):
            raise ValueError("Application source changed before the browser job")
        invoke(mode, path, session)
        result = json.loads(path.read_text())
        for key in ("id", "issue", "workspace"):
            if result.get(key) != original[key]:
                raise ValueError("Application worker changed its bound job identity")
        if mode != "stop" and (session_path.parent / "stop.json").exists():
            raise ValueError("Browser session was stopped")
        if mode != "stop" and session["source"] != inventory(session["workspace"]):
            raise ValueError("Application source changed during the browser job")
        if mode == "start":
            if not re.fullmatch(r"[0-9a-f]{64}", result.get("build", {}).get("sha256", "")):
                raise ValueError("Application worker must return a production build SHA-256")
            result.update(source=session["source"], runtime=session["runtime"], status="ready")
        elif mode == "record":
            if (not result.get("persisted_state_verified") or result.get("browser_errors") != []
                    or not result.get("results") or any(r.get("result") != "passed" for r in result["results"])):
                raise ValueError("Application worker did not confirm passing application assertions")
            result.update(source=session["source"], head=session["head"], branch=session["branch"],
                          build=session["build"], scenario=original["scenario"], session_id=session["id"],
                          worker_sha256=session["runtime"]["worker_sha256"],
                          video=video_metadata(path.parent / "capture.webm"), kind="full-app", status="completed",
                          completed_at=datetime.now(timezone.utc).isoformat())
        else:
            result["status"] = "stopped"
    except BaseException as error:
        result = {**original, "status": "failed", "error": type(error).__name__ + ": inspect private browser job log"}
        raise
    finally:
        write_json(path, result)
        if mode != "record":
            write_json(path.parent.parent / "session.json", result)


if __name__ == "__main__":
    os.umask(0o077)
    run(sys.argv[1], Path(sys.argv[2]))
