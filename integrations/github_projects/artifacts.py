"""Validate local visual evidence and stage immutable bytes for trusted upload."""

import hashlib
import os
from pathlib import Path
import re
import stat


SIGNATURES = {'.png': b'\x89PNG\r\n\x1a\n', '.jpg': b'\xff\xd8\xff',
              '.jpeg': b'\xff\xd8\xff', '.gif': b'GIF8', '.webm': b'\x1aE\xdf\xa3'}
MAX_FILE = 50 * 1024 * 1024
MAX_TOTAL = 200 * 1024 * 1024


def open_capture(workspace, relative):
    """Anchor every path component to open directories, rejecting symlink races."""
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in Path(relative).parts[:-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        fd = os.open(Path(relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        return os.fdopen(fd, 'rb')
    finally:
        os.close(directory)


def validate_descriptors(descriptors):
    if not isinstance(descriptors, list) or len(descriptors) > 20:
        raise ValueError('Provide at most 20 visual artifacts')
    seen = set()
    for row in descriptors:
        if (not isinstance(row, dict) or set(row) != {'path', 'sha256', 'caption'}
                or not all(isinstance(v, str) and v for v in row.values())
                or not re.fullmatch('[0-9a-f]{64}', row['sha256'])):
            raise ValueError('Each artifact requires path, sha256 and caption')
        path = Path(row['path'])
        if (path.is_absolute() or row['path'] != path.as_posix() or '..' in path.parts or '.git' in path.parts
                or path.suffix.lower() not in SIGNATURES or row['path'] in seen
                or len(row['caption']) > 500):
            raise ValueError('Artifact paths must be unique workspace-relative images or WebMs')
        seen.add(row['path'])


def stage(workspace, descriptors, directory, secret_pattern):
    validate_descriptors(descriptors)
    workspace = Path(workspace).resolve()
    validated, total = [], 0
    for row in descriptors:
        source = workspace / row['path']
        try:
            stream = open_capture(workspace, row['path'])
        except OSError as error:
            raise ValueError('Artifact path is missing or contains a symlink') from error
        with stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Artifacts must be regular image/video files')
            size = info.st_size
            if size > MAX_FILE or total + size > MAX_TOTAL:
                raise ValueError('Visual evidence exceeds the upload size limit')
            content = stream.read(MAX_FILE + 1)
        total += len(content)
        if (len(content) > MAX_FILE or total > MAX_TOTAL
                or not content.startswith(SIGNATURES[source.suffix.lower()])):
            raise ValueError('Artifact has an invalid image/video signature or size')
        if hashlib.sha256(content).hexdigest() != row['sha256']:
            raise ValueError('Artifact hash does not match the captured bytes')
        if secret_pattern.search(content):
            raise ValueError('Visual evidence contains a potential credential')
        validated.append((row, content, source.suffix.lower()))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged = []
    for row, content, suffix in validated:
        destination = directory / (row['sha256'] + suffix)
        if destination.exists():
            if destination.read_bytes() != content:
                raise ValueError('Existing staged evidence has changed')
        else:
            with destination.open('xb') as stream:
                os.chmod(destination, 0o600)
                stream.write(content)
        staged.append({**row, 'staged_path': str(destination), 'bytes': len(content)})
    return staged
