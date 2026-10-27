"""Regressions for corrected discovery, native acceptance and interrupted preparation."""
import copy
from datetime import timedelta
from unittest import TestCase, mock

import test_reports as reports
import test_retention as retention
import test_review_fixes as fixtures

wf = fixtures.wf


class InventoryAcceptanceTest(TestCase):
    setUp = fixtures.ReviewFixesTest.setUp
    tearDown = fixtures.ReviewFixesTest.tearDown
    write = fixtures.ReviewFixesTest.write
    cli = fixtures.ReviewFixesTest.cli
    start = fixtures.ReviewFixesTest.start
    record = fixtures.ReviewFixesTest.record
    check_sources = fixtures.ReviewFixesTest.check_sources
    state = fixtures.ReviewFixesTest.state
    inventory = fixtures.ReviewFixesTest.inventory
    ignored = fixtures.ReviewFixesTest.ignored
    preflight = fixtures.ReviewFixesTest.preflight
    execution = reports.ReportTest.execution
    bundle = reports.ReportTest.bundle
    html = reports.ReportTest.html
    blob = fixtures.ReviewFixesTest.blob
    init_attempt = fixtures.ReviewFixesTest.init_attempt
    evidence = fixtures.ReviewFixesTest.evidence
    finish = fixtures.ReviewFixesTest.finish
    collect = fixtures.ReviewFixesTest.collect
    merged = reports.ReportTest.merged

    def test_corrected_discovery_replaces_old_gap_and_releases_task_gate(self):
        self.start()
        self.check_sources()
        wrong = self.inventory()
        wrong['suites'][0]['specs'][1]['title'] = 'Wrong C2 title'
        first = self.preflight(wrong)
        self.assertEqual(['C2'], first['run']['automation_expansion']['pending_case_ids'])
        fixed = self.preflight()
        self.assertEqual([], fixed['coverage_diff']['missing_tests'])
        self.assertEqual([], self.state()['automation_gap_case_ids'])
        self.assertEqual([], fixed['run']['automation_expansion']['pending_case_ids'])
        self.assertNotIn('ask_user_to_choose_automation_expansion_scope', fixed['next_steps'])
        self.record({'type': 'task', 'task_id': 'stable-C2', 'phase': 'code',
                     'status': 'running', 'owner': self.owner, 'case_ids': ['C2']})

    def test_recomputed_gaps_keep_static_code_defects_and_archive_old_inventory(self):
        cov = wf.assets(self.project)[1]
        cov['cases']['C1']['implementation'] = 'pending'
        self.write('test/plans/coverage.json', cov)
        self.start()
        wrong = self.inventory()
        wrong['suites'][0]['specs'][1]['title'] = 'Wrong C2 title'
        self.preflight(wrong)
        previous = copy.deepcopy(self.state()['discovery_inventory'])
        fixed = self.preflight()
        state = self.state()
        self.assertEqual(['C1'], state['automation_gap_case_ids'])
        self.assertEqual(['C1'], fixed['run']['automation_expansion']['pending_case_ids'])
        self.assertEqual(previous, state['discovery_inventory_history'][-1])
        self.assertEqual(['C2'], previous['missing_tests'])
        self.preflight()
        self.assertEqual(state['discovery_inventory_history'], self.state()['discovery_inventory_history'])

    def test_preflight_without_discovery_does_not_discard_previous_evidence(self):
        self.start()
        wrong = self.inventory()
        wrong['suites'][0]['specs'][1]['title'] = 'Wrong C2 title'
        self.preflight(wrong)
        with self.ignored():
            result = self.cli('preflight', '--run', self.run, '--owner', self.owner)
        self.assertEqual('unknown', result['coverage_diff']['status'])
        self.assertEqual(['C2'], self.state()['automation_gap_case_ids'])

    def test_new_run_rejects_manual_bundle_without_plan_even_with_valid_blob(self):
        self.start()
        self.check_sources()
        bundle, _ = self.bundle()
        self.blob()
        bundle['blob_reports'] = [f'test/.cache/runs/{self.run}/attempts/E1/blob/report.zip']
        failure = self.record({'type': 'report_bundle', 'bundle': bundle}, fail=True)
        self.assertIn('plan', failure['error'])
        self.assertEqual({}, self.state().get('report_bundles', {}))

    def test_direct_collect_cannot_bypass_missing_plan(self):
        self.start()
        self.check_sources()
        bundle, _ = self.bundle()
        self.blob()
        bundle['blob_reports'] = [f'test/.cache/runs/{self.run}/attempts/E1/blob/report.zip']
        self.write('manual-bundle.json', {'type': 'report_bundle', 'bundle': bundle})
        failure = self.cli('collect-results', '--run', self.run, '--owner', self.owner,
                           '--input', str(self.project / 'manual-bundle.json'), fail=True)
        self.assertIn('plan', failure['error'])
        self.assertEqual([], self.state()['executions'])

    def test_new_run_cannot_accept_direct_execution_passes_without_bundles(self):
        self.start()
        self.check_sources()
        self.record([self.execution('E1'), self.execution('E2', 'C2')])
        failure = self.record({'type': 'acceptance', 'owner': self.owner,
                               'status': 'fully_accepted'}, fail=True)
        self.assertIn('final-regression', failure['error'])
        self.assertEqual('in_progress', self.state()['acceptance'])

    def test_new_run_summary_uses_incomplete_and_legacy_uses_not_declared(self):
        self.start()
        spec, cov = wf.assets(self.project)
        state = self.state()
        self.assertEqual('incomplete', wf.summary(self.project, state, spec, cov)['regression_completeness']['status'])
        state['native_merge_required'] = False
        self.assertEqual('not_declared', wf.summary(self.project, state, spec, cov)['regression_completeness']['status'])

    def test_complete_native_full_regression_with_merge_can_be_fully_accepted(self):
        self.start()
        self.check_sources()
        self.preflight()
        self.init_attempt(regression_mode='full', scope=['a', 'b'])
        raw = self.evidence(targets=('C1', 'C2'))
        self.collect(self.finish())
        bundle = self.state()['report_bundles']['E1']
        merged = self.merged(bundle, raw)
        merged['report'].update(started_at=(self.later-timedelta(seconds=1)).isoformat(),
                                ended_at=self.later.isoformat())
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            self.record(merged)
            self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'})
        self.assertEqual('fully_accepted', self.state()['acceptance'])

    def test_affected_attempts_with_all_passes_cannot_replace_declared_full_regression(self):
        self.start()
        self.check_sources()
        self.init_attempt(scope=['a', 'b'])
        self.evidence(targets=('C1', 'C2'))
        self.collect(self.finish())
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            failure = self.record({'type': 'acceptance', 'owner': self.owner,
                                   'status': 'fully_accepted'}, fail=True)
        self.assertIn('final-regression', failure['error'])

    def test_report_revalidation_rejects_removed_plan_binding(self):
        self.start()
        self.check_sources()
        self.init_attempt()
        self.evidence()
        self.collect(self.finish())
        state = self.state()
        state['report_bundles']['E1'].pop('attempt_plan')
        result = wf.report_consistency(self.project, state)
        self.assertEqual('failed', result['status'])
        self.assertTrue(any('plan binding' in error for error in result['errors']))

    def test_native_attempt_rejects_an_old_schema_plan(self):
        self.start()
        self.check_sources()
        result = self.init_attempt()
        self.evidence()
        plan = wf.load(self.project / result['plan'])
        plan['schema_version'] = 1
        self.write(result['plan'], plan)
        self.assertIn('current-schema', self.finish(fail=True)['error'])


class InterruptedAttemptRetentionTest(TestCase):
    setUp = retention.RetentionTest.setUp
    write = retention.RetentionTest.write
    save_state = retention.RetentionTest.save_state
    new_state = retention.RetentionTest.new_state
    refresh_bundle = retention.RetentionTest.refresh_bundle
    plan = retention.RetentionTest.plan
    paths = retention.RetentionTest.paths

    def test_interrupted_hidden_attempt_is_preserved_without_blocking_normal_retention(self):
        orphan = self.prefix + '/attempts/.E1-interrupted/results/result.json'
        self.write(orphan, {'synthetic': 'interrupted staging evidence'})
        plan = self.plan()
        self.assertNotIn(orphan, self.paths(plan))
        self.assertIn(self.raw_path, self.paths(plan))
        retention.r.apply_plan(self.project, plan)
        self.assertTrue((self.project / orphan).is_file())
        self.assertFalse((self.project / self.raw_path).exists())
