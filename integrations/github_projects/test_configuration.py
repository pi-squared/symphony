"""Portable project reads, installation, role mapping and service preparation."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import controller as c
import install
from operations import Operations
from test_controller import FakeGitHub, item


class ProjectTests(unittest.TestCase):
    def test_user_and_organization_boards_select_the_correct_graphql_owner(self):
        for kind, root in (("User", "user"), ("Organization", "organization")):
            with self.subTest(kind=kind):
                gh = c.GitHub()
                queries = []
                project = {"id": "project", "items": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
                def graphql(query, variables):
                    queries.append(query)
                    return {"owner": {"projectV2": project}}
                with patch.object(gh, "api", return_value={"type": kind}), \
                     patch.object(gh, "graphql", side_effect=graphql), \
                     patch.object(gh, "search_project_items", return_value=[]):
                    actual, rows = gh.project("another-owner", 17)
                self.assertEqual(actual["id"], "project")
                self.assertEqual(rows, [])
                self.assertIn("owner: " + root + "(login:", queries[0])

    def test_custom_status_field_is_used_for_reads_and_confirmed_writes(self):
        gh = c.GitHub(status_field="Stage")
        project = {"fields": {"nodes": [{"id": "stage", "name": "Stage",
                    "options": [{"id": "ready-id", "name": "Queued"}]}]}}
        calls = []
        def graphql(query, variables):
            calls.append(variables)
            if "updateProjectV2ItemFieldValue" in query:
                return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"fieldValueByName": {"name": "Queued"}}}}
            return {"node": {"isArchived": False, "fieldValueByName": {"name": "Queued"}}}
        with patch.object(gh, "graphql", side_effect=graphql):
            self.assertEqual(gh.item_status("item"), "Queued")
            gh.status({**project, "id": "project"}, "item", "Queued")
        self.assertTrue(all(call["fieldName"] == "Stage" for call in calls))
        self.assertEqual(calls[1]["option"], "ready-id")

    def test_second_repository_custom_roles_and_execution_fields_are_admitted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = json.loads((install.HERE / "templates/WORKFLOW.md").read_text().split("---", 2)[1])
            roles = {k: "custom-" + k for k in policy["tracker"]["provider"]["roles"]}
            policy["tracker"]["provider"]["roles"] = roles
            policy["tracker"]["active_states"] = [roles[k] for k in ("queued", "working", "rework", "merging")]
            policy["tracker"]["terminal_states"] = [roles["done"]]
            workflow = root / "WORKFLOW.md"
            workflow.write_text("---\n" + json.dumps(policy) + "\n---\nImplement.")
            row = item(status=roles["queued"])
            row["content"]["repository"]["nameWithOwner"] = "organization/second-repository"
            row["fieldValues"]["nodes"][0]["field"]["name"] = "Stage"
            row["fieldValues"]["nodes"].extend([
                {"field": {"name": "Urgency"}, "name": "P0"},
                {"field": {"name": "Rank"}, "number": 7}])
            gh = FakeGitHub([row])
            config = {"repository": "organization/second-repository", "project_owner": "organization",
                      "project_number": 17, "enabled": True, "base_branch": "develop",
                      "workspace_root": str(root / "workspaces"), "state_directory": str(root / "state"),
                      "workflow": str(workflow), "fields": {"status": "Stage", "priority": "Urgency", "execution_order": "Rank"}}
            controller = c.Controller(config, gh)
            self.addCleanup(controller.state.db.close)
            rows = controller.list_issues()
            self.assertTrue(rows[0]["dispatchable"])
            self.assertEqual(rows[0]["priority"], 1)
            self.assertEqual(rows[0]["native_ref"]["execution_order"], 7)
            self.assertEqual(controller.base_ref, "origin/develop")
            self.assertFalse(controller.in_scope(item()))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        (self.source / "elixir/bin").mkdir(parents=True)
        (self.source / "elixir/bin/symphony").write_bytes(b"synthetic built escript")
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.source)], check=True)
        for args in (("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid"),
                     ("add", "elixir/bin/symphony"), ("commit", "-qm", "Selected build")):
            subprocess.run(["git", *args], cwd=self.source, check=True)
        self.destination = self.root / "installation"
        self.settings = {"repository": "example/project", "project_owner": "example", "project_number": 17,
                         "required_checks": ["Unit tests"], "checks": [["{python}", "-m", "unittest"]],
                         "base_branch": "develop", "model": "configured-model", "reasoning": "high",
                         "fields": {"status": "Stage"}}

    def do_install(self, settings=None):
        with patch.object(install, "ROOT", self.source), \
             patch.object(install, "durable_python", return_value=Path(sys.executable)), \
             patch.object(install, "durable_codex", return_value=Path("/configured/codex")), \
             patch.object(install, "install_gh", return_value=Path("/configured/gh")):
            return install.install(self.destination, settings or self.settings)

    def test_clean_install_without_browser_is_self_contained_and_disabled(self):
        config = self.do_install()
        self.assertFalse(config["enabled"])
        self.assertNotIn("browser", config)
        workflow = (self.destination / "WORKFLOW.md").read_text()
        self.assertNotIn("__ROLE_", workflow)
        self.assertNotIn("__CONTROLLER__", workflow)
        policy = json.loads(workflow.split("---", 2)[1])
        self.assertEqual(policy["tracker"]["provider"]["repo"], "example/project")
        self.assertFalse(policy["tracker"]["provider"]["browser_enabled"])
        self.assertEqual(policy["validation"]["required_checks"], ["Unit tests"])
        self.assertIn('worker.py', policy['codex']['command'])
        self.assertIn('model="configured-model"', config['codex_argv'])
        self.assertEqual(policy["agent"]["max_concurrent_agents"], 1)
        package = self.destination / "current/controller"
        result = subprocess.run([sys.executable, "-c", "import controller, browser, attachments, sandbox"],
                                cwd=package, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        metadata = json.loads((self.destination / "installed.json").read_text())
        self.assertEqual(metadata["symphony_sha256"], hashlib.sha256(b"synthetic built escript").hexdigest())
        self.assertTrue((self.destination / "units" / config["service_name"]).is_file())

    def test_upgrade_backs_up_state_and_refuses_live_or_cross_project_switch(self):
        self.do_install()
        ledger = c.Ledger(self.destination / "state")
        ledger.save("item", task={"item_id": "item"}, phase="running", unpublished="preserve")
        ledger.db.close()
        self.do_install()
        self.assertEqual(len(list((self.destination / "backups").glob("*/state.sqlite3"))), 1)
        ledger = c.Ledger(self.destination / "state")
        self.assertEqual(ledger.get("item")["unpublished"], "preserve")
        ledger.db.close()
        with self.assertRaisesRegex(ValueError, "separate installation"):
            self.do_install({**self.settings, "repository": "example/another-project"})
        config_path = self.destination / "config.json"
        data = json.loads(config_path.read_text()); data["enabled"] = True
        c.private_json(config_path, data)
        with self.assertRaisesRegex(ValueError, "Pause admission"):
            self.do_install()

    def test_configured_browser_worker_and_custom_roles_render_into_the_installation(self):
        provider = self.root / "application-worker.py"
        provider.write_text("# Temporary operator-supplied worker\n")
        policy = json.loads((install.HERE / "templates/WORKFLOW.md").read_text().split("---", 2)[1])
        roles = {key: "custom-" + key for key in policy["tracker"]["provider"]["roles"]}
        config = self.do_install({**self.settings, "roles": roles, "browser": {
            "python": sys.executable, "worker": str(provider), "scenarios": ["test-flow"],
            "required_paths": ["ui/**"], "browser_cache": str(self.root / "browser-cache")}})
        text = Path(config["workflow"]).read_text()
        installed_policy = json.loads(text.split("---", 2)[1])
        self.assertTrue(installed_policy["tracker"]["provider"]["browser_enabled"])
        self.assertIn("custom-merging", text)
        self.assertEqual(config["browser"]["worker"], str(provider))

    def test_settings_require_real_check_configuration(self):
        for changes in ({"checks": []}, {"required_checks": []}, {"repository": "invalid"},
                        {"project_number": 0}, {"fields": {"status": ""}}, {"base_branch": "--bad"},
                        {"browser": {"scenarios": ["unknown"]}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                install.validate_settings({**self.settings, **changes})

    def test_board_upgrade_preserves_existing_ids_and_custom_field_name(self):
        config = self.do_install()
        path = self.destination / "config.json"
        policy = json.loads(Path(config["workflow"]).read_text().split("---", 2)[1])
        roles = policy["tracker"]["provider"]["roles"]
        class Fields:
            def __init__(self):
                self.options = [{"id": "id-" + roles[k], "name": roles[k], "color": "GRAY", "description": "old"}
                                for k in ("backlog", "queued", "working", "review", "done")]
                self.updates = []
            def project(self, *_):
                return {"id": "project"}, []
            def graphql(self, query, variables):
                if "updateProjectV2Field" in query:
                    self.updates.append(deepcopy(variables["options"]))
                    self.options = [{"id": "new-" + row["name"], **row} for row in variables["options"]]
                    return {}
                return {"node": {"fields": {"nodes": [{"id": "stage", "name": "Stage", "options": deepcopy(self.options)}]}}}
        gh = Fields()
        old = deepcopy(gh.options)
        operations = Operations(path, gh)
        self.assertEqual(operations.add_states(), operations.add_states())
        self.assertEqual(len(gh.updates), 1)
        self.assertEqual(gh.updates[0][:5], old)
