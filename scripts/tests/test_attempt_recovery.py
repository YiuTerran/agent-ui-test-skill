"""Offline recovery bindings and additional reporter preparation."""
import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

import test_batch as batch
import test_tools as tools

wf = tools.wf


class AttemptRecoveryTest(unittest.TestCase):
    for _name in ('setUp', 'tearDown', 'write', 'cli', 'start', 'record', 'check_sources',
                  'state', 'bundle', 'html', 'execution', 'inventory', 'ignored', 'snapshot',
                  'init_attempt', 'attempt_evidence', 'prepared', 'collect'):
        locals()[_name] = getattr(batch.BatchTest, _name)

    def prepare_recovery(self, full=False):
        at = self.prepared(full=full)
        self.clock = mock.patch.object(wf, 'now', return_value=(at + timedelta(seconds=10)).isoformat())
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.plan_path = f'test/.cache/runs/{self.run}/attempts/E1/plan.json'
        self.binding = {
            'attempt_plan': self.plan_path,
            'attempt_plan_sha256': hashlib.sha256((self.project / self.plan_path).read_bytes()).hexdigest()}
        self.original_marker = self.marker
        self.original_artifacts = {name: content for name, content in self.snapshot().items()
                                   if '/attempts/E1/' in name}
        return self.cli('attempt-bundle', '--run', self.run, '--owner', self.owner, '--bundle', 'E1')

    def resume_attempt(self, bindings=None):
        self.start('--resume', self.run, '--scopes', 'a')
        review = {'type': 'recovery_review', 'environment_checked': True, 'artifacts_checked': True}
        if bindings is not None:
            review['attempt_plans'] = bindings
        self.record(review)
        self.check_sources('S1')

    def collect_attempt(self, fail=False):
        return self.cli('attempt-collect', '--run', self.run, '--owner', self.owner,
                        '--bundle', 'E1', fail=fail)

    def test_explicit_review_recovers_full_attempt_without_rewriting_original_evidence(self):
        self.prepare_recovery(full=True)
        self.resume_attempt([self.binding])
        result = self.collect_attempt()
        self.assertTrue(result['collection']['complete'])
        self.assertTrue(self.collect_attempt()['collection']['complete'])
        state = self.state()
        self.assertEqual(1, len(state['executions']))
        self.assertEqual(self.original_marker,
                         state['report_bundles']['E1']['regression_check']['freshness_marker'])
        self.assertEqual(self.marker, state['report_bundles']['E1']['recovery_review']['freshness_marker'])
        for name, content in self.original_artifacts.items():
            self.assertEqual(content, (self.project / name).read_bytes(), name)
        completeness = wf.regression_completeness(self.project, state)
        self.assertEqual('complete', completeness['status'])
        self.assertEqual(['E1'], completeness['recovered_bundle_ids'])
        self.write('test/src/C1.spec.ts', 'changed spec after recovered full regression')
        self.assertEqual('incomplete', wf.regression_completeness(self.project, self.state())['status'])

    def test_direct_collection_uses_same_explicit_plan_binding(self):
        event = self.prepare_recovery()
        self.resume_attempt([self.binding])
        result = self.cli('collect-results', '--run', self.run, '--owner', self.owner,
                          '--input', str(self.project / event['input']))
        self.assertTrue(result['collection']['complete'])
        self.assertIn('recovery_review', self.state()['report_bundles']['E1'])

    def test_generic_recovery_review_does_not_authorize_historical_attempt(self):
        event = self.prepare_recovery()
        self.resume_attempt()
        before = self.state()
        failure = self.collect_attempt(fail=True)
        self.assertIn('explicit recovery_review.attempt_plans binding', failure['error'])
        failure = self.cli('collect-results', '--run', self.run, '--owner', self.owner,
                           '--input', str(self.project / event['input']), fail=True)
        self.assertIn('explicit recovery_review.attempt_plans binding', failure['error'])
        self.assertEqual(before, self.state())

    def test_plan_binding_is_required_before_recovery_clears(self):
        self.prepare_recovery()
        self.start('--resume', self.run, '--scopes', 'a')
        self.assertIn('review recovery', self.collect_attempt(fail=True)['error'])

    def test_binding_digest_and_plan_identity_are_checked_atomically(self):
        self.prepare_recovery()
        self.start('--resume', self.run, '--scopes', 'a')
        before = self.state()
        for bindings in ([dict(self.binding, attempt_plan_sha256='0' * 64)],
                         [self.binding, self.binding],
                         [dict(self.binding, unexpected=True)]):
            with self.subTest(bindings=bindings):
                self.record({'type': 'recovery_review', 'environment_checked': True,
                             'artifacts_checked': True, 'attempt_plans': bindings}, fail=True)
                self.assertEqual(before, self.state())
        other = 'test/.cache/runs/other/attempts/E1/plan.json'
        plan = wf.load(self.project / self.plan_path)
        plan['run_id'] = 'other'
        self.write(other, plan)
        failure = self.record({
            'type': 'recovery_review', 'environment_checked': True, 'artifacts_checked': True,
            'attempt_plans': [{'attempt_plan': other,
                              'attempt_plan_sha256': hashlib.sha256((self.project / other).read_bytes()).hexdigest()}]},
            fail=True)
        self.assertIn('belong to this run', failure['error'])
        self.assertEqual(before, self.state())

    def test_reviewed_plan_cannot_change_after_binding(self):
        self.prepare_recovery()
        self.resume_attempt([self.binding])
        plan = wf.load(self.project / self.plan_path)
        plan['planned_at'] = (wf.dt(plan['planned_at']) - timedelta(seconds=1)).isoformat()
        self.write(self.plan_path, plan)
        self.assertIn('explicit recovery_review.attempt_plans binding',
                      self.collect_attempt(fail=True)['error'])

    def test_review_cannot_bind_changed_configuration(self):
        self.prepare_recovery()
        spec = wf.parse_spec(self.project / 'test/spec.md')
        spec['env'] = {'name': 'changed-environment'}
        self.write('test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```\n')
        self.start('--resume', self.run, '--scopes', 'a')
        failure = self.record({'type': 'recovery_review', 'environment_checked': True,
                               'artifacts_checked': True, 'attempt_plans': [self.binding]}, fail=True)
        self.assertIn('configuration or coverage differs', failure['error'])
        self.assertTrue(self.state()['recovery_required'])

    def test_review_cannot_bind_changed_coverage(self):
        self.prepare_recovery()
        coverage = wf.load(self.project / 'test/plans/coverage.json')
        coverage['review_note'] = 'changed coverage inputs'
        self.write('test/plans/coverage.json', coverage)
        self.start('--resume', self.run, '--scopes', 'a')
        failure = self.record({'type': 'recovery_review', 'environment_checked': True,
                               'artifacts_checked': True, 'attempt_plans': [self.binding]}, fail=True)
        self.assertIn('configuration or coverage differs', failure['error'])
        self.assertTrue(self.state()['recovery_required'])

    def test_recovery_binding_expires_at_next_resume(self):
        self.prepare_recovery()
        self.resume_attempt([self.binding])
        self.resume_attempt()
        self.assertIn('explicit recovery_review.attempt_plans binding',
                      self.collect_attempt(fail=True)['error'])
        self.record({'type': 'recovery_review', 'environment_checked': True,
                     'artifacts_checked': True, 'attempt_plans': [self.binding]})
        self.assertTrue(self.collect_attempt()['collection']['complete'])

    def test_recovery_binding_does_not_bypass_source_gate(self):
        self.prepare_recovery()
        self.start('--resume', self.run, '--scopes', 'a')
        self.record({'type': 'recovery_review', 'environment_checked': True,
                     'artifacts_checked': True, 'attempt_plans': [self.binding]})
        self.assertIn('source_check_required', self.collect_attempt(fail=True)['error'])
        self.assertFalse(self.state().get('report_bundles'))

    def test_already_collected_full_is_reused_after_technical_resume(self):
        self.prepare_recovery(full=True)
        self.collect_attempt()
        self.assertEqual('complete', wf.regression_completeness(self.project, self.state())['status'])
        self.resume_attempt()
        state = self.state()
        spec, coverage = wf.assets(self.project)
        summary = wf.summary(self.project, state, spec, coverage, set())
        self.assertEqual('complete', summary['regression_completeness']['status'])
        self.assertEqual([], summary['regression_completeness']['recovered_bundle_ids'])
        self.assertEqual(1, len(state['executions']))

    def test_collected_full_still_checks_current_code_after_technical_resume(self):
        self.prepare_recovery(full=True)
        self.collect_attempt()
        self.resume_attempt()
        self.write('test/src/C1.spec.ts', 'changed spec after collected full regression')
        self.assertEqual('incomplete', wf.regression_completeness(self.project, self.state())['status'])


class ExtraReportersTest(unittest.TestCase):
    for _name in ('setUp', 'tearDown', 'write', 'cli', 'start', 'record', 'state', 'init_attempt'):
        locals()[_name] = getattr(tools.ToolsTest, _name)

    def test_omitted_reporters_preserve_legacy_plan_and_arguments(self):
        self.start('--scopes', 'a')
        result = self.init_attempt()
        self.assertNotIn('extra_reporters', wf.load(self.project / result['plan']))
        self.assertEqual('--reporter=list,json,html,blob', result['reporter_arguments'][0])

    def test_shipped_sidecar_and_project_reporter_are_resolved_and_ordered(self):
        self.start('--scopes', 'a')
        shipped = Path(wf.__file__).with_name('checkpoint_playwright_reporter.mjs').resolve()
        self.write('test/custom-reporter.mjs', 'export default class Reporter {}')
        result = self.init_attempt(extra_reporters=[str(shipped), 'test/custom-reporter.mjs'], cwd='test')
        expected = [str(shipped), str((self.project / 'test/custom-reporter.mjs').resolve())]
        self.assertEqual(expected, wf.load(self.project / result['plan'])['extra_reporters'])
        self.assertEqual('--reporter=' + ','.join(['list', 'json', 'html', 'blob', *expected]),
                         result['reporter_arguments'][0])
        plan = wf.load(self.project / result['plan'])
        directory = (self.project / result['plan']).resolve().parent
        wf.report_artifacts.check_attempt_command(
            plan, [*plan['command'], *result['reporter_arguments']], directory)
        for argument in ('--reporter=list,json,html,blob',
                         '--reporter=' + ','.join(['list', 'json', 'html', 'blob', *reversed(expected)])):
            with self.subTest(argument=argument), self.assertRaises(ValueError):
                wf.report_artifacts.check_attempt_command(plan, [*plan['command'], argument], directory)

    def test_invalid_reporters_leave_no_attempt_directory(self):
        self.start('--scopes', 'a')
        self.write('test/custom.mjs', 'export default class Reporter {}')
        self.write('test/comma,reporter.mjs', 'export default class Reporter {}')
        for values in (None, 'test/custom.mjs', [False], ['missing.mjs'], ['../outside.mjs'],
                       [str((self.project / 'test/custom.mjs').resolve())],
                       ['test/comma,reporter.mjs'], ['test/custom.mjs', 'test/custom.mjs']):
            with self.subTest(values=values):
                request = {'command': ['pnpm', 'exec', 'playwright', 'test'],
                           'cwd': '.', 'environment': 'synthetic', 'scope': ['a'],
                           'setup_projects': ['auth'], 'extra_reporters': copy.deepcopy(values)}
                self.write('attempt.json', request)
                self.cli('attempt-init', '--run', self.run, '--owner', self.owner, '--bundle', 'E1',
                         '--input', str(self.project / 'attempt.json'), fail=True)
                self.assertFalse((self.project / f'test/.cache/runs/{self.run}/attempts/E1').exists())


if __name__ == '__main__':
    unittest.main()
