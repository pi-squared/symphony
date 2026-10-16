"""Launch exactly one local Symphony instance from the installed configuration."""

import argparse
import fcntl
import json
import os
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
    lock = open(Path(config["state_directory"]) / "service.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("A Symphony instance already owns this installation") from None
    os.set_inheritable(lock.fileno(), True)
    environment = worker_environment(config, args.config.parent)
    command = [config["symphony"], "--i-understand-that-this-will-be-running-without-the-usual-guardrails",
               "--logs-root", str(args.config.parent / "logs"), config["workflow"]]
    os.chdir(args.config.parent)
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    main()
