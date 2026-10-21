"""Project tracker integration, workspace preparation, and recovery for Symphony.

The Elixir adapter sends JSON RPCs to this installed controller. Repository
workspaces never supply executable controller code or publication commands.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import shutil
from urllib.parse import urlencode


import support
import browser
import attachments
import artifacts
import sandbox as runtime

Error = support.ControllerError
MARKER = "<!-- symphony-github-projects -->"
ACTIVE_PHASES = {"claiming", "running", "retrying", "prepared", "published", "rework", "merging"}
FAILED = {"failure", "timed_out", "action_required", "startup_failure"}


def now():
    return datetime.now(timezone.utc).isoformat()


def private_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(data, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def verify_preflight(config_path):
    config_path = Path(config_path)
    config = json.loads(config_path.read_text())
    path = Path(config["state_directory"])/"preflight.json"
    if not path.is_file():
        raise Error("Run the installed Codex protocol preflight before enabling admission")
    receipt = json.loads(path.read_text())
    installed = json.loads((config_path.parent/"installed.json").read_text())
    digest = hashlib.sha256(Path(config["workflow"]).read_bytes()).hexdigest()
    from worker import execution_settings, execution_fingerprint
    if receipt.get("execution_sha256") != execution_fingerprint(config):
        raise Error("Worker execution configuration changed; rerun preflight")
    rework = receipt.get("rework", {})
    if (any(rework.get(key) != value for key, value in execution_settings(config, rework=True).items())
            or not rework.get("protocol_verified") or not rework.get("git_verified")):
        raise Error("Run preflight for both ordinary and Rework settings")
    if (receipt.get("installation_identity") != installed["identity"] or receipt.get("workflow_sha256") != digest
            or not receipt.get("protocol_verified") or receipt.get("model") != config.get("model", "gpt-6.1-sol")
            or receipt.get("reasoning") != config.get("reasoning", "medium") or not receipt.get("git_verified")):
        raise Error("The installation or workflow changed; rerun the Codex protocol preflight")


@dataclass(frozen=True)
class Task(support.Task):
    cycle: int = 1

    @property
    def branch(self):
        return f"codex/symphony-issue-{self.number}" + (f"-cycle-{self.cycle}" if self.cycle > 1 else "")


class Ledger:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / "state.sqlite3", timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS issues (item TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS cycles (item TEXT, cycle INTEGER, record TEXT NOT NULL, PRIMARY KEY(item, cycle))")
        # Additive, repeatable migration: preserve every original field verbatim.
        for record in self.all():
            item = record["task"]["item_id"]
            self.db.execute("INSERT OR IGNORE INTO cycles VALUES (?,?,?)",
                            (item, record.get("cycle", 1), json.dumps(record)))
        self.db.commit()

    def get(self, item):
        row = self.db.execute("SELECT record FROM issues WHERE item=?", (item,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, item, **changes):
        record = {**(self.get(item) or {}), **changes, "updated_at": now()}
        self.db.execute("INSERT OR REPLACE INTO issues VALUES (?,?)", (item, json.dumps(record)))
        self.db.commit()
        return record

    def replace(self, item, record):
        self.db.execute("INSERT OR REPLACE INTO issues VALUES (?,?)", (item, json.dumps(record)))
        self.db.commit()
        return record

    def archive(self, item, record):
        self.db.execute("INSERT OR REPLACE INTO cycles VALUES (?,?,?)",
                        (item, record.get("cycle", 1), json.dumps(record)))
        self.db.commit()

    def history(self, item):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT record FROM cycles WHERE item=? ORDER BY cycle", (item,))]

    def all(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT record FROM issues ORDER BY item")]


class GitHub(support.GitHub):
    def __init__(self, binary="gh", *, status_field="Status", base_branch="main"):
        self.binary = binary
        self.status_field = status_field
        self.base_branch = base_branch

    def api(self, endpoint, *, method="GET", payload=None):
        command = [self.binary, "api", endpoint, "--method", method]
        if payload is not None:
            command += ["--input", "-"]
        output = support.run(command, input_text=json.dumps(payload) if payload is not None else None)
        return json.loads(output) if output.strip() else None

    def issue_prs(self, task):
        pattern = re.compile(rf"codex/symphony-issue-{task.number}(?:-cycle-(\d+))?")
        rows = self.pages(f"repos/{task.repository}/pulls?state=all&base={self.base_branch}&sort=created&direction=desc")
        return [row for row in rows if (row["head"].get("repo") or {}).get("full_name") == task.repository
                and pattern.fullmatch(row["head"]["ref"])]

    def pages(self, endpoint):
        page = 1
        while True:
            separator = "&" if "?" in endpoint else "?"
            rows = self.api(f"{endpoint}{separator}per_page=100&page={page}")
            yield from rows
            if len(rows) < 100:
                break
            page += 1

    def publish_artifacts(self, task, staged, target):
        if not staged:
            return []
        tag = f"symphony-evidence-gh-{task.number}"
        endpoint = f"repos/{task.repository}/releases"
        try:
            release = self.api(f"{endpoint}/tags/{tag}")
        except Error as error:
            if "404" not in str(error):
                raise
            release = self.api(endpoint, method="POST", payload={
                "tag_name": tag, "target_commitish": target, "name": f"Local evidence for #{task.number}",
                "body": f"{MARKER}\nVisual evidence captured locally for {task.url}. Assets are immutable by SHA-256.",
                "prerelease": True})
        if MARKER not in (release.get("body") or ""):
            raise Error("The evidence release is not owned by this controller")
        published = []
        for row in staged:
            name = Path(row["staged_path"]).name
            assets = list(self.pages(f"{endpoint}/{release['id']}/assets"))
            asset = next((a for a in assets if a["name"] == name), None)
            if asset is None:
                support.run([self.binary, "release", "upload", tag, row["staged_path"], "--repo", task.repository], timeout=180)
                assets = list(self.pages(f"{endpoint}/{release['id']}/assets"))
                asset = next((a for a in assets if a["name"] == name), None)
            if (not asset or asset.get("digest") != "sha256:" + row["sha256"]
                    or asset.get("size") != row["bytes"]):
                raise Error("GitHub did not confirm the visual artifact hash and size")
            published.append({"path": row["path"], "sha256": row["sha256"], "caption": row["caption"],
                              "url": asset["browser_download_url"]})
        return published

    def remote_head(self, repository, branch):
        try:
            return self.api(f"repos/{repository}/git/ref/heads/{branch}")["object"]["sha"]
        except Error as error:
            if "404" in str(error):
                return None
            raise

    def checks(self, repository, sha):
        runs, page = [], 1
        while True:
            data = self.api(f"repos/{repository}/commits/{sha}/check-runs?per_page=100&page={page}")
            rows = data["check_runs"]
            runs.extend(rows)
            if len(rows) < 100:
                break
            page += 1
        # A repeated check name can have historical failed attempts. Only its
        # newest check run on this exact head contributes to the current result.
        latest = {}
        for row in sorted(runs, key=lambda row: row["id"]):
            latest[(row.get("app", {}).get("id"), row["name"])] = row
        values = [{"name": r["name"], "state": r["conclusion"] if r["status"] == "completed" else "pending",
                   "url": r.get("html_url"), "details": r.get("output", {}).get("summary") or ""}
                  for r in latest.values()]
        statuses = list(self.pages(f"repos/{repository}/commits/{sha}/statuses"))
        seen = set()
        for row in statuses:  # GitHub returns newest status first.
            if row["context"] not in seen:
                values.append({"name": row["context"], "state": row["state"],
                               "url": row.get("target_url"), "details": row.get("description") or ""})
                seen.add(row["context"])
        return values


def fields(item):
    return {value["field"]["name"]: value.get("name", value.get("number"))
            for value in item["fieldValues"]["nodes"] if value.get("field")}


def task_for(item, field_names=None):
    issue = item["content"]
    values = fields(item)
    field_names = field_names or {}
    return Task(issue["id"], item["id"], issue["repository"]["nameWithOwner"],
                issue["number"], issue["title"], issue["url"], values.get(field_names.get("priority", "Priority")),
                values.get(field_names.get("execution_order", "Execution order")))


def report_valid(report):
    if (not isinstance(report, dict) or not {"summary", "blocked", "blocker", "checks"} <= set(report)
            or set(report) - {"summary", "blocked", "blocker", "checks", "artifacts", "browser_capture_ids"}):
        raise Error("Handoff requires summary, blocked, blocker, and checks")
    if not isinstance(report["summary"], str) or not isinstance(report["blocker"], str):
        raise Error("The summary and blocker must be text")
    if support.SECRET_PATTERN.search(json.dumps(report).encode()):
        raise Error("The report contains a potential credential; remove it before publication")
    if not isinstance(report["blocked"], bool) or not isinstance(report["checks"], list) or not report["checks"]:
        raise Error("The report must include a boolean blocked flag and executable check evidence")
    for check in report["checks"]:
        if (not isinstance(check, dict) or set(check) != {"command", "result", "details"}
                or not all(isinstance(check[key], str) for key in check)
                or check["result"] not in {"passed", "failed", "not_run"}):
            raise Error("Invalid check evidence")
    ids = report.get("browser_capture_ids", [])
    if (not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(v, str) or not re.fullmatch("[0-9a-f]{32}", v) for v in ids)
            or len(ids) != len(set(ids))):
        raise Error("browser_capture_ids must contain unique controller capture IDs")
    try:
        artifacts.validate_descriptors(report.get("artifacts", []))
    except ValueError as error:
        raise Error(str(error)) from None
    if any(Path(r["path"]).suffix.lower() in browser.VIDEO_SUFFIXES for r in report.get("artifacts", [])):
        raise Error("Videos require completed project_browser captures, not artifact descriptors")
    if not report["blocked"] and not any(check["result"] == "passed" for check in report["checks"]):
        raise Error("At least one executable check must pass before publication")


class Controller:
    def __init__(self, config, gh=None):
        self.config = config
        self.base_branch = config.get("base_branch", "main")
        self.base_ref = "origin/" + self.base_branch
        self.field_names = config.get("fields", {})
        self.status_field = self.field_names.get("status", "Status")
        self.gh = gh or GitHub(config.get("gh", "gh"), status_field=self.status_field, base_branch=self.base_branch)
        self.state = Ledger(config["state_directory"])
        workflow = Path(config.get("workflow", Path(__file__).resolve().parent / "templates/WORKFLOW.md"))
        self.policy = json.loads(workflow.read_text().split("---", 2)[1])
        self.tracker = self.policy["tracker"]
        self.roles = self.tracker["provider"]["roles"]
        if set(self.roles) != {"backlog", "queued", "working", "review", "rework", "merging", "done"}:
            raise Error("Every lifecycle role must have one configured board state")
        if len(set(self.roles.values())) != len(self.roles):
            raise Error("Lifecycle states must be distinct")
        required = {self.roles[k] for k in ("queued", "working", "rework", "merging")}
        if not required.issubset(self.tracker["active_states"]) or self.roles["review"] in self.tracker["active_states"]:
            raise Error("Active states must include execution routes and exclude human review")

    def task(self, item):
        task = task_for(item, self.field_names)
        record = self.state.get(item["id"]) or {}
        return Task(**{**task.__dict__, "cycle": record.get("cycle", 1)})

    def role(self, item):
        status = fields(item).get(self.status_field)
        return next((k for k, v in self.roles.items() if v == status), None)

    def snapshot(self):
        project, items = self.gh.project(self.config["project_owner"], self.config["project_number"])
        if not project.get("viewerCanUpdate"):
            raise Error("The saved GitHub login cannot update the configured project")
        return project, [item for item in items if self.in_scope(item)]

    def in_scope(self, item):
        issue = item.get("content") or {}
        return (issue.get("__typename") == "Issue"
                and issue.get("repository", {}).get("nameWithOwner") == self.config["repository"])

    def routed(self, item):
        issue = item["content"]
        labels = {v["name"].strip().lower() for v in issue["labels"]["nodes"]}
        required = set(self.tracker.get("required_labels", []))
        return (not item["isArchived"] and not issue["repository"]["isArchived"]
                and issue["state"] == "OPEN" and required.issubset(labels)
                and (not self.config.get("only_issue") or issue["number"] == self.config["only_issue"]))

    def workspace(self, task, supplied=None):
        root = Path(self.config["workspace_root"]).resolve()
        expected = root / f"GH-{task.number}"
        if expected.resolve() != expected or (supplied and Path(supplied).resolve() != expected):
            raise Error("The workspace does not match this issue or escapes the configured root")
        return expected

    def owned_pr(self, task, record):
        if not record or not record.get("pr_number"):
            return None
        pr = self.gh.api(f"repos/{task.repository}/pulls/{record['pr_number']}")
        if (pr["head"]["ref"] != task.branch or pr["head"]["repo"]["full_name"] != task.repository
                or pr["base"]["ref"] != self.base_branch):
            raise Error("The recorded PR no longer matches Symphony's branch and repository")
        return pr

    def backend_for(self, item):
        record = self.state.get(item["id"]) or {}
        from jules import owns_cycle
        if owns_cycle(record):
            return "jules"
        if self.role(item) in {"rework", "merging"}:
            return "codex"
        if record:
            return record.get("backend", "codex")
        settings = self.config.get("jules", {})
        labels = {row["name"].lower() for row in item["content"]["labels"]["nodes"]}
        return "jules" if settings.get("enabled") and settings.get("label", "runner:jules").lower() in labels else "codex"

    def eligible(self, item, *, backend="codex"):
        if self.backend_for(item) != backend:
            return False
        if not self.config["enabled"] or not self.routed(item):
            return False
        role = self.role(item)
        record = self.state.get(item["id"])
        if role == "queued":
            return True
        if role == "rework":
            return bool(record)
        if role == "merging":
            return bool(record and record.get("pr_number"))
        return bool(role == "working" and record and record["phase"] in ACTIVE_PHASES)

    def normalized(self, item, dispatchable):
        task = self.task(item)
        record = self.state.get(item["id"]) or {}
        issue = item["content"]
        detail = self.gh.issue(task) if dispatchable else {}
        labels = [v["name"].strip().lower() for v in detail.get("labels", issue["labels"]["nodes"])]
        if detail and (detail["state"] != "open" or not set(self.tracker.get("required_labels", [])).issubset(labels)):
            dispatchable = False
        assignees = detail.get("assignees", [])
        allowed = self.tracker.get("assignee")
        if allowed and allowed not in {a["login"] for a in assignees}:
            dispatchable = False
        dependencies = []
        if detail:
            for row in self.gh.pages(f"repos/{task.repository}/issues/{task.number}/dependencies/blocked_by"):
                dependencies.append({"id": str(row["id"]), "identifier": f"GH-{row['number']}",
                                     "state": self.roles["done"] if row["state"] == "closed" else "open"})
        if (self.role(item) == "queued" and self.tracker["provider"].get("dependency_gating", False)
                and any(d["state"] != self.roles["done"] for d in dependencies)):
            dispatchable = False
        return {"id": item["id"], "identifier": f"GH-{task.number}", "title": task.title,
                "description": detail.get("body") or "", "priority": {"P0": 1, "P1": 2, "P2": 3}.get(task.priority),
                "state": self.roles["done"] if issue["state"] == "CLOSED" or detail.get("state") == "closed" else fields(item).get(self.status_field, self.roles["backlog"]),
                "branch_name": record.get("branch", task.branch), "url": task.url,
                "assignee_id": assignees[0]["login"] if assignees else None,
                "labels": labels, "dispatchable": dispatchable, "blocked_by": dependencies,
                "created_at": detail.get("created_at"), "updated_at": issue.get("updatedAt"),
                "native_ref": {"repo": task.repository, "number": task.number, "issue_id": task.issue_id,
                               "item_id": task.item_id, "cycle": record.get("cycle", 1),
                               "execution_order": task.order, "phase": record.get("phase", "new")}}

    def list_issues(self, states=None, ids=None):
        _, items = self.snapshot()
        result = []
        for item in items:
            record = self.state.get(item["id"]) or {}
            if record.get("phase") == "completing":
                self.done(item["id"])
                item["content"]["state"] = "CLOSED"
                item["fieldValues"]["nodes"] = [{"field": {"name": self.status_field}, "name": self.roles["done"]}]
            elif record.get("phase") == "published" and self.role(item) == "review":
                self.state.save(item["id"], phase="review")
            state = self.roles["done"] if item["content"]["state"] == "CLOSED" else fields(item).get(self.status_field, self.roles["backlog"])
            if ids is not None and item["id"] not in ids:
                continue
            if states is not None and state not in states:
                continue
            if item["isArchived"]:
                continue
            result.append(self.normalized(item, self.eligible(item)))
        # Return all candidates. Symphony alone allocates concurrency and retry slots.
        return result

    def find(self, item_id):
        project, items = self.snapshot()
        item = next((item for item in items if item["id"] == item_id), None)
        if item is None or not self.routed(item):
            raise Error("The issue is no longer an admitted member of this project and repository")
        task = self.task(item)
        # Project pages and search memberships can lag behind an issue edit.
        # Recheck both admission predicates through independent current reads.
        issue = self.gh.issue(task)
        labels = {label["name"].strip().lower() for label in issue["labels"]}
        status = self.gh.item_status(item_id)
        item["content"]["labels"]["nodes"] = issue["labels"]
        if issue["state"] != "open" or not set(self.tracker.get("required_labels", [])).issubset(labels) or status is None:
            raise Error("The issue was closed, unlabelled, archived, or removed before this operation")
        for value in item["fieldValues"]["nodes"]:
            if value.get("field", {}).get("name") == self.status_field:
                value["name"] = status
        return project, item, task

    def workpad(self, task, record, message):
        comment_id = record.get("comment_id")
        cycle_marker = f"<!-- symphony-github-projects-cycle:{record.get('cycle', 1)} -->"
        comments = list(self.gh.pages(f"repos/{task.repository}/issues/{task.number}/comments"))
        current = next((c for c in comments if c["id"] == comment_id), None)
        if not current:
            current = next((c for c in comments if cycle_marker in (c.get("body") or "")), None)
        if not current and not record.get("previous_cycle"):
            current = next((c for c in reversed(comments) if MARKER in (c.get("body") or "") or "## Codex Workpad" in (c.get("body") or "")), None)
        if current:
            comment_id = current["id"]
            body = current["body"]
            if cycle_marker not in body:
                body += "\n" + cycle_marker
            note = f"\n\n<!-- controller-note -->\n{message}"
            if note not in body:
                body += note
        else:
            body = f"{MARKER}\n{cycle_marker}\n## Codex Workpad\n\n### Plan\n\n- [ ] Reproduce and plan the issue.\n\n### Acceptance Criteria\n\n- [ ] Mirror the issue requirements.\n\n### Validation\n\n- [ ] Run required checks.\n\n### Notes\n\n{message}\n\n### Confusions\n\n- None recorded."
        if comment_id:
            self.gh.api(f"repos/{task.repository}/issues/comments/{comment_id}", method="PATCH", payload={"body": body})
        else:
            comment_id = self.gh.api(f"repos/{task.repository}/issues/{task.number}/comments", method="POST", payload={"body": body})["id"]
        self.state.save(task.item_id, comment_id=comment_id)

    def update_workpad(self, task, body):
        if not isinstance(body, str) or support.SECRET_PATTERN.search(body.encode()):
            raise Error("Workpad must be text without credentials")
        if not all(part in body for part in ("## Codex Workpad", "### Plan", "### Acceptance Criteria", "### Validation", "### Notes")):
            raise Error("Retain the complete workpad structure")
        record = self.state.get(task.item_id)
        if not record.get("comment_id"):
            raise Error("Claim the issue before replacing the workpad")
        self.gh.api(f"repos/{task.repository}/issues/comments/{record['comment_id']}", method="PATCH",
                    payload={"body": MARKER + f"\n<!-- symphony-github-projects-cycle:{record.get('cycle', 1)} -->\n" + body})
        return {"comment_id": record["comment_id"]}

    def claim_lock(self, item_id):
        directory = Path(self.config["state_directory"])/"claim-locks"/hashlib.sha256(item_id.encode()).hexdigest()
        return controller_lock(directory)

    def claim(self, item_id, workspace, *, new_attempt=False, backend="codex"):
        with self.claim_lock(item_id):
            return self._claim(item_id, workspace, new_attempt=new_attempt, backend=backend)

    def _claim(self, item_id, workspace, *, new_attempt=False, backend="codex"):
        if not self.config["enabled"]:
            raise Error("Symphony admission is paused")
        project, item, task = self.find(item_id)
        path = self.workspace(task, workspace)
        record = self.state.get(item_id)
        role = self.role(item)
        if role == "review" and record:
            return self.context(task, record)
        if not self.eligible(item, backend=backend):
            raise Error("The issue is not eligible for this Symphony backend")
        pr = self.owned_pr(task, record) if record and record.get("pr_number") else None
        restart = bool(record and ((role == "rework" and record.get("phase") != "rework")
                       or (role == "queued" and pr and pr["state"] != "open")))
        if restart:
            previous = dict(record)
            previous["workpad_snapshot"] = next((c.get("body") for c in self.gh.pages(
                f"repos/{task.repository}/issues/{task.number}/comments")
                if c["id"] == record.get("comment_id")), None)
            self.state.archive(item_id, previous)
            cycle = record.get("cycle", 1) + 1
            # Record intent before moving files: retries recover the same cycle.
            record = {"task": {**task.__dict__, "cycle": cycle}, "cycle": cycle,
                      "branch": Task(**{**task.__dict__, "cycle": cycle}).branch,
                      "phase": "rework" if role == "rework" else "claiming",
                      "workspace": str(path), "previous_cycle": previous, "started_at": now()}
            self.state.replace(item_id, record)
            task = self.task(item)
        if not record:
            # Discover all owned branch cycles, including closed PRs after a
            # lost local record. Never reuse a previously closed PR branch.
            discovered = self.gh.issue_prs(task)
            def pr_cycle(row):
                match = re.search(r"-cycle-(\d+)$", row["head"]["ref"])
                return int(match[1]) if match else 1
            opened = [row for row in discovered if row["state"] == "open"]
            if len(opened) > 1:
                raise Error("Multiple open issue-cycle PRs require ownership reconciliation")
            pr = opened[0] if opened else None
            cycle = pr_cycle(pr) if pr else max((pr_cycle(row) for row in discovered), default=0) + 1
            task = Task(**{**task.__dict__, "cycle": cycle})
            previous = None
            for row in sorted(discovered, key=pr_cycle):
                recovered = {"task": {**task.__dict__, "cycle": pr_cycle(row)},
                    "cycle": pr_cycle(row), "branch": row["head"]["ref"], "phase": row["state"],
                    "workspace": str(path), "pr_number": row["number"], "pr_url": row["html_url"],
                    "published_sha": row["head"]["sha"]}
                self.state.archive(item_id, recovered)
                if row["state"] != "open":
                    previous = recovered
            if not pr and discovered:
                while self.gh.remote_head(task.repository, task.branch):
                    task = Task(**{**task.__dict__, "cycle": task.cycle + 1})
            record = self.state.save(item_id, task=task.__dict__, cycle=task.cycle, branch=task.branch,
                phase="claiming", workspace=str(path), started_at=now(),
                **({"pr_number": pr["number"], "pr_url": pr["html_url"], "published_sha": pr["head"]["sha"]} if pr else {}),
                **({"previous_cycle": previous} if previous and not pr else {}))
        # The execution choice belongs to the cycle, not the transient board
        # state. Rework switches to working after acknowledgment; retries must
        # retain its requested model/effort.
        if not record.get("backend"):
            record = self.state.save(item_id, backend=backend)
        if not record.get("execution"):
            from worker import execution_settings
            record = self.state.save(item_id, execution=execution_settings(self.config, rework=role == "rework"))
        self.ensure_checkout(task, record, path)
        if role == "rework":
            self.state.save(item_id, phase="rework")
        else:
            if role == "queued":
                self.gh.status(project, item_id, self.roles["working"])
            self.state.save(item_id, phase="merging" if role == "merging" else "running", blocker=None)
            self.workpad(task, self.state.get(item_id), f"Cycle {record.get('cycle', 1)}; branch `{task.branch}`.")
        return self.context(task, self.state.get(item_id))

    def restart(self, task):
        project, item, _ = self.find(task.item_id)
        record = self.state.get(task.item_id)
        if self.role(item) == "working" and record["phase"] == "running" and record.get("previous_cycle"):
            return self.context(task, record)
        if self.role(item) != "rework" or record["phase"] != "rework":
            raise Error("Only an explicit Rework transition authorizes restart")
        previous = record.get("previous_cycle") or {}
        if previous.get("pr_number"):
            pr = self.gh.api(f"repos/{task.repository}/pulls/{previous['pr_number']}")
            if pr["state"] != "closed":
                raise Error("Close the previous PR before acknowledging Rework")
        if previous.get("comment_id"):
            comments = self.gh.pages(f"repos/{task.repository}/issues/{task.number}/comments")
            if any(c["id"] == previous["comment_id"] for c in comments):
                raise Error("Remove the previous workpad before acknowledging Rework")
        self.workpad(task, record, f"Rework cycle {task.cycle}; previous work preserved in cycle history.")
        self.gh.status(project, task.item_id, self.roles["working"])
        self.state.save(task.item_id, phase="running")
        return self.context(task, self.state.get(task.item_id))

    def ensure_checkout(self, task, record, path):
        previous = record.get("previous_cycle")
        if previous and record.get("base_sha") is None:
            archive = Path(self.config["state_directory"]) / "cycle-workspaces" / f"GH-{task.number}-{task.cycle - 1}"
            # A crash after rename is recoverable; never overwrite an archive.
            if path.exists() and not archive.exists():
                self.cleanup(path, terminal=False)
                archive.parent.mkdir(parents=True, exist_ok=True)
                path.rename(archive)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (path / ".git").exists():
            if any(path.iterdir()):
                raise Error("A nonempty workspace has no Git checkout")
            remote = None if previous else self.gh.remote_head(task.repository, task.branch)
            support.run(["git", "-c", "credential.helper=!gh auth git-credential", "clone", "--branch", task.branch if remote else self.base_branch,
                        "https://github.com/" + task.repository + ".git", "."], cwd=path, timeout=240)
            if not remote:
                support.run(["git", "switch", "-c", task.branch], cwd=path)
        origin = support.run(["git", "remote", "get-url", "origin"], cwd=path).strip().removesuffix(".git")
        if origin != "https://github.com/" + task.repository:
            raise Error("The workspace origin does not match the configured private repository")
        if support.run(["git", "branch", "--show-current"], cwd=path).strip() != task.branch:
            raise Error("The workspace is on another branch")
        # Codex owns local commits and synchronization; preserve all existing work.
        gh = self.config.get("gh", "gh")
        import shlex
        support.run(["git", "config", "--local", "credential.helper", "!" + shlex.quote(gh) + " auth git-credential"], cwd=path)
        self.state.save(task.item_id, base_sha=record.get("base_sha") or browser.git(path, "merge-base", self.base_ref, "HEAD"))
        skill_source = Path(__file__).resolve().parent / "templates/skills"
        if skill_source.exists() and not self.config.get("docker"):
            target = path / ".codex/skills"
            # Bootstrap skills into older application revisions, excluding them
            # from publication unless the issue deliberately tracks them.
            for source in skill_source.rglob("*"):
                if source.is_file() and not (target/source.relative_to(skill_source)).exists():
                    destination = target/source.relative_to(skill_source)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
            exclude = path / ".git/info/exclude"
            with exclude.open("a") as stream:
                stream.write("\n.codex/skills/\n")

    def context(self, task, record):
        issue = self.gh.issue(task)
        comments = [c["body"] for c in self.gh.pages(f"repos/{task.repository}/issues/{task.number}/comments")
                    if not (c.get("body") or "").startswith(MARKER)]
        result = {"issue": issue, "discussion": comments, "branch": task.branch,
                  "phase": record["phase"], "cycle": record.get("cycle", 1),
                  "execution": record.get("execution"),
                  "repository": task.repository, "base_branch": self.base_branch,
                  "project_owner": self.config["project_owner"], "project_number": self.config["project_number"],
                  "required_checks": self.policy["validation"]["required_checks"], "fields": self.field_names,
                  "roles": self.roles, "history": self.state.history(task.item_id),
                  "previous_cycle": record.get("previous_cycle"),
                  "workpad": next((c for c in self.gh.pages(f"repos/{task.repository}/issues/{task.number}/comments") if c["id"] == record.get("comment_id")), None),
                  "toolchain": self.config.get("toolchain", {}), "previous_report": record.get("report"),
                  "test_llm": {"provider": "openai", "model": os.environ.get("OPENAI_TEST_MODEL", "gpt-6-luna"),
                               "api_key_environment": "OPENAI_API_KEY"}}
        if record.get("pr_number"):
            result["pr"] = self.owned_pr(task, record)
            checks = self.gh.checks(task.repository, result["pr"]["head"]["sha"])
            result["checks"] = checks
            failures = [c for c in checks if c["state"] in FAILED or c["state"] == "error"]
            result["failed_checks"] = failures
            for check in failures[:3]:
                match = re.search(r"/actions/runs/(\d+)", check.get("url") or "")
                if match:
                    try:
                        check["log"] = support.run([self.config.get("gh", "gh"), "run", "view", match[1], "--repo", task.repository,
                                                   "--log-failed"], timeout=90)[-20000:]
                    except Error:
                        check["log"] = "Failed-job logs are unavailable; inspect the check details."
        return result

    def block(self, task, message):
        record = self.state.save(task.item_id, phase="blocked", blocker=message)
        self.workpad(task, record, f"Blocked: {message}. Work and evidence are preserved.")
        project, _, _ = self.find(task.item_id)
        self.gh.status(project, task.item_id, self.roles["review"])
        return {"status": "blocked", "blocker": message}

    def publish_evidence(self, task, path, report, record):
        if not report.get("artifacts"):
            return
        try:
            staged = artifacts.stage(path, report["artifacts"],
                                     Path(self.config["state_directory"]) / "artifacts" / str(task.number),
                                     support.SECRET_PATTERN)
        except (ValueError, OSError) as error:
            raise Error(f"Visual evidence validation failed: {error}") from None
        published = self.gh.publish_artifacts(task, staged, record["base_sha"])
        existing = {row["sha256"]: row for row in record.get("visual_evidence", [])}
        existing.update({row["sha256"]: row for row in published})
        self.state.save(task.item_id, visual_evidence=list(existing.values()))

    def browser_action(self, item_id, workspace, arguments):
        _, _, task = self.find(item_id)
        path = self.workspace(task, workspace)
        record = self.state.get(item_id)
        if not record or record.get("workspace") != str(path):
            raise Error("This issue has no matching Symphony claim")
        if support.run(["git", "branch", "--show-current"], cwd=path).strip() != task.branch:
            raise Error("The browser workspace is on another issue branch")
        return browser.BrowserJobs(self.config).action(task, path, arguments)

    def capture_evidence(self, task, path, report, record):
        captures = browser.BrowserJobs(self.config).validate(task, path, report.get("browser_capture_ids", []))
        needs_video = browser.required(self.config, path, self.base_ref)
        if needs_video and not report["blocked"] and not captures:
            raise Error("UI handoff requires completed full-app project_browser captures for current source")
        published = []
        for capture in captures:
            publisher = attachments.Attachments(self.config["browser"]["gh"],
                Path(self.config["state_directory"])/"attachments"/str(task.number), task.repository,
                runtime=self.config["browser"])
            published.append(publisher.publish(task, capture))
        if published:
            self.state.save(task.item_id, browser_evidence=published)
        return captures

    def independent_checks(self, task, path, report):
        evidence = []
        if self.config.get("docker"):
            from docker_worker import DockerSandbox
            sandbox = DockerSandbox(self.config)
        else:
            sandbox = runtime.Sandbox(self.config["runtime_roots"])
        for command in self.config["checks"]:
            argv = [str(self.config["python"]) if part == "{python}" else part for part in command]
            try:
                output = sandbox.check(path, argv, timeout=600)
            except runtime.CheckError as error:
                directory = Path(self.config["state_directory"]) / "checks" / str(task.number)
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                (directory / "stdout.txt").write_bytes(error.stdout)
                (directory / "stderr.txt").write_bytes(error.stderr)
                raise Error("An independent repository check failed; its private output is preserved") from None
            evidence.append({"command": " ".join(argv), "result": "passed", "details": output.decode(errors="replace")[-1500:]})
        return {**report, "checks": [*report["checks"], *evidence]}

    def evidence(self, item_id, workspace, report):
        report_valid(report)
        _, item, task = self.find(item_id)
        if not self.config["enabled"] or self.role(item) not in {"working", "merging"}:
            raise Error("Evidence publication requires an active claim")
        path = self.workspace(task, workspace)
        record = self.state.get(item_id)
        self.capture_evidence(task, path, report, record)
        self.publish_evidence(task, path, report, record)
        return {"browser_evidence": self.state.get(item_id).get("browser_evidence", []),
                "visual_evidence": self.state.get(item_id).get("visual_evidence", [])}

    def validate(self, item_id, workspace):
        _, _, task = self.find(item_id)
        path = self.workspace(task, workspace)
        # Scan the complete prospective PR, including already committed files.
        from checks import verify_changes
        verify_changes(path, base=self.base_ref, protected_paths=self.config.get("protected_paths", []))
        report = self.independent_checks(task, path, {"checks": []})
        self.state.save(item_id, validated_source=browser.inventory(path)["sha256"],
                        validated_sha=browser.git(path, "rev-parse", "HEAD"), validation=report)
        return report

    def git_remote(self, item_id, workspace, action):
        _, item, task = self.find(item_id)
        if not self.config["enabled"] or self.role(item) not in {"working", "merging"}:
            raise Error("Git remote operations require an active working or Merging issue")
        path = self.workspace(task, workspace)
        if browser.git(path, "branch", "--show-current") != task.branch:
            raise Error("Git remote operation must use the selected issue branch")
        origin = browser.git(path, "remote", "get-url", "origin").removesuffix(".git")
        if origin != "https://github.com/" + task.repository:
            raise Error("The Git origin changed from the bound repository")
        if action == "fetch":
            support.run(["git", "fetch", "origin", self.base_branch], cwd=path, timeout=240)
            return {"base_ref": self.base_ref, "sha": browser.git(path, "rev-parse", self.base_ref)}
        if action != "push":
            raise Error("Git remote action must be fetch or push")
        self.validate(item_id, workspace)
        sha = browser.git(path, "rev-parse", "HEAD")
        if browser.git(path, "status", "--porcelain"):
            raise Error("Commit scoped source changes before pushing")
        if self.gh.remote_head(task.repository, task.branch) != sha:
            support.run(["git", "push", "-u", "origin", task.branch], cwd=path, timeout=240)
        if self.gh.remote_head(task.repository, task.branch) != sha:
            raise Error("GitHub did not confirm the selected issue commit")
        return {"branch": task.branch, "sha": sha, "remote_verified": True}

    def review(self, item_id, workspace, arguments):
        project, item, task = self.find(item_id)
        if not self.config["enabled"] or self.role(item) != "working":
            raise Error("Review handoff requires an active working issue")
        path = self.workspace(task, workspace)
        record = self.state.get(item_id)
        number, sha = arguments["pr_number"], arguments["sha"]
        pr = self.gh.api(f"repos/{task.repository}/pulls/{number}")
        self.verify_pr(task, pr, sha)
        if not pr["draft"]:
            raise Error("Initial PR publication must be draft")
        checks = self.require_checks(task, sha)
        if browser.git(path, "status", "--porcelain"):
            raise Error("Commit and push all source changes before review")
        if browser.git(path, "rev-parse", "HEAD") != sha or record.get("validated_source") != browser.inventory(path)["sha256"]:
            raise Error("Validate the current source before review")
        if browser.required(self.config, path, self.base_ref):
            ids = [row["capture_id"] for row in record.get("browser_evidence", [])]
            if not ids:
                raise Error("Publish full-app browser evidence before review")
            browser.BrowserJobs(self.config).validate(task, path, ids)
        self.verify_pr(task, self.gh.api(f"repos/{task.repository}/pulls/{number}"), sha)
        if self.gh.item_status(item_id) != self.roles["working"]:
            raise Error("The board moved during handoff")
        self.state.save(item_id, phase="published", pr_number=number, pr_url=pr["html_url"], published_sha=sha)
        self.gh.status(project, item_id, self.roles["review"])
        self.state.save(item_id, phase="review", checks=checks)
        self.workpad(task, self.state.get(item_id), f"Validated `{sha}`; draft PR {pr['html_url']}. Waiting for human decision.")
        return {"status": "in_review", "pr_url": pr["html_url"], "sha": sha}

    def verify_pr(self, task, pr, sha):
        if (pr["head"]["sha"] != sha or pr["head"]["ref"] != task.branch
                or pr["head"]["repo"]["full_name"] != task.repository
                or pr["base"]["ref"] != self.base_branch or pr["state"] != "open"):
            raise Error("PR does not match the selected branch, repository and current head")
        attachments.reject_pr_videos(pr.get("body") or "")

    def require_checks(self, task, sha):
        checks = self.gh.checks(task.repository, sha)
        required = set(self.policy["validation"]["required_checks"])
        if (not required.issubset({c["name"] for c in checks})
                or any(c["state"] != "success" for c in checks if c["name"] in required)):
            raise Error("All expected current-head CI checks must finish successfully")
        return checks

    def done(self, item_id):
        project, items = self.snapshot()
        item = next((row for row in items if row["id"] == item_id), None)
        if item is None or self.role(item) not in {"merging", "done"}:
            raise Error("Only the human Merging transition authorizes completion")
        task = self.task(item)
        record = self.state.get(item_id)
        pr = self.gh.api(f"repos/{task.repository}/pulls/{record['pr_number']}")
        if not pr.get("merged"):
            raise Error("GitHub has not confirmed the selected PR merge")
        self.state.save(item_id, phase="completing")
        self.gh.status(project, item_id, self.roles["done"])
        self.gh.api(f"repos/{task.repository}/issues/{task.number}", method="PATCH", payload={"state": "closed"})
        self.state.save(item_id, phase="done", merged_sha=pr.get("merge_commit_sha"))
        return {"status": "done"}

    def prepare(self, path):
        match = re.fullmatch(r"GH-(\d+)", Path(path).name)
        if not match:
            raise Error("The workspace name does not identify a configured issue")
        _, items = self.snapshot()
        item = next((i for i in items if i["content"]["number"] == int(match[1])), None)
        if item is None:
            raise Error("The workspace issue is outside the configured project")
        return self.claim(item["id"], path, new_attempt=True)

    def after_run(self, path):
        record = next((r for r in self.state.all() if r.get("workspace") == str(Path(path).resolve())), None)
        if record and record["phase"] in {"running", "claiming"}:
            self.state.save(record["task"]["item_id"], phase="retrying")
        return {"status": "recorded"}

    def cleanup(self, path, terminal=True):
        path = Path(path).resolve()
        if self.config.get("docker"):
            from docker_worker import stop_owned
            stop_owned(self.config, path)
        record = next((r for r in self.state.all() if r.get("workspace") == str(path)), None)
        if not record:
            root = Path(self.config["workspace_root"]).resolve()
            if path.parent != root or not re.fullmatch(r"GH-\d+", path.name):
                raise Error("Cleanup path is outside the issue workspace root")
            if path.exists() and any(path.iterdir()):
                archive = Path(self.config["state_directory"]) / "preserved" / (path.name + "-unowned")
                archive.parent.mkdir(parents=True, exist_ok=True)
                if archive.exists():
                    raise Error("Reconcile the preserved unowned checkout before cleanup")
                path.rename(archive)
                return {"status": "preserved_unowned", "workspace": str(archive)}
            return {"status": "unowned"}
        task = Task(**record["task"])
        self.workspace(task, path)
        session = Path(self.config["state_directory"]) / "browser" / str(task.number) / "session.json"
        if session.exists():
            browser.BrowserJobs(self.config).action(task, path, {"action": "stop"})
        if terminal and (path / ".git").exists():
            dirty = browser.git(path, "status", "--porcelain")
            head = browser.git(path, "rev-parse", "HEAD")
            if dirty or head != record.get("published_sha"):
                archive = Path(self.config["state_directory"]) / "preserved" / f"GH-{task.number}-cycle-{task.cycle}"
                archive.parent.mkdir(parents=True, exist_ok=True)
                if archive.exists():
                    raise Error("A preserved checkout already exists; reconcile before cleanup")
                path.rename(archive)
                self.state.save(task.item_id, preserved_workspace=str(archive))
        return {"status": "cleaned"}

    def rpc(self, message):
        if message.get("repo") != self.config["repository"]:
            raise Error("The adapter repository does not match the installed controller")
        method, params = message.get("method"), message.get("params") or {}
        if method == "list":
            return self.list_issues(states=params["states"])
        if method == "refresh":
            return self.list_issues(ids=params["ids"])
        if method == "tool":
            item_id, workspace = params["item_id"], params["workspace"]
            name, args = params["name"], params["arguments"]
            if name == "project_claim":
                return self.claim(item_id, workspace)
            if name == "project_done":
                if not self.config["enabled"]:
                    raise Error("Symphony admission is paused")
                return self.done(item_id)
            _, item, task = self.find(item_id)
            self.workspace(task, workspace)
            record = self.state.get(item_id)
            if not record or record.get("workspace") != str(Path(workspace).resolve()):
                raise Error("Tool call requires this session's owned checkout")
            if not self.config["enabled"] or self.role(item) not in {"working", "rework", "merging"}:
                raise Error("Tools are paused outside active execution")
            if name == "project_browser":
                return self.browser_action(item_id, workspace, args)
            if name == "project_evidence":
                return self.evidence(item_id, workspace, args)
            if name == "project_workpad":
                return self.update_workpad(task, args["body"])
            if name == "project_restart":
                return self.restart(task)
            if name == "project_validate":
                return self.validate(item_id, workspace)
            if name == "project_git":
                return self.git_remote(item_id, workspace, args["action"])
            if name == "project_review":
                return self.review(item_id, workspace, args)
            if name == "project_done":
                return self.done(item_id)
            if name == "project_block":
                return self.block(task, args["reason"])
            if name == "github_api":
                return self.github_request(task, item, args)
        raise Error("Unsupported controller operation")

    def github_request(self, task, item, args):
        method, path = args["method"], args["path"]
        if path.startswith("/repos/") and not path.startswith(f"/repos/{task.repository}/"):
            raise Error("GitHub request belongs to another repository")
        if method == "PUT" and re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+/merge", path):
            if self.role(item) != "merging":
                raise Error("Only the human Merging transition authorizes landing")
            record = self.state.get(task.item_id)
            if path != f"/repos/{task.repository}/pulls/{record.get('pr_number')}/merge":
                raise Error("Landing must use the selected cycle PR")
            body = args.get("body") or {}
            pr = self.owned_pr(task, record)
            self.verify_pr(task, pr, body.get("sha"))
            if (pr["draft"] or pr.get("mergeable") is not True
                    or pr.get("mergeable_state") not in {"clean", "has_hooks", "unstable"}):
                raise Error("Repository merge rules or mergeability have not cleared; no admin bypass is allowed")
            if body.get("merge_method") != "squash":
                raise Error("Landing requires an expected-head squash merge")
            self.require_checks(task, body["sha"])
            if self.gh.item_status(task.item_id) != self.roles["merging"]:
                raise Error("The board moved before landing; preserve the selected PR")
        if path == "/graphql" and method == "POST":
            query = (args.get("body") or {}).get("query", "")
            if any(operation in query for operation in ("mergePullRequest", "enablePullRequestAutoMerge")):
                raise Error("Use the expected-head REST landing operation")
            if "updateProjectV2ItemFieldValue" in query:
                project, _ = self.snapshot()
                fields_ = project.get("fields", {}).get("nodes", [])
                forbidden = {option["id"] for field in fields_ if field.get("name") == self.status_field
                             for option in field.get("options", [])
                             if option["name"] in {self.roles["merging"], self.roles["rework"]}}
                if any(option in json.dumps(args.get("body")) for option in forbidden):
                    raise Error("Merging and Rework transitions belong to the human")
        endpoint = path.lstrip("/")
        if args.get("params"):
            endpoint += "?" + urlencode(args["params"])
        result = self.gh.api(endpoint, method=method, payload=args.get("body"))
        return {"status": 200, "body": result}

def artifact_links(rows):
    rows = [r for r in rows if Path(r.get("path", "")).suffix.lower() not in browser.VIDEO_SUFFIXES]
    if not rows:
        return ""
    return "\n\nLocal visual evidence:\n" + "\n".join(
        f"- [{row['caption'].replace('[', '').replace(']', '')}]({row['url']}) — `{row['sha256']}`"
        for row in rows)


@contextmanager
def controller_lock(directory):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (path / "controller.lock").open("a") as stream:
        # Waiting for a short controller operation is infrastructure latency,
        # never an implementation failure or a new agent attempt. Long browser
        # jobs and handoffs use issue locks instead of this global lock.
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("command", choices=["rpc", "bootstrap", "prepare", "after-run", "cleanup", "status", "enable", "disable"])
    parser.add_argument("payload", nargs="?")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--only-issue", type=int)
    args = parser.parse_args(argv)
    os.umask(0o077)
    config = json.loads(args.config.read_text())
    message = json.loads(args.payload) if args.command == "rpc" else None
    directory = Path(config["state_directory"])
    if message and message.get("method") == "tool":
        directory /= "issue-locks/" + hashlib.sha256(message["params"]["item_id"].encode()).hexdigest()
    with controller_lock(directory):
        controller = Controller(config)
        if args.command == "rpc":
            result = controller.rpc(message)
        elif args.command in {"prepare", "bootstrap"}:
            result = controller.prepare(args.workspace or Path.cwd())
        elif args.command == "after-run":
            result = controller.after_run(args.workspace or Path.cwd())
        elif args.command == "cleanup":
            result = controller.cleanup(args.workspace or Path.cwd())
        elif args.command in {"enable", "disable"}:
            if args.command == "enable":
                verify_preflight(args.config)
            config["enabled"] = args.command == "enable"
            config["only_issue"] = args.only_issue
            private_json(args.config, config)
            result = {"enabled": config["enabled"], "only_issue": config["only_issue"]}
        else:
            result = {"enabled": config["enabled"], "only_issue": config.get("only_issue"), "issues": controller.state.all()}
    print(json.dumps({"ok": True, "result": result}))


if __name__ == "__main__":
    # Reusable checks import this module during an RPC; share its Error types.
    sys.modules["controller"] = sys.modules[__name__]
    try:
        main()
    except (Error, ValueError, OSError, KeyError, sqlite3.Error) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        raise SystemExit(1) from None
