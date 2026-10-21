"""Launch exactly one local Symphony instance from the installed configuration."""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from controller import verify_preflight


def worker_environment(config, install_root, source=None):
    source = os.environ if source is None else source
    keep = {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "CODEX_CA_CERTIFICATE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
            "OPENAI_API_KEY", "OPENAI_TEST_MODEL", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"}
    environment = {key: value for key, value in source.items() if key in keep}
    environment.setdefault("OPENAI_TEST_MODEL", "gpt-6-luna")
    environment["PATH"] = ":".join([*config["runtime_bin"], str(Path.home() / ".local/bin"),
                                     "/usr/local/bin", "/usr/bin", "/bin"])
    environment["SYMPHONY_INSTALL_ROOT"] = str(install_root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["ERL_CRASH_DUMP"] = str(Path(install_root) / "logs/erl_crash.dump")
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if config["enabled"]:
        verify_preflight(args.config)
    environment = worker_environment(config, args.config.parent)
    if config.get("jules", {}).get("enabled"):
        from jules import validate_runtime
        validate_runtime(config, environment)
    lock = open(Path(config["state_directory"]) / "service.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("A Symphony instance already owns this installation") from None
    os.set_inheritable(lock.fileno(), True)
    command = [config["symphony"], "--i-understand-that-this-will-be-running-without-the-usual-guardrails",
               "--logs-root", str(args.config.parent / "logs"), config["workflow"]]
    os.chdir(args.config.parent)
    if not config.get("jules", {}).get("enabled"):
        os.execvpe(command[0], command, environment)
    processes = [subprocess.Popen(command, env=environment, start_new_session=True),
                 subprocess.Popen([config["python"], str(Path(__file__).with_name("jules.py")),
                                   "--config", str(args.config)], env=environment, start_new_session=True)]
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)
    try:
        while not stopping and all(process.poll() is None for process in processes):
            time.sleep(0.25)
    finally:
        stop(None, None)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    raise SystemExit(next((p.returncode for p in processes if p.returncode), 0))


if __name__ == "__main__":
    main()
