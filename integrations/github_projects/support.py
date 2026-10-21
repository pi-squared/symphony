"""GitHub Projects reads and credential-aware host subprocesses.

Extracted from the OpenContracts Symphony integration; no application fixtures.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import host_git
from urllib.parse import urlencode


PROJECT_ITEM_FRAGMENT = """
fragment DispatchProjectItem on ProjectV2Item {
  id isArchived
  fieldValues(first: 100) {
    nodes {
      ... on ProjectV2ItemFieldSingleSelectValue {
        name field { ... on ProjectV2SingleSelectField { name } }
      }
      ... on ProjectV2ItemFieldNumberValue {
        number field { ... on ProjectV2Field { name } }
      }
    }
  }
  content {
    __typename
    ... on Issue {
      id number title state url updatedAt
      repository { nameWithOwner isArchived }
      labels(first: 100) { nodes { name } }
    }
  }
}
"""


PROJECT_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  owner: __OWNER_KIND__(login: $owner) {
    projectV2(number: $number) {
      id title url viewerCanUpdate
      fields(first: 100) {
        nodes {
          ... on ProjectV2FieldCommon { id name }
          ... on ProjectV2SingleSelectField { options { id name } }
        }
      }
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { ...DispatchProjectItem }
      }
    }
  }
}
""" + PROJECT_ITEM_FRAGMENT


PROJECT_ISSUE_SEARCH_QUERY = """
query($query: String!, $cursor: String) {
  search(query: $query, type: ISSUE, first: 20, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Issue {
        id
        projectItems(first: 20) {
          pageInfo { hasNextPage endCursor }
          nodes { project { id } ...DispatchProjectItem }
        }
      }
    }
  }
}
""" + PROJECT_ITEM_FRAGMENT


ISSUE_PROJECT_ITEMS_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on Issue {
      projectItems(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { project { id } ...DispatchProjectItem }
      }
    }
  }
}
""" + PROJECT_ITEM_FRAGMENT


ITEM_QUERY = """
query($id: ID!, $fieldName: String!) {
  node(id: $id) {
    ... on ProjectV2Item {
      isArchived
      fieldValueByName(name: $fieldName) {
        ... on ProjectV2ItemFieldSingleSelectValue { name }
      }
    }
  }
}
"""


STATUS_MUTATION = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!, $fieldName: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: { singleSelectOptionId: $option }
  }) {
    projectV2Item {
      id
      fieldValueByName(name: $fieldName) {
        ... on ProjectV2ItemFieldSingleSelectValue { name }
      }
    }
  }
}
"""


SECRET_PATTERN = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bAKIA[A-Z0-9]{16}\b|"
    rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"
)


class ControllerError(RuntimeError):
    pass


def github_environment() -> dict[str, str]:
    env = dict(os.environ)
    # Repository GITHUB_TOKEN cannot access user projects. Use the saved login.
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(key, None)
    return env


def run(command: list[str], *, cwd: Path | None = None, timeout: int = 120,
        input_text: str | None = None) -> str:
    env = github_environment()
    if command[0] == "git":
        command, env = host_git.prepare(command[1:], cwd=cwd, env=env, gh="gh")
    result = subprocess.run(command, cwd=cwd, env=env,
                            input=input_text, capture_output=True, text=True,
                            timeout=timeout)
    if result.returncode:
        raise ControllerError(f"{command[0]} failed: {result.stderr.strip()[:1000]}")
    return result.stdout


class GitHub:
    def api(self, endpoint: str, *, method: str = "GET", payload: dict | None = None):
        command = ["gh", "api", endpoint, "--method", method]
        if payload is not None:
            command.extend(["--input", "-"])
        response = run(command, input_text=json.dumps(payload) if payload is not None else None)
        return json.loads(response) if response.strip() else None

    def graphql(self, query: str, variables: dict):
        response = self.api("graphql", method="POST", payload={"query": query, "variables": variables})
        if response.get("errors"):
            raise ControllerError(str(response["errors"]))
        return response["data"]

    def project(self, owner: str, number: int) -> tuple[dict, list[dict]]:
        owner_type = self.api("users/" + owner)["type"]
        if owner_type not in {"User", "Organization"}:
            raise ControllerError("Project owner must be a user or organization")
        query = PROJECT_QUERY.replace("__OWNER_KIND__", "organization" if owner_type == "Organization" else "user")
        cursor = None
        items = []
        while True:
            data = self.graphql(query, {"owner": owner, "number": number, "cursor": cursor})
            project = (data.get("owner") or {}).get("projectV2")
            if not project:
                raise ControllerError("The configured project is missing or inaccessible.")
            items.extend(project["items"]["nodes"])
            page = project["items"]["pageInfo"]
            if not page["hasNextPage"]:
                break
            cursor = page["endCursor"]
        # GitHub can omit a newly linked issue from ProjectV2.items while the
        # issue's projectItems already confirms membership. Supplement the
        # listing with project-scoped search and verify every item's project ID.
        merged = {item["id"]: item for item in items}
        for item in self.search_project_items(owner, number, project["id"]):
            merged[item["id"]] = item
        return project, list(merged.values())

    def search_project_items(self, owner: str, number: int, project_id: str) -> list[dict]:
        query = f"is:issue is:open project:{owner}/{number}"
        cursor = None
        items = []
        while True:
            search = self.graphql(PROJECT_ISSUE_SEARCH_QUERY, {"query": query, "cursor": cursor})["search"]
            if search["issueCount"] > 1000:
                raise ControllerError("Project issue search exceeds GitHub's 1,000-result limit; narrow the queue before dispatching.")
            for issue in search["nodes"]:
                if not issue or "projectItems" not in issue:
                    continue
                memberships = issue["projectItems"]
                while True:
                    items.extend(item for item in memberships["nodes"] if item["project"]["id"] == project_id)
                    page = memberships["pageInfo"]
                    if not page["hasNextPage"]:
                        break
                    memberships = self.graphql(ISSUE_PROJECT_ITEMS_QUERY, {
                        "id": issue["id"], "cursor": page["endCursor"],
                    })["node"]["projectItems"]
            page = search["pageInfo"]
            if not page["hasNextPage"]:
                return items
            cursor = page["endCursor"]

    def item_status(self, item_id: str) -> str | None:
        item = self.graphql(ITEM_QUERY, {"id": item_id, "fieldName": getattr(self, "status_field", "Status")}).get("node")
        if not item or item["isArchived"]:
            return None
        return (item.get("fieldValueByName") or {}).get("name")

    def status(self, project: dict, item_id: str, name: str) -> None:
        field = next(f for f in project["fields"]["nodes"] if f.get("name") == getattr(self, "status_field", "Status"))
        option = next(o for o in field["options"] if o["name"] == name)
        data = self.graphql(STATUS_MUTATION, {"project": project["id"], "item": item_id,
                                            "field": field["id"], "option": option["id"], "fieldName": field["name"]})
        updated = data["updateProjectV2ItemFieldValue"]["projectV2Item"]
        if (updated.get("fieldValueByName") or {}).get("name") != name:
            raise ControllerError(f"GitHub did not confirm the project Status update to {name}.")

    def issue(self, task: "Task") -> dict:
        return self.api(f"repos/{task.repository}/issues/{task.number}")

    def comment(self, task: "Task", body: str) -> None:
        self.api(f"repos/{task.repository}/issues/{task.number}/comments", method="POST", payload={"body": body})

    def pull_request(self, task: "Task") -> dict | None:
        query = urlencode({"state": "open", "head": f"{task.repository.split('/')[0]}:{task.branch}", "per_page": 100})
        prs = self.api(f"repos/{task.repository}/pulls?{query}")
        return prs[0] if prs else None


@dataclass(frozen=True)
class Task:
    issue_id: str
    item_id: str
    repository: str
    number: int
    title: str
    url: str
    priority: str | None
    order: float | None

    @property
    def branch(self) -> str:
        return f"codex/symphony-issue-{self.number}"


def check_publication_paths(workspace: Path) -> list[str]:
    tracked = run(["git", "diff", "HEAD", "--name-only", "--no-renames", "-z"], cwd=workspace).split("\0")
    untracked = run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=workspace).split("\0")
    untracked = [name for name in untracked if name
                 and not {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".venv"}.intersection(Path(name).parts)
                 and not name.endswith((".pyc", ".pyo"))]
    paths = sorted(set(filter(None, tracked + untracked)))
    for name in paths:
        path = workspace / name
        if path.name == ".env" or (path.name.startswith(".env.") and path.name not in {".env.example", ".env.sample", ".env.template"}):
            raise ControllerError(f"Refusing to publish a credential configuration file: {name}")
        if path.is_file() and not path.is_symlink():
            if path.stat().st_size >= 100 * 1024 * 1024:
                raise ControllerError(f"File exceeds GitHub's ordinary Git size limit: {name}")
            content = path.read_bytes()
            if (SECRET_PATTERN.search(content)
                    ):
                raise ControllerError(f"A potential credential was found in a changed file: {name}")
    return paths
