"""Launch the configured issue worker with durable per-cycle model settings."""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys


def execution_settings(config, *, rework=False):
    return {"model": config.get("rework_model", "gpt-6.1-sol") if rework else config.get("model", "gpt-6.1-sol"),
            "reasoning": config.get("rework_reasoning", "xhigh") if rework else config.get("reasoning", "medium")}


def bound_record(config, workspace):
    # Import lazily: this module is also used by the installer and tests.
    from controller import Ledger, Error
    path = Path(workspace).resolve()
    root = Path(config["workspace_root"]).resolve()
    if path.parent != root:
        raise Error("Worker workspace is outside the configured issue root")
    ledger = Ledger(config["state_directory"])
    try:
        return next((r for r in ledger.all() if r.get("workspace") == str(path)), None)
    finally:
        ledger.db.close()


def codex_command(config, record=None, *, rework=False):
    settings = (record or {}).get("execution") or execution_settings(config, rework=rework)
    command = list(config["codex_argv"])
    for index, value in enumerate(command):
        if value.startswith("model="):
            command[index] = "model=" + json.dumps(settings["model"])
        elif value.startswith("model_reasoning_effort="):
            command[index] = "model_reasoning_effort=" + json.dumps(settings["reasoning"])
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--rework-preflight", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    record = bound_record(config, Path.cwd())
    if record and args.rework_preflight:
        raise SystemExit("Rework preflight requires an unclaimed disposable workspace")
    command = codex_command(config, record, rework=args.rework_preflight)
    if config.get("docker"):
        from docker_worker import run
        raise SystemExit(run(config, record, command, Path.cwd()))
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
