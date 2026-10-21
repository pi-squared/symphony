"""Extract only this image's bundled skills, with no model invocation."""
import json
import os
from pathlib import Path
import shutil
import subprocess

root = Path('/tmp/codex-seed')
root.mkdir()
process = subprocess.Popen(['/opt/codex/bin/codex', 'app-server'],
    env={**os.environ, 'CODEX_HOME': str(root)}, stdin=subprocess.PIPE,
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
def request(identifier, method, params):
    process.stdin.write(json.dumps({'id': identifier, 'method': method, 'params': params})+'\n')
    process.stdin.flush()
    for line in process.stdout:
        message = json.loads(line)
        if message.get('id') == identifier:
            if 'error' in message:
                raise RuntimeError(method + ' failed')
            return message['result']
    raise RuntimeError('Codex exited during '+method)
try:
    request(1, 'initialize', {'clientInfo': {'name': 'symphony_image', 'version': '1'},
                              'capabilities': {'experimentalApi': True}})
    process.stdin.write('{"method":"initialized","params":{}}\n');process.stdin.flush()
    inventory = request(2, 'skills/list', {'cwds': ['/tmp'], 'forceReload': True})
    assert inventory['data'] and all(not entry['errors'] and all(skill['scope'] == 'system' for skill in entry['skills'])
                                     for entry in inventory['data'])
finally:
    process.terminate();process.wait(timeout=10)
shutil.copytree(root/'skills', '/opt/codex/packaged-skills')
shutil.rmtree(root)
