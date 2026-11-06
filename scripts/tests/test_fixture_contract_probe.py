"""Exercise fixture behavior and guarded dispatch through real subprocesses and HTTP."""
import base64
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fixture_contract_probe as probe


FIXTURE = r'''
import base64
import io
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

class Fixture:
    def __init__(self, directory, base_url, mode):
        self.directory = Path(directory)
        self.base_url = base_url
        self.mode = mode
        self.ledger = self.directory / 'ledger.json'
        self.state = json.loads(self.ledger.read_text()) if self.ledger.exists() else {}

    def save(self):
        self.ledger.write_text(json.dumps(self.state), encoding='utf-8')

    def request(self, method, path, body=None):
        if self.mode == 'prepare_query' and method == 'POST':
            path += '?alternate=true'
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base_url + path, data=data, method=method,
                          headers={'Content-Type': 'application/json'})
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            value = json.loads(raw) if raw else {}
            if 'secret' in value and self.mode.startswith('leak'):
                if self.mode == 'leak_stdout':
                    print(value['secret'], flush=True)
                elif self.mode == 'leak_stderr':
                    print(value['secret'], file=sys.stderr, flush=True)
                    raise RuntimeError('expected')
                elif self.mode == 'leak_encoded':
                    (self.directory / 'attachment.json').write_text(
                        json.dumps({'body': base64.b64encode(raw).decode()}))
                elif self.mode == 'leak_zip':
                    with zipfile.ZipFile(self.directory / 'attachment.zip', 'w',
                                         compression=zipfile.ZIP_DEFLATED) as archive:
                        archive.writestr('response.json', raw)
                else:
                    (self.directory / 'raw-response.json').write_bytes(raw)
                if self.mode == 'leak_timeout':
                    import time
                    time.sleep(30)
            return response.status, value

    def partial(self):
        self.state = {'resources': []}
        try:
            for number in range(3):
                status, value = self.request('POST', '/resources', {'name': str(number)})
                if status != 201:
                    raise RuntimeError('prepare failed')
                self.state['resources'].append({'id': value['id'], 'status': 'confirmed'})
                self.save()
        finally:
            for entry in self.state['resources']:
                if self.mode == 'readback_before_delete' and entry['id'] == 'second':
                    self.request('GET', '/resources/second')
                status, value = self.request('DELETE', '/resources/' + entry['id'])
                if status != 204:
                    entry['status'] = 'cleanup_failed'
                    self.save()
                    if self.mode == 'cleanup_stops':
                        break
                    continue
                if self.mode == 'readback_before_delete':
                    status = 404
                else:
                    status, value = self.request('GET', '/resources/' + entry['id'])
                entry['status'] = 'absence_verified' if status == 404 else 'ambiguous'
                if self.mode != 'closure_not_persisted':
                    self.save()

    def restore(self):
        status, original = self.request('GET', '/config')
        (self.directory / 'original.json').write_text(json.dumps(original))
        self.state = {'status': 'intent', 'original_snapshot': 'original.json'}
        self.save()
        self.request('PUT', '/config', {'value': 'temporary'})
        self.request('GET', '/config')
        self.recover()
        if self.mode == 'false_restore':
            self.state['status'] = 'absence_verified'
            self.save()

    def recover(self):
        if self.state['status'] == 'absence_verified' and self.mode != 'replay_closed':
            if self.mode == 'lost_closed_binding':
                self.state.update(original_snapshot='missing-original.json',
                                  restored_snapshot='missing-restored.json')
                self.save()
            return
        original = json.loads((self.directory / 'original.json').read_text())
        if self.mode == 'overwrite_original':
            status, original = self.request('GET', '/config')
            (self.directory / 'original.json').write_text(json.dumps(original))
        status, value = self.request('PUT', '/config', original)
        self.state['status'] = 'cleanup_failed'
        if status == 200:
            if self.mode != 'missing_readback':
                status, value = self.request('GET', '/config')
            (self.directory / 'restored.json').write_text(json.dumps(value))
            self.state.update(status='absence_verified', restored_snapshot='restored.json')
        self.save()

    def readback(self, scenario):
        if scenario == 'hidden':
            status, value = self.request('GET', '/resources')
            if self.mode != 'list_only':
                path = '/resources/hidden'
                if self.mode == 'query_secret':
                    path += '?secret=' + value['secret']
                status, value = self.request('GET', path)
        elif scenario == 'denied':
            status, value = self.request('GET', '/resources/hidden')
        else:
            status, value = self.request('GET', '/resources')
        identifier = 'unknown-create' if scenario == 'paginated' else 'hidden'
        state = 'absence_verified' if self.mode == 'false_' + scenario else 'ambiguous'
        self.state = {'resources': [{'id': identifier, 'status': state}]}
        self.save()
'''

ADAPTER = r'''
import json
import os
from pathlib import Path
import sys
import time
from fixture import Fixture

request = json.loads(sys.stdin.readline())
mode = sys.argv[1]
directory = Path(request['directory'])
scenario = request['scenario']
if request['operation'] == 'observe':
    fixture = Fixture(directory, '', mode)
    if mode in ('observe_http', 'observe_head'):
        fixture.base_url = json.loads((directory / 'url.json').read_text())
        fixture.request('HEAD' if mode == 'observe_head' else 'GET', '/resources')
    Path(request['observation_path']).write_text(
        'private-invalid-json' if mode == 'malformed' else json.dumps(fixture.state))
    sys.exit(0)
if mode == 'timeout':
    time.sleep(30)
if mode == 'output_limit':
    sys.stdout.write('x' * (3 * 1024 * 1024))
    sys.exit(0)
if mode == 'exit':
    sys.exit(3)
if mode == 'changed_input':
    with open('fixture.py', 'a') as stream:
        stream.write('\n')
fixture = Fixture(directory, request['base_url'], mode)
if mode in ('observe_http', 'observe_head'):
    (directory / 'url.json').write_text(json.dumps(request['base_url']))
if mode == 'no_http':
    fixture.state = {'resources': [
        {'id': 'first', 'status': 'cleanup_failed'},
        {'id': 'second', 'status': 'absence_verified'}]}
    fixture.save()
elif scenario == 'partial':
    try:
        fixture.partial()
    except RuntimeError:
        pass
elif scenario == 'restore':
    fixture.restore()
elif scenario in ('recover', 'recovered'):
    fixture.recover()
else:
    fixture.readback(scenario)
path = Path(request['observation_path'])
if mode == 'symlink':
    path.symlink_to(directory / 'ledger.json')
elif mode == 'malformed':
    path.write_text('private-invalid-json')
else:
    path.write_text(json.dumps(fixture.state))
'''


class FixtureContractProbeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        (self.project / 'fixture.py').write_text(FIXTURE, encoding='utf-8')
        (self.project / 'adapter.py').write_text(ADAPTER, encoding='utf-8')
        self.parent = self.project / 'test/.cache/fixture-contract-probes'
        self.parent.mkdir(parents=True)
        self.sibling = self.parent / 'prior-report'
        self.sibling.write_text('retain', encoding='utf-8')

    def request(self, mode='healthy', **extra):
        return dict(dict(command=[sys.executable, '-u', 'adapter.py', mode],
                         inputs=['adapter.py', 'fixture.py'], timeout_seconds=5), **extra)

    def inspect(self, mode='healthy', **extra):
        result = probe.inspect(self.project, self.request(mode, **extra))
        self.assertEqual([self.sibling], list(self.parent.iterdir()))
        self.assertEqual('retain', self.sibling.read_text())
        self.assertNotIn(str(self.project), json.dumps(result))
        self.assertNotIn('AUTO_TEST_SECRET', json.dumps(result))
        return result

    def test_loopback_service_does_not_require_hostname_resolution(self):
        with mock.patch('socket.getfqdn', side_effect=AssertionError('unexpected DNS')):
            result = self.inspect(contracts=['partial'])
        self.assertTrue(result['valid'], result)
        self.assertTrue(result['checks']['partial'])

    def test_actual_fixture_passes_all_http_failure_and_recovery_phases(self):
        result = self.inspect()
        self.assertTrue(result['valid'], result)
        self.assertEqual(set(result['checks']), {
            'partial', 'restore', 'recover', 'recovered', 'hidden', 'denied',
            'paginated', 'inputs_unchanged'})
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual(64, len(result['adapter_fingerprint']))

    def test_select_only_applicable_contracts(self):
        result = self.inspect(contracts=['absence'])
        self.assertTrue(result['valid'], result)
        self.assertEqual(set(result['checks']), {'hidden', 'denied', 'paginated', 'inputs_unchanged'})
        self.assertEqual(['absence'], result['contracts'])

    def test_partial_cleanup_continues_after_independent_failure(self):
        result = self.inspect('cleanup_stops')
        self.assertFalse(result['valid'])
        self.assertEqual('partial_state_mismatch', result['error_type'])

    def test_truthy_export_cannot_replace_actual_http_execution(self):
        result = self.inspect('no_http')
        self.assertFalse(result['valid'])
        self.assertEqual('partial_state_mismatch', result['error_type'])

    def test_memory_closure_without_persisting_ledger_is_rejected(self):
        result = self.inspect('closure_not_persisted', contracts=['partial'])
        self.assertFalse(result['valid'])
        self.assertEqual('partial_ledger_mismatch', result['error_type'])

    def test_observation_cannot_reconstruct_ledger_from_new_http_queries(self):
        for mode in ('observe_http', 'observe_head'):
            with self.subTest(mode=mode):
                result = self.inspect(mode, contracts=['partial'])
                self.assertFalse(result['valid'])
                self.assertEqual('observation_must_be_offline', result['error_type'])

    def test_readback_before_delete_cannot_prove_postdelete_absence(self):
        result = self.inspect('readback_before_delete', contracts=['partial'])
        self.assertFalse(result['valid'])
        self.assertEqual('absence_readback_before_delete', result['error_type'])

    def test_nonmatching_prepare_routes_fail_with_structured_error(self):
        result = self.inspect('prepare_query', contracts=['partial'])
        self.assertFalse(result['valid'])
        self.assertEqual('prepare_sequence_mismatch', result['error_type'])

    def test_http_failure_trace_never_outputs_sensitive_query(self):
        result = self.inspect('query_secret', contracts=['absence'])
        self.assertFalse(result['valid'])
        self.assertEqual('hidden_detail_readback_missing', result['error_type'])
        responses = result['http_observations']['hidden']['responses']
        self.assertEqual('/resources/hidden', responses[-1]['endpoint'])
        self.assertEqual(200, responses[-1]['status'])
        self.assertNotIn('?secret', json.dumps(result))

    def test_config_false_closure_original_overwrite_readback_and_replay_are_rejected(self):
        expected = {'false_restore': 'failed_restore_marked_resolved',
                    'overwrite_original': 'failed_restore_marked_resolved',
                    'missing_readback': 'restoration_readback_missing',
                    'lost_closed_binding': 'closed_restore_binding_changed',
                    'replay_closed': 'closed_restore_replayed'}
        for mode, reason in expected.items():
            with self.subTest(mode=mode):
                result = self.inspect(mode, contracts=['config'])
                self.assertFalse(result['valid'], result)
                self.assertEqual(reason, result['error_type'])

    def test_hidden_denied_and_incomplete_pages_never_prove_absence(self):
        for phase in ('hidden', 'denied', 'paginated'):
            with self.subTest(phase=phase):
                result = self.inspect('false_' + phase, contracts=['absence'])
                self.assertFalse(result['valid'])
                self.assertEqual('false_absence', result['error_type'])
                self.assertEqual(phase, result['stage'])

    def test_empty_list_without_exact_lookup_is_rejected(self):
        result = self.inspect('list_only', contracts=['absence'])
        self.assertFalse(result['valid'])
        self.assertEqual('hidden_detail_readback_missing', result['error_type'])

    def test_secret_leaks_in_logs_error_paths_encoded_attachments_and_archives_fail(self):
        for mode in ('leak_stdout', 'leak_stderr', 'leak_raw', 'leak_encoded', 'leak_zip'):
            with self.subTest(mode=mode):
                result = self.inspect(mode, contracts=['partial'])
                self.assertFalse(result['valid'])
                self.assertEqual('secret_leak', result['error_type'])

    def test_failure_and_timeout_still_scan_config_recovery_and_partial_artifacts(self):
        result = self.inspect('leak_raw', contracts=['config'])
        self.assertFalse(result['valid'])
        self.assertEqual('restore', result['stage'])
        self.assertEqual('secret_leak', result['error_type'])
        result = self.inspect('leak_timeout', contracts=['partial'], timeout_seconds=1)
        self.assertFalse(result['valid'])
        self.assertEqual('secret_leak', result['error_type'])

    def test_invalid_export_is_sanitized(self):
        result = self.inspect('malformed')
        self.assertFalse(result['valid'])
        self.assertNotIn('private-invalid', json.dumps(result))

    def test_input_changes_invalidate_completed_probe(self):
        result = self.inspect('changed_input', contracts=['partial'])
        self.assertFalse(result['valid'])
        self.assertEqual('adapter_inputs_changed', result['error_type'])

    def test_timeout_waits_for_owned_process_exit_before_cleanup(self):
        processes = []
        popen = probe.subprocess.Popen

        def launch(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process

        with mock.patch.object(probe.subprocess, 'Popen', side_effect=launch):
            result = self.inspect('timeout', timeout_seconds=1)
        self.assertFalse(result['valid'])
        self.assertEqual('adapter_timeout', result['error_type'])
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_process_failure_output_limit_and_missing_executable(self):
        for mode, reason in [('exit', 'adapter_exit_failure'),
                             ('output_limit', 'adapter_output_limit')]:
            with self.subTest(mode=mode):
                result = self.inspect(mode)
                self.assertFalse(result['valid'])
                self.assertEqual(reason, result['error_type'])
        request = self.request()
        request['command'] = [str(self.project / 'missing')]
        result = probe.inspect(self.project, request)
        self.assertFalse(result['valid'])
        self.assertEqual('FileNotFoundError', result['error_type'])
        self.assertEqual([self.sibling], list(self.parent.iterdir()))

    def test_bad_requests_fail_without_starting_adapter(self):
        changes = [{'command': 'node'}, {'inputs': []}, {'inputs': ['../outside']},
                   {'inputs': ['missing.py']}, {'contracts': []}, {'contracts': ['other']},
                   {'contracts': ['partial', 'partial']}, {'timeout_seconds': True},
                   {'timeout_seconds': float('nan')}, {'directory': '../outside'},
                   {'directory': 'test/output'}, {'unexpected': True}]
        for delta in changes:
            with self.subTest(delta=delta), mock.patch.object(probe.subprocess, 'Popen') as launch:
                result = probe.inspect(self.project, dict(self.request(), **delta))
                self.assertFalse(result['valid'])
                launch.assert_not_called()

    def test_input_and_artifact_symlinks_are_rejected(self):
        alias = self.project / 'alias.py'
        try:
            alias.symlink_to(self.project / 'fixture.py')
        except OSError:
            self.skipTest('symlinks unavailable')
        result = probe.inspect(self.project, dict(self.request(), inputs=['alias.py']))
        self.assertFalse(result['valid'])
        self.assertEqual('adapter_input_symlink_or_reparse', result['error_type'])
        result = self.inspect('symlink')
        self.assertFalse(result['valid'])
        self.assertEqual('artifact_symlink_or_reparse', result['error_type'])

    def cli(self, request, sentinel, extra_env=None):
        path = self.project / 'probe-input.json'
        path.write_text(json.dumps(request), encoding='utf-8')
        command = [sys.executable, str(Path(probe.__file__)), '--project', str(self.project),
                   '--input', str(path), '--run', sys.executable, '-c',
                   "from pathlib import Path; Path('runner-started').write_text('yes')"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, **(extra_env or {})))
        self.assertEqual(sentinel, (self.project / 'runner-started').exists(), result.stdout)
        self.assertNotIn('AUTO_TEST_SECRET', result.stdout + result.stderr)
        return result

    def test_guarded_runner_starts_only_after_success(self):
        result = self.cli(self.request(contracts=['partial']), True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['valid'])

    def test_broken_and_missing_adapter_never_start_guarded_runner(self):
        result = self.cli(self.request('false_hidden', contracts=['absence']), False)
        self.assertEqual(2, result.returncode)
        request = dict(self.request(), inputs=['missing.py'])
        self.assertEqual(2, self.cli(request, False).returncode)
        result = subprocess.run(
            [sys.executable, str(Path(probe.__file__)), '--project', str(self.project),
             '--input', str(self.project / 'missing-input.json'), '--run', sys.executable,
             '-c', "from pathlib import Path; Path('runner-started').touch()"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(2, result.returncode)
        self.assertFalse((self.project / 'runner-started').exists())

    def test_runner_exit_code_is_preserved_after_successful_probe(self):
        path = self.project / 'input.json'
        path.write_text(json.dumps(self.request(contracts=['absence'])), encoding='utf-8')
        result = subprocess.run(
            [sys.executable, str(Path(probe.__file__)), '--project', str(self.project),
             '--input', str(path), '--run', sys.executable, '-c', 'raise SystemExit(7)'],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(7, result.returncode)
        self.assertTrue(json.loads(result.stdout)['valid'])

    def test_guard_preserves_inherited_environment(self):
        adapter = self.project / 'adapter.py'
        adapter.write_text(
            "import os\nassert os.environ['FIXTURE_PROBE_MARKER'] == 'retained'\n" + ADAPTER)
        result = self.cli(self.request(contracts=['partial']), True,
                          {'FIXTURE_PROBE_MARKER': 'retained'})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class SecretScannerTest(unittest.TestCase):
    def test_encoded_and_compressed_bodies_are_inspected(self):
        secret = 'AUTO_TEST_SECRET_0123456789abcdef0123456789abcdef'
        body = json.dumps({'secret': secret}).encode()
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('response.json', body)
        for value in [body, body.decode().encode('utf-16'), body.hex().encode(),
                      base64.b64encode(body), base64.urlsafe_b64encode(body),
                      gzip.compress(body), zipped.getvalue()]:
            with self.subTest(encoding=value[:10]):
                with self.assertRaisesRegex(probe.ProbeFailure, 'secret_leak'):
                    probe.SecretScanner(secret).scan(value)

    def test_compressed_bomb_is_bounded(self):
        body = gzip.compress(b'x' * (probe.MAX_BYTES + 1))
        with self.assertRaisesRegex(probe.ProbeFailure, 'artifact_size_limit'):
            probe.SecretScanner('unrelated').scan(body)

    def test_artifact_path_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            for path in ('../outside', '/absolute', 'missing.json'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    probe.artifact_path(Path(temp), path)


if __name__ == '__main__':
    unittest.main()
