#!/usr/bin/env python3
"""Local Linux Docker helper. No third-party Python dependencies."""
import argparse
import datetime
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess
import sys
import tarfile
import urllib.parse


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def docker(*args, data=None):
    proc = subprocess.run(['docker', *args], input=data, capture_output=True)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode(errors='replace').strip())
    return proc.stdout


def inspect(name, image=False):
    return json.loads(docker(*(['image', 'inspect'] if image else ['inspect']), name))[0]


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    path.chmod(0o600)


def read_file(container, path):
    archive = docker('cp', '-L', container + ':' + path, '-')
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        files = [item for item in tar.getmembers() if item.isfile()]
        require(len(files) == 1, 'Expected one file: ' + path)
        return tar.extractfile(files[0]).read()


def put_root_file(container, path, data):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        entry = tarfile.TarInfo(PurePosixPath(path).name)
        entry.uid = entry.gid = 0
        entry.uname = entry.gname = 'root'
        entry.mode = 0o600
        entry.size = len(data)
        tar.addfile(entry, io.BytesIO(data))
    docker('cp', '-a', '-', container + ':' + str(PurePosixPath(path).parent), data=buf.getvalue())


def endpoint():
    if sys.platform.startswith('linux'):
        if os.environ.get('DOCKER_CONTEXT'):
            ctx = json.loads(docker('context', 'inspect', os.environ['DOCKER_CONTEXT']))[0]
            host = ctx['Endpoints']['docker']['Host']
        elif os.environ.get('DOCKER_HOST'):
            host = os.environ['DOCKER_HOST']
        else:
            ctx = json.loads(docker('context', 'inspect'))[0]
            host = ctx['Endpoints']['docker']['Host']
        require(host.startswith('unix://'), 'Only a local unix:// Docker endpoint is supported.')
        return host[len('unix://'):]
    # Windows: Docker Desktop's named pipe can't be used by the raw HTTP calls below.
    # Enable "Expose daemon on tcp://localhost:2375 without TLS" in Docker Desktop
    # Settings → General, then connect over TCP here.
    require(sys.platform == 'win32', 'Unsupported platform: ' + sys.platform)
    host = os.environ.get('DOCKER_HOST', 'tcp://localhost:2375')
    require(host.startswith('tcp://'), 'On Windows set DOCKER_HOST to a tcp:// endpoint.')
    return host


class DockerHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=120)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def _connection(target):
    if target.startswith('tcp://'):
        url = urllib.parse.urlsplit(target)
        return http.client.HTTPConnection(url.hostname, url.port or 2375, timeout=120)
    return DockerHTTP(target)


def api(target, method, path, body=None):
    conn = _connection(target)
    try:
        conn.request(method, path, body=None if body is None else json.dumps(body),
                     headers={'Content-Type': 'application/json'})
        response = conn.getresponse()
        data = response.read()
        value = json.loads(data) if data else {}
        require(200 <= response.status < 300, 'Docker API: ' + str(value))
        return value
    finally:
        conn.close()


def preflight(target, image):
    sock = endpoint()
    require(re.fullmatch(r'[A-Za-z0-9]+-(k1|t1|t2)', target) is not None,
            'Use the exact Studio container name, e.g. abc12-k1.')
    old = inspect(target)
    source = inspect(image, image=True)
    base = inspect(old['Image'], image=True)
    require((source['Os'], source['Architecture']) == (base['Os'], base['Architecture']),
            'Image platform differs from the target image.')
    require(old['HostConfig']['NetworkMode'] == 'bridge', 'Only the tested bridge network layout is supported.')
    require(all(m['Type'] == 'bind' for m in old['Mounts']), 'Unexpected non-bind volume; review manually.')
    wanted = old['Config'].get('Labels') or {}
    actual = source['Config'].get('Labels') or {}
    for key in ['simulator.protocol', 'simulator.library.layout', 'simulator.owner-lease',
                'com.booster-robotics.image.version', 'com.booster-robotics.image.ros-distro']:
        require(wanted.get(key) == actual.get(key), 'Image compatibility label differs: ' + key)
    for mount in old['Mounts']:
        require(Path(mount['Source']).exists(), 'Missing host mount: ' + mount['Source'])
    env = dict(v.split('=', 1) for v in old['Config']['Env'])
    require(bool(env.get('SSH_KEY')), 'Target SSH_KEY is missing.')
    identity = {path: read_file(target, path) for path in
                ['/root/.env', '/opt/booster/robot_info.txt', '/root/.ssh/authorized_keys']}
    require(identity['/root/.ssh/authorized_keys'].strip() == env['SSH_KEY'].strip().encode(),
            'Target authorized_keys differs from its SSH_KEY; repair target first.')
    print('Target:', target, 'state:', old['State']['Status'])
    print('Image:', image, source['Id'], source['Os'] + '/' + source['Architecture'])
    print('Checks passed: platform, compatibility labels, mounts, target identity.')
    return sock, old, source, identity


def replace(args):
    sock, old, source, identity = preflight(args.target, args.image)
    require(not old['State']['Running'], 'Deactivate this robot in Studio before replacing it.')
    root = Path(args.backup_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    suffix = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    directory = root / (args.target + '-' + suffix)
    directory.mkdir(mode=0o700)
    backup = args.target + '-before-replace-' + suffix
    save_json(directory / 'original.inspect.json', old)
    for path, data in identity.items():
        out = directory / PurePosixPath(path).name
        out.write_bytes(data)
        out.chmod(0o600)
    state = dict(target=args.target, backup=backup, original_id=old['Id'],
                 image_id=source['Id'], replacement_id=None, status='prepared')
    state_path = directory / 'state.json'
    save_json(state_path, state)
    body = dict(old['Config'])
    body['Image'] = source['Id']
    body['HostConfig'] = old['HostConfig']
    version = api(sock, 'GET', '/version')['ApiVersion']
    fresh = inspect(args.target)
    require(fresh['Id'] == old['Id'] and not fresh['State']['Running'], 'Target changed; aborting.')
    docker('rename', args.target, backup)
    try:
        result = api(sock, 'POST', '/v' + version + '/containers/create?name=' + urllib.parse.quote(args.target), body)
        state['replacement_id'] = result['Id']
        state['status'] = 'created'
        save_json(state_path, state)
        for path, data in identity.items():
            put_root_file(args.target, path, data)
        put_root_file(args.target, '/root/robocup_clone_verification.json', json.dumps(state).encode())
        new = inspect(args.target)
        require(new['Image'] == source['Id'], 'Unexpected replacement image.')
        for key in ['Env', 'Cmd', 'Entrypoint', 'Labels']:
            require(new['Config'][key] == old['Config'][key], 'Config mismatch: ' + key)
        for key in ['Binds', 'PortBindings', 'Privileged', 'Init', 'NetworkMode']:
            require(new['HostConfig'][key] == old['HostConfig'][key], 'HostConfig mismatch: ' + key)
        archive = docker('cp', args.target + ':/root/.ssh/authorized_keys', '-')
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            item = next(m for m in tar if m.isfile())
            require((item.uid, item.gid, item.mode) == (0, 0, 0o600), 'SSH file ownership mismatch.')
        state['status'] = 'ready_for_studio_activation'
        save_json(state_path, state)
    except Exception:
        # Keep any failed replacement for diagnosis; never delete user data.
        candidate = subprocess.run(['docker', 'inspect', args.target], capture_output=True)
        if candidate.returncode == 0:
            current = json.loads(candidate.stdout)[0]
            if not current['State']['Running']:
                docker('rename', args.target, args.target + '-failed-' + suffix)
                docker('rename', backup, args.target)
                state['status'] = 'failed_original_restored'
            else:
                state['status'] = 'failed_requires_manual_recovery'
        else:
            docker('rename', backup, args.target)
            state['status'] = 'failed_original_restored'
        save_json(state_path, state)
        raise
    print('Replacement created and left stopped:', state['replacement_id'])
    print('Original retained as:', backup)
    print('State file:', state_path)
    print('Now activate the SAME robot in Booster Studio.')


def rollback(args):
    endpoint()
    path = Path(args.state).expanduser().resolve()
    state = json.loads(path.read_text())
    require(state['status'] == 'ready_for_studio_activation', 'State is not ready for rollback.')
    new = inspect(state['target']); old = inspect(state['backup'])
    require(new['Id'] == state['replacement_id'] and old['Id'] == state['original_id'], 'Container identity changed.')
    require(not new['State']['Running'] and not old['State']['Running'], 'Deactivate the robot in Studio first.')
    retired = state['target'] + '-retired-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    docker('rename', state['target'], retired)
    try:
        docker('rename', state['backup'], state['target'])
    except Exception:
        docker('rename', retired, state['target'])
        raise
    state.update(status='rolled_back', retired=retired)
    save_json(path, state)
    print('Original restored. Activate the same robot in Studio. Clone retained as:', retired)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ['check', 'replace']:
        p = sub.add_parser(name)
        p.add_argument('--target', required=True)
        p.add_argument('--image', required=True)
        if name == 'replace':
            p.add_argument('--backup-dir', default='./migration-backups')
    p = sub.add_parser('rollback')
    p.add_argument('--state', required=True)
    args = parser.parse_args()
    try:
        if args.action == 'check': preflight(args.target, args.image)
        elif args.action == 'replace': replace(args)
        else: rollback(args)
    except Exception as exc:
        print('ERROR:', exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
