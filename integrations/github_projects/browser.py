"""Issue-bound full application jobs and source-bound capture admission.

Only trusted host workers write capture receipts. Applications configure their
own scenario names and worker; issue tools never accept executable paths.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import host_git
import sys
import uuid
from fnmatch import fnmatchcase


def configured(config):
    runtime = config.get("browser", {})
    return bool(runtime.get("worker") and runtime.get("scenarios"))


def worker_digest(config):
    return hashlib.sha256(Path(config["browser"]["worker"]).read_bytes()).hexdigest()


def required(config, workspace, base):
    patterns = config.get("browser", {}).get("required_paths", [])
    if not patterns:
        return False
    if not configured(config):
        raise ValueError("Required browser evidence needs a configured worker and scenarios")
    merge_base = git(workspace, "merge-base", base, "HEAD")
    names = git(workspace, "diff", "--name-only", merge_base).splitlines()
    names += git(workspace, "ls-files", "--others", "--exclude-standard").splitlines()
    return any(fnmatchcase(name, pattern) for name in names for pattern in patterns)


VIDEO_SUFFIXES = {".webm", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".ogv"}


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(data, stream, indent=2)
        stream.write("\n")
    temporary.replace(path)


def git(workspace, *args):
    return host_git.output(workspace, *args).decode().strip()


def inventory(workspace):
    """Hash tracked and new application/configuration files, including deletions.

    Commit identity is retained separately: committing tested bytes does not
    invalidate them. Ignored build/dependency outputs are never source inputs.
    """
    root = Path(workspace).resolve()
    names = host_git.output(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")
    entries = {}
    for name in sorted(set(filter(None, names))):
        if Path(name).suffix.lower() in VIDEO_SUFFIXES:
            continue
        path = root / name
        if path.is_symlink():
            raise ValueError("Application source inventory contains a symlink: " + name)
        if not path.exists():
            continue
        elif path.is_file():
            entries[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "executable": bool(path.stat().st_mode & 0o111)}
    return {"sha256": hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest(),
            "files": entries}


def reject_video_changes(workspace, base="origin/main"):
    # Check the complete prospective PR, including committed changes and the
    # working tree. Deleted historical videos are explicitly permitted.
    merge_base = git(workspace, "merge-base", base, "HEAD")
    names = host_git.output(workspace, "diff", "--no-ext-diff", "--name-only", "--diff-filter=ACMRT", "-z", merge_base).decode().split("\0")
    names += host_git.output(workspace, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
    for name in filter(None, names):
        path = Path(workspace) / name
        if path.suffix.lower() in VIDEO_SUFFIXES:
            raise ValueError("PR changes contain a video; publish it in the issue discussion: " + name)
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                signature = stream.read(16)
            if signature.startswith(b"\x1aE\xdf\xa3") or signature[4:8] == b"ftyp":
                raise ValueError("PR changes contain a video disguised as another file: " + name)




class BrowserJobs:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["state_directory"]) / "browser"

    def directory(self, number):
        return self.root / str(number)

    def session(self, number):
        path = self.directory(number) / "session.json"
        if not path.is_file():
            raise ValueError("Start this issue's full application first")
        return json.loads(path.read_text())

    def spawn(self, job_path, mode):
        environment = {k: v for k, v in os.environ.items() if not any(
            token in k.upper() for token in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))}
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        with job_path.with_suffix(".log").open("ab") as log:
            process = subprocess.Popen([self.config["browser"]["python"],
                str(Path(__file__).with_name("browser_worker.py")), mode, str(job_path)],
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True, close_fds=True, env=environment)
        return process.pid

    def terminate(self, pid, session):
        if not pid:
            return
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes()
            bound = str(self.directory(session["issue"]) / session["id"]).encode()
            if bound not in argv:
                raise ValueError("Refusing to stop a process no longer owned by this browser session")
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, FileNotFoundError):
            pass

    def action(self, task, workspace, arguments):
        if (not isinstance(arguments, dict) or set(arguments) - {"action", "scenario"}
                or arguments.get("action") not in {"start", "status", "record", "stop"}):
            raise ValueError("project_browser requires start, status, record, or stop")
        action = arguments["action"]
        if "scenario" in arguments and action != "record":
            raise ValueError("Only record accepts a scenario")
        if not configured(self.config):
            raise ValueError("Configure an external browser worker and scenario names first")
        workspace = Path(workspace).resolve()
        runtime = self.config["browser"]
        directory = self.directory(task.number)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if action == "start":
            worker = Path(runtime["worker"])
            if not worker.is_absolute() or not worker.is_file() or worker.resolve().is_relative_to(workspace):
                raise ValueError("Browser worker must be a trusted file outside the issue checkout")
            current = self.session(task.number) if (directory / "session.json").exists() else None
            if current and current["status"] in {"starting", "ready", "stopping"}:
                if current["workspace"] != str(workspace):
                    raise ValueError("The browser session belongs to another workspace")
                if current["source"] == inventory(workspace) and current["status"] != "stopping":
                    return self.status(task.number)
                raise ValueError("Source changed during an active browser session; stop it before rebuilding")
            session = {"id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
                "issue": task.number, "workspace": str(workspace), "source": inventory(workspace),
                "head": git(workspace, "rev-parse", "HEAD").strip(),
                "branch": git(workspace, "branch", "--show-current").strip(),
                "runtime": {**runtime, "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest()},
                "status": "starting", "provider_state": {}}
            job = directory / session["id"] / "session.json"
            write_json(job, session)
            write_json(directory / "session.json", session)
            self.spawn(job, "start")
            return {"status": "starting", "session_id": session["id"]}
        session = self.session(task.number)
        if session["workspace"] != str(workspace):
            raise ValueError("The browser session belongs to another workspace")
        if action == "status":
            return self.status(task.number)
        if action == "stop":
            private = directory / session["id"]
            write_json(private / "stop.json", {"stop": True})
            self.terminate(session.get("worker_pid"), session)
            for path in private.glob("*/capture.json"):
                capture = json.loads(path.read_text())
                if capture["status"] == "recording":
                    self.terminate(capture.get("worker_pid"), session)
                    write_json(path, {**capture, "status": "cancelled"})
            session["status"] = "stopping"
            write_json(private / "session.json", session)
            write_json(directory / "session.json", session)
            self.spawn(private / "session.json", "stop")
            return {"status": "stopping"}
        scenario = arguments.get("scenario")
        if scenario not in runtime["scenarios"]:
            raise ValueError("Choose a registered scenario from the installation configuration")
        if session["runtime"] != {**runtime, "worker_sha256": hashlib.sha256(Path(runtime["worker"]).read_bytes()).hexdigest()}:
            raise ValueError("Browser worker configuration changed; stop and restart the session")
        if session["status"] != "ready" or session["source"] != inventory(workspace):
            raise ValueError("A ready production build matching the current source is required")
        if any(row["status"] == "recording" for row in self.status(task.number)["captures"]):
            raise ValueError("This issue already has a recording in progress")
        capture_id = uuid.uuid4().hex
        capture = {"id": capture_id, "created_at": datetime.now(timezone.utc).isoformat(),
            "issue": task.number, "workspace": str(workspace), "session_id": session["id"],
            "scenario": scenario, "status": "recording"}
        path = directory / session["id"] / capture_id / "capture.json"
        write_json(path, capture)
        self.spawn(path, "record")
        return capture

    def status(self, number):
        session = self.session(number)
        rows = [json.loads(path.read_text()) for path in
                (self.directory(number) / session["id"]).glob("*/capture.json")]
        return {"status": session["status"], "session_id": session["id"],
                "captures": [{k: row[k] for k in ("id", "scenario", "status", "results", "video", "error")
                              if k in row} for row in rows],
                "scenarios": self.config["browser"]["scenarios"], "error": session.get("error"),
                "source_sha256": session["source"]["sha256"],
                "build_sha256": session.get("build", {}).get("sha256"),
                "stale": session["source"] != inventory(session["workspace"])}

    def validate(self, task, workspace, capture_ids):
        if not isinstance(capture_ids, list) or len(capture_ids) != len(set(capture_ids)):
            raise ValueError("browser_capture_ids must be a unique list")
        if not capture_ids:
            return []
        source = inventory(workspace)
        rows = []
        for capture_id in capture_ids:
            if not isinstance(capture_id, str) or not re.fullmatch("[0-9a-f]{32}", capture_id):
                raise ValueError("Invalid controller capture ID")
            matches = list(self.directory(task.number).glob("*/" + capture_id + "/capture.json"))
            if len(matches) != 1:
                raise ValueError("Unknown capture ID for this issue")
            receipt = json.loads(matches[0].read_text())
            if (receipt.get("id") != capture_id or receipt.get("status") != "completed"
                    or receipt.get("kind") != "full-app" or receipt.get("workspace") != str(workspace)
                    or receipt.get("issue") != task.number or receipt.get("source") != source
                    or receipt.get("scenario") not in self.config.get("browser", {}).get("scenarios", [])
                    or receipt.get("worker_sha256") != worker_digest(self.config)
                    or not receipt.get("persisted_state_verified") or receipt.get("browser_errors") != []
                    or not receipt.get("video", {}).get("decoding_verified")
                    or receipt.get("video", {}).get("width") != 1440 or receipt.get("video", {}).get("height") != 900
                    or not receipt.get("results") or any(r.get("result") != "passed" for r in receipt["results"])):
                raise ValueError("Capture is incomplete, component-only, or stale for the current application source")
            video = matches[0].parent / "capture.webm"
            if (not video.is_file() or video.is_symlink() or video.stat().st_size > 50*1024*1024
                    or video.stat().st_size != receipt["video"]["bytes"]
                    or hashlib.sha256(video.read_bytes()).hexdigest() != receipt["video"]["sha256"]):
                raise ValueError("Controller capture bytes changed")
            rows.append({**receipt, "video_path": str(video), "receipt_path": str(matches[0])})
        return rows
