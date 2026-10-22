"""Install a selected fork build and a configurable GitHub Projects controller."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys

from controller import Ledger, private_json
from github_cli import install as install_gh

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def capture(command, **kwargs):
    return subprocess.check_output([str(p) for p in command], text=True, **kwargs).strip()


def durable_python(destination):
    target = destination / "runtime/python"
    if not target.exists():
        shutil.copytree(Path(sys.base_prefix).resolve(), target, symlinks=True)
    python = target / "bin/python3.11"
    capture([python, "-c", "import sqlite3, ssl; assert __import__('sys').version_info[:2] == (3,11)"])
    return python


def durable_codex(destination):
    command = shutil.which("codex")
    if not command:
        raise ValueError("Install the complete standalone Codex package first")
    source = Path(command).resolve()
    package = source.parent.parent
    if not (package / "codex-package.json").is_file():
        raise ValueError("Install the complete standalone Codex package, including its code-mode host")
    target = destination / "runtime/codex" / package.name
    if not target.exists():
        shutil.copytree(package, target, symlinks=True)
    binary = target / "bin/codex"
    capture([binary, "--version"])
    if not binary.with_name("codex-code-mode-host").is_file():
        raise ValueError("Codex's companion code-mode host is missing")
    return binary


def validate_settings(settings):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", settings.get("repository", "")):
        raise ValueError("Configure repository as owner/name")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", settings.get("project_owner", "")):
        raise ValueError("Configure the project owner login")
    number = settings.get("project_number")
    if type(number) is not int or number < 1:
        raise ValueError("Configure a positive project_number")
    for key in ("required_checks", "checks"):
        if not isinstance(settings.get(key), list) or not settings[key]:
            raise ValueError("Configure at least one " + key + " entry")
    if any(not isinstance(x, str) or not x.strip() for x in settings["required_checks"]):
        raise ValueError("Required check names must be nonempty strings")
    if any(not isinstance(c, list) or not c or any(not isinstance(p, str) for p in c) for c in settings["checks"]):
        raise ValueError("Independent checks must be nonempty argument arrays")
    base = settings.get("base_branch", "main")
    if not isinstance(base, str) or base.startswith("-"):
        raise ValueError("Invalid base_branch")
    subprocess.run(["git", "check-ref-format", "--branch", base], check=True, capture_output=True)
    fields = settings.get("fields", {})
    if set(fields) - {"status", "priority", "execution_order"} or any(not isinstance(v, str) or not v.strip() for v in fields.values()):
        raise ValueError("Configure nonempty status, priority and execution_order field names")
    jules = settings.get("jules")
    if jules:
        if not isinstance(jules, dict) or type(jules.get("enabled", False)) is not bool:
            raise ValueError("Configure the optional Jules issue dispatcher explicitly")
        if not isinstance(jules.get("label", "runner:jules"), str) or not jules.get("label", "runner:jules").strip():
            raise ValueError("Configure a nonempty Jules issue label")
    docker = settings.get("docker")
    if docker:
        if not isinstance(docker, dict) or not isinstance(docker.get("image"), str):
            raise ValueError("Docker workers require a pinned image")
        if not docker["image"].startswith("sha256:") and "@sha256:" not in docker["image"]:
            raise ValueError("Pin Docker image by ID or digest")
        if not Path(docker.get("auth_file", "")).is_absolute():
            raise ValueError("Configure a dedicated absolute Codex auth_file")
    browser = settings.get("browser", {})
    if browser:
        if not isinstance(browser.get("scenarios"), list) or not browser["scenarios"]:
            raise ValueError("Browser support requires configured scenario names")
        if any(not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", s) for s in browser["scenarios"]):
            raise ValueError("Scenario names must be simple identifiers")
        for key in ("worker", "python"):
            if not Path(browser.get(key, "")).is_absolute() or not Path(browser[key]).is_file():
                raise ValueError("Browser " + key + " must be an absolute existing file")
    for key in ("protected_paths",):
        if not isinstance(settings.get(key, []), list) or any(not isinstance(p, str) for p in settings.get(key, [])):
            raise ValueError(key + " must be a list of path patterns")
    roles = settings.get("roles")
    expected = {"backlog", "queued", "working", "review", "rework", "merging", "done"}
    if roles is not None and (not isinstance(roles, dict) or set(roles) != expected
            or any(not isinstance(s, str) or not s.strip() for s in roles.values())
            or len(set(roles.values())) != len(roles)):
        raise ValueError("Configure seven distinct nonempty lifecycle roles")
    if type(settings.get("max_concurrent_agents", 1)) is not int or settings.get("max_concurrent_agents", 1) < 1:
        raise ValueError("max_concurrent_agents must be positive")
    for key in ("model", "reasoning", "rework_model", "rework_reasoning"):
        if key in settings and (not isinstance(settings[key], str) or not settings[key].strip()):
            raise ValueError(key + " must be a nonempty string")
    patterns = browser.get("required_paths", [])
    if not isinstance(patterns, list) or any(not isinstance(p, str) for p in patterns):
        raise ValueError("Browser required_paths must be a list of patterns")


@contextmanager
def stopped_installation(destination):
    state = destination / "state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Stop this installation's service before upgrading") from None
        yield


def install(destination, settings):
    validate_settings(settings)
    destination = Path(destination).expanduser().resolve()
    if any(c in str(destination) for c in ("\n", "\r", "%")):
        raise ValueError("Installation paths cannot contain newlines or systemd specifiers")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with stopped_installation(destination):
        return install_stopped(destination, settings)


def install_stopped(destination, settings):
    config_path = destination / "config.json"
    previous = json.loads(config_path.read_text()) if config_path.exists() else {}
    if previous.get("enabled"):
        raise ValueError("Pause admission before upgrading")
    if previous and any(previous.get(k) != settings[k] for k in ("repository", "project_owner", "project_number")):
        raise ValueError("Use a separate installation directory for a different repository or project")
    binary = ROOT / "elixir/bin/symphony"
    if not binary.is_file():
        raise ValueError("Build this fork with make -C elixir build before installation")
    if previous:
        backup = destination / "backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup.mkdir(parents=True, mode=0o700)
        for name in ("config.json", "WORKFLOW.md", "installed.json"):
            shutil.copy2(destination / name, backup / name)
        database = destination / "state/state.sqlite3"
        if database.exists():
            with sqlite3.connect(database) as source_db, sqlite3.connect(backup / "state.sqlite3") as target_db:
                source_db.backup(target_db)
    sources = {str(p.relative_to(HERE)): p for p in HERE.rglob("*")
               if p.is_file() and "__pycache__" not in p.parts}
    identity = hashlib.sha256(binary.read_bytes() + b"".join(
        name.encode() + sources[name].read_bytes() for name in sorted(sources))).hexdigest()
    release = destination / "releases" / identity
    package = release / "controller"
    package.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, source in sources.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, path)
        path.chmod(0o600)
    (release / "bin").mkdir(exist_ok=True, mode=0o700)
    shutil.copyfile(binary, release / "bin/symphony")
    (release / "bin/symphony").chmod(0o700)
    gh = Path(settings["gh"]).resolve() if settings.get("gh") else install_gh(destination)
    python, codex = durable_python(destination), durable_codex(destination)
    current = destination / "current"
    config = {**settings, "enabled": False, "only_issue": previous.get("only_issue"),
        "base_branch": settings.get("base_branch", "main"), "model": settings.get("model", "gpt-6.1-sol"),
        "reasoning": settings.get("reasoning", "medium"), "workspace_root": str(destination / "workspaces"),
        "state_directory": str(destination / "state"), "python": str(python), "codex": str(codex),
        "gh": str(gh), "symphony": str(current / "bin/symphony"), "workflow": str(destination / "WORKFLOW.md"),
        "port": settings.get("port", 4001), "runtime_roots": list(dict.fromkeys([
            str(python.parent.parent), *settings.get("runtime_roots", [])])),
        "runtime_bin": list(dict.fromkeys([str(gh.parent), *settings.get("runtime_bin", []),
            *[str(Path(path).parent) for name in ("erl", "escript") if (path := shutil.which(name))]])),
        "service_name": "symphony-github-projects-" + re.sub(r"[^A-Za-z0-9_.-]", "-", destination.name) + ".service"}
    for name in ("logs", "workspaces", "units"):
        (destination / name).mkdir(exist_ok=True, mode=0o700)
    front, body = (HERE / "templates/WORKFLOW.md").read_text().split("---", 2)[1:]
    policy = json.loads(front)
    policy["tracker"]["provider"].update(repo=config["repository"], controller=str(destination / "controller"),
        browser_enabled=bool(config.get("browser")), roles=settings.get("roles", policy["tracker"]["provider"]["roles"]))
    roles = policy["tracker"]["provider"]["roles"]
    policy["tracker"].update(required_labels=settings.get("required_labels", ["ready-for-agent"]),
        active_states=[roles[k] for k in ("queued", "working", "rework", "merging")], terminal_states=[roles["done"]])
    if settings.get("assignee"):
        policy["tracker"]["assignee"] = settings["assignee"]
    policy["tracker"]["provider"]["dependency_gating"] = settings.get("dependency_gating", True)
    policy["workspace"]["root"] = config["workspace_root"]
    policy["agent"]["max_concurrent_agents"] = settings.get("max_concurrent_agents", 1)
    for key, action in (("after_create", "bootstrap"), ("before_run", "prepare"),
                        ("after_run", "after-run"), ("before_remove", "cleanup")):
        policy["hooks"][key] = shlex.quote(str(destination / "controller")) + ' ' + action + ' --workspace "$PWD"'
    command = shlex.split(policy["codex"]["command"])
    for i, value in enumerate(command):
        if value.startswith("model="):
            command[i] = "model=" + json.dumps(config["model"])
        elif value.startswith("model_reasoning_effort="):
            command[i] = "model_reasoning_effort=" + json.dumps(config["reasoning"])
    command[0] = str(codex)
    config["codex_argv"] = command
    config["rework_model"] = settings.get("rework_model", "gpt-6.1-sol")
    config["rework_reasoning"] = settings.get("rework_reasoning", "xhigh")
    policy["codex"]["command"] = shlex.join([
        str(python), str(current / "controller/worker.py"), "--config", str(config_path)])
    policy["validation"]["required_checks"] = config["required_checks"]
    policy["server"]["port"] = config["port"]
    # Role placeholders keep the same prompt usable with renamed board states.
    for role, value in roles.items():
        body = body.replace("__ROLE_" + role.upper() + "__", value)
    if config.get("docker"):
        # The operational procedures remain ordinary task instructions. No
        # downloaded skill catalog is installed into the issue or worker home.
        body = body.replace(".codex/skills/land/SKILL.md", "the embedded land procedure")
        body = body.replace("skill", "procedure").replace("Skill", "Procedure")
        body += "\n\n## Embedded operating procedures\n\n"
        for source in sorted((HERE / "templates/skills").glob("*/SKILL.md")):
            body += "\n### " + source.parent.name + "\n" + source.read_text().split("---", 2)[-1]
        body += ("\nUse packaged OpenAI skills only. Keep additional skill catalogs uninstalled.\n"
                 "For authenticated Git remote I/O call project_git with action fetch before merging "
                 "origin/BASE_BRANCH, and action push after committing and validating. These host-bound "
                 "operations replace shell git fetch/push in the embedded procedures. The host verifies "
                 "the repository/branch and remote SHA; GitHub credentials remain outside this container. "
                 "Use local Git for commits and merges, and github_api for draft PR creation.\n")
    workflow = "---\n" + json.dumps(policy, indent=2) + "\n---" + body
    (release / "WORKFLOW.md").write_text(workflow)
    ledger = Ledger(config["state_directory"])
    ledger.db.close()
    private_json(config_path, config)
    (destination / "WORKFLOW.md").write_text(workflow)
    (destination / "WORKFLOW.md").chmod(0o600)
    temporary = destination / "current.new"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(release, target_is_directory=True)
    temporary.replace(current)
    for name, script in (("controller", "controller.py"), ("operate", "operations.py"), ("preflight", "preflight.py")):
        launcher = destination / name
        launcher.write_text("#!/bin/sh\nexec " + shlex.quote(str(python)) + " " +
            shlex.quote(str(current / "controller" / script)) + " --config " + shlex.quote(str(config_path)) + ' "$@"\n')
        launcher.chmod(0o700)
    executable = shlex.join([str(python), str(current / "controller/launch.py"), "--config", str(config_path)])
    unit = ("[Unit]\nDescription=Symphony GitHub Projects\nAfter=network-online.target\n\n[Service]\n"
        f"Type=simple\nWorkingDirectory={destination}\nExecStart={executable}\nRestart=on-failure\nRestartSec=10\n"
        "KillMode=control-group\nTimeoutStopSec=30\nUMask=0077\n"
        f"Environment=PATH={':'.join([*config['runtime_bin'], '/usr/local/bin', '/usr/bin', '/bin'])}\n\n"
        "[Install]\nWantedBy=default.target\n")
    (destination / "units" / config["service_name"]).write_text(unit)
    private_json(destination / "installed.json", {"identity": identity,
        "source_commit": capture(["git", "rev-parse", "HEAD"], cwd=ROOT),
        "source_worktree_dirty": bool(capture(["git", "status", "--porcelain"], cwd=ROOT)),
        "symphony_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "sources_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()}})
    return config


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--destination", type=Path, default=Path.home()/".local/share/symphony-github-projects")
    args = parser.parse_args()
    os.umask(0o077)
    result = install(args.destination, json.loads(args.settings.read_text()))
    print(json.dumps({"installed": str(args.destination), "repository": result["repository"], "enabled": False}))
