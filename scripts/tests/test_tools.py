"""Observable preparation/dry-run invariants; all evidence is synthetic."""
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
from unittest import TestCase, mock
import zipfile

import test_workflow as base
import test_reports as reports

wf = base.wf


class ToolsTest(TestCase):
    setUp = base.WorkflowTest.setUp
    tearDown = base.WorkflowTest.tearDown
    write = base.WorkflowTest.write
    cli = base.WorkflowTest.cli
    start = base.WorkflowTest.start
    record = base.WorkflowTest.record
    check_sources = base.WorkflowTest.check_sources
    state = base.WorkflowTest.state
    bundle = reports.ReportTest.bundle
    html = reports.ReportTest.html
    execution = base.WorkflowTest.execution

    def inventory(self):
        return {'config': {'rootDir': str(self.project / 'test/src')},
                'suites': [{'specs': [
                    {'title': 'Synthetic ' + cid, 'file': cid + '.spec.ts',
                     'tests': [{'projectName': 'chromium'}]} for cid in ('C1', 'C2')]}]}

    def ignored(self):
        return mock.patch.object(wf.subprocess, 'run',
                                 return_value=type('GitResult', (), {'returncode': 0, 'stdout': 'test/.cache/\0'})())

    def snapshot(self):
        return {p.relative_to(self.project).as_posix(): p.read_bytes()
                for p in self.project.rglob('*') if p.is_file()}

    def validate(self, event, kind='record', fail=False, owner=None):
        self.write('validate.json', event)
        before = self.snapshot()
        result = self.cli('validate-input', '--type', kind, '--run', self.run,
                          '--owner', owner or self.owner, '--input', str(self.project / 'validate.json'), fail=fail)
        self.assertEqual(before, self.snapshot())
        return result

    def init_attempt(self, bundle='E1', **extra):
        request = {'command': ['pnpm', 'exec', 'playwright', 'test'],
                   'cwd': '.', 'environment': 'synthetic',
                   'scope': ['a'], 'setup_projects': ['auth']}
        self.write('attempt.json', dict(request, **extra))
        return self.cli('attempt-init', '--run', self.run, '--owner', self.owner,
                        '--bundle', bundle, '--input', str(self.project / 'attempt.json'))

    def attempt_evidence(self, retry=False, failed=False):
        bundle, raw = self.bundle(retry=retry)
        raw['config'] = {'rootDir': str(self.project / 'test/src')}
        started = datetime.now(timezone.utc)
        # Keep receipt times genuine with respect to this synthetic preparation, without sleeping.
        with mock.patch.object(wf, 'now', return_value=(started + timedelta(seconds=5)).isoformat()):
            raw['stats']['startTime'] = started.isoformat()
            raw['stats']['duration'] = 2000
            raw['suites'][0]['specs'][0]['file'] = 'C1.spec.ts'
            for result in raw['suites'][0]['specs'][0]['tests'][0]['results']:
                result['startTime'] = started.isoformat()
            if failed:
                raw['stats'].update(expected=0, unexpected=1)
                raw['suites'][0]['specs'][0]['tests'][0].update(status='unexpected')
                raw['suites'][0]['specs'][0]['tests'][0]['results'][0]['status'] = 'failed'
            self.write(bundle['execution_result'], raw)
            self.html(bundle, raw)
            prefix = Path(bundle['execution_result']).parent.parent
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr('report.jsonl', json.dumps({'method': 'onBlobReportMetadata',
                                                             'params': {'version': 2,
                                                                        'userAgent': 'Playwright/1.64.1 synthetic'}}) + '\n')
            (self.project / prefix / 'blob/report.zip').write_bytes(stream.getvalue())
            receipt = {'command': ['pnpm', 'exec', 'playwright', 'test', '--reporter=list,json,html,blob'],
                       'cwd': '.', 'environment': 'synthetic', 'scope': ['a'], 'setup_projects': ['auth'],
                       'started_at': started.isoformat(),
                       'ended_at': (started+timedelta(seconds=3)).isoformat(), 'exit_code': 1 if failed else 0}
            self.write((prefix / 'command.json').as_posix(), receipt)
        return started + timedelta(seconds=5)

    def finish_attempt(self, command='attempt-bundle', fail=False, at=None):
        with mock.patch.object(wf, 'now', return_value=at.isoformat() if at else wf.now()):
            return self.cli(command, '--run', self.run, '--owner', self.owner, '--bundle', 'E1', fail=fail)

    def test_record_validation_replays_batch_without_mutating_state_or_coverage(self):
        self.start()
        events = [{'type': 'source', 'source_id': 'S1', 'freshness_marker': self.marker,
                   'status': 'unchanged', 'observed_revision': '1', 'observed_fingerprint': 'f1',
                   'evidence': ['test/evidence.txt']},
                  {'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'}]
        self.assertFalse(self.validate(events)['committed'])
        self.assertEqual([], self.state()['events'])
        self.record(events)
        self.assertEqual(2, len(self.state()['events']))

    def test_invalid_acceptance_owner_and_source_marker_rejected_without_writes(self):
        self.start()
        result = self.validate({'type': 'acceptance', 'status': 'partially_blocked'}, fail=True)
        self.assertIn('owner', result['error'])
        result = self.validate({'type': 'source', 'source_id': 'S1', 'freshness_marker': 'old',
                                'status': 'unchanged'}, 'source', fail=True)
        self.assertIn('freshness', result['error'])
        self.validate({'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'},
                      fail=True, owner='other')

    def test_validation_does_not_create_a_missing_workflow_lock(self):
        self.start()
        lock = self.project / 'test/.cache/workflow.lock'
        lock.unlink()
        self.validate({'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'})
        self.assertFalse(lock.exists())

    def test_typed_validation_and_delegated_writes_share_record_gates(self):
        self.start()
        self.validate({'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'},
                      'source', fail=True)
        self.write('validate.json', {'type': 'task', 'task_id': 'unassigned', 'owner': 'worker',
                                     'phase': 'code', 'status': 'complete', 'case_ids': ['C1']})
        self.cli('validate-input', '--type', 'record', '--run', self.run, '--owner', self.owner,
                 '--writer', 'worker', '--input', str(self.project / 'validate.json'), fail=True)

    def test_preflight_aggregates_discovery_and_never_executes_tests(self):
        self.write('discovery.json', self.inventory())
        self.write('preflight.json', {'discovery': {'path': 'discovery.json', 'complete': True, 'setup_projects': []}})
        with self.ignored():
            result = self.cli('preflight', '--owner', self.owner, '--input', str(self.project / 'preflight.json'))
        self.run = result['run']['run_id']
        diff = result['coverage_diff']
        self.assertEqual(['C1', 'C2'], diff['matched_cases'])
        self.assertFalse(diff['independent_test_execution_proved'])
        self.assertEqual([], self.state()['events'])
        self.assertEqual('observe', result['sources']['S1']['action'])
        self.assertFalse(result['run']['global_residuals_checked'])

    def test_preflight_missing_inventory_is_unknown_not_complete_coverage(self):
        self.start()
        with self.ignored():
            result = self.cli('preflight', '--run', self.run, '--owner', self.owner)
        self.assertEqual('unknown', result['coverage_diff']['status'])
        self.assertEqual({}, self.state()['source_checks'])

    def test_preflight_returns_created_run_when_supplied_evidence_is_invalid(self):
        self.write('preflight.json', {'discovery': {'path': 'missing.json', 'complete': True, 'setup_projects': []}})
        with self.ignored():
            result = self.cli('preflight', '--owner', self.owner,
                              '--input', str(self.project / 'preflight.json'), fail=True)
        self.run = result['run']['run_id']
        self.assertTrue(result['run_started'])
        self.assertEqual(self.owner, self.state()['claim']['owner'])

    def test_preflight_detects_missing_unmapped_and_duplicate_tests(self):
        self.start()
        raw = self.inventory()
        specs = raw['suites'][0]['specs']
        specs[1]['title'] = 'unknown'
        specs[0]['tests'].append(dict(specs[0]['tests'][0]))
        self.write('discovery.json', raw)
        self.write('preflight.json', {'discovery': {'path': 'discovery.json', 'complete': True, 'setup_projects': []}})
        with self.ignored():
            result = self.cli('preflight', '--run', self.run, '--owner', self.owner,
                              '--input', str(self.project / 'preflight.json'))
        diff = result['coverage_diff']
        self.assertEqual(['C1', 'C2'], diff['missing_tests'])
        self.assertEqual(1, len(diff['unmapped_tests']))
        self.assertTrue(diff['ambiguous_tests'])

    def test_preflight_checks_fresh_manifest_without_approving_or_recording_sources(self):
        self.start()
        started = self.state()['created_at']
        tools = __import__('importlib.util', fromlist=['util'])
        module_spec = tools.spec_from_file_location('tools_source_cache', Path(wf.__file__).with_name('source_cache.py'))
        cache = tools.module_from_spec(module_spec)
        module_spec.loader.exec_module(cache)
        manifest = cache.capture(self.project, {'source_id': 'S1', 'revision': '1', 'fingerprint': 'f1',
                                                'fingerprint_basis': 'canonical_content',
                                                'files': ['test/source-a.txt']})
        self.write('manifest.json', manifest)
        self.write('preflight.json', {'sources': [{'manifest': 'manifest.json',
            'observation': {'source_id': 'S1', 'revision': '1', 'fingerprint': 'f1', 'readable': True,
                            'fingerprint_basis': 'canonical_content', 'observed_at': wf.now(),
                            'evidence': 'test/evidence.txt'}}]})
        with self.ignored():
            result = self.cli('preflight', '--run', self.run, '--owner', self.owner,
                              '--input', str(self.project / 'preflight.json'))
        self.assertEqual('reuse', result['sources']['S1']['action'])
        self.assertTrue(result['sources']['S1']['matches_effective_baseline'])
        self.assertEqual({}, self.state()['source_checks'])
        self.assertEqual(started, self.state()['created_at'])
        self.write('test/source-a.txt', 'tampered cache')
        with self.ignored():
            result = self.cli('preflight', '--run', self.run, '--owner', self.owner,
                              '--input', str(self.project / 'preflight.json'))
        self.assertFalse(result['sources']['S1']['cache_hit'])

    def test_attempt_init_has_no_fake_process_receipt_and_refuses_overwrite(self):
        self.start()
        result = self.init_attempt()
        directory = self.project / Path(result['plan']).parent
        self.assertFalse((directory / 'command.json').exists())
        self.assertNotIn('started_at', wf.load(directory / 'plan.json'))
        before = self.snapshot()
        self.cli('attempt-init', '--run', self.run, '--owner', self.owner, '--bundle', 'E1',
                 '--input', str(self.project / 'attempt.json'), fail=True)
        self.assertEqual(before, self.snapshot())

    def test_attempt_rejects_path_escape_secrets_and_symlinks(self):
        self.start()
        self.init_attempt()
        self.cli('attempt-init', '--run', self.run, '--owner', self.owner, '--bundle', '../escape',
                 '--input', str(self.project / 'attempt.json'), fail=True)
        attempts = self.project / 'test/.cache/runs' / self.run / 'attempts'
        (attempts / 'alias').symlink_to(attempts / 'E1', target_is_directory=True)
        self.cli('attempt-init', '--run', self.run, '--owner', self.owner, '--bundle', 'alias',
                 '--input', str(self.project / 'attempt.json'), fail=True)
        self.write('attempt.json', {'password': 'should-never-be-printed'})
        result = self.cli('attempt-init', '--run', self.run, '--owner', self.owner, '--bundle', 'E2',
                          '--input', str(self.project / 'attempt.json'), fail=True)
        self.assertNotIn('should-never-be-printed', result['error'])

    def test_attempt_bundle_preserves_retries_and_collects_through_existing_gates(self):
        self.start()
        self.check_sources()
        self.init_attempt()
        at = self.attempt_evidence(retry=True)
        before = self.state()
        result = self.finish_attempt(at=at)
        self.assertEqual(before, self.state())
        event = wf.load(self.project / result['input'])
        with mock.patch.object(wf, 'now', return_value=at.isoformat()):
            preview = self.validate(event, 'report_bundle')
            self.assertEqual(2, preview['collection']['events_recorded'])
            self.assertFalse(preview['collection']['committed'])
            collected = self.cli('collect-results', '--run', self.run, '--owner', self.owner,
                                 '--input', str(self.project / result['input']))
        self.assertTrue(collected['collection']['complete'])
        self.assertEqual(['fail', 'pass'], [r['result'] for r in self.state()['executions']])
        self.assertEqual(result['input'], self.finish_attempt(at=at)['input'])

    def test_failed_attempt_is_bundleable_and_missing_blob_is_not(self):
        self.start()
        self.check_sources()
        self.init_attempt()
        at = self.attempt_evidence(failed=True)
        result = self.finish_attempt(at=at)
        event = wf.load(self.project / result['input'])
        self.assertEqual(1, event['bundle']['exit_code'])
        (self.project / event['bundle']['blob_reports'][0]).unlink()
        self.finish_attempt(fail=True, at=at)

    def test_receipt_future_and_changed_configuration_are_rejected(self):
        self.start()
        self.init_attempt()
        at = self.attempt_evidence()
        receipt_path = f'test/.cache/runs/{self.run}/attempts/E1/command.json'
        receipt = wf.load(self.project / receipt_path)
        receipt['ended_at'] = (at+timedelta(days=1)).isoformat()
        self.write(receipt_path, receipt)
        self.finish_attempt(fail=True, at=at)
        spec, _ = wf.assets(self.project)
        spec['env'] = {'name': 'another-env'}
        self.write('test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```\n')
        self.finish_attempt(fail=True, at=at)

    def test_bundle_validation_reports_unresolved_mapping_without_inventing_pass(self):
        self.start()
        self.check_sources()
        self.init_attempt()
        at = self.attempt_evidence()
        raw_path = f'test/.cache/runs/{self.run}/attempts/E1/results/result.json'
        raw = wf.load(self.project / raw_path)
        raw['suites'][0]['specs'][0]['title'] = 'unmapped'
        self.write(raw_path, raw)
        self.html({'html_report': f'test/.cache/runs/{self.run}/attempts/E1/report/playwright/index.html'}, raw)
        result = self.finish_attempt(at=at)
        with mock.patch.object(wf, 'now', return_value=at.isoformat()):
            preview = self.validate(wf.load(self.project / result['input']), 'report_bundle')
        self.assertFalse(preview['collection']['complete'])
        self.assertEqual(0, preview['collection']['events_recorded'])

    def test_timing_unions_parallel_result_intervals_and_preserves_unknowns(self):
        self.start()
        state = self.state()
        start = datetime.now(timezone.utc)-timedelta(seconds=10)
        end = start+timedelta(seconds=10)
        raw_path = f'test/.cache/runs/{self.run}/attempts/time/results/result.json'
        raw = {'suites': [{'specs': [{'tests': [{'results': [
            {'startTime': (start+timedelta(seconds=1)).isoformat(), 'duration': 6000},
            {'startTime': (start+timedelta(seconds=3)).isoformat(), 'duration': 6000}]}]}]}]}
        self.write(raw_path, raw)
        state['created_at'] = start.isoformat()
        state['report_bundles'] = {'time': {'started_at': start.isoformat(), 'ended_at': end.isoformat(),
            'execution_result': raw_path, 'artifact_sha256': {
                raw_path: hashlib.sha256((self.project / raw_path).read_bytes()).hexdigest()}}}
        state['phase_times'] = [
            {'phase': 'exploration', 'started_at': start.isoformat(), 'ended_at': (start+timedelta(seconds=5)).isoformat()},
            {'phase': 'code', 'started_at': (start+timedelta(seconds=2)).isoformat(), 'ended_at': end.isoformat()}]
        self.write(f'test/.cache/runs/{self.run}/state.json', state)
        before = self.snapshot()
        with mock.patch.object(wf, 'now', return_value=end.isoformat()):
            result = self.cli('timing', '--run', self.run)
        self.assertEqual(before, self.snapshot())
        row = result['runners'][0]
        self.assertEqual(12, row['result_duration_sum_seconds'])
        self.assertEqual(8, row['result_interval_union_seconds'])
        self.assertEqual(2, row['unattributed_wall_seconds'])
        self.assertEqual(0, result['workflow']['unknown_wall_seconds'])
        self.assertIsNone(result['session'])
        self.assertIsNone(result['model_cpu_seconds'])
        (self.project / raw_path).unlink()
        result = self.cli('timing', '--run', self.run)
        self.assertIsNone(result['runners'][0]['unattributed_wall_seconds'])

    def test_timing_requires_session_boundary_evidence(self):
        self.start()
        self.write('timing.json', {'session': {'started_at': wf.now(), 'ended_at': wf.now()}})
        self.cli('timing', '--run', self.run, '--input', str(self.project / 'timing.json'), fail=True)

    def test_timing_streams_bounded_host_log_without_emitting_arguments(self):
        self.start()
        start = datetime.now(timezone.utc)-timedelta(seconds=5)
        end = start+timedelta(seconds=4)
        rows = [
            {'timestamp': start.isoformat(), 'type': 'response_item',
             'payload': {'type': 'function_call', 'name': 'exec_command',
                         'call_id': 'tool-1', 'arguments': 'credential-content-not-for-output'}},
            {'timestamp': end.isoformat(), 'type': 'response_item',
             'payload': {'type': 'function_call_output', 'call_id': 'tool-1',
                         'output': 'Process exited with code 2\ncredential-content-not-for-output'}},
            {'timestamp': (start+timedelta(seconds=2)).isoformat(), 'type': 'response_item',
             'payload': {'type': 'custom_tool_call', 'name': 'apply_patch', 'call_id': 'tool-2'}},
            {'timestamp': (end+timedelta(seconds=1)).isoformat(), 'type': 'response_item',
             'payload': {'type': 'function_call', 'name': 'should-not-count'}}]
        self.write('host.jsonl', '\n'.join(json.dumps(row) for row in rows))
        request = {'session': {'started_at': start.isoformat(), 'ended_at': end.isoformat(),
                               'log': 'host.jsonl', 'evidence': ['host.jsonl']}}
        self.write('timing.json', request)
        result = self.cli('timing', '--run', self.run, '--input', str(self.project / 'timing.json'))
        self.assertEqual(2, result['session']['tool_calls'])
        self.assertEqual(1, result['session']['nonzero_tool_results'])
        self.assertNotIn('credential-content-not-for-output', json.dumps(result))
        request['session']['started_at'] = (start+timedelta(microseconds=1)).isoformat()
        self.write('timing.json', request)
        self.cli('timing', '--run', self.run, '--input', str(self.project / 'timing.json'), fail=True)

    def claude_session(self, rows, start, end, host_format=None):
        self.write('host.jsonl', '\n'.join(json.dumps(row) for row in rows))
        session = {'started_at': start.isoformat(), 'ended_at': end.isoformat(),
                   'log': 'host.jsonl', 'evidence': ['host.jsonl']}
        if host_format:
            session['host_format'] = host_format
        self.write('timing.json', {'session': session})
        return self.cli('timing', '--run', self.run, '--input', str(self.project / 'timing.json'))['session']

    def test_timing_counts_claude_code_tools_and_deduplicated_tokens(self):
        self.start()
        start = datetime.now(timezone.utc)-timedelta(seconds=5)
        end = start+timedelta(seconds=4)
        usage = {'input_tokens': 2, 'cache_creation_input_tokens': 10,
                 'cache_read_input_tokens': 100, 'output_tokens': 30}
        # Claude Code writes one row per content block and repeats the message usage on each.
        rows = [{'timestamp': start.isoformat(), 'type': 'user',
                 'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'go'}]}},
                *[{'timestamp': (start+timedelta(seconds=1)).isoformat(), 'type': 'assistant',
                   'message': {'id': 'msg-1', 'role': 'assistant', 'usage': usage,
                               'content': [block]}}
                  for block in ({'type': 'thinking', 'thinking': 'x'},
                                {'type': 'tool_use', 'id': 'tu-1', 'name': 'Bash',
                                 'input': {'command': 'credential-content-not-for-output'}},
                                {'type': 'tool_use', 'id': 'tu-2', 'name': 'Read', 'input': {}})],
                {'timestamp': (start+timedelta(seconds=2)).isoformat(), 'type': 'user',
                 'message': {'role': 'user', 'content': [
                     {'type': 'tool_result', 'tool_use_id': 'tu-1', 'is_error': True,
                      'content': 'Exit code 2\ncredential-content-not-for-output'},
                     {'type': 'tool_result', 'tool_use_id': 'tu-2', 'is_error': True,
                      'content': [{'type': 'text', 'text': 'Permission denied'}]}]}},
                {'timestamp': end.isoformat(), 'type': 'assistant',
                 'message': {'id': 'msg-2', 'role': 'assistant', 'usage': usage,
                             'content': [{'type': 'text', 'text': 'done'}]}},
                {'timestamp': (end+timedelta(seconds=1)).isoformat(), 'type': 'assistant',
                 'message': {'id': 'msg-3', 'usage': usage,
                             'content': [{'type': 'tool_use', 'id': 'tu-3', 'name': 'late'}]}}]
        result = self.claude_session(rows, start, end)
        self.assertEqual('claude_code', result['host_log_format'])
        self.assertEqual({'Bash': 1, 'Read': 1}, result['tool_counts'])
        self.assertEqual(1, result['nonzero_tool_results'])
        self.assertEqual(2, result['error_tool_results'])
        self.assertEqual(2, result['tokens']['assistant_messages'])
        self.assertEqual(60, result['tokens']['output_tokens'])
        self.assertEqual(284, result['tokens']['total_tokens'])
        self.assertNotIn('credential-content-not-for-output', json.dumps(result))
        self.assertEqual('claude_code', self.claude_session(rows, start, end, 'claude_code')['host_log_format'])
        self.assertEqual('unsupported_host_log', self.claude_session(rows, start, end, 'codex')['host_log_status'])

    def test_claude_code_missing_usage_field_keeps_total_unknown(self):
        self.start()
        start = datetime.now(timezone.utc)-timedelta(seconds=3)
        rows = [{'timestamp': start.isoformat(), 'type': 'assistant',
                 'message': {'id': 'msg-1', 'usage': {'input_tokens': 1, 'output_tokens': 2},
                             'content': [{'type': 'text', 'text': 'x'}]}}]
        tokens = self.claude_session(rows, start, start)['tokens']
        self.assertEqual(2, tokens['output_tokens'])
        self.assertIsNone(tokens['cache_read_input_tokens'])
        self.assertIsNone(tokens['total_tokens'])

    def test_timing_source_read_uses_union_instead_of_parallel_sum(self):
        self.start()
        start = datetime.now(timezone.utc)-timedelta(seconds=4)
        for sid, offset in [('S1', 0), ('S2', 1)]:
            self.write(f'test/.cache/runs/{self.run}/sources/{sid}/observation.json', {
                'source_id': sid, 'started_at': (start+timedelta(seconds=offset)).isoformat(),
                'observed_at': (start+timedelta(seconds=offset+2)).isoformat()})
        result = self.cli('timing', '--run', self.run)
        self.assertEqual(3, result['source_read_interval_union_seconds'])
        self.assertEqual(4, sum(v['seconds'] for v in result['source_observations']))
