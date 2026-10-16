"""Credential-free, network-isolated independent repository checks."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess


class CheckError(RuntimeError):
    def __init__(self, message, stdout=b"", stderr=b""):
        super().__init__(message)
        self.stdout, self.stderr = stdout, stderr


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(value, output, indent=2)
        output.write("\n")
    os.replace(temporary, path)


def clean_environment():
    # An allowlist is deliberate: repository tests and agents never inherit cloud,
    # Actions, GitHub, SSH-agent, Docker, browser, or proxy authentication.
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "HOME": "/home/symphony", "TMPDIR": "/tmp", "CI": "true",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}


def execute(command, *, cwd=None, env=None, timeout=120, input=None):
    """Terminate the entire owned process group on timeout, including grandchildren."""
    process = subprocess.Popen([str(part) for part in command], cwd=cwd, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise CheckError("Owned subprocess exceeded its time limit") from None
    if process.returncode:
        # Subprocess output can contain credentials. Keep it in private evidence,
        # never embed it in exception messages or Actions console output.
        raise CheckError(f"{Path(str(command[0])).name} exited with status {process.returncode}", stdout, stderr)
    return stdout


class Sandbox:
    """A mount/PID namespace with no host home, /run, sockets, or credentials.

    Independent checks run with networking disabled and Git metadata read-only.
    """

    def __init__(self, runtime_roots=()):
        self.runtime_roots = [str(Path(root).absolute()) for root in runtime_roots]

    def command(self, workspace, command, *, home=None, network=False, writable=()):
        if not shutil.which("bwrap"):
            raise CheckError("bubblewrap is required; refusing unsandboxed execution")
        workspace = Path(workspace).resolve()
        args = ["bwrap", "--die-with-parent", "--new-session", "--unshare-user",
                "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--clearenv",
                "--tmpfs", "/tmp"]
        if not network:
            args += ["--unshare-net"]
        for name in ("/usr", "/bin", "/lib", "/lib64", *self.runtime_roots):
            if Path(name).exists():
                args += ["--ro-bind", name, name]
        for name in ("/etc/ssl", "/etc/hosts", "/etc/resolv.conf", "/etc/nsswitch.conf",
                     "/etc/passwd", "/etc/group", "/etc/ld.so.cache"):
            if Path(name).exists():
                args += ["--ro-bind", name, name]
        # A venv can link through a user bin alias before reaching its Python
        # runtime. Mount just those executable aliases, never the whole home.
        binary = Path(str(command[0]))
        visited = set()
        while binary.is_symlink() and str(binary) not in visited:
            visited.add(str(binary))
            target = Path(os.readlink(binary))
            binary = target if target.is_absolute() else binary.parent / target
            covered = [workspace, *self.runtime_roots, "/usr", "/bin", "/lib", "/lib64", *writable]
            if not any(binary.absolute().is_relative_to(Path(root).absolute()) for root in covered):
                args += ["--ro-bind", str(binary.resolve()), str(binary)]
        args += ["--proc", "/proc", "--dev", "/dev",
                 "--dir", "/home/symphony", "--bind", str(workspace), str(workspace),
                 "--chdir", str(workspace)]
        if (workspace / ".git").exists():
            args += ["--ro-bind", str(workspace / ".git"), str(workspace / ".git")]
        for name in writable:
            args += ["--bind", str(Path(name).resolve()), str(Path(name).resolve())]
        environment = clean_environment()
        executable_paths = [root if Path(root).name == "bin" else str(Path(root) / "bin")
                            for root in self.runtime_roots]
        environment["PATH"] = ":".join([*executable_paths, environment["PATH"]])
        if home:
            args += ["--bind", str(home), "/home/symphony/.codex"]
            environment["CODEX_HOME"] = "/home/symphony/.codex"
        for key, value in environment.items():
            args += ["--setenv", key, value]
        return [*args, "--", *map(str, command)]

    def check(self, workspace, command, *, timeout=600):
        return execute(self.command(workspace, command), timeout=timeout)
