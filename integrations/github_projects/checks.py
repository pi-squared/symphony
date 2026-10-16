"""Reusable pre-push/CI checks over the entire prospective PR."""

import argparse
from pathlib import Path
import subprocess
from fnmatch import fnmatchcase

from controller import support, browser, Error


def verify_changes(workspace, base="origin/main", protected_paths=()):
    workspace = Path(workspace).resolve()
    merge_base = browser.git(workspace, "merge-base", base, "HEAD")
    names = subprocess.check_output(
        ["git", "diff", "--name-only", "--no-renames", "-z", merge_base], cwd=workspace
    ).decode().split("\0")
    names += support.check_publication_paths(workspace)
    for name in set(filter(None, names)):
        path = workspace / name
        if any(fnmatchcase(name, pattern) for pattern in protected_paths):
            raise Error("Frozen inputs cannot be changed: " + name)
        if path.name == ".env" or (path.name.startswith(".env.") and path.name not in {".env.example", ".env.sample", ".env.template"}):
            raise Error("Credential configuration cannot be published: " + name)
        if path.is_symlink():
            raise Error("Review publication symlinks before pushing: " + name)
        if path.is_file():
            content = path.read_bytes()
            if len(content) >= 100 * 1024 * 1024:
                raise Error("File exceeds ordinary Git size limit: " + name)
            if support.SECRET_PATTERN.search(content):
                raise Error("Potential credential in PR changes: " + name)
    browser.reject_video_changes(workspace, base)
    subprocess.run(["git", "diff", "--check", merge_base], cwd=workspace, check=True)
    return {"files_checked": len(set(filter(None, names)))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args()
    print(verify_changes(args.workspace, args.base))
