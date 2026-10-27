"""Coverage diagnostics, batch cost and local doctor regression checks."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import test_workflow as base

wf = base.wf


class ReviewFixTest(unittest.TestCase):
    setUp = base.WorkflowTest.setUp
    tearDown = base.WorkflowTest.tearDown
    write = base.WorkflowTest.write
    cli = base.WorkflowTest.cli
    start = base.WorkflowTest.start
    record = base.WorkflowTest.record
    check_sources = base.WorkflowTest.check_sources
    execution = base.WorkflowTest.execution
    state = base.WorkflowTest.state

    def test_duplicate_test_ids_are_invalid_with_concrete_work_reasons(self):
        spec, cov = wf.assets(self.project)
        cov['cases']['C2']['test_id'] = 'C1'
        self.write('test/plans/coverage.json', cov)
        self.assertIn('duplicate test_id: C1/C2 (C1)', wf.asset_errors(self.project, spec, cov))
        self.cli('validate', fail=True)
        self.start()
        view = self.check_sources()
        for cid in ['C1', 'C2']:
            self.assertIn('duplicate_test_id:C1/C2', view['work_needed_reasons'][cid])
            self.assertIn('duplicate_test_id:C1/C2', view['regression_inventory'][cid]['blocked_by'])
        self.record(self.execution('bad'), fail=True)
        cov['cases']['C2']['implementation'] = 'retired'
        self.assertFalse(any('duplicate test_id' in e for e in wf.asset_errors(self.project, spec, cov)))

    def test_work_needed_reasons_identify_each_repair(self):
        spec, cov = wf.assets(self.project)
        cov['cases']['C1'].update(review='pending', implementation='draft', acceptance_version='old')
        cov['requirements']['R1']['review'] = 'pending'
        (self.project / cov['cases']['C1']['plan']).unlink()
        (self.project / cov['cases']['C1']['spec']).unlink()
        self.write('test/plans/coverage.json', cov)
        self.start()
        view = self.check_sources()
        self.assertEqual({'review_not_approved', 'implementation_not_valid',
                          'acceptance_version_mismatch:R1', 'requirement_review_not_approved:R1',
                          'missing_plan_file:test/plans/C1.md', 'missing_spec_file:test/src/C1.spec.ts'},
                         set(view['work_needed_reasons']['C1']))
        self.assertEqual(['C2'], view['regression_ready'])

    def test_legacy_cases_may_share_test_id_without_claiming_independent_coverage(self):
        spec, cov = wf.assets(self.project)
        for case in cov['cases'].values():
            case.update(test_id='old-group', legacy_group=True)
        self.write('test/plans/coverage.json', cov)
        self.assertEqual([], wf.asset_errors(self.project, spec, cov))
        self.cli('validate')
        self.start()
        self.check_sources()
        view = self.record([
            {'type': 'case', 'case_id': cid, 'action': 'updated', 'coverage': case}
            for cid, case in cov['cases'].items()])
        for cid in cov['cases']:
            self.assertFalse(wf.covered(self.project, cov, cid))
            self.assertEqual(['legacy_group_requires_independent_test'], view['work_needed_reasons'][cid])
        self.record(self.execution('old', test_id='old-group', case_ids=['C1', 'C2'], legacy_group=True,
                                   steps={cid: {'result': 'pass', 'evidence': ['test/evidence.txt']}
                                          for cid in cov['cases']}))
        self.assertEqual({'C1': 'pass', 'C2': 'pass'}, wf.case_results(self.project, self.state(), cov))

    def test_legacy_exemption_does_not_hide_independent_duplicates(self):
        spec, cov = wf.assets(self.project)
        cov['cases']['C2'].update(test_id='C1', legacy_group=True)
        self.assertEqual([], wf.asset_errors(self.project, spec, cov))
        self.assertTrue(wf.covered(self.project, cov, 'C1'))
        cov['cases']['C3'] = dict(cov['cases']['C1'])
        cov['requirements']['R1']['case_ids'].append('C3')
        self.assertIn('duplicate test_id: C1/C3 (C1)', wf.asset_errors(self.project, spec, cov))
        self.assertIn('duplicate_test_id:C1/C3', wf.coverage_reasons(self.project, cov, 'C1'))

    def test_output_flags_fail_before_side_effects_on_unsupported_commands(self):
        before = sorted(self.project.rglob('*'))
        for command in ['init', 'doctor', 'schema', 'start', 'renew', 'prepare-regression',
                        'validate', 'report', 'release']:
            for flag in ['--brief', '--full']:
                with self.subTest(command=command, flag=flag):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = wf.main([command, flag])
                    self.assertEqual(2, code)
                    self.assertEqual('--brief/--full only apply to status, record and collect-results',
                                     json.loads(output.getvalue())['error'])
        self.assertEqual(before, sorted(self.project.rglob('*')))

    def test_execution_does_not_build_full_view_or_scan_history(self):
        self.start()
        self.check_sources()
        state = self.state()
        spec, cov = wf.assets(self.project)
        with mock.patch.object(wf, 'status', side_effect=AssertionError('full status forbidden')), \
                mock.patch.object(wf, 'global_residuals', side_effect=AssertionError('history forbidden')):
            wf.record_event(self.project, state, spec, cov, self.execution('fast'))
        self.assertEqual('fast', state['executions'][-1]['execution_id'])

    def test_batch_cache_does_not_bypass_later_human_or_mapping_change(self):
        self.start()
        self.check_sources()
        state = self.state()
        spec, cov = wf.assets(self.project)
        context = {}
        wf.record_event(self.project, state, spec, cov, self.execution('E1'), context)
        wf.record_event(self.project, state, spec, cov, {
            'type': 'human', 'request_id': 'H', 'mode': 'explicit_decision',
            'status': 'waiting', 'case_ids': ['C1']}, context)
        with self.assertRaisesRegex(ValueError, 'human_decision_required'):
            wf.record_event(self.project, state, spec, cov, self.execution('E2', attempt=2), context)
        wf.record_event(self.project, state, spec, cov, {
            'type': 'human', 'request_id': 'H', 'status': 'approved'}, context)
        case = dict(cov['cases']['C1'], implementation='draft')
        wf.record_event(self.project, state, spec, cov, {
            'type': 'case', 'case_id': 'C1', 'action': 'updated', 'coverage': case}, context)
        with self.assertRaisesRegex(ValueError, 'reviewed code'):
            wf.record_event(self.project, state, spec, cov, self.execution('E2', attempt=2), context)

    def test_nonexecution_invalidates_selection_without_leaving_a_stale_cache(self):
        self.start()
        self.check_sources()
        state, context = self.state(), {}
        spec, cov = wf.assets(self.project)
        wf.record_event(self.project, state, spec, cov, self.execution('E1'), context)
        self.assertIn('selected', context)
        wf.record_event(self.project, state, spec, cov, {
            'type': 'case', 'case_id': 'C3', 'action': 'added',
            'coverage': dict(cov['cases']['C1'], test_id='C3'),
            'requirements': {'R1': dict(cov['requirements']['R1'], case_ids=['C1', 'C3'])}}, context)
        self.assertEqual({}, context)
        wf.record_event(self.project, state, spec, cov, self.execution('E3', cid='C3'), context)
        self.assertEqual('E3', state['executions'][-1]['execution_id'])

    def test_400_execution_batch_has_bounded_mapping_and_history_work(self):
        self.start()
        self.check_sources()
        spec, cov = wf.assets(self.project)
        template = cov['cases']['C1']
        cov['cases'] = {f'C{i:04}': dict(template, test_id=f'C{i:04}') for i in range(400)}
        cov['requirements']['R1']['case_ids'] = list(cov['cases'])
        state, context = self.state(), {}
        state['asset_fingerprint'] = wf.fingerprint(cov)
        with mock.patch.object(wf, 'status', side_effect=AssertionError('no full view per receipt')), \
                mock.patch.object(wf, 'global_residuals', side_effect=AssertionError('no history')), \
                mock.patch.object(wf, 'test_id_index', wraps=wf.test_id_index) as index, \
                mock.patch.object(wf, 'fingerprint', wraps=wf.fingerprint) as fingerprints, \
                mock.patch.object(wf, 'safe_path', wraps=wf.safe_path) as paths:
            for cid in cov['cases']:
                wf.record_event(self.project, state, spec, cov, self.execution('E' + cid, cid=cid), context)
        self.assertEqual(400, len(state['executions']))
        self.assertEqual(1, index.call_count)
        self.assertLessEqual(fingerprints.call_count, 3)
        self.assertEqual(402, paths.call_count)  # One evidence per event, shared plan/spec once.

    def test_status_reuses_coverage_paths_and_source_impact(self):
        self.start()
        self.check_sources()
        spec, cov = wf.assets(self.project)
        with mock.patch.object(wf, 'source_affected', wraps=wf.source_affected) as affected, \
                mock.patch.object(wf, 'safe_path', wraps=wf.safe_path) as paths:
            wf.status(self.project, self.state(), spec, cov)
        self.assertEqual(2, affected.call_count)
        self.assertEqual(4, paths.call_count)

    def test_default_record_is_brief_and_explicit_full_preserves_details(self):
        self.start()
        self.check_sources()
        self.write('brief.json', self.execution('brief'))
        with mock.patch.object(wf, 'global_residuals', side_effect=AssertionError('history forbidden')):
            brief = self.cli('record', '--run', self.run, '--owner', self.owner,
                             '--input', str(self.project / 'brief.json'))
        self.assertNotIn('regression_inventory', brief)
        self.assertFalse(brief['global_residuals_checked'])
        self.assertEqual(1, brief['counts']['executions'])
        self.assertEqual(['C1', 'C2'], brief['regression_ready'])
        full = self.cli('status', '--run', self.run, '--full')
        self.assertIn('regression_inventory', full)
        self.assertTrue(full['global_residuals_checked'])
        self.assertLess(len(json.dumps(brief)), len(json.dumps(full)))

    def test_missing_assets_offer_init_and_doctor_is_read_only(self):
        (self.project / 'test/spec.md').unlink()
        before = sorted(str(p.relative_to(self.project)) for p in self.project.rglob('*'))
        error = self.cli('status', fail=True)
        self.assertIn('init --project', error['error'])
        result = self.cli('doctor', fail=True)
        self.assertTrue(any(c['check'] == 'test/spec.md' and c['status'] == 'error' for c in result['checks']))
        self.assertEqual(before, sorted(str(p.relative_to(self.project)) for p in self.project.rglob('*')))

    def test_doctor_aggregates_parse_missing_coverage_and_gitignore(self):
        self.write('test/spec.md', 'not a config')
        (self.project / 'test/plans/coverage.json').unlink()
        result = self.cli('doctor', fail=True)
        errors = {c['check'] for c in result['checks'] if c['status'] == 'error'}
        self.assertEqual({'test/spec.md', 'test/plans/coverage.json', 'cache_gitignore'}, errors)
        self.assertIn('doctor', self.cli('validate', fail=True)['error'])

    def test_doctor_uses_actual_git_ignore_policy(self):
        subprocess.run(['git', 'init', '--quiet', str(self.project)], check=True, capture_output=True)
        self.write('.gitignore', '/test/.cache/\n')
        result = self.cli('doctor')
        self.assertTrue(result['valid'])
        self.write('.gitignore', '/test/.cache/*\n!test/.cache/__e2e_ignore_probe__\n')
        self.cli('doctor', fail=True)

    def test_doctor_ignore_probe_survives_windows_newline_translation(self):
        subprocess.run(['git', 'init', '--quiet', str(self.project)], check=True, capture_output=True)
        self.write('.gitignore', '/test/.cache/*\n!test/.cache/__e2e_ignore_probe__\n')
        run = subprocess.run
        def windows_text_input(*args, **kwargs):
            if kwargs.get('text') and isinstance(kwargs.get('input'), str):
                kwargs['input'] = kwargs['input'].replace('\n', '\r\n')
            return run(*args, **kwargs)
        with mock.patch.object(wf.subprocess, 'run', side_effect=windows_text_input):
            result = self.cli('doctor', fail=True)
        checks = {item['check']: item['status'] for item in result['checks']}
        self.assertEqual('error', checks['cache_gitignore'])

    def test_preflight_commit_merges_batches_added_while_unlocked(self):
        self.start()
        concurrent = {'scopes': ['b'], 'retention': {'skipped': True, 'reason': 'synthetic'}}
        def cleanup(project, plan):
            state = self.state()
            state['regression_preflight'] = {
                'freshness_marker': state['freshness_marker'],
                'config_fingerprint': state['config_fingerprint'],
                'scopes': ['b'], 'cleanup_batches': [concurrent], 'retention': concurrent['retention']}
            self.write('test/.cache/runs/' + self.run + '/state.json', state)
            return {'completed': True, 'files': 0, 'bytes': 0}
        with mock.patch.object(wf.retention, 'make_plan', return_value={}), \
                mock.patch.object(wf.retention, 'apply_plan', side_effect=cleanup):
            self.cli('prepare-regression', '--run', self.run, '--owner', self.owner, '--scopes', 'a')
        result = self.state()['regression_preflight']
        self.assertEqual(['a', 'b'], result['scopes'])
        self.assertEqual(concurrent, result['cleanup_batches'][0])
        self.assertEqual(2, len(result['cleanup_batches']))
        self.assertEqual(['a'], result['plan_scopes'])
        # Individually cleaned concurrent scopes do not imply mixed bundles were cleaned.
        with mock.patch.object(wf.retention, 'make_plan', wraps=wf.retention.make_plan) as plan:
            self.cli('prepare-regression', '--run', self.run, '--owner', self.owner)
        self.assertEqual(['a', 'b'], plan.call_args.args[1])

    def test_schema_is_available_without_project_or_state(self):
        execution = wf.event_schema('execution')
        self.assertEqual('execution', execution['properties']['type']['const'])
        self.assertIn('started_at', execution['required'])
        bundle = wf.event_schema('report_bundle')
        self.assertIn('blob_reports', bundle['properties']['bundle']['required'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = wf.main(['schema', '--type', 'execution'])
        self.assertEqual(0, code)
        self.assertEqual(execution, json.loads(output.getvalue()))

    def test_schema_encodes_dependency_and_failure_detail_conditions(self):
        schema = wf.event_schema('execution')
        dependency = next(rule['then'] for rule in schema['allOf']
                          if rule.get('if') == {'required': ['blocked_by']})
        self.assertEqual({'runner_result', 'reason'}, set(dependency['required']))
        self.assertEqual({'const': 'notrun'}, dependency['properties']['result'])
        self.assertEqual({'const': 'skip'}, dependency['properties']['runner_result'])
        detail = schema['properties']['failure_details']
        self.assertEqual('object', detail['type'])
        self.assertEqual({'expected', 'actual', 'phase', 'classification', 'location', 'cleanup'},
                         set(detail['required']))
        self.assertIn('runtime', schema['properties']['blocked_by']['description'])
        self.assertNotEqual(False, schema.get('additionalProperties'))

    def test_dependency_target_and_present_failure_details_are_checked_at_runtime(self):
        self.start()
        self.check_sources()
        for detail in [{}, None, {'expected': 'pass'}]:
            with self.subTest(detail=detail):
                result = self.record(self.execution('bad', failure_details=detail), fail=True)
                self.assertIn('failure_details requires', result['error'])
        self.record(self.execution('setup-pass', setup=True, case_ids=[]))
        event = self.execution('blocked', result='notrun', runner_result='skip',
                               blocked_by='setup-pass', reason='dependency')
        self.assertIn('failed Setup', self.record(event, fail=True)['error'])
        self.record(self.execution('setup-fail', setup=True, case_ids=[], result='fail', attempt=2))
        event['blocked_by'] = 'setup-fail'
        event['result'] = 'pass'
        self.assertIn('dependency skip requires', self.record(event, fail=True)['error'])
        event['result'] = 'notrun'
        self.record(event)
        self.assertEqual('blocked', self.state()['executions'][-1]['execution_id'])


if __name__ == '__main__':
    unittest.main()
