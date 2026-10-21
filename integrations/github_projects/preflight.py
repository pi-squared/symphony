"""Exercise the installed Codex protocol without starting a model turn."""

import argparse
import hashlib
import json
from pathlib import Path
import queue
import shlex
import subprocess
import tempfile
import threading
import time
import os


from controller import private_json


def verify_profile(config_path, *, rework=False):
    config_path = Path(config_path)
    config = json.loads(config_path.read_text())
    workflow_bytes = Path(config["workflow"]).read_bytes()
    workflow = json.loads(workflow_bytes.decode().split("---", 2)[1])
    from worker import execution_settings
    settings = execution_settings(config, rework=rework)
    model_name, reasoning = settings["model"], settings["reasoning"]
    messages = queue.Queue()
    command = shlex.split(workflow["codex"]["command"])
    if rework:
        command.append("--rework-preflight")
    with tempfile.TemporaryDirectory(prefix=".preflight-", dir=config["workspace_root"]) as workspace:
        if config.get("docker"):
            # An existing tracked catalog/config catches approaches that hide
            # files from Git or fail to disable project configuration discovery.
            skill = Path(workspace)/".agents/skills/custom/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: forbidden-custom\ndescription: Synthetic custom skill\n---\nCustom\n")
            project_config = Path(workspace)/".codex/config.toml"
            project_config.parent.mkdir()
            project_config.write_text('developer_instructions="FORBIDDEN_PROJECT_SENTINEL"\n')
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace, check=True)
            subprocess.run(["git", "add", "--", ".agents", ".codex"], cwd=workspace, check=True)
            subprocess.run(["git", "-c", "user.name=Preflight", "-c", "user.email=preflight@example.invalid",
                            "commit", "-qm", "Tracked catalog fixture"], cwd=workspace, check=True)
        error_path = Path(config["state_directory"])/("preflight-rework.log" if rework else "preflight-normal.log")
        error_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        error_log = error_path.open("wb")
        os.chmod(error_path, 0o600)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=error_log, text=True, bufsize=1, cwd=workspace)
        def read():
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass
        threading.Thread(target=read, daemon=True).start()
        def send(message):
            process.stdin.write(json.dumps(message)+"\n")
            process.stdin.flush()
        def request(identifier, method, params):
            send({"id": identifier, "method": method, "params": params})
            deadline = time.monotonic() + 45
            while True:
                try:
                    message = messages.get(timeout=0.25)
                except queue.Empty:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(method + " did not complete; inspect the private preflight log") from None
                    continue
                if message.get("id") == identifier:
                    if "error" in message:
                        raise RuntimeError(f"{method}: {message['error']}")
                    return message["result"]
        try:
            request(1, "initialize", {"clientInfo": {"name": "symphony_preflight", "version": "1"},
                                       "capabilities": {"experimentalApi": True}})
            send({"method": "initialized", "params": {}})
            effective = request(2, "config/read", {"includeLayers": False})["config"]
            if config.get("docker") and effective.get("developer_instructions") == "FORBIDDEN_PROJECT_SENTINEL":
                raise RuntimeError("Docker worker loaded project Codex configuration")
            if (effective.get("model"), effective.get("model_reasoning_effort")) != (model_name, reasoning):
                raise RuntimeError("The requested model and reasoning are not effective")
            models = request(3, "model/list", {"limit": 100})["data"]
            model = next((m for m in models if m.get("model") == model_name), None)
            if not model or reasoning not in {r["reasoningEffort"] for r in model["supportedReasoningEfforts"]}:
                raise RuntimeError("The signed-in account does not expose the requested model and effort")
            if config.get("docker"):
                inventory = request(31, "skills/list", {"cwds": [workspace], "forceReload": True})
                private_json(Path(config["state_directory"])/"preflight-skills.json", inventory)
                for entry in inventory["data"]:
                    if entry.get("errors") or any(s.get("enabled", True) and s.get("scope") != "system" for s in entry["skills"]):
                        raise RuntimeError("Docker worker exposes nonpackaged skills or discovery errors")
            permissions = workflow["codex"].get("permissions")
            if permissions:
                if any(key in workflow["codex"] for key in ("thread_sandbox", "turn_sandbox_policy")):
                    raise RuntimeError("Profile mode cannot include legacy sandbox overrides")
                profiles, cursor = [], None
                while True:
                    page = request(4, "permissionProfile/list", {"cwd": workspace, "cursor": cursor})
                    profiles.extend(page["data"])
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                if not any(p["id"] == permissions and p["allowed"] for p in profiles):
                    raise RuntimeError("The selected permission profile is absent or disallowed")
                sandbox_params = {"permissions": permissions}
            else:
                sandbox_params = {"sandbox": workflow["codex"]["thread_sandbox"]}
            thread = request(5, "thread/start", {"cwd": workspace, "ephemeral": True,
                "approvalPolicy": workflow["codex"]["approval_policy"], **sandbox_params,
                "dynamicTools": [{"name": "symphony_preflight", "description": "Protocol preflight only",
                                  "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]})
            if permissions:
                probe = request(6, "command/exec", {"cwd": workspace,
                    "permissionProfile": permissions, "timeoutMs": 30000,
                    "command": ["sh", "-ec",
                        "if test -d .git; then test -z \"$(git status --porcelain)\"; fi; git init -q -b main; git config user.name Preflight; git config user.email preflight@example.invalid; "
                        "mkdir -p .codex/skills .agents; printf probe > probe.txt; "
                        "printf probe > .codex/skills/probe.txt; printf probe > .agents/probe.txt; "
                        "git add -- probe.txt .codex .agents; git commit -qm preflight; "
                        "git switch -qc preflight-sync; printf synced > .codex/skills/probe.txt; "
                        "git add -- .codex/skills/probe.txt; git commit -qm sync; "
                        "git switch -q main; git merge -q --no-edit preflight-sync; git rev-parse --verify HEAD"]})
                if probe.get("exitCode") != 0:
                    raise RuntimeError("Git permission probe failed: " + str(probe))
            if not thread.get("thread", {}).get("id"):
                raise RuntimeError("Codex did not confirm the thread")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()
            error_log.close()
    receipt = {"installation_identity": json.loads((config_path.parent/"installed.json").read_text())["identity"],
               "workflow_sha256": hashlib.sha256(workflow_bytes).hexdigest(),
               "model": model_name, "reasoning": reasoning, "protocol_verified": True,
               "approval_policy": workflow["codex"]["approval_policy"], "model_turns_started": 0,
               "permissions": permissions, "git_verified": bool(permissions),
               "skills_verified": bool(config.get("docker"))}
    return receipt


def verify(config_path):
    config = json.loads(Path(config_path).read_text())
    receipt = verify_profile(config_path)
    receipt["rework"] = verify_profile(config_path, rework=True)
    from worker import execution_fingerprint
    receipt["execution_sha256"] = execution_fingerprint(config)
    private_json(Path(config["state_directory"])/"preflight.json", receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.config)))
    except (RuntimeError, OSError, ValueError, KeyError, queue.Empty) as error:
        raise SystemExit("Codex protocol preflight failed: "+str(error)) from None
