#!/usr/bin/env python3
"""Exercise a trusted offline fixture adapter with real competing and killed processes."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import math
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading

from runtime_probe import platform_support, probe_parent


class ProbeFailure(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise ProbeFailure(reason)


def request_settings(request):
    require(isinstance(request, dict) and set(request) <= {
        'command', 'inputs', 'directory', 'timeout_seconds', 'acquire_timeout_ms', 'workers'},
        'invalid_request_fields')
    command = request.get('command')
    require(isinstance(command, list) and command
            and all(isinstance(part, str) and part and '\0' not in part for part in command),
            'command_must_be_nonempty_argv')
    inputs = request.get('inputs')
    require(isinstance(inputs, list) and inputs
            and all(isinstance(name, str) and name for name in inputs),
            'project_adapter_inputs_required')
    timeout = request.get('timeout_seconds', 10)
    acquire = request.get('acquire_timeout_ms', 250)
    workers = request.get('workers', 4)
    require(type(timeout) in {int, float} and math.isfinite(timeout) and 1 <= timeout <= 30,
            'invalid_timeout_seconds')
    require(type(acquire) is int and 50 <= acquire <= 5000
            and acquire / 1000 + .5 < timeout, 'invalid_acquire_timeout_ms')
    require(type(workers) is int and 2 <= workers <= 8, 'invalid_workers')
    return command, inputs, timeout, acquire, workers


def input_hashes(project, inputs):
    hashes = {}
    for name in inputs:
        current = project
        for part in platform_support.relative_parts(name):
            current = current / part
            require(not current.is_symlink()
                    and not (current.exists()
                             and getattr(current.lstat(), 'st_file_attributes', 0) & 0x400),
                    'adapter_input_symlink_or_reparse')
        require(current.is_file() and current.resolve().is_relative_to(project),
                'adapter_input_missing_or_outside_project')
        hashes[name] = hashlib.sha256(current.read_bytes()).hexdigest()
    return hashes


class Worker:
    def __init__(self, command, project, directory, operation, timeout, acquire):
        self.timeout = timeout
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            command, cwd=project, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8')
        self.reader = threading.Thread(target=self.read_output, daemon=True)
        self.reader.start()
        try:
            self.send(json.dumps({'protocol_version': 1, 'directory': str(directory),
                                  'operation': operation, 'acquire_timeout_ms': acquire}))
        except BaseException:
            self.close()
            raise

    def read_output(self):
        try:
            while True:
                line = self.process.stdout.readline(4097)
                if not line:
                    break
                if len(line) > 4096:
                    self.messages.put(None)
                    break
                self.messages.put(line)
        except (OSError, UnicodeError, ValueError):
            self.messages.put(None)
        finally:
            self.messages.put(None)

    def send(self, value):
        self.process.stdin.write(value + '\n')
        self.process.stdin.flush()

    def message(self, status):
        try:
            line = self.messages.get(timeout=self.timeout)
        except queue.Empty:
            raise ProbeFailure('adapter_response_timeout') from None
        try:
            value = json.loads(line) if line is not None else None
        except ValueError:
            value = None
        require(isinstance(value, dict) and value.get('status') == status,
                'adapter_protocol_error')
        return value

    def ready(self):
        self.message('ready')
        require(self.process.poll() is None, 'adapter_exited_before_dispatch')

    def finish(self, code=0):
        try:
            actual = self.process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise ProbeFailure('adapter_exit_timeout') from None
        self.reader.join(timeout=self.timeout)
        require(not self.reader.is_alive(), 'adapter_output_not_closed')
        require(actual == code, 'adapter_exit_code_mismatch')
        require(self.messages.get(timeout=self.timeout) is None and self.messages.empty(),
                'unexpected_adapter_output')

    def kill(self):
        require(self.process.poll() is None, 'owner_exited_before_kill')
        self.process.kill()
        self.process.wait(timeout=self.timeout)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=self.timeout)
        self.reader.join(timeout=self.timeout)
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except OSError:
                pass


def inspect(project, request):
    result = {'valid': False, 'checks': {}, 'stage': 'input',
              'note': 'Only the supplied offline adapter and declared inputs are checked. '
                      'No live identity lease or business cleanup is proved.'}
    try:
        project = Path(project).resolve()
        require(project.is_dir(), 'project_directory_required')
        command, inputs, timeout, acquire, workers = request_settings(request)
        hashes = input_hashes(project, inputs)
        binding = dict(command=command, inputs=hashes, timeout_seconds=timeout,
                       acquire_timeout_ms=acquire, workers=workers)
        result['adapter_fingerprint'] = hashlib.sha256(
            json.dumps(binding, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        result['stage'] = 'directory'
        parent = probe_parent(project, request.get('directory', 'test/.cache/fixture-lock-probes'))
        with tempfile.TemporaryDirectory(prefix='.fixture-lock-', dir=parent) as temp, ExitStack() as stack:
            directory = Path(temp).resolve()

            def launch(operation):
                worker = Worker(command, project, directory, operation, timeout, acquire)
                stack.callback(worker.close)
                worker.ready()
                return worker

            def hold():
                worker = launch('hold')
                worker.send('go')
                worker.message('acquired')
                require(worker.process.poll() is None, 'owner_exited_without_release')
                return worker

            def allocate(worker):
                value = worker.message('allocated').get('value')
                require(isinstance(value, str) and value, 'allocation_value_must_be_string')
                worker.finish()
                return value

            def reserve():
                worker = launch('reserve')
                worker.send('go')
                return allocate(worker)

            result['stage'] = 'live_owner_exclusive'
            owner = hold()
            contender = launch('reserve')
            contender.send('go')
            contender.message('blocked')
            contender.finish(2)
            require(owner.process.poll() is None, 'live_owner_was_displaced')
            result['checks'][result['stage']] = True

            result['stage'] = 'normal_exit_releases'
            owner.send('release')
            owner.message('released')
            owner.finish()
            values = [reserve()]
            result['checks'][result['stage']] = True

            result['stage'] = 'killed_owner_releases'
            owner = hold()
            owner.kill()
            values.append(reserve())
            result['checks'][result['stage']] = True

            result['stage'] = 'concurrent_allocations_unique'
            contenders = [launch('reserve') for _ in range(workers)]
            for worker in contenders:
                worker.send('go')
            values.extend(allocate(worker) for worker in contenders)
            require(len(set(values)) == len(values), 'duplicate_allocation')
            result['checks'][result['stage']] = True
            result['allocation_count'] = len(values)
            result['stage'] = 'cleanup'
        result['stage'] = 'inputs_unchanged'
        require(input_hashes(project, inputs) == hashes, 'adapter_inputs_changed')
        result['checks'][result['stage']] = True
        result.update(valid=True, stage='complete')
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, queue.Empty) as error:
        result['checks'][result['stage']] = False
        result['error_type'] = str(error) if isinstance(error, ProbeFailure) else type(error).__name__
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--input', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        request = json.loads(args.input.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        result = {'valid': False, 'stage': 'input', 'error_type': type(error).__name__}
    else:
        result = inspect(args.project, request)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if result['valid'] else 2


if __name__ == '__main__':
    sys.exit(main())
