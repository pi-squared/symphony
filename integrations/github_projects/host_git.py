"""Host Git commands over worker-controlled checkouts use trusted execution policy."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess


POLICY = ['-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
          '-c', 'core.attributesFile=/dev/null', '-c', 'core.pager=cat',
          '-c', 'credential.helper=', '-c', 'protocol.allow=never',
          '-c', 'protocol.https.allow=always', '-c', 'protocol.file.allow=never',
          '-c', 'protocol.ext.allow=never', '-c', 'protocol.ssh.allow=never',
          '-c', 'fetch.recurseSubmodules=false', '-c', 'submodule.recurse=false',
          '-c', 'http.sslVerify=true', '-c', 'diff.external=']


def environment(source=None):
    source = os.environ if source is None else source
    env = {key: value for key, value in source.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM='/dev/null', GIT_CONFIG_GLOBAL='/dev/null',
               GIT_TERMINAL_PROMPT='0', GIT_PAGER='cat')
    return env


def metadata(workspace):
    workspace = Path(workspace).resolve()
    path = workspace/'.git'
    if path.is_symlink() or path.exists() and not path.is_dir():
        raise ValueError('Host Git requires an ordinary in-checkout .git directory')
    if path.exists():
        if path.resolve() != path:
            raise ValueError('Git metadata escapes the bound checkout')
        for directory, dirs, files in os.walk(path, followlinks=False):
            if any((Path(directory)/name).is_symlink() for name in [*dirs, *files]):
                raise ValueError('Host Git rejects symlinks in worker-controlled metadata')
        if (path/'commondir').exists() or (path/'objects/info/alternates').exists():
            raise ValueError('Host Git requires self-contained object storage')
    return path


def prepare(argv, *, cwd=None, env=None, gh=None):
    env = environment(env)
    if cwd:
        path = metadata(cwd)
        if (path/'config').exists():
            # Reading local config with includes disabled does not execute its
            # hooks/filters. Reject config that can rewrite destinations or
            # introduce executable conversion/network behavior before any read.
            result = subprocess.run(['/usr/bin/git', *POLICY, 'config', '--local', '--no-includes', '--null', '--list'],
                                    cwd=cwd, env=env, capture_output=True, check=True)
            keys = [row.split(b'\n', 1)[0].decode().lower() for row in result.stdout.split(b'\0') if row]
            forbidden = ('include.', 'includeif.', 'url.', 'filter.', 'http.', 'https.', 'diff.', 'merge.')
            if any(key.startswith(forbidden) for key in keys):
                raise ValueError('Worker Git config contains untrusted includes, rewrites, filters or network overrides')
    policy = list(POLICY)
    if cwd:
        policy += ['-c', 'core.worktree=' + str(Path(cwd).resolve()), '-c', 'core.bare=false']
    if gh:
        policy += ['-c', 'credential.helper=!' + shlex.quote(str(Path(shutil.which(gh) or gh).resolve())) + ' auth git-credential']
    return ['/usr/bin/git', *policy, *map(str, argv)], env


def output(workspace, *argv):
    command, env = prepare(argv, cwd=workspace)
    return subprocess.check_output(command, cwd=workspace, env=env)
