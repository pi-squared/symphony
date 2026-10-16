"""Exercise the reusable capture boundary with synthetic temporary workers."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import browser as b
import browser_worker as worker


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "GH-1"
        self.workspace.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.workspace / "app.txt").write_text("application")
        self.git("add", "app.txt")
        self.git("commit", "-qm", "Base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.provider = self.root / "provider.py"
        self.provider.write_text("# Synthetic operator worker\n")
        self.config = {"state_directory": str(self.root / "state"),
                       "browser": {"python": sys.executable, "worker": str(self.provider),
                                   "scenarios": ["test-flow"]}}
        self.jobs = b.BrowserJobs(self.config)
        self.task = SimpleNamespace(number=1)

    def git(self, *args):
        return b.git(self.workspace, *args)

    def capture(self):
        capture_id = "a" * 32
        directory = self.jobs.directory(1) / ("b" * 32) / capture_id
        directory.mkdir(parents=True)
        content = b"\x1aE\xdf\xa3synthetic video"
        (directory / "capture.webm").write_bytes(content)
        row = {"id": capture_id, "issue": 1, "workspace": str(self.workspace), "scenario": "test-flow",
               "kind": "full-app", "source": b.inventory(self.workspace), "status": "completed",
               "results": [{"name": "Application assertion", "result": "passed", "seconds": 1}],
               "persisted_state_verified": True, "browser_errors": [],
               "worker_sha256": b.worker_digest(self.config),
               "video": {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                         "width": 1440, "height": 900, "decoding_verified": True}}
        b.write_json(directory / "capture.json", row)
        return directory, row

    def session(self):
        with patch.object(self.jobs, "spawn", return_value=123):
            self.jobs.action(self.task, self.workspace, {"action": "start"})
        session = self.jobs.session(1)
        return self.jobs.directory(1) / session["id"] / "session.json"

    def test_browser_is_optional_and_requires_explicit_external_registration(self):
        self.assertFalse(b.configured({}))
        self.assertFalse(b.required({}, self.workspace, "origin/main"))
        self.config.pop("browser")
        with self.assertRaisesRegex(ValueError, "external browser worker"):
            self.jobs.action(self.task, self.workspace, {"action": "start"})

    def test_start_background_job_and_record_select_only_configured_names(self):
        path = self.session()
        session = json.loads(path.read_text())
        session["status"] = "ready"
        b.write_json(path.parent.parent / "session.json", session)
        with self.assertRaisesRegex(ValueError, "registered scenario"):
            self.jobs.action(self.task, self.workspace, {"action": "record", "scenario": "unregistered"})
        with patch.object(self.jobs, "spawn", return_value=123):
            capture = self.jobs.action(self.task, self.workspace, {"action": "record", "scenario": "test-flow"})
        self.assertEqual(capture["scenario"], "test-flow")
        with self.assertRaisesRegex(ValueError, "already has a recording"):
            self.jobs.action(self.task, self.workspace, {"action": "record", "scenario": "test-flow"})

    def test_worker_inside_issue_checkout_is_rejected(self):
        self.config["browser"]["worker"] = str(self.workspace / "worker.py")
        Path(self.config["browser"]["worker"]).write_text("# untrusted checkout")
        with self.assertRaisesRegex(ValueError, "outside the issue checkout"):
            self.jobs.action(self.task, self.workspace, {"action": "start"})

    def test_source_changes_and_worker_changes_invalidate_captures(self):
        _, row = self.capture()
        self.assertEqual(len(self.jobs.validate(self.task, self.workspace, [row["id"]])), 1)
        (self.workspace / "app.txt").write_text("changed")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.jobs.validate(self.task, self.workspace, [row["id"]])
        (self.workspace / "app.txt").write_text("application")
        self.provider.write_text("# a changed validation worker\n")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.jobs.validate(self.task, self.workspace, [row["id"]])

    def test_incomplete_foreign_and_tampered_captures_are_rejected(self):
        directory, row = self.capture()
        for change in ({"kind": "component"}, {"status": "recording"}, {"issue": 2},
                       {"results": []}, {"scenario": "unknown"}, {"results": [{"result": "failed"}]}):
            b.write_json(directory / "capture.json", {**row, **change})
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.jobs.validate(self.task, self.workspace, [row["id"]])
        b.write_json(directory / "capture.json", row)
        (directory / "capture.webm").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "bytes changed"):
            self.jobs.validate(self.task, self.workspace, [row["id"]])
        with self.assertRaisesRegex(ValueError, "Unknown"):
            self.jobs.validate(SimpleNamespace(number=2), self.workspace, [row["id"]])

    def test_committing_tested_bytes_and_deletions_preserves_source_identity(self):
        (self.workspace / "app.txt").unlink()
        (self.workspace / "replacement.txt").write_text("replacement")
        _, row = self.capture()
        self.git("add", "-A")
        self.git("commit", "-qm", "Tested bytes")
        self.assertEqual(len(self.jobs.validate(self.task, self.workspace, [row["id"]])), 1)

    def test_changed_path_rules_use_project_configuration(self):
        self.config["browser"]["required_paths"] = ["ui/**"]
        (self.workspace / "unrelated.txt").write_text("new")
        self.assertFalse(b.required(self.config, self.workspace, "origin/main"))
        (self.workspace / "ui").mkdir()
        (self.workspace / "ui/view.txt").write_text("new")
        self.assertTrue(b.required(self.config, self.workspace, "origin/main"))

    def test_complete_pr_guard_catches_committed_and_disguised_video(self):
        video = self.workspace / "recording.webm"
        video.write_bytes(b"\x1aE\xdf\xa3video")
        self.git("add", "recording.webm")
        self.git("commit", "-qm", "Historical recording")
        with self.assertRaisesRegex(ValueError, "video"):
            b.reject_video_changes(self.workspace)
        video.unlink()
        b.reject_video_changes(self.workspace)
        (self.workspace / "hidden.txt").write_bytes(b"\x1aE\xdf\xa3video")
        with self.assertRaisesRegex(ValueError, "disguised"):
            b.reject_video_changes(self.workspace)

    def test_real_external_worker_start_and_stop_use_private_state(self):
        self.provider.write_text('''import json, sys
from pathlib import Path
path = Path(sys.argv[2]); state = json.loads(path.read_text())
state['provider_state'] = {'last_action': sys.argv[1]}
state['build'] = {'sha256': 'a' * 64}
path.write_text(json.dumps(state))
''')
        path = self.session()
        worker.run("start", path)
        self.assertEqual(self.jobs.status(1)["status"], "ready")
        with patch.object(self.jobs, "spawn", return_value=123), patch.object(self.jobs, "terminate"):
            self.jobs.action(self.task, self.workspace, {"action": "stop"})
        worker.run("stop", path)
        self.assertEqual(self.jobs.status(1)["status"], "stopped")
        self.assertEqual(json.loads(path.read_text())["provider_state"]["last_action"], "stop")

    def test_failed_or_cancelled_worker_never_marks_session_ready(self):
        path = self.session()
        b.write_json(path.parent / "stop.json", {"stop": True})
        with self.assertRaisesRegex(ValueError, "stopped"):
            worker.run("start", path)
        self.assertEqual(self.jobs.status(1)["status"], "failed")
        (path.parent / "stop.json").unlink()
        self.provider.write_text("raise RuntimeError('synthetic worker failure')\n")
        state = json.loads(path.read_text())
        state["runtime"]["worker_sha256"] = b.worker_digest(self.config)
        b.write_json(path, state)
        with self.assertRaises(subprocess.CalledProcessError):
            worker.run("start", path)
        self.assertEqual(self.jobs.status(1)["status"], "failed")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe unavailable")
    def test_real_webm_decode_verifies_video_before_acceptance(self):
        video = self.root / "capture.webm"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1440x900:r=5",
                        "-t", "0.4", "-c:v", "libvpx", "-y", str(video)], check=True)
        metadata = worker.video_metadata(video)
        self.assertTrue(metadata["decoding_verified"])
        self.assertEqual(metadata["sha256"], hashlib.sha256(video.read_bytes()).hexdigest())
        video.write_bytes(b"invalid recording")
        with self.assertRaises(subprocess.CalledProcessError):
            worker.video_metadata(video)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe unavailable")
    def test_external_record_worker_produces_a_validated_source_bound_receipt(self):
        self.provider.write_text('''import json, subprocess, sys
from pathlib import Path
mode, path = sys.argv[1], Path(sys.argv[2])
state = json.loads(path.read_text())
if mode == 'start':
    state['build'] = {'sha256': 'a' * 64}
elif mode == 'record':
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=1440x900:r=5',
                    '-t', '0.4', '-c:v', 'libvpx', '-y', str(path.parent/'capture.webm')], check=True)
    state.update(results=[{'name': 'Synthetic contract assertion', 'result': 'passed', 'seconds': 0.1}],
                 browser_errors=[], persisted_state_verified=True)
path.write_text(json.dumps(state))
''')
        path = self.session()
        worker.run("start", path)
        with patch.object(self.jobs, "spawn", return_value=123):
            result = self.jobs.action(self.task, self.workspace, {"action": "record", "scenario": "test-flow"})
        capture_path = path.parent / result["id"] / "capture.json"
        worker.run("record", capture_path)
        captures = self.jobs.validate(self.task, self.workspace, [result["id"]])
        self.assertEqual(captures[0]["status"], "completed")
        self.assertEqual(captures[0]["build"]["sha256"], "a" * 64)
        self.assertEqual(captures[0]["scenario"], "test-flow")
