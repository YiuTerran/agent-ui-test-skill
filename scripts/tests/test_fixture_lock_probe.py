"""Offline fixture adapters exercise real exclusion, interruption and allocation failures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fixture_lock_probe as probe


ADAPTER = r'''
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

spec = importlib.util.spec_from_file_location('platform', sys.argv[1])
platform = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform)
mode = sys.argv[2]
request = json.loads(sys.stdin.readline())
directory = Path(request['directory'])
lock = directory / 'pool.lock'
ledger = directory / 'allocations.json'
assert request['protocol_version'] == 1

def emit(status, **extra):
    print(json.dumps(dict(status=status, **extra)), flush=True)

@contextmanager
def locked():
    if mode == 'unlocked':
        yield
    elif mode == 'sentinel':
        deadline = time.monotonic() + request['acquire_timeout_ms'] / 1000
        while True:
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                time.sleep(.01)
        try:
            yield
        finally:
            lock.unlink()
    else:
        with lock.open('a+b') as stream, platform.file_lock(
                stream.fileno(), timeout=request['acquire_timeout_ms'] / 1000):
            yield

if mode == 'invalid':
    print('private-output-and-token', flush=True)
    sys.exit(0)
if mode == 'silent':
    time.sleep(30)
emit('ready')
assert sys.stdin.readline().strip() == 'go'
if mode == 'changed_input':
    with open(__file__, 'a', encoding='utf-8') as output:
        output.write('\n')
stale = None
if request['operation'] == 'reserve' and mode == 'stale_read':
    stale = json.loads(ledger.read_text()) if ledger.exists() else []
    time.sleep(.15)
try:
    with locked():
        if request['operation'] == 'hold':
            emit('acquired')
            assert sys.stdin.readline().strip() == 'release'
        else:
            values = stale if stale is not None else (
                json.loads(ledger.read_text()) if ledger.exists() else [])
            value = str(len(values) + 1)
            time.sleep(.02)
            ledger.write_text(json.dumps(values + [value]), encoding='utf-8')
            emit('allocated', value=1 if mode == 'numeric_value' else value)
except TimeoutError:
    emit('blocked')
    sys.exit(2)
if request['operation'] == 'hold':
    emit('released')
'''


class FixtureLockProbeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.adapter = self.project / 'adapter.py'
        self.adapter.write_text(ADAPTER, encoding='utf-8')
        self.parent = self.project / 'test/.cache/fixture-lock-probes'
        self.parent.mkdir(parents=True)
        self.retained = self.parent / 'existing-result'
        self.retained.write_text('retain', encoding='utf-8')

    def request(self, mode='healthy', **extra):
        return dict(command=[sys.executable, '-u', str(self.adapter),
                             str(Path(probe.__file__).with_name('platform_support.py')), mode],
                    inputs=['adapter.py'], timeout_seconds=3, **extra)

    def inspect(self, mode='healthy', **extra):
        result = probe.inspect(self.project, self.request(mode, **extra))
        self.assertEqual([self.retained], list(self.parent.iterdir()))
        self.assertEqual('retain', self.retained.read_text(encoding='utf-8'))
        return result

    def test_real_kernel_adapter_passes_lifecycle_and_concurrent_allocations(self):
        result = self.inspect()
        self.assertTrue(result['valid'], result)
        self.assertEqual('complete', result['stage'])
        self.assertEqual(6, result['allocation_count'])
        self.assertEqual(64, len(result['adapter_fingerprint']))
        self.assertTrue(all(result['checks'].values()))

    def test_dead_sentinel_is_rejected_without_deleting_it_to_fake_recovery(self):
        result = self.inspect('sentinel')
        self.assertFalse(result['valid'], result)
        self.assertTrue(result['checks']['live_owner_exclusive'])
        self.assertTrue(result['checks']['normal_exit_releases'])
        self.assertEqual('killed_owner_releases', result['stage'])

    def test_unlocked_adapter_cannot_pass_live_owner_check(self):
        result = self.inspect('unlocked')
        self.assertFalse(result['valid'], result)
        self.assertEqual('live_owner_exclusive', result['stage'])

    def test_stale_read_outside_lock_duplicates_concurrent_allocations(self):
        result = self.inspect('stale_read')
        self.assertFalse(result['valid'], result)
        self.assertEqual('concurrent_allocations_unique', result['stage'])
        self.assertEqual('duplicate_allocation', result['error_type'])

    def test_invalid_protocol_is_sanitized(self):
        result = self.inspect('invalid')
        self.assertFalse(result['valid'], result)
        self.assertEqual('adapter_protocol_error', result['error_type'])
        self.assertNotIn('private-output', json.dumps(result))
        self.assertNotIn(str(self.project), json.dumps(result))

    def test_numeric_allocations_are_rejected(self):
        result = self.inspect('numeric_value')
        self.assertFalse(result['valid'], result)
        self.assertEqual('allocation_value_must_be_string', result['error_type'])

    def test_declared_source_change_invalidates_success(self):
        result = self.inspect('changed_input')
        self.assertFalse(result['valid'], result)
        self.assertEqual('inputs_unchanged', result['stage'])
        self.assertEqual('adapter_inputs_changed', result['error_type'])

    def test_missing_executable_is_failure_and_cleans_own_directory(self):
        request = self.request()
        request['command'] = [str(self.project / 'missing-executable')]
        result = probe.inspect(self.project, request)
        self.assertFalse(result['valid'], result)
        self.assertEqual('FileNotFoundError', result['error_type'])
        self.assertEqual([self.retained], list(self.parent.iterdir()))

    def test_timeout_kills_started_worker(self):
        started = []
        popen = probe.subprocess.Popen

        def launch(*args, **kwargs):
            process = popen(*args, **kwargs)
            started.append(process)
            return process

        request = self.request('silent')
        request['timeout_seconds'] = 1
        with mock.patch.object(probe.subprocess, 'Popen', side_effect=launch):
            result = probe.inspect(self.project, request)
        self.assertFalse(result['valid'], result)
        self.assertEqual('adapter_response_timeout', result['error_type'])
        self.assertEqual(1, len(started))
        self.assertIsNotNone(started[0].poll())
        self.assertEqual([self.retained], list(self.parent.iterdir()))

    def test_invalid_request_fails_before_launch(self):
        for delta in [{'inputs': []}, {'inputs': ['../outside']}, {'workers': True},
                      {'workers': 1}, {'acquire_timeout_ms': 5000},
                      {'timeout_seconds': float('nan')}, {'command': 'node'},
                      {'unexpected': True}]:
            with self.subTest(delta=delta), mock.patch.object(probe.subprocess, 'Popen') as launched:
                result = probe.inspect(self.project, dict(self.request(), **delta))
                self.assertFalse(result['valid'], result)
                launched.assert_not_called()

    def test_input_symlink_is_rejected(self):
        original = self.project / 'original.py'
        self.adapter.rename(original)
        try:
            self.adapter.symlink_to(original)
        except OSError as error:
            if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
                self.skipTest('Windows symlink requires Developer Mode or privilege')
            raise
        with mock.patch.object(probe.subprocess, 'Popen') as launched:
            result = probe.inspect(self.project, self.request())
        self.assertFalse(result['valid'], result)
        self.assertEqual('adapter_input_symlink_or_reparse', result['error_type'])
        launched.assert_not_called()

    def test_cli_returns_json_failure_and_success_exit_codes(self):
        request_file = self.project / 'request.json'
        for mode, expected in [('sentinel', 2), ('healthy', 0)]:
            with self.subTest(mode=mode):
                request_file.write_text(json.dumps(self.request(mode)), encoding='utf-8')
                completed = subprocess.run(
                    [sys.executable, str(Path(probe.__file__)), '--project', str(self.project),
                     '--input', str(request_file)], capture_output=True, text=True, timeout=30)
                self.assertEqual(expected, completed.returncode, completed.stderr + completed.stdout)
                self.assertEqual(expected == 0, json.loads(completed.stdout)['valid'])
        request_file.write_text('invalid-json', encoding='utf-8')
        completed = subprocess.run(
            [sys.executable, str(Path(probe.__file__)), '--project', str(self.project),
             '--input', str(request_file)], capture_output=True, text=True, timeout=10)
        self.assertEqual(2, completed.returncode)
        self.assertEqual('input', json.loads(completed.stdout)['stage'])


if __name__ == '__main__':
    unittest.main()
