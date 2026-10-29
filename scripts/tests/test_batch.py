"""Batch paths exercise real recorder gates with synthetic, offline artifacts."""
import json
from pathlib import Path
import unittest
from unittest import mock

import test_tools as tools
import test_reports as reports

wf = tools.wf


class BatchTest(unittest.TestCase):
    for _name in ('setUp', 'tearDown', 'write', 'cli', 'start', 'record', 'check_sources',
                  'state', 'bundle', 'html', 'execution', 'inventory', 'ignored', 'snapshot',
                  'init_attempt', 'attempt_evidence'):
        locals()[_name] = getattr(tools.ToolsTest, _name)
    merged = reports.ReportTest.merged

    def prepared(self, retry=False, full=True):
        self.start('--scopes', 'a')
        self.check_sources('S1')
        if full:
            inventory = self.inventory()
            inventory['suites'][0]['specs'] = inventory['suites'][0]['specs'][:1]
            self.write('discovery.json', inventory)
            self.write('preflight.json', {'discovery': {
                'path': 'discovery.json', 'complete': True, 'setup_projects': ['auth']}})
            with self.ignored():
                self.cli('preflight', '--run', self.run, '--owner', self.owner,
                         '--input', str(self.project / 'preflight.json'))
        self.init_attempt(regression_mode='full' if full else 'affected')
        at = self.attempt_evidence(retry=retry)
        return at

    def collect(self, at, fail=False):
        with mock.patch.object(wf, 'now', return_value=at.isoformat()):
            return self.cli('attempt-collect', '--run', self.run, '--owner', self.owner,
                            '--bundle', 'E1', fail=fail)

    def final_input(self, require_full=True, registered=False):
        bundle = self.state()['report_bundles']['E1']
        raw = wf.load(self.project / bundle['execution_result'])
        event = self.merged(bundle, raw)
        if registered:
            self.record(event)
        request = {'acceptance': {'status': 'completed_with_failures', 'reason': 'Synthetic decision',
                                  'evidence': ['test/evidence.txt']},
                   'require_full': require_full, 'events': [] if registered else [event]}
        self.write('finalize.json', request)
        return request

    def finish(self, fail=False):
        return self.cli('finalize', '--run', self.run, '--owner', self.owner,
                        '--input', str(self.project / 'finalize.json'), fail=fail)

    def test_collect_binds_retry_history_once_and_finalize_releases(self):
        at = self.prepared(retry=True)
        self.assertTrue(self.collect(at)['collection']['complete'])
        self.collect(at)
        self.assertEqual(['fail', 'pass'], [x['result'] for x in self.state()['executions']])
        self.final_input()
        with mock.patch.object(wf.subprocess, 'run', side_effect=AssertionError('must reuse existing artifacts')):
            result = self.finish()
        self.assertTrue(result['released'])
        self.assertIsNone(self.state()['claim'])
        self.assertEqual('checked', result['report_consistency'])
        self.assertEqual('complete', result['regression_completeness'])
        self.assertEqual(1, result['execution_results']['fail'])
        self.assertEqual(1, result['execution_results']['pass'])
        self.assertLess(len(json.dumps(result)), 5000)
        self.assertTrue((self.project / result['report_paths']['review_html']).is_file())

    def test_collect_does_not_hide_unmapped_result(self):
        at = self.prepared()
        path = self.project / f'test/.cache/runs/{self.run}/attempts/E1/results/result.json'
        raw = wf.load(path)
        raw['suites'][0]['specs'][0]['title'] = 'Unknown case'
        self.write(path, raw)
        self.html({'html_report': f'test/.cache/runs/{self.run}/attempts/E1/report/playwright/index.html'}, raw)
        result = self.collect(at, fail=True)
        self.assertFalse(result['collection']['complete'])
        self.assertTrue(result['collection']['unresolved'])
        self.assertEqual([], self.state()['executions'])
        self.assertIsNotNone(self.state()['claim'])

    def test_full_check_failure_keeps_claim_and_does_not_repeat_events_on_retry(self):
        at = self.prepared(full=False)
        self.collect(at)
        self.final_input()
        result = self.finish(fail=True)
        self.assertFalse(result['released'])
        self.assertIsNotNone(self.state()['claim'])
        count = len(self.state()['events'])
        self.finish(fail=True)
        self.assertEqual(count, len(self.state()['events']))
        self.assertEqual('in_progress', self.state()['acceptance'])

    def test_lost_success_receipt_replays_without_rewriting_report(self):
        at = self.prepared()
        self.collect(at)
        self.final_input()
        first = self.finish()
        report_path = self.project / first['report_paths']['machine_report']
        report = wf.load(report_path)
        self.assertIsNone(report['status']['claim'])
        before = self.snapshot()
        second = self.finish()
        self.assertTrue(second['replayed'])
        self.assertTrue(second['released'])
        self.assertEqual(before, self.snapshot())

    def test_failed_finalization_accepts_revised_decision_without_replaying_receipts(self):
        at = self.prepared(full=False)
        self.collect(at)
        request = self.final_input()
        self.assertEqual('report_checks', self.finish(fail=True)['stage'])
        first = self.state()['finalization']['input_sha256']
        request['require_full'] = False
        request['acceptance']['reason'] = 'User limited this round to targeted verification'
        self.write('finalize.json', request)
        result = self.finish()
        self.assertTrue(result['released'])
        finalization = self.state()['finalization']
        self.assertEqual([first], [item['input_sha256'] for item in finalization['superseded_inputs']])
        self.assertEqual(1, sum(e['type'] == 'merged_report' for e in self.state()['events']))

    def test_targeted_finalization_does_not_require_full(self):
        at = self.prepared(full=False)
        self.collect(at)
        self.final_input(require_full=False)
        self.assertTrue(self.finish()['released'])

    def test_bad_report_keeps_claim_and_report_error(self):
        at = self.prepared()
        self.collect(at)
        self.final_input(registered=True)
        html = self.project / f'test/.cache/runs/{self.run}/merged/playwright/index.html'
        html.write_text('corrupted')
        result = self.finish(fail=True)
        self.assertFalse(result['released'])
        self.assertEqual('failed', result['report_consistency']['status'])
        self.assertIsNotNone(self.state()['claim'])

    def test_wrong_owner_and_invented_full_acceptance_still_rejected(self):
        at = self.prepared()
        self.collect(at)
        request = self.final_input()
        request['acceptance']['owner'] = 'different-owner'
        self.write('finalize.json', request)
        before = self.snapshot()
        self.finish(fail=True)
        self.assertEqual(before, self.snapshot())
        request['acceptance'].update(owner=self.owner, status='fully_accepted')
        # An unresolved owned-resource intent blocks the original full-acceptance gate.
        self.record({'type': 'resource', 'resource_id': 'pending', 'status': 'intent',
                     'created_by_run': self.run, 'evidence': ['test/evidence.txt']})
        self.write('finalize.json', request)
        self.finish(fail=True)
        self.assertEqual('in_progress', self.state()['acceptance'])

    def test_offline_merge_invoked_once_with_all_blobs_and_real_receipt(self):
        at = self.prepared(retry=True)
        self.collect(at)
        request = self.final_input()
        raw = wf.load(self.project / self.state()['report_bundles']['E1']['execution_result'])
        request.pop('events')
        request['merge'] = {'command': ['pnpm', 'exec', 'playwright']}
        self.write('finalize.json', request)
        calls = []

        def merge(argv, **kw):
            calls.append(argv)
            self.assertEqual(['pnpm', 'exec', 'playwright', 'merge-reports', '--reporter=html'], argv[:5])
            self.assertFalse(kw['shell'])
            self.assertEqual(1, len(list(Path(argv[-1]).glob('*.zip'))))
            self.assertEqual('0', kw['env']['COREPACK_ENABLE_NETWORK'])
            self.html({'html_report': str(Path(kw['env']['PLAYWRIGHT_HTML_OUTPUT_DIR']) / 'index.html')}, raw)
            return type('Process', (), {'returncode': 0})()

        with mock.patch('subprocess.run', side_effect=merge):
            result = self.finish()
        self.assertEqual(1, len(calls))
        self.assertTrue(result['released'])
        self.assertEqual(calls[0], self.state()['merged_report']['command'])

    def test_merge_launcher_accepts_local_playwright_forms_only(self):
        tools_module = __import__('importlib.util', fromlist=['util'])
        spec = tools_module.spec_from_file_location('batch_under_test', Path(wf.__file__).with_name('workflow_batch.py'))
        batch = tools_module.module_from_spec(spec)
        spec.loader.exec_module(batch)
        for command in (['pnpm', 'exec', 'playwright'], ['npx', 'playwright'],
                        ['corepack', 'pnpm@9.0.0', 'exec', 'playwright'],
                        ['node', 'node_modules/@playwright/test/cli.js'],
                        ['node_modules/.bin/playwright'], ['C:\\repo\\node_modules\\.bin\\playwright.cmd']):
            with self.subTest(command=command):
                batch.check_merge_launcher(wf, command)
        for command in (['python3', 'x.py', 'playwright'], ['bash', '-c', 'playwright'],
                        ['node', '--require', 'evil.js', 'node_modules/@playwright/test/cli.js'],
                        ['node', 'scripts/cli.js'], ['node_modules/.bin/playwright', 'extra'],
                        ['npx', 'playwright', 'test'], ['pnpm', 'dlx', 'playwright']):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    batch.check_merge_launcher(wf, command)

    def test_merge_nonzero_preserves_active_state(self):
        at = self.prepared()
        self.collect(at)
        request = self.final_input()
        request.pop('events')
        request['merge'] = {'command': ['pnpm', 'exec', 'playwright']}
        self.write('finalize.json', request)
        with mock.patch('subprocess.run', return_value=type('Process', (), {'returncode': 1})()):
            self.finish(fail=True)
        self.assertEqual('in_progress', self.state()['acceptance'])
        self.assertIsNotNone(self.state()['claim'])

    def test_active_quota_blocks_finalization_before_acceptance_or_release(self):
        self.start('--scopes', 'a')
        self.write('test/.cache/scheduling/quota.json', {'schema_version': 1, 'leases': {
            'active': {'run_id': self.run, 'status': 'active'}}})
        self.write('finalize.json', {'acceptance': {'status': 'partially_blocked', 'reason': 'Synthetic'},
                                    'require_full': False})
        result = self.finish(fail=True)
        self.assertIn('active_leases', result['error'])
        self.assertEqual('in_progress', self.state()['acceptance'])
        self.assertIsNotNone(self.state()['claim'])

    def test_merge_timeout_returns_bounded_diagnostic_without_fake_receipt(self):
        import subprocess
        at = self.prepared()
        self.collect(at)
        request = self.final_input()
        request.pop('events')
        request['merge'] = {'command': ['pnpm', 'exec', 'playwright'], 'timeout_seconds': 1}
        self.write('finalize.json', request)

        def timeout(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw['timeout'])

        with mock.patch('subprocess.run', side_effect=timeout):
            result = self.finish(fail=True)
        self.assertEqual('offline_merge', result['stage'])
        self.assertEqual('in_progress', self.state()['acceptance'])
        self.assertIsNotNone(self.state()['claim'])
        self.assertFalse(list((self.project / result['evidence_directory']).glob('command.json')))

    def test_no_targets_can_close_as_blocked_without_inventing_results(self):
        self.start('--scopes', 'a')
        self.write('finalize.json', {'acceptance': {'status': 'partially_blocked', 'reason': 'No runnable targets'},
                                    'require_full': False})
        self.assertTrue(self.finish()['released'])
        self.assertEqual([], self.state()['executions'])

    def test_finalize_unrelated_issue_never_syncs_pools_with_corrupt_history(self):
        self.start('--scopes', 'a')
        self.write('test/.cache/runs/broken/state.json', '{')
        self.write('finalize.json', {
            'acceptance': {'status': 'partially_blocked', 'reason': 'Synthetic stop'},
            'require_full': False,
            'events': [{'type': 'issue', 'issue_id': 'I1', 'kind': 'environment', 'status': 'open'}]})
        with mock.patch.object(wf, 'sync_pool_ledger', wraps=wf.sync_pool_ledger) as sync:
            self.assertTrue(self.finish()['released'])
        sync.assert_not_called()
        count = len(self.state()['events'])
        self.assertTrue(self.finish()['replayed'])
        self.assertEqual(count, len(self.state()['events']))

    def test_finalize_retained_sync_failure_leaves_checkpoint_and_events_uncommitted(self):
        self.start('--scopes', 'a')
        # Use the same resource lifecycle as the standalone pool tests.
        spec = wf.parse_spec(self.project / 'test/spec.md')
        spec['identity_pools'] = {'pool-a': {'max_accounts': 1}}
        self.write('test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```\n')
        self.start('--resume', self.run, '--scopes', 'a')
        self.record({'type': 'recovery_review', 'environment_checked': True, 'artifacts_checked': True})
        resource = {'type': 'resource', 'resource_id': 'a1', 'pool': 'pool-a', 'created_by_run': self.run,
                    'status': 'intent', 'kind': 'account'}
        self.record(resource)
        self.record(dict(resource, status='confirmed', owned=True, receipt='synthetic-create'))
        self.write('finalize.json', {
            'acceptance': {'status': 'partially_blocked', 'reason': 'Synthetic stop'},
            'require_full': False,
            'events': [dict(resource, status='retained_verified', retention='pooled',
                            pool_ref='user-01', evidence=['test/evidence.txt'])]})
        before = self.state()
        with mock.patch.object(wf, 'sync_pool_ledger', side_effect=OSError('synthetic sync failure')) as sync:
            self.assertIn('sync failure', self.finish(fail=True)['error'])
        self.assertEqual(before, self.state())
        self.assertTrue(sync.call_args.kwargs['current_only'])
        self.assertTrue(self.finish()['released'])
        self.assertEqual(1, sum(e.get('status') == 'retained_verified' for e in self.state()['events']))
