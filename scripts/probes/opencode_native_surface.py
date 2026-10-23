"""OC-00: inspect a preinstalled OpenCode image offline, without a model or host mounts.

Run with an explicit --image-id sha256:... . Only this probe's temporary container is
removed. JSON output contains synthetic metadata and the native route inventory.
This is a transport probe, not the production bridge or an agent launcher.
"""

import argparse
import json
import re
import subprocess
import uuid
from pathlib import Path

CONTAINER_PROBE = r'''
import base64
import hashlib
import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location('worker', '/opt/agent-harness/worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
password = secrets.token_urlsafe(24)
environment = dict(os.environ)
environment.update({
    'HOME': '/tmp/home', 'XDG_DATA_HOME': '/tmp/data',
    'XDG_CACHE_HOME': '/tmp/cache', 'XDG_STATE_HOME': '/tmp/state',
    'XDG_CONFIG_HOME': '/opt/agent-harness/config',
    'OPENCODE_DISABLE_AUTOUPDATE': '1', 'OPENCODE_DISABLE_MODELS_FETCH': '1',
    'OPENCODE_DISABLE_PROJECT_CONFIG': '1', 'OPENCODE_DISABLE_AUTOCOMPACT': '1',
    'OPENCODE_DISABLE_PRUNE': '1', 'OPENCODE_DISABLE_FFF': '1',
    'OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER': '1',
    'OPENCODE_SERVER_PASSWORD': password,
    'OPENCODE_CONFIG_CONTENT': json.dumps(worker.configuration(
        'http://127.0.0.1:19091/v1', 256)),
})
Path('/tmp/home').mkdir()
token = base64.b64encode(('opencode:' + password).encode()).decode()
base = 'http://127.0.0.1:19090'
model_requests = []

class ModelTrap(BaseHTTPRequestHandler):
    def do_POST(self):
        model_requests.append(self.path)
        self.send_response(503)
        self.end_headers()
    do_GET = do_POST
    def log_message(self, *_):
        pass

trap = ThreadingHTTPServer(('127.0.0.1', 19091), ModelTrap)
threading.Thread(target=trap.serve_forever, daemon=True).start()

def request(path, *, authorized=True, method='GET', body=None, origin=None):
    headers = {'Authorization': 'Basic ' + token} if authorized else {}
    if body is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(body).encode()
    if origin:
        headers['Origin'] = origin
    req = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, headers, response.read(4_000_000)
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read(1024)

log = open('/tmp/server.log', 'wb')
child = subprocess.Popen(['/usr/local/bin/opencode', 'serve', '--hostname',
                          '127.0.0.1', '--port', '19090'], env=environment,
                         stdout=log, stderr=log, cwd='/work')
try:
    for _ in range(100):
        try:
            health = request('/global/health')
            if health[0] == 200:
                break
        except (OSError, TimeoutError):
            pass
        if child.poll() is not None:
            raise RuntimeError('NATIVE_SERVER_EXITED')
        time.sleep(0.3)
    else:
        raise RuntimeError('NATIVE_SERVER_TIMEOUT')
    output = {'health': json.loads(health[2]), 'routes': {}, 'checks': {}}
    assert output['health']['version'] == '1.19.3', 'UNEXPECTED_NATIVE_VERSION'
    status, _, body = request('/doc')
    assert status == 200, 'OPENAPI_UNAVAILABLE'
    document = json.loads(body)
    output['openapi_sha256'] = hashlib.sha256(body).hexdigest()
    output['openapi_version'] = document.get('openapi')
    for path, operations in document['paths'].items():
        output['routes'][path] = {
            method: operation.get('operationId') for method, operation in operations.items()
            if method in ('get', 'post', 'patch', 'put', 'delete', 'options')
        }
    for path in ['/global/health', '/doc', '/session', '/provider', '/config', '/path']:
        output['checks'][path] = {
            'authorized': request(path)[0], 'anonymous': request(path, authorized=False)[0]
        }
    status, headers, html = request('/')
    output['web'] = {'status': status, 'content_type': headers.get('content-type'),
                     'html_bytes': len(html),
                     'anonymous_status': request('/', authorized=False)[0],
                     'csp': headers.get('content-security-policy')}
    assert status == 200 and 'text/html' in output['web']['content_type'], 'WEB_UNAVAILABLE'
    assets = re.findall(r'(?:src|href)="([^"#]+)"', html.decode(errors='replace'))
    output['web']['assets'] = []
    for asset in assets:
        if not asset.startswith('/'):
            continue
        code, asset_headers, data = request(asset)
        output['web']['assets'].append({
            'path': asset, 'status': code, 'bytes': len(data),
            'type': asset_headers.get('content-type'),
            'sha256': hashlib.sha256(data).hexdigest(),
        })
        assert code == 200, 'LOCAL_ASSET_UNAVAILABLE'
    assert any(a['type'] == 'text/javascript' for a in output['web']['assets'])
    assert any(a['type'] == 'text/css' for a in output['web']['assets'])
    code, _, body = request('/session', method='POST', body={'title': 'OC-00 fictional session'})
    output['session_create_status'] = code
    if code == 200:
        session_id = json.loads(body)['id']
        output['session_read_status'] = request('/session/' + session_id)[0]
        output['session_delete_status'] = request('/session/' + session_id, method='DELETE')[0]
    assert all(output[key] == 200 for key in (
        'session_create_status', 'session_read_status', 'session_delete_status'))
    assert all(check['anonymous'] == 401 for check in output['checks'].values())
    assert output['web']['anonymous_status'] == 401
    for path in ['/event', '/global/event']:
        req = urllib.request.Request(base + path, headers={'Authorization': 'Basic ' + token})
        with urllib.request.urlopen(req, timeout=8) as response:
            first_line = response.readline(1024).decode().strip()
            event = json.loads(first_line.removeprefix('data: '))
            output['checks'][path] = {'status': response.status,
                'type': response.headers.get('content-type'),
                'first_type': event.get('payload', event)['type']}
    code, headers, _ = request('/session', origin='https://untrusted.invalid')
    output['cross_origin'] = {'status': code,
        'allow_origin': headers.get('access-control-allow-origin')}
    output['ephemeral_files'] = sorted([
        {'path': str(p.relative_to('/tmp')), 'bytes': p.stat().st_size}
        for p in Path('/tmp').rglob('*') if p.is_file() and p.name != 'server.log'
    ], key=lambda row: row['path'])
    output['model_requests'] = len(model_requests)
    assert not model_requests, 'UNEXPECTED_MODEL_REQUEST'
    print(json.dumps(output))
finally:
    child.terminate()
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
    log.close()
    trap.shutdown()
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', args.image_id):
        parser.error('an immutable, locally installed image ID is required')
    name = 'oc00-probe-' + uuid.uuid4().hex[:12]
    command = [
        'docker', 'run', '--rm', '--pull=never', '--name', name, '-i',
        '--network=none', '--read-only', '--user', '65534:65534',
        '--cap-drop=ALL', '--security-opt=no-new-privileges',
        '--pids-limit=256', '--memory=1g', '--cpus=2',
        '--tmpfs', '/tmp:rw,nosuid,nodev,size=134217728,mode=1777',
        '--tmpfs', '/work:rw,nosuid,nodev,size=16777216,mode=1777',
        '--workdir=/work', '--entrypoint=/usr/local/bin/python',
        args.image_id, '-I', '-',
    ]
    try:
        result = subprocess.run(
            command, input=CONTAINER_PROBE, text=True, capture_output=True, timeout=150
        )
        if result.returncode:
            print(result.stderr[-2000:])
            return result.returncode
        output = json.loads(result.stdout)
        output['image_id'] = args.image_id
        output['isolation'] = {
            'network': 'none', 'mounts': [], 'published_ports': [],
            'read_only_root': True, 'non_root': True,
            'model_endpoint': 'unbound container loopback; no provider credentials',
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({key: output[key] for key in (
            'health', 'web', 'session_create_status', 'session_read_status',
            'session_delete_status', 'cross_origin'
        )}, indent=2))
        print('Native route count:', len(output['routes']))
        return 0
    finally:
        # Exact randomly named probe container only; never prune or touch other workloads.
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)


if __name__ == '__main__':
    raise SystemExit(main())
