"""Install the pinned official GitHub CLI without changing the user's login."""

import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
from urllib.request import urlopen

VERSION = "2.102.0"
ARCHIVE_SHA256 = "bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386"
URL = f"https://github.com/cli/cli/releases/download/v{VERSION}/gh_{VERSION}_linux_amd64.tar.gz"


def install(destination):
    directory = Path(destination) / "runtime" / f"gh-{VERSION}"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = directory / "official.tar.gz"
    if not archive.exists():
        with urlopen(URL, timeout=120) as response:
            data = response.read()
    else:
        data = archive.read_bytes()
    if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("GitHub CLI official archive hash mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
        member = bundle.getmember(f"gh_{VERSION}_linux_amd64/bin/gh")
        if not member.isfile():
            raise ValueError("The official GitHub CLI executable is missing")
        binary = bundle.extractfile(member).read()
    archive.write_bytes(data)
    executable = directory / "gh"
    if executable.exists() and executable.read_bytes() != binary:
        raise ValueError("The installed GitHub CLI differs from its pinned archive")
    executable.write_bytes(binary)
    executable.chmod(0o700)
    version = subprocess.check_output([str(executable), "--version"], text=True)
    if not version.startswith(f"gh version {VERSION} "):
        raise ValueError("Unexpected GitHub CLI version")
    return executable
