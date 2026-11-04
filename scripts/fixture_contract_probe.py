#!/usr/bin/env python3
"""Check an offline project fixture against controlled failures before guarded dispatch."""
import argparse
import base64
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import zipfile
import zlib

from fixture_lock_probe import ProbeFailure, input_hashes, require
from runtime_probe import platform_support, probe_parent


MAX_BYTES = 2 * 1024 * 1024
MAX_TOTAL = 8 * MAX_BYTES
UNRESOLVED = {'intent', 'confirmed', 'ambiguous', 'cleanup_failed'}
CONTRACTS = ('partial', 'config', 'absence')


def settings(request):
    require(isinstance(request, dict) and set(request) <= {
        'command', 'inputs', 'directory', 'timeout_seconds', 'contracts'}, 'invalid_request_fields')
    command = request.get('command')
    require(isinstance(command, list) and command and all(
        isinstance(part, str) and part and '\0' not in part for part in command),
        'command_must_be_nonempty_argv')
    inputs = request.get('inputs')
    require(isinstance(inputs, list) and inputs and all(
        isinstance(name, str) and name for name in inputs), 'project_adapter_inputs_required')
    timeout = request.get('timeout_seconds', 10)
    require(type(timeout) in {int, float} and math.isfinite(timeout) and 1 <= timeout <= 30,
            'invalid_timeout_seconds')
    contracts = request.get('contracts', list(CONTRACTS))
    require(isinstance(contracts, list) and contracts and all(
        isinstance(value, str) and value in CONTRACTS for value in contracts)
        and len(set(contracts)) == len(contracts), 'invalid_contracts')
    return command, inputs, timeout, contracts


class Service:
    def __init__(self, secret):
        self.secret = secret
        self.phase = ''
        self.resources = set()
        self.config = {'value': 'baseline'}
        self.requests = []
        self.responses = []
        self.creates = 0
        service = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def log_message(self, *args):
                pass

            def parse_request(self):
                parsed = super().parse_request()
                if parsed:
                    self.request_index = len(service.requests)
                    service.requests.append((self.command, self.path, None))
                return parsed

            def send_response(self, code, message=None):
                service.responses.append((getattr(self, 'command', None),
                                          getattr(self, 'path', ''), code))
                super().send_response(code, message)

            def do_GET(self):
                self.respond('GET')

            def do_POST(self):
                self.respond('POST')

            def do_PUT(self):
                self.respond('PUT')

            def do_DELETE(self):
                self.respond('DELETE')

            def respond(self, method):
                self.connection.settimeout(2)
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    require(0 <= length <= 65536, 'service_request_too_large')
                    body = json.loads(self.rfile.read(length)) if length else None
                    service.requests[self.request_index] = (method, self.path, body)
                    status, value = service.dispatch(method, self.path, body)
                    data = b'' if status == 204 else json.dumps(value).encode()
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (OSError, ValueError):
                    self.close_connection = True

        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=.02), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    @property
    def base_url(self):
        return 'http://127.0.0.1:' + str(self.server.server_address[1])

    def begin(self, phase):
        self.phase = phase
        self.requests = []
        self.responses = []
        if phase == 'partial':
            self.resources = set()
            self.creates = 0
        elif phase in {'hidden', 'denied', 'paginated'}:
            self.resources = {'hidden'}

    def dispatch(self, method, target, body):
        path = urlsplit(target).path
        sensitive = {'secret': self.secret}
        if path == '/config':
            if method == 'GET':
                return 200, self.config.copy()
            if method == 'PUT' and isinstance(body, dict) and set(body) == {'value'}:
                if self.phase == 'restore' and body == {'value': 'baseline'}:
                    return 503, dict(error='restore_unavailable', **sensitive)
                self.config = body.copy()
                return 200, self.config.copy()
        if path == '/resources' and method == 'POST' and self.phase == 'partial':
            self.creates += 1
            if self.creates > 2:
                return 400, dict(error='prepare_rejected', **sensitive)
            identifier = ('first', 'second')[self.creates - 1]
            self.resources.add(identifier)
            return 201, dict(id=identifier, **sensitive)
        if path == '/resources' and method == 'GET':
            if self.phase == 'paginated':
                if '?' in target:
                    return 503, dict(error='page_unavailable', **sensitive)
                return 200, {'items': [], 'next': '2', 'total': 1, **sensitive}
            return 200, {'items': [], **sensitive}
        if path.startswith('/resources/'):
            identifier = path.removeprefix('/resources/')
            if method == 'DELETE' and self.phase == 'partial':
                if identifier == 'first':
                    return 503, dict(error='cleanup_unavailable', **sensitive)
                self.resources.discard(identifier)
                return 204, {}
            if method == 'GET':
                if self.phase == 'denied':
                    return 403, dict(error='forbidden', **sensitive)
                if identifier in self.resources:
                    return 200, dict(id=identifier, **sensitive)
                return 404, dict(error='not_found', **sensitive)
        return 400, dict(error='unsupported_probe_request', **sensitive)

    def called(self, method, path):
        return any(entry[:2] == (method, path) for entry in self.requests)


def artifact_path(directory, name):
    require(isinstance(name, str), 'invalid_artifact_path')
    current = directory
    for part in platform_support.relative_parts(name):
        current = current / part
        require(not current.is_symlink() and not (
            current.exists() and getattr(current.lstat(), 'st_file_attributes', 0) & 0x400),
            'artifact_symlink_or_reparse')
    require(current.is_file() and current.resolve().is_relative_to(directory),
            'artifact_missing_or_outside_probe')
    require(current.stat().st_size <= MAX_BYTES, 'artifact_size_limit')
    return current


def read_json(directory, name):
    return json.loads(artifact_path(directory, name).read_bytes())


class SecretScanner:
    def __init__(self, secret):
        encoded = secret.encode()
        self.needles = [encoded, secret.encode('utf-16-le'), secret.encode('utf-16-be'),
                        encoded.hex().encode(), encoded.hex().upper().encode(),
                        base64.b64encode(encoded), base64.urlsafe_b64encode(encoded)]
        self.remaining = MAX_TOTAL
        self.files = 0

    def scan(self, data, depth=0):
        self.remaining -= len(data)
        require(len(data) <= MAX_BYTES and self.remaining >= 0, 'artifact_size_limit')
        require(not any(needle in data for needle in self.needles), 'secret_leak')
        if depth >= 3:
            require(not data.startswith((b'PK\x03\x04', b'\x1f\x8b')), 'archive_depth_limit')
            return
        if data.startswith(b'PK\x03\x04'):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                require(len(archive.infolist()) <= 128, 'archive_entry_limit')
                for entry in archive.infolist():
                    require(entry.file_size <= MAX_BYTES, 'artifact_size_limit')
                    with archive.open(entry) as stream:
                        self.scan(stream.read(MAX_BYTES + 1), depth + 1)
        elif data.startswith(b'\x1f\x8b'):
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                self.scan(stream.read(MAX_BYTES + 1), depth + 1)
        for match in re.finditer(rb'[A-Za-z0-9_+/\-]{24,}={0,2}', data):
            token = match.group()
            try:
                decoded = base64.b64decode(token + b'=' * (-len(token) % 4),
                                           altchars=b'-_', validate=True)
            except ValueError:
                continue
            if depth < 3:
                self.scan(decoded, depth + 1)

    def tree(self, directory):
        for child in directory.iterdir():
            self.files += 1
            require(self.files <= 256, 'artifact_count_limit')
            require(not child.is_symlink() and not (
                getattr(child.lstat(), 'st_file_attributes', 0) & 0x400),
                'artifact_symlink_or_reparse')
            self.scan(child.name.encode())
            if child.is_dir():
                self.tree(child)
            else:
                require(child.is_file(), 'unsupported_artifact_type')
                require(child.stat().st_size <= MAX_BYTES, 'artifact_size_limit')
                with child.open('rb') as stream:
                    self.scan(stream.read(MAX_BYTES + 1))


def invoke(command, project, request, timeout, secret):
    process = subprocess.Popen(command, cwd=project, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    chunks = [bytearray(), bytearray()]
    overflow = threading.Event()
    read_error = threading.Event()

    def consume(stream, output):
        descriptor = stream.fileno()
        try:
            while True:
                data = os.read(descriptor, 4096)
                if not data:
                    return
                remaining = MAX_BYTES - len(output)
                output.extend(data[:remaining])
                if len(data) > remaining:
                    overflow.set()
        except OSError:
            read_error.set()

    readers = [threading.Thread(target=consume, args=(stream, output), daemon=True)
               for stream, output in zip((process.stdout, process.stderr), chunks)]
    for reader in readers:
        reader.start()
    failure = None
    try:
        process.stdin.write(json.dumps(request).encode() + b'\n')
        process.stdin.close()
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if overflow.is_set():
                raise ProbeFailure('adapter_output_limit')
            if time.monotonic() >= deadline:
                raise ProbeFailure('adapter_timeout')
            time.sleep(.01)
    except (OSError, ValueError) as error:
        failure = error
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    scanner = SecretScanner(secret)
    for output in chunks:
        scanner.scan(bytes(output))
    require(not any(reader.is_alive() for reader in readers), 'adapter_descendant_or_open_output')
    require(not read_error.is_set(), 'adapter_output_read_error')
    require(not overflow.is_set(), 'adapter_output_limit')
    if failure:
        raise failure
    require(process.returncode == 0, 'adapter_exit_failure')


def resource_states(observation):
    require(isinstance(observation, dict), 'invalid_observation')
    entries = observation.get('resources')
    require(isinstance(entries, list) and all(isinstance(entry, dict) and
            isinstance(entry.get('id'), str) and isinstance(entry.get('status'), str)
            for entry in entries), 'invalid_resource_observation')
    require(len({entry['id'] for entry in entries}) == len(entries), 'duplicate_resource_observation')
    return {entry['id']: entry['status'] for entry in entries}


def run_scenarios(project, command, timeout, root, service, result, contracts):
    def run(phase, directory):
        result['stage'] = phase
        service.begin(phase)
        observation = directory / 'observation.json'
        observation.unlink(missing_ok=True)
        request = dict(protocol_version=1, scenario=phase, directory=str(directory),
                       observation_path=str(observation))
        try:
            invoke(command, project, dict(request, operation='execute',
                   base_url=service.base_url), timeout, service.secret)
            SecretScanner(service.secret).tree(root)
            observation.unlink(missing_ok=True)
            request_count = len(service.requests)
            invoke(command, project, dict(request, operation='observe'), timeout, service.secret)
            require(len(service.requests) == request_count, 'observation_must_be_offline')
        finally:
            endpoints = {'/config', '/resources', '/resources/first',
                         '/resources/second', '/resources/hidden'}
            result.setdefault('http_observations', {})[phase] = {
                'request_count': len(service.requests),
                'responses': [
                    {'method': method if method in {
                        'GET', 'POST', 'PUT', 'DELETE', 'HEAD', 'PATCH', 'OPTIONS',
                        'CONNECT', 'TRACE'} else 'other', 'endpoint': urlsplit(target).path
                     if urlsplit(target).path in endpoints else 'other', 'status': status}
                    for method, target, status in service.responses[:64]],
                'trace_truncated': len(service.responses) > 64}
            SecretScanner(service.secret).tree(root)
        return read_json(directory, 'observation.json')

    if 'partial' in contracts:
        partial = root / 'partial'
        partial.mkdir()
        states = resource_states(run('partial', partial))
        require(service.creates == 3 and service.resources == {'first'}, 'partial_state_mismatch')
        require(all(service.called('DELETE', '/resources/' + identifier)
                    for identifier in ('first', 'second')), 'independent_cleanup_not_attempted')
        require(service.called('GET', '/resources/second'), 'cleanup_readback_missing')
        requests = [(method, path) for method, path, body in service.requests]
        prepares = [index for index, entry in enumerate(requests)
                    if entry == ('POST', '/resources')]
        require(len(prepares) == 3, 'prepare_sequence_mismatch')
        failed_prepare = prepares[2]
        for identifier in ('first', 'second'):
            require(requests.index(('DELETE', '/resources/' + identifier)) > failed_prepare,
                    'cleanup_before_prepare_failure')
        second_delete = requests.index(('DELETE', '/resources/second'))
        require(('GET', '/resources/second') in requests[second_delete + 1:],
                'absence_readback_before_delete')
        require(states.get('first') in UNRESOLVED
                and states.get('second') == 'absence_verified', 'partial_ledger_mismatch')
        result['checks']['partial'] = True

    if 'config' in contracts:
        config = root / 'config'
        config.mkdir()
        observation = run('restore', config)
        require(isinstance(observation, dict) and observation.get('status') in UNRESOLVED,
                'failed_restore_marked_resolved')
        require(service.config == {'value': 'temporary'}, 'failed_restore_state_mismatch')
        actions = [(method, body) for method, path, body in service.requests if path == '/config']
        require(len(actions) >= 4 and actions[0] == ('GET', None)
                and ('PUT', {'value': 'temporary'}) in actions
                and ('PUT', {'value': 'baseline'}) in actions, 'restore_sequence_missing')
        temporary_put = actions.index(('PUT', {'value': 'temporary'}))
        restore_put = actions.index(('PUT', {'value': 'baseline'}))
        require(temporary_put < restore_put and ('GET', None) in actions[temporary_put + 1:restore_put],
                'temporary_config_readback_missing')
        original_name = observation.get('original_snapshot')
        original = artifact_path(config, original_name).read_bytes()
        require(json.loads(original) == {'value': 'baseline'}, 'original_snapshot_mismatch')
        result['checks']['restore'] = True

        observation = run('recover', config)
        require(isinstance(observation, dict) and observation.get('status') == 'absence_verified',
                'restoration_not_resolved')
        require(service.config == {'value': 'baseline'}, 'restoration_not_applied')
        require(service.called('PUT', '/config'), 'restoration_write_missing')
        actions = [method for method, path, body in service.requests if path == '/config']
        require('GET' in actions[actions.index('PUT') + 1:], 'restoration_readback_missing')
        require(observation.get('original_snapshot') == original_name and
                artifact_path(config, original_name).read_bytes() == original,
                'original_snapshot_overwritten')
        restored_name = observation.get('restored_snapshot')
        restored = artifact_path(config, restored_name).read_bytes()
        require(json.loads(restored) == {'value': 'baseline'}, 'restored_snapshot_mismatch')
        result['checks']['recover'] = True

        service.config = {'value': 'later-legitimate-change'}
        observation = run('recovered', config)
        require(isinstance(observation, dict) and observation.get('status') == 'absence_verified',
                'closed_restore_lost')
        require(observation.get('original_snapshot') == original_name and
                observation.get('restored_snapshot') == restored_name,
                'closed_restore_binding_changed')
        require(service.config == {'value': 'later-legitimate-change'}
                and not service.called('PUT', '/config'), 'closed_restore_replayed')
        require(artifact_path(config, original_name).read_bytes() == original
                and artifact_path(config, restored_name).read_bytes() == restored,
                'closed_snapshot_overwritten')
        result['checks']['recovered'] = True

    for phase in ('hidden', 'denied', 'paginated') if 'absence' in contracts else ():
        directory = root / phase
        directory.mkdir()
        states = resource_states(run(phase, directory))
        identifier = 'unknown-create' if phase == 'paginated' else 'hidden'
        require(states.get(identifier) in UNRESOLVED, 'false_absence')
        require(service.resources == {'hidden'}, 'readback_mutated_resource')
        require(all(method == 'GET' for method, target, body in service.requests),
                'readback_attempted_write')
        if phase == 'hidden':
            require(service.called('GET', '/resources')
                    and service.called('GET', '/resources/hidden'), 'hidden_detail_readback_missing')
        elif phase == 'denied':
            require(service.called('GET', '/resources/hidden'), 'denied_readback_missing')
        else:
            require(service.called('GET', '/resources'), 'pagination_readback_missing')
        result['checks'][phase] = True


def inspect(project, request):
    result = {'valid': False, 'checks': {}, 'stage': 'input',
              'note': 'Offline adapter behavior only; no live cleanup, business pass, '
                      'network isolation or exhaustive secret detection is proved.'}
    root = None
    try:
        project = Path(project).resolve()
        require(project.is_dir(), 'project_directory_required')
        command, inputs, timeout, contracts = settings(request)
        hashes = input_hashes(project, inputs)
        binding = dict(command=command, inputs=hashes, protocol_version=1,
                       project=str(project), timeout_seconds=timeout, contracts=contracts,
                       probe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        result['adapter_fingerprint'] = hashlib.sha256(
            json.dumps(binding, sort_keys=True).encode()).hexdigest()
        result['contracts'] = contracts
        result['stage'] = 'directory'
        parent = probe_parent(project, request.get('directory', 'test/.cache/fixture-contract-probes'))
        root = Path(tempfile.mkdtemp(prefix='.fixture-contract-', dir=parent)).resolve()
        with Service('AUTO_TEST_SECRET_' + secrets.token_hex(24)) as service:
            run_scenarios(project, command, timeout, root, service, result, contracts)
        result['stage'] = 'inputs_unchanged'
        require(input_hashes(project, inputs) == hashes, 'adapter_inputs_changed')
        result['checks']['inputs_unchanged'] = True
        result.update(valid=True, stage='complete')
    except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError,
            zipfile.BadZipFile, EOFError, zlib.error, RuntimeError, NotImplementedError) as error:
        result['checks'][result['stage']] = False
        result['error_type'] = str(error) if isinstance(error, ProbeFailure) else type(error).__name__
    finally:
        if root is not None:
            try:
                shutil.rmtree(root)
            except OSError:
                result.update(valid=False, stage='cleanup', error_type='probe_cleanup_failed')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--run', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        request = json.loads(args.input.read_text(encoding='utf-8'))
        result = inspect(args.project, request)
    except (OSError, ValueError):
        result = {'valid': False, 'stage': 'input', 'error_type': 'invalid_probe_input'}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False), flush=True)
    if not result['valid']:
        return 2
    if args.run is not None:
        if not args.run:
            return 2
        try:
            code = subprocess.call(args.run, cwd=args.project.resolve())
            return code if code >= 0 else 128 - code
        except OSError:
            print('{"valid":false,"stage":"runner_spawn","error_type":"runner_spawn_failed"}',
                  flush=True)
            return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
