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

    def full(self):
        self.start()
        self.check_sources()
        self.write('test/src/fixtures/shared.ts', '// original shared fixture')
        self.preflight()
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
