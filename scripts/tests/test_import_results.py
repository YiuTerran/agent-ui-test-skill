"""Synthetic importer/record integration checks; never execute a real project."""
import test_platform_support as platform_tests
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest

import test_workflow as base
import test_reports

MODULE = Path(__file__).resolve().parents[1] / 'import_results.py'
SPEC = importlib.util.spec_from_file_location('import_results', MODULE)
imp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(imp)
wf = base.wf


class ImportResultsTest(unittest.TestCase):
    tearDown = base.WorkflowTest.tearDown
    write = base.WorkflowTest.write
    cli = base.WorkflowTest.cli
    start = base.WorkflowTest.start
    record = base.WorkflowTest.record
    state = base.WorkflowTest.state
    check_sources = base.WorkflowTest.check_sources

    def setUp(self):
        base.WorkflowTest.setUp(self)
        self.start()
        # These importer fixtures exercise backward-compatible JSON-only runs.
        # New-round blob/merge gates are covered by test_reports.
        state = self.state()
        state['native_merge_required'] = False
        self.write(f'test/.cache/runs/{self.run}/state.json', state)
        self.check_sources()
        self.raw = None
        self.bundle = None

    def target(self, cid='C1', project='chromium', statuses=('passed',), outcome=None, **fields):
        results = [{'status': status, 'startTime': f'2026-01-01T00:00:0{i}Z',
                    'duration': 100, 'retry': i, 'attachments': []}
                   for i, status in enumerate(statuses)]
        if outcome is None:
            outcome = ('skipped' if not statuses or set(statuses) == {'skipped'} else
                       'flaky' if statuses[-1] == 'passed' and len(statuses) > 1 else
                       'expected' if statuses[-1] == 'passed' else 'unexpected')
        test = dict(projectName=project, status=outcome, results=results, **fields)
        return {'file': cid + '.spec.ts', 'title': 'Synthetic ' + cid, 'line': 12, 'column': 3, 'tests': [test]}

    def prepare(self, targets=None, projects=None, nested=False, html=False, suites=None, **bundle_fields):
        targets = targets if targets is not None else [self.target()]
        suite = {'specs': targets}
        self.raw = {'config': {'rootDir': str(self.project / 'test/src'), 'projects': projects or []},
                    'stats': {'startTime': '2026-01-01T00:00:00Z', 'duration': 9000},
                    'errors': [], 'suites': suites if suites is not None else (
                        [{'suites': [suite]}] if nested else [suite])}
        pairs = wf.report_artifacts.json_tests(self.raw)
        for outcome in imp.OUTCOMES:
            self.raw['stats'][outcome] = sum(t['status'] == outcome for _, t in pairs)
        prefix = f'test/.cache/runs/{self.run}/attempts/B1'
        self.bundle = {'bundle_id': 'B1', 'run_id': self.run, 'command': ['synthetic-playwright', 'test'],
                       'cwd': '.', 'environment': 'synthetic-only', 'scope': ['a', 'b'],
                       'setup_projects': ['auth'], 'exit_code': 1 if any(
                           t['status'] == 'unexpected' for _, t in pairs) else 0,
                       'started_at': '2026-01-01T00:00:00Z', 'ended_at': '2026-01-01T00:00:10Z',
                       'execution_result': prefix + '/results/result.json',
                       'html_report': prefix + '/report/index.html' if html else None,
                       'html_origin': 'same_execution' if html else 'none'}
        self.bundle.update(bundle_fields)
        self.write(self.bundle['execution_result'], self.raw)
        if html:
            test_reports.ReportTest.html(self, self.bundle, self.raw)
        self.record({'type': 'report_bundle', 'bundle': self.bundle})
        return self.bundle

    def result(self):
        result = imp.import_results(self.project, self.run, 'B1')
        counts = result['counts']
        self.assertEqual(counts['raw_slots'], counts['mapped'] + counts['unresolved'] + counts['already_registered'])
        self.assertEqual(counts['mapped'], len(result['events']))
        self.assertEqual(counts['unresolved'], len(result['unresolved']))
        for key in ('raw_slots', 'mapped', 'unresolved', 'already_registered'):
            self.assertEqual(counts[key], counts['setup'][key] + counts['business'][key])
        return result

    def coverage(self, change):
        path = self.project / 'test/plans/coverage.json'
        cov = wf.load(path)
        change(cov)
        self.write('test/plans/coverage.json', cov)
        state = self.state()
        state['asset_fingerprint'] = wf.fingerprint(cov)
        self.write(f'test/.cache/runs/{self.run}/state.json', state)

    def importer_cli(self, *extra, code=0):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            actual = imp.main(['--project', str(self.project), '--run', self.run, '--bundle', 'B1', *extra])
        value = json.loads(buffer.getvalue())
        self.assertEqual(code, actual, value)
        return value

    def test_pass_exact_match_and_read_only(self):
        self.prepare()
        before = {p: p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        result = self.result()
        self.assertEqual(before, {p: p.read_bytes() for p in self.project.rglob('*') if p.is_file()})
        event = result['events'][0]
        self.assertEqual(['C1'], event['case_ids'])
        self.assertEqual('pass', event['result'])
        self.assertEqual('passed', event['runner_status'])
        self.assertEqual('2026-01-01T00:00:00Z', event['started_at'])
        self.assertEqual('2026-01-01T00:00:00.100000+00:00', event['ended_at'])
        self.record(result['events'])
        consistency = wf.report_consistency(self.project, self.state())
        self.assertEqual('checked', consistency['status'], consistency)

    def test_setup_designation_uses_bundle_not_title(self):
        auth = self.target('unknown', 'auth')
        auth['title'] = 'ordinary business-looking title'
        business = self.target()
        business['title'] = 'authentication setup'
        self.coverage(lambda c: c['cases']['C1'].update(title=business['title']))
        self.prepare([business, auth])
        result = self.result()
        self.assertEqual([True, False], [e['setup'] for e in result['events']])
        self.assertEqual([], result['events'][0]['case_ids'])
        self.assertEqual(1, result['counts']['setup']['mapped'])
        self.assertEqual(1, result['counts']['business']['mapped'])
        self.record(result['events'])

    def test_retry_repeat_nested_slots_and_monotonic_attempts(self):
        target = self.target(statuses=('failed', 'passed'), repeatEachIndex=0)
        target['tests'].extend(self.target(repeatEachIndex=1)['tests'])
        self.prepare([target], nested=True)
        events = self.result()['events']
        self.assertEqual(['fail', 'pass', 'pass'], [e['result'] for e in events])
        self.assertEqual([0, 1, 0], [e['runner_retry'] for e in events])
        self.assertEqual([0, 0, 1], [e['runner_repeat_each_index'] for e in events])
        self.assertEqual([1, 2, 3], [e['attempt'] for e in events])
        self.assertEqual(3, len({e['execution_id'] for e in events}))
        self.assertTrue(all('/suites/0/suites/0/' in e['runner_test_pointer'] for e in events))
        self.record(events)
        consistency = wf.report_consistency(self.project, self.state())
        self.assertEqual('checked', consistency['status'], consistency)

    def test_many_repeats_use_numeric_pointer_order(self):
        target = self.target()
        target['tests'] = [self.target(repeatEachIndex=i)['tests'][0] for i in range(12)]
        self.prepare([target])
        events = self.result()['events']
        self.assertEqual(list(range(12)), [e['runner_repeat_each_index'] for e in events])
        self.assertEqual(list(range(1, 13)), [e['attempt'] for e in events])
        self.record(events)

    def test_same_short_title_in_distinct_describes_is_not_a_repeat(self):
        self.prepare(suites=[{'title': 'describe-a', 'specs': [self.target()]},
                             {'title': 'describe-b', 'specs': [self.target()]}])
        result = self.result()
        self.assertEqual([], result['events'])
        self.assertEqual(2, result['counts']['unresolved'])
        self.assertEqual({'ambiguous_runner_spec_identity'},
                         {item['reason'] for item in result['unresolved']})

    def test_raw_attempt_and_explicit_end_time_are_preserved_separately(self):
        self.record(base.WorkflowTest.execution(self, 'previous', attempt=7))
        target = self.target()
        target['tests'][0]['results'][0].update(attempt=3, endTime='2026-01-01T00:00:00.100Z')
        self.prepare([target])
        event = self.result()['events'][0]
        self.assertEqual(3, event['runner_attempt'])
        self.assertEqual(8, event['attempt'])
        self.assertEqual('2026-01-01T00:00:00.100Z', event['ended_at'])
        self.record([event])

    def test_conflicting_end_time_and_invalid_attempt_are_unresolved(self):
        target = self.target()
        target['tests'][0]['results'][0]['endTime'] = '2026-01-01T00:00:09Z'
        invalid = self.target('C2')
        invalid['tests'][0]['results'][0]['attempt'] = False
        self.prepare([target, invalid])
        result = self.result()
        self.assertEqual(2, result['counts']['unresolved'])
        self.assertEqual([], result['events'])

    def test_original_fail_statuses_including_expected_failure(self):
        for status in ('failed', 'timedOut', 'interrupted'):
            with self.subTest(status=status):
                test = self.target(statuses=(status,), outcome='expected')
                test['tests'][0]['expectedStatus'] = 'failed'
                # Reuse one registered invocation only inside each isolated fixture.
                self.raw = None
                state = self.state()
                state['report_bundles'] = {}
                self.write(f'test/.cache/runs/{self.run}/state.json', state)
                self.prepare([test])
                event = self.result()['events'][0]
                self.assertEqual('fail', event['result'])
                self.assertEqual(status, event['runner_status'])
                self.assertEqual('expected', event['runner_test_status'])

    def test_structured_failure_details_keep_zero_false_and_location(self):
        target = self.target(statuses=('failed',))
        target['tests'][0]['results'][0]['errors'] = [{
            'matcherResult': {'expected': 0, 'actual': False},
            'location': {'file': str(self.project / 'test/src/C1.spec.ts'), 'line': 44, 'column': 2},
            'message': 'unstructured diagnostics are never copied', 'stack': 'private raw stack'}]
        self.prepare([target])
        events = self.result()['events']
        detail = events[0]['failure_details']
        self.assertEqual('0', detail['expected'])
        self.assertEqual('false', detail['actual'])
        self.assertEqual('test/src/C1.spec.ts:44:2', detail['location'])
        self.assertEqual('not_verified', detail['cleanup'])
        self.assertEqual('needs_decision', detail['classification'])
        self.assertNotIn('private raw stack', json.dumps(events))
        self.record(events)

    def test_structured_error_expected_actual_outside_matcher_are_preserved(self):
        target = self.target(statuses=('failed',))
        target['tests'][0]['results'][0]['error'] = {
            'expected': {'count': 2}, 'actual': {'count': 1},
            'location': {'file': 'C1.spec.ts', 'line': 25}}
        self.prepare([target])
        event = self.result()['events'][0]
        detail = event['failure_details']
        self.assertEqual({'count': 2}, json.loads(detail['expected']))
        self.assertEqual({'count': 1}, json.loads(detail['actual']))
        self.assertEqual('test/src/C1.spec.ts:25', detail['location'])
        self.record([event])

    def test_diagnostic_redaction_no_auth_response_credentials_or_attachment_body(self):
        auth = self.target('auth', 'auth', ('failed',))
        business = self.target(statuses=('failed',))
        for target in (auth, business):
            target['tests'][0]['results'][0].update(
                errors=[{'message': '401 Authentication response: opaque-private-body',
                         'matcherResult': {'expected': 'plain-secret-in-response',
                                           'actual': {'access_token': 'fixture-secret', 'data': 'opaque-private-body'}}}],
                stdout=[{'text': 'stdout-private-body'}],
                attachments=[{'name': 'private', 'body': 'base64-private-body'}])
        self.prepare([auth, business])
        result = self.result()
        text = json.dumps(result)
        for private in ('opaque-private-body', 'plain-secret-in-response', 'fixture-secret',
                        'stdout-private-body', 'base64-private-body', 'access_token', 'Authentication response'):
            self.assertNotIn(private, text)
        self.assertTrue(all(e['failure_details']['actual'] == imp.REDACTED for e in result['events']))
        self.record(result['events'])

    def test_safe_diagnostic_data_and_labelled_secret_echo(self):
        target = self.target(statuses=('failed',))
        target['tests'][0]['results'][0]['errors'] = [{'matcherResult': {
            'expected': {'count': 2, 'values': ['Saved', 'https://user:pass@example.test/a?key=private']},
            'actual': 'opaque-labelled-private'}}]
        self.prepare([target])
        # Label a credential in config without including it in the exported fields.
        self.raw['config']['metadata'] = {'password': 'opaque-labelled-private'}
        self.write(self.bundle['execution_result'], self.raw)
        saved = self.state()
        saved['report_bundles']['B1'] = wf.report_artifacts.inspect_bundle(self.project, self.run, self.bundle)
        self.write(f'test/.cache/runs/{self.run}/state.json', saved)
        result = self.result()
        detail = result['events'][0]['failure_details']
        self.assertIn('Saved', detail['expected'])
        self.assertIn('[URL REDACTED]', detail['expected'])
        self.assertEqual(imp.REDACTED, detail['actual'])
        self.assertNotIn('user:pass', json.dumps(result))
        self.assertNotIn('opaque-labelled-private', json.dumps(result))

    def test_explicit_skip_remains_skip_despite_failed_setup_and_dependency(self):
        target = self.target(statuses=('skipped',), annotations=[{'type': 'skip', 'description': 'explicit'}])
        self.prepare([self.target('auth', 'auth', ('failed',)), target], projects=[
            {'name': 'auth'}, {'name': 'chromium', 'dependencies': ['auth']}])
        events = self.result()['events']
        self.assertEqual('skip', events[1]['result'])
        self.assertNotIn('blocked_by', events[1])
        self.record(events)

    def test_no_result_invocation_window_is_explicit_and_counted(self):
        self.prepare([self.target(statuses=())])
        event = self.result()['events'][0]
        self.assertIsNone(event['runner_result_index'])
        self.assertIsNone(event['runner_status'])
        self.assertIsNone(event['runner_retry'])
        self.assertEqual('invocation_window', event['timing_source'])
        self.assertEqual(self.bundle['ended_at'], event['ended_at'])
        self.record([event])

    def test_no_result_non_skip_is_unresolved_not_dropped(self):
        self.prepare([self.target(statuses=(), outcome='unexpected')])
        result = self.result()
        self.assertEqual([], result['events'])
        self.assertEqual('no_result_for_non_skipped_target', result['unresolved'][0]['reason'])
        self.assertEqual(1, result['counts']['raw_slots'])

    def test_dependency_skip_and_independent_business_after_failed_setup(self):
        self.coverage(lambda c: c['cases']['C2'].update(project='independent'))
        self.prepare([self.target(statuses=()), self.target('C2', 'independent', ('skipped',)),
                      self.target('auth', 'auth', ('failed',))], projects=[
            {'name': 'auth', 'id': 'setup-id'},
            {'name': 'chromium', 'dependencies': ['setup-id']},
            {'name': 'independent', 'dependencies': []}])
        result = self.result()
        events = result['events']
        self.assertEqual(['fail', 'notrun', 'skip'], [e['result'] for e in events])
        self.assertEqual(events[0]['execution_id'], events[1]['blocked_by'])
        self.assertEqual('skip', events[1]['runner_result'])
        self.assertNotIn('blocked_by', events[2])
        self.assertEqual(1, result['counts']['dependency_blocked'])
        self.record(events)

    def test_failed_setup_does_not_block_independent_pass_or_unknown_dependency_skip(self):
        self.prepare([self.target('auth', 'auth', ('failed',)), self.target(),
                      self.target('C2', statuses=('skipped',))])
        events = self.result()['events']
        self.assertEqual(['fail', 'pass', 'skip'], [e['result'] for e in events])
        self.assertTrue(all('blocked_by' not in e for e in events))
        self.record(events)

    def test_transitive_dependencies_supported_and_cycle_terminates(self):
        self.prepare([self.target(statuses=('skipped',)), self.target('auth', 'auth', ('failed',))], projects=[
            {'name': 'auth'}, {'name': 'middle', 'dependencies': ['auth', 'chromium']},
            {'name': 'chromium', 'dependencies': ['middle']}])
        self.assertEqual('notrun', self.result()['events'][1]['result'])

    def test_failed_retry_then_successful_setup_does_not_block_skip(self):
        self.prepare([self.target('auth', 'auth', ('failed', 'passed')), self.target(statuses=('skipped',))],
                     projects=[{'name': 'auth'}, {'name': 'chromium', 'dependencies': ['auth']}])
        events = self.result()['events']
        self.assertEqual(['fail', 'pass', 'skip'], [e['result'] for e in events])
        self.assertNotIn('blocked_by', events[-1])

    def test_expected_setup_failure_does_not_block_skip(self):
        self.prepare([self.target('auth', 'auth', ('failed',), outcome='expected'),
                      self.target(statuses=('skipped',))], projects=[
            {'name': 'auth'}, {'name': 'chromium', 'dependencies': ['auth']}])
        self.assertEqual('skip', self.result()['events'][-1]['result'])

    def test_ambiguous_dependency_name_is_not_evidence(self):
        self.prepare([self.target('auth', 'auth', ('failed',)), self.target(statuses=('skipped',))], projects=[
            {'name': 'auth', 'id': 'one'}, {'name': 'auth', 'id': 'two'},
            {'name': 'chromium', 'dependencies': ['auth']}])
        self.assertEqual('skip', self.result()['events'][-1]['result'])

    def test_explicit_slot_binding_requires_tracked_evidence(self):
        raw_path = f'test/.cache/runs/{self.run}/attempts/B1/results/result.json'
        binding = {'runner_test_pointer': '/suites/0/specs/0/tests/0', 'runner_result_index': 0,
                   'setup_test_pointer': '/suites/0/specs/1/tests/0', 'setup_result_index': 0,
                   'evidence': [raw_path]}
        self.prepare([self.target(statuses=('skipped',)), self.target('auth', 'auth', ('failed',))],
                     dependency_bindings=[binding])
        events = self.result()['events']
        self.assertEqual('notrun', events[1]['result'])
        self.assertEqual([raw_path], events[1]['dependency_evidence'])
        self.record(events)

    def test_invalid_binding_is_unresolved(self):
        self.prepare([self.target(statuses=('skipped',)), self.target('auth', 'auth', ('failed',))],
                     dependency_bindings=[{
                         'runner_test_pointer': '/suites/0/specs/0/tests/0', 'runner_result_index': 0,
                         'setup_test_pointer': '/suites/0/specs/1/tests/0', 'setup_result_index': 0,
                         'evidence': ['test/evidence.txt']}])
        result = self.result()
        self.assertEqual(1, len(result['events']))
        self.assertEqual('invalid_dependency_binding', result['unresolved'][0]['reason'])

    def test_dependency_uses_previously_registered_setup_execution_id(self):
        self.prepare([self.target('auth', 'auth', ('failed',)), self.target(statuses=('skipped',))], projects=[
            {'name': 'auth'}, {'name': 'chromium', 'dependencies': ['auth']}])
        setup = self.result()['events'][0]
        setup['execution_id'] = 'existing-auth-receipt'
        self.record([setup])
        result = self.result()
        self.assertEqual(1, result['counts']['already_registered'])
        self.assertEqual('existing-auth-receipt', result['events'][0]['blocked_by'])
        self.record(result['events'])

    def test_unavailable_setup_receipt_does_not_create_dangling_reference(self):
        auth = self.target('auth', 'auth', ('failed',))
        del auth['tests'][0]['results'][0]['startTime']
        self.prepare([auth, self.target(statuses=('skipped',))], projects=[
            {'name': 'auth'}, {'name': 'chromium', 'dependencies': ['auth']}])
        result = self.result()
        self.assertEqual([], result['events'])
        self.assertEqual({'invalid_result_timing_or_retry', 'dependency_blocker_unresolved'},
                         {e['reason'] for e in result['unresolved']})

    def test_unknown_mapping_includes_each_retry_in_unresolved(self):
        self.prepare([self.target('C-unknown', statuses=('failed', 'passed'))])
        result = self.result()
        self.assertEqual(2, result['counts']['business']['unresolved'])
        self.assertEqual([0, 1], [u['runner_result_index'] for u in result['unresolved']])

    def test_no_basename_title_substring_or_case_id_guess(self):
        targets = [self.target() for _ in range(3)]
        targets[0]['file'] = 'other/C1.spec.ts'
        targets[1]['title'] = 'prefix Synthetic C1'
        targets[2]['title'] = 'C1'
        self.prepare(targets)
        self.assertEqual(3, self.result()['counts']['unresolved'])

    def test_unique_exact_project_mapping_and_wrong_project_unresolved(self):
        self.coverage(lambda c: c['cases']['C1'].update(project='chromium'))
        self.prepare([self.target(), self.target(project='firefox')])
        result = self.result()
        self.assertEqual(1, result['counts']['mapped'])
        self.assertEqual('no_exact_coverage_match', result['unresolved'][0]['reason'])

    def test_unspecified_coverage_project_cannot_guess_among_runner_projects(self):
        self.prepare([self.target(), self.target(project='firefox')])
        result = self.result()
        self.assertEqual(2, result['counts']['unresolved'])
        self.assertEqual({'coverage_project_required'}, {u['reason'] for u in result['unresolved']})

    def test_ambiguous_and_legacy_mappings_explicitly_unresolved(self):
        self.coverage(lambda c: c['cases'].update(C3=copy.deepcopy(c['cases']['C1'])))
        self.prepare()
        self.assertEqual('ambiguous_coverage_match', self.result()['unresolved'][0]['reason'])
        self.coverage(lambda c: (c['cases'].pop('C3'), c['cases']['C1'].update(legacy_group=True)))
        self.assertEqual('legacy_group_requires_explicit_review', self.result()['unresolved'][0]['reason'])

    def test_file_and_project_name_aliases_are_exact(self):
        self.coverage(lambda c: c['cases']['C1'].update(file='test/src/C1.spec.ts', projectName='chromium'))
        self.prepare()
        self.assertEqual(1, self.result()['counts']['mapped'])

    def test_retired_out_of_scope_shared_test_id_unsafe_identity_unresolved(self):
        self.prepare()
        changes = [(dict(implementation='retired'), 'retired_case'),
                   (dict(scope='outside'), 'case_outside_bundle_scope'),
                   (dict(test_id='C2'), 'missing_or_shared_test_id'),
                   (dict(test_id='token=private'), 'unsafe_coverage_identity')]
        original = wf.load(self.project / 'test/plans/coverage.json')['cases']['C1']
        for change, expected in changes:
            with self.subTest(expected=expected):
                self.coverage(lambda c: c['cases'].update(C1=dict(original, **change)))
                self.assertEqual(expected, self.result()['unresolved'][0]['reason'])

    def test_absolute_files_inside_project_without_root_dir(self):
        target = self.target()
        target['file'] = str(self.project / 'test/src/C1.spec.ts')
        self.prepare([target])
        self.assertEqual(1, self.result()['counts']['mapped'])

    def test_deterministic_reimport_and_partial_then_complete_idempotency(self):
        self.prepare([self.target(statuses=('failed', 'passed'))])
        first = self.result()
        self.assertEqual(first, self.result())
        self.record(first['events'][:1])
        partial = self.result()
        self.assertEqual(first['events'][1:], partial['events'])
        self.assertEqual(1, partial['counts']['already_registered'])
        self.record(partial['events'])
        final = self.result()
        self.assertEqual([], final['events'])
        self.assertEqual([], final['unresolved'])
        self.assertEqual(2, final['counts']['already_registered'])

    def test_existing_attempt_from_another_bundle_is_respected(self):
        self.record(base.WorkflowTest.execution(self, 'old', attempt=7))
        self.prepare()
        event = self.result()['events'][0]
        self.assertEqual(8, event['attempt'])
        self.record([event])

    def test_import_does_not_approve_historical_unreviewed_case(self):
        self.coverage(lambda c: c['cases']['C1'].update(review='pending'))
        self.prepare()
        result = self.result()
        self.assertEqual(1, len(result['events']))
        before = copy.deepcopy(self.state())
        rejection = self.record(result['events'], fail=True)
        self.assertIn('reviewed', rejection['error'])
        self.assertEqual(before, self.state())
        self.assertNotIn('acceptance_versions', result['events'][0])

    def test_current_source_gate_is_still_enforced(self):
        self.prepare()
        state = self.state()
        state['source_checks'] = {}
        self.write(f'test/.cache/runs/{self.run}/state.json', state)
        result = self.result()
        self.assertIn('blocked case', self.record(result['events'], fail=True)['error'])

    def test_tampered_json_html_or_attachment_is_rejected(self):
        self.prepare(html=True)
        self.assertEqual(1, self.result()['counts']['mapped'])
        path = self.project / self.bundle['html_report']
        path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        with self.assertRaises(imp.ImportError):
            self.result()
        self.assertEqual({'error': 'invalid_input_or_bundle_integrity'}, self.importer_cli(code=2))

    def test_tampered_raw_json_is_rejected_even_when_valid_json(self):
        self.prepare()
        path = self.project / self.bundle['execution_result']
        path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        with self.assertRaises(imp.ImportError):
            self.result()

    def test_tracked_attachment_mutation_or_policy_removal_is_rejected(self):
        path = f'test/.cache/runs/{self.run}/attempts/B1/results/trace.zip'
        self.write(path, 'synthetic trace, not real product evidence')
        target = self.target()
        target['tests'][0]['results'][0]['attachments'] = [{'name': 'trace', 'path': path}]
        self.prepare([target])
        self.assertEqual(1, self.result()['counts']['mapped'])
        self.write(path, 'changed')
        with self.assertRaises(imp.ImportError):
            self.result()
        (self.project / path).unlink()
        state = self.state()
        state['acceptance'] = 'completed_with_failures'
        self.write(f'test/.cache/runs/{self.run}/state.json', state)
        with self.assertRaises(imp.ImportError):
            self.result()

    def test_missing_raw_after_terminal_run_is_not_imported_from_old_receipts(self):
        self.prepare()
        self.record(self.result()['events'])
        state = self.state()
        state['acceptance'] = 'fully_accepted'
        self.write(f'test/.cache/runs/{self.run}/state.json', state)
        (self.project / self.bundle['execution_result']).unlink()
        with self.assertRaises(imp.ImportError):
            self.result()

    def test_project_path_alias_and_canonical_path_produce_identical_candidates(self):
        self.prepare()
        canonical = self.project.resolve()
        expected = imp.import_results(canonical, self.run, 'B1')
        self.assertEqual(expected, imp.import_results(self.project, self.run, 'B1'))
        alias = self.project / 'project-alias'
        platform_tests.symlink(self, alias, canonical, target_is_directory=True)
        self.assertEqual(expected, imp.import_results(alias, self.run, 'B1'))
        completed = subprocess.run([sys.executable, str(MODULE), '--project', str(alias),
                                    '--run', self.run, '--bundle', 'B1'], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(expected, json.loads(completed.stdout))

    def test_state_symlink_outside_project_is_rejected(self):
        self.prepare()
        path = self.project / f'test/.cache/runs/{self.run}/state.json'
        destination = self.project.parent / (self.project.name + '-external-state.json')
        try:
            destination.write_bytes(path.read_bytes())
            path.unlink()
            platform_tests.symlink(self, path, destination)
            with self.assertRaisesRegex(imp.ImportError, 'input_path_outside_project'):
                self.result()
        finally:
            destination.unlink(missing_ok=True)

    def test_missing_registered_bundle_invalid_run_and_path_escape(self):
        with self.assertRaisesRegex(imp.ImportError, 'bundle_not_registered'):
            self.result()
        for run, bundle in (('../escape', 'B1'), (self.run, '../../B1')):
            with self.assertRaisesRegex(imp.ImportError, 'invalid_run_or_bundle_id'):
                imp.import_results(self.project, run, bundle)

    def test_registered_slot_is_validated_before_exclusion(self):
        self.prepare()
        event = self.result()['events'][0]
        event['result'] = 'fail'
        state = self.state()
        state['executions'].append(event)
        self.write(f'test/.cache/runs/{self.run}/state.json', state)
        with self.assertRaises(imp.ImportError):
            self.result()

    def test_duplicate_registered_slot_is_not_silently_deduplicated(self):
        self.prepare()
        event = self.result()['events'][0]
        self.record([event])
        state = self.state()
        state['executions'].append(dict(event, execution_id='duplicate', attempt=2))
        self.write(f'test/.cache/runs/{self.run}/state.json', state)
        with self.assertRaisesRegex(imp.ImportError, 'invalid_registered_slot'):
            self.result()

    def test_unknown_status_and_missing_timing_are_counted_unresolved(self):
        unknown = self.target()
        unknown['tests'][0]['results'][0]['status'] = 'unknown-private-status'
        missing = self.target('C2')
        del missing['tests'][0]['results'][0]['duration']
        self.prepare([unknown, missing])
        result = self.result()
        self.assertEqual(2, result['counts']['unresolved'])
        self.assertNotIn('unknown-private-status', json.dumps(result))

    def test_empty_report_has_balanced_zero_counts(self):
        self.prepare([])
        self.assertEqual(0, self.result()['counts']['raw_slots'])

    def test_cli_stdout_output_file_exclusive_and_python_entrypoint(self):
        self.prepare()
        expected = self.result()
        output = self.project / 'candidates.json'
        self.assertEqual(expected, self.importer_cli('--output', str(output)))
        self.assertEqual(expected, json.loads(output.read_text(encoding='utf-8')))
        saved = output.read_bytes()
        self.importer_cli('--output', str(output), code=2)
        self.assertEqual(saved, output.read_bytes())
        completed = subprocess.run([sys.executable, str(MODULE), '--project', str(self.project),
                                    '--run', self.run, '--bundle', 'B1'], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(expected, json.loads(completed.stdout))
        self.assertEqual('', completed.stderr)

    def test_cli_cannot_overwrite_state_coverage_or_raw(self):
        self.prepare()
        for relative in (f'test/.cache/runs/{self.run}/state.json', 'test/plans/coverage.json',
                         self.bundle['execution_result']):
            path = self.project / relative
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            self.importer_cli('--output', str(path), code=2)
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_errors_never_echo_raw_paths_or_cli_secret_arguments(self):
        self.prepare()
        state = self.state()
        state['report_bundles']['B1']['execution_result'] = 'token=do-not-print/missing.json'
        self.write(f'test/.cache/runs/{self.run}/state.json', state)
        self.assertNotIn('do-not-print', json.dumps(self.importer_cli(code=2)))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(2, imp.main(['--unexpected=secret-do-not-print']))
        self.assertNotIn('secret-do-not-print', buffer.getvalue())


if __name__ == '__main__':
    unittest.main()
