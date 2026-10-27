"""Report provenance and readability checks using synthetic runner artifacts."""
import base64
import copy
from datetime import datetime
import io
import json
from pathlib import Path
import unittest
from unittest import mock
import zipfile

import test_workflow as base

wf = base.wf


class ReportTest(unittest.TestCase):
    setUp = base.WorkflowTest.setUp
    tearDown = base.WorkflowTest.tearDown
    write = base.WorkflowTest.write
    cli = base.WorkflowTest.cli
    start = base.WorkflowTest.start
    record = base.WorkflowTest.record
    check_sources = base.WorkflowTest.check_sources
    execution = base.WorkflowTest.execution
    report = base.WorkflowTest.report
    state = base.WorkflowTest.state

    def start(self, *args):
        result = base.WorkflowTest.start(self, *args)
        state = self.state()
        state['native_merge_required'] = False
        self.write(f'test/.cache/runs/{self.run}/state.json', state)
        return result

    def bundle(self, html=True, retry=False):
        prefix = f'test/.cache/runs/{self.run}/attempts/E1'
        raw = {'stats': {'startTime': '2026-01-01T00:00:00Z', 'duration': 1000,
                         'expected': 1, 'unexpected': 0, 'skipped': 0, 'flaky': 0},
               'errors': [],
               'suites': [{'specs': [{'title': 'Synthetic C1', 'file': 'C1.spec.ts',
                                      'tests': [{'projectName': 'chromium', 'status': 'expected',
                                                 'results': [{'startTime': '2026-01-01T00:00:00Z',
                                                              'retry': 0, 'duration': 1000, 'status': 'passed',
                                                              'attachments': []}]}]}]}]}
        if retry:
            raw['stats'].update(expected=0, flaky=1)
            test = raw['suites'][0]['specs'][0]['tests'][0]
            test['status'] = 'flaky'
            test['results'].insert(0, dict(test['results'][0], status='failed'))
            test['results'][1]['retry'] = 1
        self.write(prefix + '/results/result.json', raw)
        bundle = {'bundle_id': 'E1', 'run_id': self.run, 'command': ['synthetic-playwright', 'test'],
                  'cwd': '.', 'environment': 'synthetic', 'scope': ['a'], 'setup_projects': ['auth'],
                  'started_at': '2026-01-01T00:00:00Z', 'ended_at': '2026-01-01T00:00:02Z', 'exit_code': 0,
                  'execution_result': prefix + '/results/result.json',
                  'html_report': prefix + '/report/playwright/index.html' if html else None,
                  'html_origin': 'same_execution' if html else 'none'}
        if html:
            self.html(bundle, raw)
        return bundle, raw

    def html(self, bundle, raw, mutate=None):
        stats = raw['stats']
        tests = [{'title': spec['title'], 'projectName': test['projectName'],
                  'outcome': test['status'], 'location': {'file': spec['file']},
                  'results': copy.deepcopy(test.get('results', []))}
                 for spec, test in wf.report_artifacts.json_tests(raw)]
        index = {'stats': dict(stats, total=len(tests)),
                 'startTime': datetime.fromisoformat(stats['startTime'].replace('Z', '+00:00')).timestamp() * 1000,
                 'duration': stats['duration'], 'files': [{'fileId': 'fixture'}]}
        if mutate:
            mutate(index, tests)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('report.json', json.dumps(index))
            archive.writestr('fixture.json', json.dumps({'tests': tests}))
        self.write(bundle['html_report'], '<script>data:application/zip;base64,' +
                   base64.b64encode(buffer.getvalue()).decode() + '</script>')

    def register(self, bundle, fail=False):
        return self.record({'type': 'report_bundle', 'bundle': bundle}, fail=fail)

    def bound(self, **extra):
        return self.execution('X1', bundle_id='E1', runner_test_pointer='/suites/0/specs/0/tests/0',
                              runner_result_index=0, **extra)

    def prepare(self, html=True):
        self.start()
        self.check_sources()
        return self.bundle(html)

    def collectable(self, retry=False):
        bundle, raw = self.bundle(retry=retry)
        raw['config'] = {'rootDir': str(self.project / 'test/src')}
        self.write(bundle['execution_result'], raw)
        self.html(bundle, raw)
        return bundle, raw

    def collect(self, bundle, fail=False, owner=None, writer=None):
        self.write('collect.json', {'type': 'report_bundle', 'bundle': bundle})
        args = ['--run', self.run, '--owner', owner or self.owner,
                '--input', str(self.project / 'collect.json')]
        if writer:
            args += ['--writer', writer]
        return self.cli('collect-results', *args, fail=fail)

    def test_collect_default_output_is_brief_without_historical_scan(self):
        self.start()
        self.check_sources()
        bundle, _ = self.collectable()
        with mock.patch.object(wf, 'global_residuals', side_effect=AssertionError('no history scan')):
            result = self.collect(bundle)
        self.assertTrue(result['collection']['complete'])
        self.assertFalse(result['global_residuals_checked'])
        self.assertNotIn('regression_inventory', result)

    def test_collect_registers_all_retries_atomically_and_is_idempotent(self):
        self.start()
        self.check_sources()
        bundle, _ = self.collectable(retry=True)
        result = self.collect(bundle)['collection']
        self.assertTrue(result['committed'])
        self.assertTrue(result['complete'])
        self.assertEqual(2, result['events_recorded'])
        summary = self.report()
        self.assertEqual('checked', summary['report_consistency']['status'])
        self.assertEqual(1, summary['counts']['execution_results']['fail'])
        self.assertEqual(1, summary['counts']['execution_results']['pass'])
        before = self.state()
        self.assertEqual(0, self.collect(bundle)['collection']['events_recorded'])
        after = self.state()
        self.assertEqual(before['executions'], after['executions'])
        self.assertEqual(before['events'], after['events'])

    def test_collect_preserves_unmapped_results_for_strict_final_check(self):
        self.start()
        self.check_sources()
        bundle, raw = self.collectable()
        raw['suites'][0]['specs'][0]['title'] = 'Unmapped synthetic test'
        self.write(bundle['execution_result'], raw)
        self.html(bundle, raw)
        result = self.collect(bundle)['collection']
        self.assertFalse(result['complete'])
        self.assertTrue(result['unresolved'])
        self.assertEqual(0, result['events_recorded'])
        self.assertIn('not fully mapped', str(self.cli(
            'report', '--run', self.run, '--owner', self.owner, fail=True)))

    def test_collect_gate_failure_leaves_no_partial_bundle_or_events(self):
        self.start()
        bundle, _ = self.collectable()
        before = self.state()
        self.collect(bundle, fail=True)
        self.assertEqual(before, self.state())
        self.check_sources()
        self.record({'type': 'human', 'request_id': 'H1', 'status': 'waiting',
                     'mode': 'explicit_decision', 'case_ids': ['C1']})
        before = self.state()
        self.collect(bundle, fail=True)
        self.assertEqual(before, self.state())

    def test_collect_rejects_foreign_owner_or_delegated_writer(self):
        self.start()
        self.check_sources()
        bundle, _ = self.collectable()
        before = self.state()
        self.collect(bundle, fail=True, owner='other')
        self.collect(bundle, fail=True, writer='worker')
        self.assertEqual(before, self.state())

    def test_collect_rejects_mutated_registration_and_artifacts_without_commit(self):
        self.start()
        self.check_sources()
        bundle, _ = self.collectable()
        self.collect(bundle)
        before = self.state()
        self.collect(dict(bundle, command=['other-command']), fail=True)
        self.assertEqual(before, self.state())
        path = self.project / bundle['execution_result']
        path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        self.collect(bundle, fail=True)
        self.assertEqual(before, self.state())

    def test_collect_rejects_invalid_bundle_and_terminal_round(self):
        self.start()
        self.check_sources()
        before = self.state()
        self.collect([], fail=True)
        self.assertEqual(before, self.state())
        bundle, _ = self.collectable()
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'})
        before = self.state()
        self.collect(bundle, fail=True)
        self.assertEqual(before, self.state())

    def test_collect_setup_failure_binds_dependency_notrun_in_one_commit(self):
        self.start()
        self.check_sources()
        bundle, raw = self.collectable()
        business = raw['suites'][0]['specs'][0]
        business['tests'][0].update(status='skipped', results=[])
        setup = copy.deepcopy(business)
        setup.update(title='Synthetic auth', file='auth.spec.ts')
        setup['tests'] = [{'projectName': 'auth', 'status': 'unexpected', 'results': [
            {'startTime': '2026-01-01T00:00:00Z', 'retry': 0, 'duration': 500,
             'status': 'failed', 'attachments': []}]}]
        raw['suites'][0]['specs'].append(setup)
        raw['config']['projects'] = [{'name': 'auth'}, {'name': 'chromium', 'dependencies': ['auth']}]
        raw['stats'].update(expected=0, unexpected=1, skipped=1)
        bundle['exit_code'] = 1
        self.write(bundle['execution_result'], raw)
        self.html(bundle, raw)
        result = self.collect(bundle)['collection']
        self.assertTrue(result['complete'])
        self.assertEqual(1, result['counts']['dependency_blocked'])
        executions = self.state()['executions']
        self.assertEqual(['fail', 'notrun'], [e['result'] for e in executions])
        self.assertEqual(executions[0]['execution_id'], executions[1]['blocked_by'])
        self.assertEqual('checked', self.report()['report_consistency']['status'])

    def test_collect_late_gate_failure_also_rolls_back_setup_receipt(self):
        self.start()
        bundle, raw = self.collectable()
        setup = copy.deepcopy(raw['suites'][0]['specs'][0])
        setup.update(title='Synthetic auth', file='auth.spec.ts')
        setup['tests'][0]['projectName'] = 'auth'
        raw['suites'][0]['specs'].append(setup)
        raw['stats']['expected'] = 2
        self.write(bundle['execution_result'], raw)
        self.html(bundle, raw)
        before = self.state()
        self.collect(bundle, fail=True)
        self.assertEqual(before, self.state())

    def test_collect_rejects_unapproved_case_without_commit(self):
        self.start()
        self.check_sources()
        bundle, _ = self.collectable()
        cov = wf.load(self.project / 'test/plans/coverage.json')
        cov['cases']['C1']['review'] = 'pending'
        self.write('test/plans/coverage.json', cov)
        state = self.state()
        state['asset_fingerprint'] = wf.fingerprint(cov)
        self.write('test/.cache/runs/' + self.run + '/state.json', state)
        before = self.state()
        self.collect(bundle, fail=True)
        self.assertEqual(before, self.state())

    def test_matching_html_json_and_receipts(self):
        bundle, _ = self.prepare()
        self.register(bundle)
        self.record(self.bound())
        result = self.report()
        self.assertEqual('checked', result['report_consistency']['status'])
        self.assertEqual('passed', result['report_bundles']['E1']['report_consistency'])
        self.assertEqual(1, result['report_bundles']['E1']['business_attempts'])

    def add_blob(self, bundle):
        relative = (Path(bundle['execution_result']).parent / 'blob.zip').as_posix()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('report.jsonl', json.dumps({'method': 'onBlobReportMetadata',
                                                         'params': {'version': 2,
                                                                    'userAgent': 'Playwright/1.64.1 synthetic'}}) + '\n')
        (self.project / relative).write_bytes(buffer.getvalue())
        bundle['blob_reports'] = [relative]

    def merged(self, bundle, raw, mutate=None, command=None):
        report = {'html_report': f'test/.cache/runs/{self.run}/merged/playwright/index.html',
                  'bundle_ids': [bundle['bundle_id']],
                  'command': command or ['node', 'node_modules/@playwright/test/cli.js',
                                         'merge-reports', '--reporter=html', 'synthetic-input'],
                  'started_at': '2026-01-01T00:01:00Z', 'ended_at': '2026-01-01T00:01:01Z',
                  'exit_code': 0}
        self.html(report, raw, mutate)
        return {'type': 'merged_report', 'report': report}

    def test_native_merge_requires_all_attempts_and_publishes_one_entry(self):
        self.start()
        self.check_sources()
        bundle, raw = self.collectable(retry=True)
        self.add_blob(bundle)
        self.collect(bundle)
        self.cli('report', '--run', self.run, '--owner', self.owner, fail=True)
        event = self.merged(bundle, raw)
        self.record(event)
        result = self.report()
        self.assertEqual('checked', result['report_consistency']['status'])
        self.assertEqual(event['report']['html_report'], result['report_paths']['merged_html'])
        content = (self.project / result['report_paths']['review_html']).read_text(encoding='utf-8')
        self.assertLess(content.index('<td>failed</td>'), content.index('<td>passed</td>'))
        self.assertIn('merged/playwright/index.html', content)
        self.assertEqual(1, result['counts']['execution_results']['fail'])
        self.assertEqual(1, result['counts']['execution_results']['pass'])
        self.assertEqual(1, len(self.state()['merged_report']['inputs']['E1']))

    def test_native_merge_rejects_missing_retry_and_preserves_state(self):
        self.start()
        self.check_sources()
        bundle, raw = self.collectable(retry=True)
        self.add_blob(bundle)
        self.collect(bundle)
        before = self.state()
        changed = copy.deepcopy(raw)
        changed['suites'][0]['specs'][0]['tests'][0]['results'].pop(0)
        self.record(self.merged(bundle, changed), fail=True)
        self.assertEqual(before, self.state())

    def test_native_merge_rejects_incomplete_invocation_list(self):
        self.start()
        self.check_sources()
        bundle, raw = self.collectable()
        self.add_blob(bundle)
        self.collect(bundle)
        event = self.merged(bundle, raw)
        event['report']['bundle_ids'] = []
        self.record(event, fail=True)
        event['report']['bundle_ids'] = ['E1', 'E1']
        self.record(event, fail=True)

    def test_native_merge_rejects_outcome_rewrite_even_when_stats_are_changed(self):
        self.start()
        self.check_sources()
        bundle, raw = self.collectable()
        test = raw['suites'][0]['specs'][0]['tests'][0]
        test['status'] = 'unexpected'
        test['results'][0].update(status='failed', errors=[{'message': 'synthetic failure'}])
        raw['stats'].update(expected=0, unexpected=1)
        bundle['exit_code'] = 1
        self.write(bundle['execution_result'], raw)
        self.html(bundle, raw)
        self.add_blob(bundle)
        self.collect(bundle)
        self.record(self.merged(bundle, raw, mutate=lambda index, tests: (
            tests[0].update(outcome='expected'),
            index['stats'].update(expected=1, unexpected=0))), fail=True)

    def test_native_merge_rejects_dropped_failure_details(self):
        self.start()
        self.check_sources()
        bundle, raw = self.collectable()
        test = raw['suites'][0]['specs'][0]['tests'][0]
        test['status'] = 'unexpected'
        test['results'][0].update(status='failed', errors=[{'message': 'synthetic failure'}])
        raw['stats'].update(expected=0, unexpected=1)
        bundle['exit_code'] = 1
        self.write(bundle['execution_result'], raw)
        self.html(bundle, raw)
        self.add_blob(bundle)
        self.collect(bundle)
        self.record(self.merged(bundle, raw, mutate=lambda index, tests:
                                tests[0]['results'][0].update(errors=[])), fail=True)

    def test_native_merge_rejects_dropped_inline_attachments(self):
        self.start()
        self.check_sources()
        bundle, raw = self.collectable()
        raw['suites'][0]['specs'][0]['tests'][0]['results'][0]['attachments'] = [
            {'name': 'fixture-detail', 'contentType': 'text/plain', 'body': 'retained evidence'}]
        self.write(bundle['execution_result'], raw)
        self.html(bundle, raw)
        self.add_blob(bundle)
        self.collect(bundle)
        self.record(self.merged(bundle, raw, mutate=lambda index, tests:
                                tests[0]['results'][0].update(attachments=[])), fail=True)

    def test_native_merge_rejects_non_playwright_command(self):
        self.start()
        self.check_sources()
        bundle, raw = self.collectable()
        self.add_blob(bundle)
        self.collect(bundle)
        event = self.merged(bundle, raw, command=['echo', 'merge-reports', '--reporter=html', 'synthetic-input'])
        self.record(event, fail=True)

    def test_native_merge_does_not_claim_json_only_reconstruction(self):
        self.start()
        self.check_sources()
        bundle, raw = self.collectable()
        self.collect(bundle)
        self.record(self.merged(bundle, raw), fail=True)
        result = self.report()
        self.assertIsNone(result['report_paths']['merged_html'])
        content = (self.project / result['report_paths']['review_html']).read_text(encoding='utf-8')
        self.assertIn('尚无原生合并报告', content)

    def test_new_round_requires_blob_for_each_invocation_and_final_merge(self):
        self.start()
        self.check_sources()
        state = self.state()
        state['native_merge_required'] = True
        self.write(f'test/.cache/runs/{self.run}/state.json', state)
        bundle, _ = self.collectable()
        before = self.state()
        self.collect(bundle, fail=True)
        self.assertEqual(before, self.state())
        self.add_blob(bundle)
        self.collect(bundle, fail=True)
        self.assertEqual(before, self.state())
        plan_path = f'test/.cache/runs/{self.run}/attempts/E1/plan.json'
        plan = {key: bundle[key] for key in ('command', 'cwd', 'environment', 'scope', 'setup_projects')}
        plan.update(schema_version=2, run_id=self.run, bundle_id='E1', regression_mode='affected', shard=None,
                    planned_at=bundle['started_at'], freshness_marker=state['freshness_marker'],
                    config_fingerprint=state['config_fingerprint'], asset_fingerprint=state['asset_fingerprint'])
        self.write(plan_path, plan)
        bundle.update(attempt_plan=plan_path, attempt_plan_sha256=wf.hashlib.sha256(
            (self.project / plan_path).read_bytes()).hexdigest())
        self.collect(bundle)
        result = self.cli('report', '--run', self.run, '--owner', self.owner, fail=True)
        self.assertIn('merged_report: native merge required before final report', result['report_consistency']['errors'])

    def test_native_merge_rechecks_html_and_blob_fingerprints(self):
        self.start()
        self.check_sources()
        bundle, raw = self.collectable()
        self.add_blob(bundle)
        self.collect(bundle)
        event = self.merged(bundle, raw)
        self.record(event)
        merged_path = self.project / event['report']['html_report']
        original = merged_path.read_text(encoding='utf-8')
        merged_path.write_text(original + '\n', encoding='utf-8')
        self.cli('report', '--run', self.run, '--owner', self.owner, fail=True)
        merged_path.write_text(original, encoding='utf-8')
        with (self.project / bundle['blob_reports'][0]).open('ab') as stream:
            stream.write(b'\n')
        self.cli('report', '--run', self.run, '--owner', self.owner, fail=True)

    def test_recycled_merged_only_reports_recycled_not_checked(self):
        self.start()
        self.check_sources()
        bundle, raw = self.collectable()
        self.add_blob(bundle)
        self.collect(bundle)
        event = self.merged(bundle, raw)
        self.record(event)
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'})
        self.cli('release', '--run', self.run, '--owner', self.owner)
        state = self.state()
        state['humans']['H-keep-attempts'] = dict(
            status='approved', evidence=list(state['report_bundles']['E1']['artifact_sha256']))
        self.write(f'test/.cache/runs/{self.run}/state.json', state)
        plan = wf.retention.make_plan(self.project.resolve(), ['a'])
        selected = {item['path'] for item in plan['files']}
        self.assertIn(event['report']['html_report'], selected)
        self.assertNotIn(bundle['execution_result'], selected)
        wf.retention.apply_plan(self.project.resolve(), plan)
        result = wf.report_consistency(self.project, self.state())
        self.assertEqual('recycled', result['status'])
        self.assertEqual([], result['recycled_bundles'])
        self.assertEqual([event['report']['html_report']], result['recycled_merged_reports'][0]['paths'])

    def test_new_invocation_requires_remerge_of_all_bundles(self):
        self.start()
        self.check_sources()
        first, raw = self.collectable()
        self.add_blob(first)
        self.collect(first)
        event = self.merged(first, raw)
        self.record(event)
        second = copy.deepcopy(first)
        second['bundle_id'] = 'E2'
        for key in ('execution_result', 'html_report'):
            second[key] = second[key].replace('/E1/', '/E2/')
        self.write(second['execution_result'], raw)
        self.html(second, raw)
        self.add_blob(second)
        self.collect(second)
        self.cli('report', '--run', self.run, '--owner', self.owner, fail=True)
        combined = copy.deepcopy(raw)
        combined['suites'].extend(copy.deepcopy(raw['suites']))
        combined['stats']['expected'] *= 2
        event['report']['bundle_ids'] = ['E1', 'E2']
        self.html(event['report'], combined)
        self.record(event)
        self.assertEqual('checked', self.report()['report_consistency']['status'])

    def test_derived_index_keeps_unbound_failures_before_passes_and_escapes_errors(self):
        bundle, raw = self.prepare()
        raw['suites'][0]['specs'][0]['tests'][0].update(status='unexpected')
        raw['suites'][0]['specs'][0]['tests'][0]['results'][0].update(
            status='failed', errors=[{'message': '<script>unsafe()</script>'}])
        passed = copy.deepcopy(raw['suites'][0]['specs'][0])
        passed.update(title='Passed fixture', file='passed.spec.ts')
        passed['tests'][0].update(status='expected')
        passed['tests'][0]['results'][0].update(status='passed', errors=[])
        raw['suites'][0]['specs'].insert(0, passed)
        raw['stats'].update(expected=1, unexpected=1)
        bundle['exit_code'] = 1
        self.write(bundle['execution_result'], raw)
        self.html(bundle, raw)
        self.register(bundle)
        self.cli('report', '--run', self.run, '--owner', self.owner, fail=True)
        content = (self.project / f'test/.cache/runs/{self.run}/report.html').read_text(encoding='utf-8')
        self.assertLess(content.index('<td>failed</td>'), content.index('<td>passed</td>'))
        self.assertIn('&lt;script&gt;unsafe()', content)
        self.assertNotIn('<script>unsafe()', content)
        self.assertIn('not fully mapped', content)

    def test_blob_report_must_be_inside_its_invocation_and_is_tracked(self):
        bundle, _ = self.prepare()
        self.add_blob(bundle)
        wrong = copy.deepcopy(bundle)
        wrong['blob_reports'] = ['test/../outside.zip']
        self.register(wrong, fail=True)
        self.register(bundle)
        self.assertIn(bundle['blob_reports'][0], self.state()['report_bundles']['E1']['artifact_sha256'])

    def test_windows_report_file_case_aliases_match(self):
        bundle, raw = self.prepare()
        self.html(bundle, raw, lambda index, tests: tests[0]['location'].update(file='c1.SPEC.TS'))
        with mock.patch.object(wf.report_artifacts.platform_support, 'WINDOWS', True):
            self.register(bundle)
            self.record(self.bound())
            self.assertEqual('checked', self.report()['report_consistency']['status'])

    def test_direct_report_api_normalizes_project_without_relaxing_boundary(self):
        bundle, _ = self.prepare()
        self.register(bundle)
        self.record(self.bound())
        alias = self.project / 'test' / '..'
        self.assertEqual('checked', wf.report_consistency(alias, self.state())['status'])
        self.assertEqual((self.project / 'test/evidence.txt').resolve(),
                         wf.safe_path(alias, 'test/evidence.txt'))
        with self.assertRaises(ValueError):
            wf.safe_path(alias, '../outside-project.txt')

    def test_stale_html_statistics_and_same_counts_different_tests_rejected(self):
        bundle, raw = self.prepare()
        self.html(bundle, raw, lambda index, tests: index['stats'].update(expected=0, skipped=1))
        self.assertIn('statistics mismatch', self.register(bundle, fail=True)['error'])
        self.html(bundle, raw, lambda index, tests: tests[0].update(title='Other run'))
        self.assertIn('identities', self.register(bundle, fail=True)['error'])

    def test_same_titles_and_counts_different_run_times_rejected(self):
        bundle, raw = self.prepare()
        self.html(bundle, raw, lambda index, tests: index.update(startTime=index['startTime'] + 100000))
        self.assertIn('time mismatch', self.register(bundle, fail=True)['error'])

    def test_default_or_other_run_report_path_rejected(self):
        bundle, raw = self.prepare()
        bundle['html_report'] = 'test/.cache/report/index.html'
        self.html(bundle, raw)
        self.assertIn('outside invocation', self.register(bundle, fail=True)['error'])

    def test_json_only_never_claims_html(self):
        bundle, _ = self.prepare(html=False)
        self.register(bundle)
        self.record(self.bound())
        result = self.report()['report_bundles']['E1']
        self.assertEqual('json_only', result['report_consistency'])
        self.assertIsNone(result['html_report'])

    def test_rebuilt_html_requires_source_and_note(self):
        bundle, _ = self.prepare()
        bundle['html_origin'] = 'rebuilt'
        self.register(bundle, fail=True)
        bundle.update(rebuild_source=bundle['execution_result'], rebuild_note='Offline reconstruction; no test run')
        self.register(bundle)
        self.assertEqual('rebuilt', self.state()['report_bundles']['E1']['html_origin'])

    def test_missing_html_attachment_rejected(self):
        bundle, raw = self.prepare()
        self.html(bundle, raw, lambda index, tests: tests[0]['results'][0].update(
            attachments=[{'name': 'trace', 'path': 'data/missing.zip'}]))
        self.assertIn('file missing', self.register(bundle, fail=True)['error'])

    def test_unknown_html_format_never_passes(self):
        bundle, _ = self.prepare()
        self.write(bundle['html_report'], '<html>Unknown reporter</html>')
        self.assertIn('unsupported HTML format', self.register(bundle, fail=True)['error'])

    def test_report_mutation_detected_after_registration(self):
        bundle, _ = self.prepare()
        self.register(bundle)
        self.record(self.bound())
        path = self.project / bundle['execution_result']
        path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        result = self.cli('validate', '--run', self.run, fail=True)
        self.assertIn('changed after registration', str(result['errors']))

    def test_regression_preflight_recycles_history_and_preserves_current_bundle(self):
        old_bundle, _ = self.prepare()
        self.register(old_bundle)
        self.record(self.bound())
        old_run = self.run
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'})
        self.cli('release', '--run', old_run, '--owner', self.owner)
        self.start('--new-round')
        self.check_sources()
        current_bundle, _ = self.bundle()
        self.register(current_bundle)
        self.record(self.bound())
        result = self.cli('prepare-regression', '--run', self.run, '--owner', self.owner)
        self.assertTrue(result['retention']['completed'])
        self.assertGreater(result['retention']['files'], 0)
        self.assertGreater(result['retention']['bytes'], 0)
        self.assertGreater(result['retention']['diagnostics']['selected_files'], 0)
        self.assertGreaterEqual(result['retention']['timing']['cleanup_scan_seconds'], 0)
        self.assertGreaterEqual(result['retention']['timing']['cleanup_apply_seconds'], 0)
        self.assertEqual(result['retention'], self.state()['regression_preflight']['retention'])
        self.assertFalse((self.project / old_bundle['execution_result']).exists())
        self.assertTrue((self.project / current_bundle['execution_result']).is_file())
        historical = self.cli('validate', '--run', old_run)
        self.assertEqual('recycled', historical['report_consistency']['status'])
        current = self.cli('validate', '--run', self.run)
        self.assertEqual('checked', current['report_consistency']['status'])
        self.assertEqual(1, self.report()['counts']['execution_results']['pass'])
        with mock.patch.object(wf.retention, 'make_plan', side_effect=AssertionError('must not rescan')):
            again = self.cli('prepare-regression', '--run', self.run, '--owner', self.owner)
        self.assertTrue(again['preflight_reused'])
        self.assertEqual(result['retention'], again['retention'])

    def test_preflight_scope_expansion_and_resume_invalidate_cache(self):
        self.start()
        self.cli('prepare-regression', '--run', self.run, '--owner', self.owner, '--scopes', 'a')
        with mock.patch.object(wf.retention, 'make_plan', wraps=wf.retention.make_plan) as plan:
            self.cli('prepare-regression', '--run', self.run, '--owner', self.owner)
        self.assertEqual(['a', 'b'], plan.call_args.args[1])
        self.assertEqual(['a', 'b'], self.state()['regression_preflight']['scopes'])
        self.assertEqual(2, len(self.state()['regression_preflight']['cleanup_batches']))
        self.start('--resume', self.run)
        self.record({'type': 'recovery_review', 'environment_checked': True, 'artifacts_checked': True})
        with mock.patch.object(wf.retention, 'make_plan', wraps=wf.retention.make_plan) as plan:
            result = self.cli('prepare-regression', '--run', self.run, '--owner', self.owner)
        self.assertFalse(result['preflight_reused'])
        self.assertEqual(['a', 'b'], plan.call_args.args[1])

    def test_preflight_expansion_recycles_previously_mixed_module_bundle(self):
        old_bundle, _ = self.prepare()
        old_bundle['scope'] = ['a', 'b']
        self.register(old_bundle)
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'})
        self.cli('release', '--run', self.run, '--owner', self.owner)
        self.start('--new-round')
        self.cli('prepare-regression', '--run', self.run, '--owner', self.owner, '--scopes', 'a')
        self.assertTrue((self.project / old_bundle['execution_result']).is_file())
        result = self.cli('prepare-regression', '--run', self.run, '--owner', self.owner)
        self.assertFalse(result['preflight_reused'])
        self.assertFalse((self.project / old_bundle['execution_result']).exists())

    def test_unexpected_missing_is_not_policy_recycling(self):
        bundle, _ = self.prepare()
        self.register(bundle)
        self.record(self.bound())
        (self.project / bundle['execution_result']).unlink()
        result = self.cli('validate', '--run', self.run, fail=True)
        self.assertEqual('failed', result['report_consistency']['status'])
        self.assertFalse(wf.retention.is_recycled(self.project.resolve(), bundle['execution_result']))

    def test_legacy_broken_bundle_does_not_block_new_round_preflight(self):
        self.start()
        old_run = self.run
        state = self.state()
        state.update(acceptance='partially_blocked', claim=None)
        state['report_bundles'] = {'cli-01': {'execution_result': 'missing-old-result.json'}}
        self.write('test/.cache/runs/' + old_run + '/state.json', state)
        self.start('--new-round')
        result = self.cli('prepare-regression', '--run', self.run, '--owner', self.owner)
        self.assertTrue(result['retention']['completed'])
        self.assertIn('historical_bundle_identity_incomplete',
                      {item['reason'] for item in result['retention']['limitations']})
        historical = self.cli('validate', '--run', old_run, fail=True)
        self.assertEqual('failed', historical['report_consistency']['status'])
        self.assertEqual('unregistered', self.cli('validate', '--run', self.run)['report_consistency']['status'])

    def test_preflight_preserves_explicit_retention_override(self):
        self.start()
        spec, _ = wf.assets(self.project)
        spec['retention'] = {'before_regression': False}
        self.write('test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```\n')
        self.cli('prepare-regression', '--run', self.run, '--owner', self.owner, fail=True)
        self.start('--resume', self.run)
        self.record({'type': 'recovery_review', 'environment_checked': True, 'artifacts_checked': True})
        result = self.cli('prepare-regression', '--run', self.run, '--owner', self.owner)
        self.assertTrue(result['retention']['skipped'])
        self.assertEqual(False, wf.assets(self.project)[0]['retention']['before_regression'])

    def test_preflight_candidate_mismatch_returns_details_without_marking_complete(self):
        self.start()
        detail = {'candidate_files': 1, 'selected_files': 0, 'protected_files': 0,
                  'unexplained_paths': ['test/.cache/runs/old/attempts/B/report/index.html']}
        before = self.state()
        with mock.patch.object(wf.retention, 'make_plan',
                               side_effect=wf.retention.CleanupCandidateMismatch(detail)):
            result = self.cli('prepare-regression', '--run', self.run, '--owner', self.owner, fail=True)
        self.assertEqual('cleanup_candidate_mismatch', result['error'])
        self.assertEqual(detail, result['diagnostics'])
        self.assertEqual(before, self.state())

    def test_missing_receipts_fail_report_and_duplicate_slot_rejected(self):
        bundle, _ = self.prepare()
        self.register(bundle)
        result = self.cli('report', '--run', self.run, '--owner', self.owner, fail=True)
        self.assertIn('not fully mapped', str(result['report_consistency']['errors']))
        self.record(self.bound())
        self.record(dict(self.bound(attempt=2), execution_id='X2'), fail=True)

    def test_receipt_cannot_turn_failure_into_pass(self):
        bundle, raw = self.prepare(html=False)
        raw['stats'].update(expected=0, unexpected=1)
        raw['suites'][0]['specs'][0]['tests'][0].update(status='unexpected')
        raw['suites'][0]['specs'][0]['tests'][0]['results'][0]['status'] = 'failed'
        self.write(bundle['execution_result'], raw)
        bundle['exit_code'] = 1
        self.register(bundle)
        self.assertIn('differs from raw', self.record(self.bound(), fail=True)['error'])

    def test_all_retries_required_and_first_failure_preserved(self):
        self.start()
        self.check_sources()
        bundle, _ = self.bundle(retry=True)
        self.register(bundle)
        self.record(self.bound(result='fail'))
        self.record(dict(self.bound(attempt=2), execution_id='X2', runner_result_index=1))
        result = self.report()
        self.assertEqual('checked', result['report_consistency']['status'])
        self.assertEqual({'fail': 1, 'notrun': 0, 'pass': 1, 'skip': 0}, result['counts']['execution_results'])

    def test_dependency_skips_normalized_and_only_actual_attempts_counted(self):
        self.start()
        self.check_sources()
        self.record(self.execution('AUTH', cid='auth', result='fail', setup=True, case_ids=[]))
        self.record(self.execution('B1', result='notrun', blocked_by='AUTH', runner_result='skip',
                                   reason='Chromium depends on failed auth project'))
        result = self.report()
        self.assertEqual(0, result['counts']['business_actual_attempts'])
        self.assertEqual(1, result['counts']['dependency_blocked'])
        self.assertEqual(0, result['counts']['execution_results']['skip'])
        self.assertEqual('notrun', result['cases']['C1']['result'])
        self.record(self.execution('B2', result='notrun', attempt=2, blocked_by='unknown',
                                   runner_result='skip', reason='Invalid reference'), fail=True)

    def test_failure_markdown_describes_expected_actual_and_readback(self):
        self.start()
        self.check_sources()
        self.record(self.execution('F1', result='fail', failure_details={
            'expected': '保存后updateTime应推进',
            'actual': '编辑保存成功，但修改时间保持不变',
            'phase': 'API回读后断言', 'classification': 'needs_decision',
            'location': 'test/src/C1.spec.ts:121',
            'readback': {'before': '15:37:59', 'after': '15:37:59'},
            'cleanup': '已按准确ID清理并确认不存在', 'evidence': ['test/evidence.txt']}))
        self.report()
        text = (self.project / f'test/.cache/runs/{self.run}/report.md').read_text(encoding='utf-8')
        for value in ['保存后updateTime应推进', '修改时间保持不变', '15:37:59', 'C1.spec.ts:121', '确认不存在']:
            self.assertIn(value, text)
        self.assertIn('unregistered', text)


if __name__ == '__main__':
    unittest.main()
