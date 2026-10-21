"""Local issue containers; the trusted host alone owns Docker lifecycle."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import re
import io
import tarfile
import tomllib

import sandbox


class DockerError(RuntimeError):
    pass


def binary(config):
    return config.get("docker", {}).get("binary", "docker")


def name_for(config, workspace, cycle=0, purpose="worker"):
    identity = hashlib.sha256((str(Path(config["state_directory"]).resolve()) + str(Path(workspace).resolve())).encode()).hexdigest()[:16]
    return f"symphony-{purpose}-{identity}-{cycle}"


def inspect(config, name):
    result = subprocess.run([binary(config), "inspect", name], capture_output=True, text=True)
    if result.returncode:
        if "No such" in result.stderr:
            return None
        raise DockerError("Cannot reconcile the named Docker container")
    return json.loads(result.stdout)[0]


def stop(config, name, workspace):
    row = inspect(config, name)
    if row is None:
        return
    expected = str(Path(workspace).resolve())
    labels = row["Config"].get("Labels") or {}
    if labels.get("symphony.workspace") != expected or labels.get("symphony.installation") != str(Path(config["state_directory"]).resolve()):
        raise DockerError("Refusing to stop a container without matching ownership")
    subprocess.run([binary(config), "rm", "--force", name], check=True, stdout=subprocess.DEVNULL)


def stop_owned(config, workspace):
    filters = ['--filter', 'label=symphony.installation=' + str(Path(config['state_directory']).resolve()),
               '--filter', 'label=symphony.workspace=' + str(Path(workspace).resolve())]
    result = subprocess.run([binary(config), 'ps', '--all', *filters, '--format', '{{.Names}}'],
                            capture_output=True, text=True)
    if result.returncode:
        raise DockerError('Cannot reconcile owned Docker containers')
    for name in result.stdout.splitlines():
        stop(config, name, workspace)


def repository_skills(workspace):
    """Disable discovery while preserving tracked bytes and Git semantics."""
    paths = []
    for directory, names, files in os.walk(workspace, followlinks=False):
        for name in ('.codex', '.agents'):
            if (Path(directory)/name).is_symlink():
                raise DockerError('Repository configuration parents must not be symlinks')
        names[:] = [name for name in names if name not in {'.git', 'node_modules', '.venv', 'venv'}]
        parent = Path(directory)
        if parent.name == 'skills' and parent.parent.name in {'.codex', '.agents'}:
            if parent.is_symlink():
                raise DockerError('Repository skill catalogs must not be symlinks')
            paths.extend(path for path in parent.rglob('SKILL.md'))
    return sorted(set(paths))


def discovery_arguments(workspace):
    folders = repository_skills(Path(workspace).resolve())
    entries = ','.join('{path=' + json.dumps(str(path)) + ',enabled=false}' for path in folders)
    return ['--config', 'skills.config=[' + entries + ']', '--config',
            'projects.' + json.dumps(str(Path(workspace).resolve())) + '.trust_level="untrusted"']


def command(config, workspace, argv, *, home=None, name=None, validation=False, packaged=None):
    docker = config["docker"]
    image = docker["image"]
    if not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", image):
        raise DockerError("Pin the worker image by immutable image ID or digest")
    workspace = Path(workspace).resolve()
    if ',' in str(workspace):
        raise DockerError("Docker mount paths must not contain commas")
    name = name or name_for(config, workspace, purpose="check")
    args = [binary(config), "run", "--rm", "--init", "--name", name,
            "--label", "symphony.installation=" + str(Path(config["state_directory"]).resolve()),
            "--label", "symphony.workspace=" + str(workspace),
            "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", str(docker.get("pids_limit", 512)),
            "--memory", docker.get("memory", "4g"), "--cpus", str(docker.get("cpus", 2)),
            "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777",
            "--mount", f"type=bind,src={workspace},dst={workspace}",
            "--workdir", str(workspace)]
    if validation:
        args += ["--network", "none", "--env", "HOME=/tmp", "--env", "CI=true",
                 "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "GIT_CONFIG_GLOBAL=/dev/null"]
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy'):
            args += ['--env', key+'=']
        if (workspace / '.git').exists():
            args += ['--mount', f'type=bind,src={workspace / ".git"},dst={workspace / ".git"},readonly']
    else:
        if home is None:
            raise DockerError("A dedicated worker auth home is required")
        args += ["--interactive", "--mount", f"type=bind,src={home},dst=/home/symphony/.codex",
                 "--env", "HOME=/home/symphony", "--env", "CODEX_HOME=/home/symphony/.codex"]
        if packaged:
            args += ['--mount', f'type=bind,src={packaged},dst=/home/symphony/.codex/skills,readonly']
        gh_auth = docker.get('git_auth_file')
        if gh_auth:
            path = Path(gh_auth).resolve()
            if not path.is_file() or path.stat().st_mode & 0o077:
                raise DockerError('Use a private repository-scoped GitHub credential file')
            args += ['--mount', f'type=bind,src={path},dst=/home/symphony/.config/gh/hosts.yml,readonly',
                     '--env', 'GIT_CONFIG_COUNT=2', '--env', 'GIT_CONFIG_KEY_0=credential.helper',
                     '--env', 'GIT_CONFIG_VALUE_0=', '--env', 'GIT_CONFIG_KEY_1=credential.helper',
                     '--env', 'GIT_CONFIG_VALUE_1=!gh auth git-credential']
        # Preserve configured proxy and CA trust without copying host settings.
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy'):
            if key in os.environ:
                args += ['--env', key]
        if ca := docker.get('ca_bundle'):
            args += ['--mount', f'type=bind,src={Path(ca).resolve()},dst=/run/symphony-ca.pem,readonly']
            for key in ('SSL_CERT_FILE', 'CODEX_CA_CERTIFICATE', 'NODE_EXTRA_CA_CERTS'):
                args += ['--env', key + '=/run/symphony-ca.pem']
    return [*args, image, *map(str, argv)]


def auth_home(config, workspace):
    root = Path(config['state_directory']) / 'worker-homes' / hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Only the auth cache is seeded; host profiles, memories and skills stay out.
    auth = Path(config['docker']['auth_file']).expanduser().resolve()
    target = root / 'auth.json'
    if not target.exists():
        if not auth.is_file():
            raise DockerError('Configure an existing dedicated Codex auth cache')
        import shutil
        shutil.copyfile(auth, target)
        target.chmod(0o600)
    for path in root.iterdir():
        if path.name == 'skills':
            if path.is_symlink() or any(p.name != '.system' for p in path.iterdir()):
                raise DockerError('Downloaded or installed worker skills require removal before restart')
        elif path.name == 'config.toml':
            # thread/start records project trust even when CLI overrides keep
            # that project untrusted. Discard only this exact bookkeeping;
            # arbitrary worker configuration must never survive a restart.
            if path.is_symlink() or not path.is_file():
                raise DockerError('Worker auth home contains custom configuration')
            try:
                saved = tomllib.loads(path.read_text())
            except (OSError, ValueError):
                raise DockerError('Worker auth home contains custom configuration') from None
            expected = {'projects': {str(Path(workspace).resolve()): {'trust_level': 'trusted'}}}
            if saved != expected:
                raise DockerError('Worker auth home contains custom configuration')
            path.unlink()
        elif path.name == 'AGENTS.md':
            raise DockerError('Worker auth home contains custom configuration')
    return root


def packaged_skills(config, workspace):
    from controller import controller_lock
    image = config['docker']['image']
    key = hashlib.sha256(image.encode()).hexdigest()
    directory = Path(config['state_directory'])/'packaged-skills'/key
    with controller_lock(Path(config['state_directory'])/'packaged-skill-lock'):
        if not directory.exists():
            # Extract from the pinned, trusted worker image, never a host user
            # catalog or a writable worker home. No authentication or networking.
            name = name_for(config, workspace, purpose='seed')
            stop(config, name, workspace)
            args = command(config, workspace, ['tar', '-C', '/opt/codex/packaged-skills', '-cf', '-', '.'],
                           name=name, validation=True)
            data = sandbox.execute(args, timeout=60)
            directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(dir=directory.parent) as temporary:
                with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                    if any(member.issym() or member.islnk() for member in archive.getmembers()):
                        raise DockerError('Packaged skill archive contains symlinks')
                    archive.extractall(temporary, filter='data')
                Path(temporary).rename(directory)
        return directory


def run(config, record, argv, workspace):
    workspace = Path(workspace).resolve()
    home = auth_home(config, workspace)
    lock_path = home / 'container.lock'
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DockerError('An issue container launcher is already active') from None
        name = name_for(config, workspace, (record or {}).get('cycle', 0))
        # A prior container outliving its launcher must be stopped before reuse.
        stop_owned(config, workspace)
        argv = [config['docker'].get('codex', '/opt/codex/bin/codex'), *argv[1:]]
        argv[-1:-1] = discovery_arguments(workspace)
        packaged = packaged_skills(config, workspace)
        args = command(config, workspace, argv, home=home, name=name, packaged=packaged)
        process = subprocess.Popen(args)
        def terminate(signum, frame):
            stop(config, name, workspace)
            if process.poll() is None:
                process.terminate()
        previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        try:
            return process.wait()
        finally:
            stop(config, name, workspace)
            for sig, handler in previous.items():
                signal.signal(sig, handler)



class DockerSandbox:
    def __init__(self, config):
        self.config = config

    def check(self, workspace, argv, *, timeout=600):
        name = name_for(self.config, workspace, purpose='check')
        stop(self.config, name, workspace)
        # Use image executables, not the host's copied Python runtime paths.
        argv = ['python3' if str(part) == self.config.get('python') else part for part in argv]
        try:
            return sandbox.execute(command(self.config, workspace, argv, name=name, validation=True), timeout=timeout)
        finally:
            stop(self.config, name, workspace)
