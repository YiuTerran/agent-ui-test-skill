"""Current-code full acceptance and receipt-only interruptions; synthetic/local only."""
import copy
from datetime import datetime, timedelta, timezone
import json
from unittest import TestCase, mock

import test_platform_support as platform_tests
import test_review_fixes as base
import test_retention as retention
import test_reports as reports

wf = base.wf


class CodeAndInterruptionTest(TestCase):
    setUp = base.ReviewFixesTest.setUp
    tearDown = base.ReviewFixesTest.tearDown
    write = base.ReviewFixesTest.write
    cli = base.ReviewFixesTest.cli
    start = base.ReviewFixesTest.start
    record = base.ReviewFixesTest.record
    check_sources = base.ReviewFixesTest.check_sources
    state = base.ReviewFixesTest.state
    inventory = base.ReviewFixesTest.inventory
    ignored = base.ReviewFixesTest.ignored
    init_attempt = base.ReviewFixesTest.init_attempt
    html = base.ReviewFixesTest.html
    blob = base.ReviewFixesTest.blob
    preflight = base.ReviewFixesTest.preflight
    evidence = base.ReviewFixesTest.evidence
    finish = base.ReviewFixesTest.finish
    collect = base.ReviewFixesTest.collect
    merged = reports.ReportTest.merged

    def full(self, raw=None):
        self.start()
        self.check_sources()
        self.write('test/src/fixtures/shared.ts', '// original shared fixture')
        self.preflight(raw)
        result = self.init_attempt(regression_mode='full', scope=['a', 'b'])
        raw = self.evidence(targets=('C1', 'C2'))
        self.collect(self.finish())
        merged = self.merged(self.state()['report_bundles']['E1'], raw)
        merged['report'].update(started_at=(self.later-timedelta(seconds=1)).isoformat(),
                                ended_at=self.later.isoformat())
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            self.record(merged)
        return wf.load(self.project / result['plan'])

    def completeness(self, state=None):
        state = state or self.state()
        return wf.report_artifacts.regression_completeness(
            state['report_bundles'], state['scopes'], required=True, project=self.project)

    def test_full_hashes_discovered_specs_fixture_files_and_setup(self):
        self.start()
        self.check_sources()
        raw = self.inventory()
        self.write('test/src/auth.setup.ts', '// auth setup')
        self.write('test/src/fixtures/nested/fixture.ts', '// fixture')
        self.write('test/helpers/actions.ts', '// external helper')
        raw['suites'][0]['specs'].append({'title': 'Auth', 'file': 'auth.setup.ts',
                                        'tests': [{'projectName': 'auth'}]})
        self.preflight(raw)
        self.write('full.json', {'command': ['pnpm', 'exec', 'playwright', 'test'],
                                'cwd': '.', 'environment': 'synthetic', 'scope': ['a', 'b'],
                                'setup_projects': ['auth'], 'regression_mode': 'full',
                                'code_roots': ['test/helpers']})
        result = self.cli('attempt-init', '--run', self.run, '--owner', self.owner,
                          '--bundle', 'E1', '--input', str(self.project / 'full.json'))
        snapshot = wf.load(self.project / result['plan'])['code_snapshot']
        self.assertEqual({'test/src/C1.spec.ts', 'test/src/C2.spec.ts', 'test/src/auth.setup.ts',
                          'test/src/fixtures/nested/fixture.ts', 'test/helpers/actions.ts'}, set(snapshot['files']))
        self.assertEqual(64, len(snapshot['sha256']))

    def test_code_changes_preserve_receipts_but_invalidate_full_and_acceptance(self):
        self.full()
        self.assertEqual('complete', self.completeness()['status'])
        for path, text in [('test/src/C1.spec.ts', '// same title, different implementation'),
                           ('test/src/fixtures/shared.ts', '// changed fixture'),
                           ('test/src/fixtures/new.ts', '// new helper')]:
            with self.subTest(path=path):
                target = self.project / path
                before = target.read_bytes() if target.exists() else None
                self.write(path, text)
                result = self.completeness()
                self.assertEqual('incomplete', result['status'])
                self.assertEqual('code_changed_after_full', result['groups'][0]['code_check']['reason'])
                with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
                    failure = self.record({'type': 'acceptance', 'owner': self.owner,
                                           'status': 'fully_accepted'}, fail=True)
                self.assertIn('code_changed_after_full', failure['error'])
                self.assertEqual('checked', wf.report_consistency(self.project, self.state())['status'])
                if before is None:
                    target.unlink()
                else:
                    target.write_bytes(before)
        (self.project / 'test/src/fixtures/shared.ts').unlink()
        self.assertEqual('incomplete', self.completeness()['status'])

    def test_default_full_includes_config_pages_and_helpers_and_ignores_caches(self):
        self.write('playwright.config.ts', 'export default { timeout: 60000 }')
        self.write('test/src/pages/model.ts', 'export const model = 1')
        self.write('test/src/helpers/action.ts', 'export const action = 1')
        self.write('test/src/.cache/result.json', '{}')
        self.write('test/src/node_modules/dependency/index.js', 'ignored')
        raw = self.inventory()
        raw['config']['configFile'] = str(self.project / 'playwright.config.ts')
        plan = self.full(raw)
        files = plan['code_snapshot']['files']
        self.assertTrue({'playwright.config.ts', 'test/src/pages/model.ts',
                         'test/src/helpers/action.ts'} <= files.keys())
        self.assertFalse(any('/.cache/' in p or '/node_modules/' in p for p in files))
        self.write('test/src/.cache/result.json', '{"changed":true}')
        self.write('test/src/node_modules/new/index.js', 'changed')
        self.assertEqual('complete', self.completeness()['status'])
        for value in ('playwright.config.ts', 'test/src/pages/model.ts', 'test/src/helpers/action.ts',
                      'test/src/helpers/new.ts'):
            with self.subTest(value=value):
                target = self.project / value
                before = target.read_bytes() if target.exists() else None
                self.write(value, '// changed code, unchanged test title')
                result = self.completeness()
                self.assertEqual('incomplete', result['status'])
                self.assertEqual('code_changed_after_full', result['groups'][0]['code_check']['reason'])
                with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
                    self.record({'type': 'acceptance', 'owner': self.owner,
                                 'status': 'fully_accepted'}, fail=True)
                if before is None:
                    target.unlink()
                else:
                    target.write_bytes(before)
        (self.project / 'test/src/pages/model.ts').unlink()
        self.assertEqual('incomplete', self.completeness()['status'])

    def test_nonstandard_config_file_and_relative_config_path_are_hashed(self):
        self.write('configs/e2e.ts', 'export default {}')
        raw = self.inventory()
        raw['config']['configFile'] = 'configs/e2e.ts'
        plan = self.full(raw)
        self.assertIn('configs/e2e.ts', plan['code_snapshot']['files'])
        self.write('configs/e2e.ts', 'export default { projects: [] }')
        self.assertEqual('incomplete', self.completeness()['status'])

    def test_project_root_falls_back_to_specs_fixtures_and_config(self):
        self.write('playwright.config.mjs', 'export default {}')
        self.write('fixtures/shared.ts', '// fixture')
        self.write('src/business.ts', '// not the test root')
        raw = self.inventory()
        raw['config']['rootDir'] = str(self.project)
        for spec in raw['suites'][0]['specs']:
            spec['file'] = 'test/src/' + spec['file']
        plan = self.full(raw)
        files = plan['code_snapshot']['files']
        self.assertIn('playwright.config.mjs', files)
        self.assertIn('fixtures/shared.ts', files)
        self.assertNotIn('src/business.ts', files)
        self.assertNotIn('test/plans/coverage.json', files)
        self.assertNotIn('.', plan['code_snapshot']['roots'])
        self.assertEqual('complete', self.completeness()['status'])

    def test_preflight_traces_relative_imports_and_suggests_only_uncovered_roots(self):
        self.start()
        self.write('test/src/C1.spec.ts', '''import {
  page,
} from './pages/model.js';
import '../../shared/root.ts';
// import '../../ignored/comment.ts';
''')
        self.write('test/src/pages/model.ts', "export { action } from '../../helpers/action';")
        self.write('test/helpers/action.ts', "const value = require('../../shared/utility');")
        self.write('shared/utility.ts', 'export const value = 1')
        self.write('shared/root.ts', 'export {}')
        diff = self.preflight()['coverage_diff']
        self.assertEqual(['shared', 'test/helpers'], diff['code_roots_suggested'])
        self.assertEqual([], diff['code_import_warnings'])
        self.write('preflight-extra.json', {
            'discovery': {'path': 'discovery.json', 'complete': True, 'setup_projects': ['auth']},
            'code_roots': ['shared', 'test/helpers']})
        with self.ignored():
            result = self.cli('preflight', '--run', self.run, '--owner', self.owner,
                              '--input', str(self.project / 'preflight-extra.json'))
        self.assertEqual([], result['coverage_diff']['code_roots_suggested'])
        initialized = self.init_attempt(regression_mode='full', scope=['a', 'b'],
                                        code_roots=['shared', 'test/helpers'])
        self.assertEqual([], initialized['code_roots_suggested'])
        self.assertIn('shared/utility.ts', wf.load(self.project / initialized['plan'])['code_snapshot']['files'])

    def test_preflight_reports_unresolved_and_outside_project_imports_without_executing(self):
        self.start()
        self.write('test/src/C1.spec.ts', "import './missing';\nimport '../../../outside';\n")
        diff = self.preflight()['coverage_diff']
        self.assertEqual({'relative_import_unresolved', 'relative_import_outside_project'},
                         {v['reason'] for v in diff['code_import_warnings']})
        self.assertEqual([], self.state()['executions'])

    def test_root_level_import_suggests_a_file_not_the_whole_project(self):
        self.start()
        self.write('test/src/C1.spec.ts', "import '../../runtime';")
        self.write('runtime.ts', 'export {}')
        diff = self.preflight()['coverage_diff']
        self.assertEqual(['runtime.ts'], diff['code_roots_suggested'])
        initialized = self.init_attempt(regression_mode='full', scope=['a', 'b'], code_roots=['runtime.ts'])
        self.assertEqual([], initialized['code_roots_suggested'])
        self.assertIn('runtime.ts', wf.load(self.project / initialized['plan'])['code_snapshot']['files'])

    def test_config_outside_project_and_config_symlink_are_rejected(self):
        raw = self.inventory()
        raw['config']['configFile'] = '../external.config.ts'
        with self.assertRaisesRegex(ValueError, 'config outside project'):
            wf.report_artifacts.discovered_code_snapshot(self.project, raw, ['auth'])
        self.write('target.ts', 'export {}')
        platform_tests.symlink(self, self.project / 'playwright.config.ts', self.project / 'target.ts')
        raw['config']['configFile'] = str(self.project / 'playwright.config.ts')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            wf.report_artifacts.discovered_code_snapshot(self.project, raw, ['auth'])

    def test_old_narrow_snapshot_requires_new_current_code_full(self):
        self.full()
        state = self.state()
        state['report_bundles']['E1']['regression_check']['code_snapshot'].pop('schema_version')
        self.assertEqual('code_fingerprint_missing',
                         self.completeness(state)['groups'][0]['code_check']['reason'])

    def test_summary_and_html_report_current_code_drift_without_rewriting_history(self):
        self.full()
        receipts = copy.deepcopy(self.state()['executions'])
        self.write('test/src/fixtures/shared.ts', '// changed')
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            result = self.cli('report', '--run', self.run, '--owner', self.owner)
        self.assertEqual('checked', result['report_consistency']['status'])
        self.assertEqual('incomplete', result['regression_completeness']['status'])
        html = (self.project / f'test/.cache/runs/{self.run}/report.html').read_text(encoding='utf-8')
        self.assertIn('code_changed_after_full', html)
        self.assertEqual(receipts, self.state()['executions'])

    def test_missing_fingerprint_is_not_current_code_full_proof(self):
        self.full()
        state = self.state()
        state['report_bundles']['E1']['regression_check'].pop('code_snapshot')
        result = self.completeness(state)
        self.assertEqual('incomplete', result['status'])
        self.assertEqual('code_fingerprint_missing', result['groups'][0]['code_check']['reason'])
        without_project = wf.report_artifacts.regression_completeness(
            self.state()['report_bundles'], ['a', 'b'], required=True)
        self.assertEqual('incomplete', without_project['status'])

    def test_new_full_after_code_change_supersedes_stale_code_group(self):
        self.full()
        self.write('test/src/fixtures/shared.ts', '// changed fixture')
        self.assertEqual('incomplete', self.completeness()['status'])
        self.write('full-new.json', {'command': ['pnpm', 'exec', 'playwright', 'test'],
                                    'cwd': '.', 'environment': 'synthetic', 'scope': ['a', 'b'],
                                    'setup_projects': ['auth'], 'regression_mode': 'full'})
        self.cli('attempt-init', '--run', self.run, '--owner', self.owner, '--bundle', 'E2',
                 '--input', str(self.project / 'full-new.json'))
        self.evidence(bid='E2', targets=('C1', 'C2'))
        self.collect(self.finish(bid='E2'))
        result = self.completeness()
        self.assertEqual('complete', result['status'])
        self.assertEqual([['E2']], [g['bundle_ids'] for g in result['groups'] if g['complete']])

    def test_shards_from_different_code_snapshots_do_not_combine(self):
        self.full()
        bundles = copy.deepcopy(self.state()['report_bundles'])
        bundles['E2'] = copy.deepcopy(bundles['E1'])
        first, second = (bundles[b]['regression_check'] for b in ('E1', 'E2'))
        first['shard'], second['shard'] = {'current': 1, 'total': 2}, {'current': 2, 'total': 2}
        first['actual']['business'] = first['actual']['business'][:1]
        second['actual']['business'] = second['actual']['business'][1:]
        self.write('test/src/fixtures/shared.ts', '// second snapshot')
        saved = first['code_snapshot']
        second['code_snapshot'] = wf.report_artifacts.code_snapshot(self.project, saved['specs'], saved['roots'])
        result = wf.report_artifacts.regression_completeness(bundles, ['a', 'b'], required=True, project=self.project)
        self.assertEqual('incomplete', result['status'])
        self.assertEqual(2, len(result['groups']))

    def test_fixture_symlinks_are_rejected_before_attempt_publication(self):
        self.start()
        self.preflight()
        platform_tests.symlink(self, self.project / 'test/src/fixtures',
                               self.project / 'test/plans', target_is_directory=True)
        self.write('full.json', {'command': ['pnpm', 'exec', 'playwright', 'test'],
                                'cwd': '.', 'environment': 'synthetic', 'scope': ['a', 'b'],
                                'setup_projects': ['auth'], 'regression_mode': 'full'})
        failure = self.cli('attempt-init', '--run', self.run, '--owner', self.owner, '--bundle', 'E1',
                           '--input', str(self.project / 'full.json'), fail=True)
        self.assertIn('symlink', failure['error'])
        self.assertFalse((self.project / f'test/.cache/runs/{self.run}/attempts/E1').exists())

    def interrupted_receipt(self, full=False):
        self.start()
        self.check_sources()
        if full:
            self.preflight()
        initialized = self.init_attempt(regression_mode='full' if full else 'affected', scope=['a', 'b'])
        plan = wf.load(self.project / initialized['plan'])
        start = datetime.now(timezone.utc)
        self.later = start + timedelta(seconds=5)
        receipt = {k: plan[k] for k in ('command', 'cwd', 'environment', 'scope', 'setup_projects')}
        receipt.update(started_at=start.isoformat(), ended_at=(start + timedelta(seconds=3)).isoformat(),
                       exit_code=130)
        self.receipt_path = initialized['receipt_path']
        self.write(self.receipt_path, receipt)
        return receipt

    def interrupted_event(self, fail=False):
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            return self.cli('attempt-interrupted', '--run', self.run, '--owner', self.owner,
                            '--bundle', 'E1', '--reason', 'runner stopped after no progress', fail=fail)

    def record_interruption(self, output):
        event = wf.load(self.project / output['input'])
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            self.record(event)
        return event

    def test_interrupted_full_is_visible_in_timing_and_reports_but_never_results(self):
        self.interrupted_receipt(full=True)
        output = self.interrupted_event()
        self.assertNotIn('E1', self.state().get('interrupted_attempts', {}))
        self.record_interruption(output)
        state = self.state()
        self.assertEqual([], state['executions'])
        self.assertEqual({}, state.get('report_bundles', {}))
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            timing = self.cli('timing', '--run', self.run)
            report = self.cli('report', '--run', self.run, '--owner', self.owner)
            self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'}, fail=True)
        self.assertEqual(3, timing['runner_invocation_sum_seconds'])
        self.assertTrue(timing['runners'][0]['receipt_only'])
        self.assertIsNone(timing['runners'][0]['result_duration_sum_seconds'])
        self.assertEqual(130, timing['runners'][0]['exit_code'])
        self.assertEqual(0, report['counts']['business_executions'])
        self.assertEqual('incomplete', report['regression_completeness']['status'])
        self.assertEqual('receipt_only', report['report_consistency']['status'])
        prefix = self.project / f'test/.cache/runs/{self.run}'
        self.assertIn('exit=130', (prefix / 'report.html').read_text(encoding='utf-8'))
        self.assertIn('exit=130', (prefix / 'report.md').read_text(encoding='utf-8'))

    def test_partial_raw_json_is_not_imported_as_interrupted_results(self):
        self.interrupted_receipt()
        self.write(self.receipt_path.replace('command.json', 'results/result.json'), {'partial': True})
        self.record_interruption(self.interrupted_event())
        self.assertEqual([], self.state()['executions'])

    def test_unknown_exit_code_round_trips_through_state_timing_and_reports(self):
        receipt = self.interrupted_receipt(full=True)
        reason = 'Host session terminated; process exit observed but exit status was not delivered'
        self.write(self.receipt_path, dict(receipt, exit_code=None, reason=reason))
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            output = self.cli('attempt-interrupted', '--run', self.run, '--owner', self.owner,
                              '--bundle', 'E1', '--reason', reason)
        self.record_interruption(output)
        self.assertIsNone(self.state()['interrupted_attempts']['E1']['exit_code'])
        self.assertEqual([], self.state()['executions'])
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            timing = self.cli('timing', '--run', self.run)
            result = self.cli('report', '--run', self.run, '--owner', self.owner)
        self.assertIsNone(timing['runners'][0]['exit_code'])
        self.assertEqual(3, timing['runner_invocation_sum_seconds'])
        self.assertEqual('incomplete', result['regression_completeness']['status'])
        self.assertEqual(0, result['counts']['business_executions'])
        prefix = self.project / f'test/.cache/runs/{self.run}'
        for file in ('report.html', 'report.md'):
            text = (prefix / file).read_text(encoding='utf-8')
            self.assertIn(reason, text)
            self.assertIn('exit=unknown', text)
        schema = wf.load(wf.Path(wf.__file__).parent.parent / 'assets/events.schema.json')
        self.assertIn('null', schema['$defs']['interrupted_attempt']['properties']['attempt']['properties']['exit_code']['type'])

    def test_unknown_exit_code_requires_explicit_null_and_matching_receipt_reason(self):
        receipt = self.interrupted_receipt()
        for update in ({'exit_code': None}, {'exit_code': None, 'reason': ''},
                       {'exit_code': None, 'reason': 'different'}, {'exit_code': 'unknown'}):
            with self.subTest(update=update):
                self.write(self.receipt_path, dict(receipt, **update))
                self.interrupted_event(fail=True)
        without_code = dict(receipt, reason='runner stopped after no progress')
        without_code.pop('exit_code')
        self.write(self.receipt_path, without_code)
        self.interrupted_event(fail=True)

    def test_interruption_rejects_exit_zero_and_receipt_plan_mismatch(self):
        original = self.interrupted_receipt()
        for changed in ({'exit_code': 0}, {'command': ['different']}, {'scope': ['a']}, {'exit_code': True}):
            with self.subTest(changed=changed):
                self.write(self.receipt_path, dict(original, **changed))
                self.interrupted_event(fail=True)
        self.assertEqual([], self.state()['executions'])

    def test_complete_artifacts_cannot_be_downgraded_to_receipt_only(self):
        self.interrupted_receipt()
        self.evidence()
        receipt = wf.load(self.project / self.receipt_path)
        self.write(self.receipt_path, dict(receipt, exit_code=130))
        failure = self.interrupted_event(fail=True)
        self.assertIn('normal attempt-bundle', failure['error'])

    def test_registered_interrupted_receipts_are_rechecked_and_ids_are_exclusive(self):
        self.interrupted_receipt()
        event = self.record_interruption(self.interrupted_event())
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            self.record(event, fail=True)
        self.assertIn('registered as interrupted', self.finish(fail=True)['error'])
        receipt = wf.load(self.project / self.receipt_path)
        self.write(self.receipt_path, dict(receipt, exit_code=137))
        result = wf.report_consistency(self.project, self.state())
        self.assertEqual('failed', result['status'])
        self.assertIn('changed', str(result['errors']))
        self.cli('timing', '--run', self.run, fail=True)

    def test_retention_preserves_receipt_only_plan_and_custom_receipt_path(self):
        self.interrupted_receipt()
        custom = self.receipt_path.replace('command.json', 'results/output/command-receipt.json')
        self.write(custom, wf.load(self.project / self.receipt_path))
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            output = self.cli('attempt-interrupted', '--run', self.run, '--owner', self.owner,
                              '--bundle', 'E1', '--input', custom, '--reason', 'interrupted')
        self.record_interruption(output)
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'})
        self.cli('release', '--run', self.run, '--owner', self.owner)
        plan = wf.retention.make_plan(self.project.resolve(), ['a', 'b'])
        self.assertNotIn(custom, {f['path'] for f in plan['files']})
        wf.retention.apply_plan(self.project.resolve(), plan)
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            self.assertEqual(3, self.cli('timing', '--run', self.run)['runner_invocation_sum_seconds'])


class EvidenceBatchTest(TestCase):
    setUp = retention.RetentionTest.setUp
    write = retention.RetentionTest.write
    save_state = retention.RetentionTest.save_state
    new_state = retention.RetentionTest.new_state
    refresh_bundle = retention.RetentionTest.refresh_bundle
    plan = retention.RetentionTest.plan

    def test_require_evidence_batches_recycled_paths_and_preserves_missing_gate(self):
        retention.r.apply_plan(self.project, self.plan())
        event = {'evidence': [self.html, self.trace, self.raw_path] * 20}
        with mock.patch.object(wf.retention, 'manifests', wraps=wf.retention.manifests) as scans:
            wf.require_evidence(self.project, event, allow_recycled=True)
        self.assertEqual(1, scans.call_count)
        event['evidence'].append('test/.cache/runs/old/attempts/B/results/not-recorded.json')
        with self.assertRaisesRegex(ValueError, 'evidence file missing'):
            wf.require_evidence(self.project, event, allow_recycled=True)
