"""Operate one configured installation and preserve Project status option IDs."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from controller import Controller, Error, GitHub


def systemctl(*args):
    environment = {**os.environ, "XDG_RUNTIME_DIR": "/run/user/" + str(os.getuid()),
                   "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/" + str(os.getuid()) + "/bus"}
    result = subprocess.run(["systemctl", "--user", *args], env=environment,
                            capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise Error("systemctl failed: " + result.stderr.strip())
    return result.stdout.strip()


class Operations:
    def __init__(self, path, gh=None):
        self.path = Path(path)
        self.root = self.path.parent
        self.config = json.loads(self.path.read_text())
        self.status_field = self.config.get("fields", {}).get("status", "Status")
        self.gh = gh or GitHub(self.config.get("gh", "gh"), status_field=self.status_field)

    def add_states(self):
        roles = json.loads(Path(self.config["workflow"]).read_text().split("---", 2)[1])["tracker"]["provider"]["roles"]
        project, _ = self.gh.project(self.config["project_owner"], self.config["project_number"])
        query = """query($id: ID!) { node(id:$id) { ... on ProjectV2 { fields(first:100) { nodes {
          ... on ProjectV2SingleSelectField { id name options { id name color description } }
        } } } } }"""
        def read():
            data = self.gh.graphql(query, {"id": project["id"]})
            return next(f for f in data["node"]["fields"]["nodes"] if f.get("name") == self.status_field)
        field = read()
        previous = {o["name"]: o["id"] for o in field["options"]}
        options = list(field["options"])
        for role in ("rework", "merging"):
            if roles[role] not in previous:
                options.append({"name": roles[role], "color": "PURPLE" if role == "rework" else "GREEN",
                                "description": "Human-authorized " + role})
        if len(options) != len(field["options"]):
            mutation = """mutation($field:ID!, $options:[ProjectV2SingleSelectFieldOptionInput!]!) {
              updateProjectV2Field(input:{fieldId:$field,singleSelectOptions:$options}) {
                projectV2Field { ... on ProjectV2SingleSelectField { id } }
              }
            }"""
            self.gh.graphql(mutation, {"field": field["id"], "options": options})
        actual = {o["name"]: o["id"] for o in read()["options"]}
        if any(actual.get(name) != value for name, value in previous.items()):
            raise Error("Existing status option IDs changed; keep admission paused")
        if not set(roles.values()).issubset(actual):
            raise Error("The project is missing configured lifecycle states")
        return {"existing_option_ids_preserved": True, "options": actual}

    def install_service(self):
        source = self.root / "units" / self.config["service_name"]
        destination = Path.home() / ".config/systemd/user" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        systemctl("daemon-reload")
        systemctl("enable", "--now", source.name)
        return {"service": systemctl("is-active", source.name), "admission_enabled": self.config["enabled"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("command", choices=("add-states", "install-service", "status"))
    args = parser.parse_args()
    operation = Operations(args.config)
    if args.command == "add-states":
        result = operation.add_states()
    elif args.command == "install-service":
        result = operation.install_service()
    else:
        controller = Controller(operation.config)
        try:
            result = {"enabled": operation.config["enabled"], "issues": controller.state.all()}
        finally:
            controller.state.db.close()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
