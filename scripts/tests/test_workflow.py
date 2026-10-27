"""Synthetic behavior tests; no browser, network, or product execution."""
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'workflow.py'
SPEC = importlib.util.spec_from_file_location('workflow', MODULE)
wf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wf)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='workflow-synthetic-')
        self.project = Path(self.temp.name)
        self.owner = 'synthetic-main'
        self.cli('init')
        spec = {'schema_version': 1, 'scope': {'fixed': ['a', 'b'], 'modules': []},
                'source': [{'id': 'S1', 'scope': 'a', 'location': 'test/source-a.txt'}, {'id': 'S2', 'scope': 'b', 'location': 'test/source-b.txt'}],
                'env': {'name': 'synthetic'}, 'authref': 'fixture-reference-only',
                'data-policy': {'allow': ['synthetic-only']}, 'commands': {'test': 'synthetic-command-not-executed'},
                'concurrency': {'max_parallel_agents': 'auto', 'auto_parallel_cap': 2, 'playwright_workers': 1},
                'wait': {'timeout_seconds': 300, 'poll_seconds': 15}}
        self.write('test/spec.md', '# Synthetic\n\n```json workflow-config\n' + json.dumps(spec) + '\n```\n')
        self.write('test/evidence.txt', 'Synthetic evidence, not product proof.\n')
        self.write('test/source-a.txt', 'old confirmed baseline a')
        self.write('test/source-b.txt', 'old confirmed baseline b')
        cov = {'schema_version': 1, 'scopes': ['a', 'b'], 'sources': {
            'S1': {'scope': 'a', 'revision': '1', 'fingerprint': 'f1', 'baseline_path': 'test/source-a.txt'},
            'S2': {'scope': 'b', 'revision': '1', 'fingerprint': 'f1', 'baseline_path': 'test/source-b.txt'}}, 'requirements': {}, 'cases': {}}
        for cid, scope, sid in [('C1', 'a', 'S1'), ('C2', 'b', 'S2')]:
            rid = 'R' + cid[1:]
            self.write('test/plans/' + cid + '.md', '# Synthetic case')
            self.write('test/src/' + cid + '.spec.ts', '// Synthetic; not executable product evidence')
            cov['requirements'][rid] = {'scope': scope, 'source_id': sid, 'expected_version': '1', 'case_ids': [cid], 'review': 'approved'}
            cov['cases'][cid] = {'scope': scope, 'source_id': sid, 'requirement_ids': [rid], 'plan': 'test/plans/' + cid + '.md',
                                 'spec': 'test/src/' + cid + '.spec.ts', 'test_id': cid, 'title': 'Synthetic ' + cid,
                                 'acceptance_version': '1', 'execution': 'automation', 'review': 'approved', 'implementation': 'valid'}
        self.write('test/plans/coverage.json', cov)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, value):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf-8')

    def cli(self, command, *args, fail=False):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = wf.main([command, '--project', str(self.project), *args])
        result = json.loads(buffer.getvalue())
        self.assertEqual(2 if fail else 0, code, result)
        return result

    def start(self, *args):
        result = self.cli('start', '--owner', self.owner, *args)
        self.run = result['run_id']
        self.marker = result['freshness_marker']
        return result

    def record(self, events, fail=False, writer=None):
        self.write('input.json', events)
        args = ['--run', self.run, '--owner', self.owner, '--full', '--input', str(self.project / 'input.json')]
        if writer:
            args += ['--writer', writer]
        return self.cli('record', *args, fail=fail)

    def check_sources(self, *source_ids):
        return self.record([{'type': 'source', 'source_id': sid, 'freshness_marker': self.marker, 'status': 'unchanged',
                             'observed_revision': '1', 'observed_fingerprint': 'f1', 'evidence': ['test/evidence.txt']}
                            for sid in (source_ids or ('S1', 'S2'))])

    def state(self):
        return wf.load(self.project / 'test/.cache/runs' / self.run / 'state.json')

    def execution(self, eid, cid='C1', result='pass', attempt=1, **extra):
        value = {'type': 'execution', 'execution_id': eid, 'test_id': cid, 'case_ids': [cid], 'setup': False,
                 'attempt': attempt, 'result': result, 'started_at': '2026-01-01T00:00:00+00:00',
                 'ended_at': '2026-01-01T00:00:01+00:00', 'evidence': ['test/evidence.txt']}
        value.update(extra)
        return value

    def report(self):
        return self.cli('report', '--run', self.run, '--owner', self.owner)

    def test_fresh_start_requires_current_checks_and_refuses_overwrite(self):
        result = self.start()
        self.assertEqual(['source_check_required'], result['blocked_cases']['C1'])
        before = (self.project / 'test/spec.md').read_text(encoding='utf-8')
        result = self.cli('init')
        self.assertEqual([], result['created'])
        self.assertEqual(before, (self.project / 'test/spec.md').read_text(encoding='utf-8'))
        self.assertTrue((self.project / 'test/.cache/runs' / self.run / 'evidence').is_dir())

    def test_init_discovers_without_authorizing(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root)
            (project / 'package.json').write_text('{"scripts":{"test":"dangerous-not-executed"}}')
            result = wf.initialize(project)
            spec, cov = wf.assets(project)
            self.assertIsNone(spec['authref'])
            self.assertEqual({}, spec['commands'])
            self.assertEqual([], cov['cases'] if isinstance(cov['cases'], list) else list(cov['cases']))
            self.assertIn('package.json', result['discovered_commands'])

    def test_unchanged_covered_direct_regression(self):
        self.start()
        result = self.check_sources()
        self.assertEqual(['C1', 'C2'], result['regression_ready'])
        self.assertEqual([], result['work_needed'])
        self.record([self.execution('E1'), self.execution('E2', 'C2')])
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'})
        self.assertEqual(2, self.report()['counts']['case_results']['pass'])

    def test_renew_preserves_source_observation_and_marker(self):
        self.start()
        self.check_sources()
        before = self.state()
        result = self.cli('renew', '--run', self.run, '--owner', self.owner)
        self.assertEqual(self.marker, result['freshness_marker'])
        self.assertEqual(before['source_checks'], self.state()['source_checks'])
        self.assertFalse(self.state()['recovery_required'])
        self.assertEqual(['C1', 'C2'], result['regression_ready'])
        self.cli('renew', '--run', self.run, '--owner', 'other', fail=True)

    def test_stable_execution_can_finish_while_another_flow_is_exploring(self):
        self.start()
        self.check_sources()
        result = self.record({'type': 'task', 'task_id': 'explore-c1', 'phase': 'exploration',
                              'status': 'running', 'owner': 'explorer', 'case_ids': ['C1']})
        self.assertEqual(['C2'], result['regression_ready'])
        self.record(self.execution('stable-early', 'C2'))
        self.assertEqual('running', self.state()['tasks']['explore-c1']['status'])
        self.assertEqual(['C2'], self.state()['executions'][0]['case_ids'])
        self.record(self.execution('unsafe-early', 'C1'), fail=True)

    def test_renew_rejects_changed_configuration_and_expired_claim(self):
        self.start()
        original = (self.project / 'test/spec.md').read_text(encoding='utf-8')
        spec, _ = wf.assets(self.project)
        spec['env']['name'] = 'different'
        self.write('test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```\n')
        self.cli('renew', '--run', self.run, '--owner', self.owner, fail=True)

        result = self.cli('status', '--run', self.run)
        self.assertTrue(result['configuration_drift'])
        self.assertIn('configuration_review_required', result['blocked_cases']['C1'])
        self.write('test/spec.md', original)
        state = self.state()
        state['claim']['renewed_at'] = '2020-01-01T00:00:00+00:00'
        self.write('test/.cache/runs/' + self.run + '/state.json', state)
        self.cli('renew', '--run', self.run, '--owner', self.owner, fail=True)

    def test_default_claim_lease_covers_long_running_e2e(self):
        result = self.start()
        self.assertEqual(3600, result['claim']['lease_seconds'])
        state = self.state()
        state['claim']['renewed_at'] = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        wf.owner_check(state, self.owner)
        state['claim']['renewed_at'] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with self.assertRaisesRegex(ValueError, 'claim expired'):
            wf.owner_check(state, self.owner)
        state['claim'] = {'owner': self.owner, 'lease_seconds': 300,
                           'renewed_at': (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}
        with self.assertRaisesRegex(ValueError, 'claim expired'):
            wf.owner_check(state, self.owner)

    def test_new_round_cannot_supersede_an_active_command_reservation(self):
        self.start()
        before = self.state()
        self.write('test/.cache/scheduling/quota.json', {'schema_version': 1, 'leases': {
            'lease': {'run_id': self.run, 'status': 'active'}}})
        self.cli('start', '--owner', self.owner, '--new-round', fail=True)
        self.assertEqual(before, self.state())

    def test_new_round_carries_pending_obligations_not_old_execution_or_reports(self):
        self.start()
        old = self.run
        self.check_sources()
        self.record(self.execution('old-execution'))
        self.record({'type': 'human', 'request_id': 'H1', 'mode': 'explicit_decision',
                     'status': 'waiting', 'case_ids': ['C1']})
        self.record({'type': 'issue', 'issue_id': 'I1', 'status': 'open',
                     'kind': 'needs_decision', 'affected_case_ids': ['C1']})
        state = self.state()
        state['report_bundles'] = {'broken-old-report': {'execution_result': 'missing'}}
        self.write('test/.cache/runs/' + old + '/state.json', state)
        result = self.start('--new-round')
        self.assertNotEqual(old, self.run)
        self.assertIn('H1', result['pending_humans'])
        self.assertIn('I1', self.state()['issues'])
        self.assertEqual([], self.state()['executions'])
        self.assertEqual({}, self.state().get('report_bundles', {}))
        self.assertEqual('unregistered', self.report()['report_consistency']['status'])
        self.assertEqual('partially_blocked', wf.load(self.project / 'test/.cache/runs' / old / 'state.json')['acceptance'])

    def test_new_round_does_not_replace_running_tasks_or_other_owner(self):
        self.start()
        self.record({'type': 'task', 'task_id': 'T1', 'owner': 'worker', 'phase': 'code',
                     'status': 'running', 'case_ids': ['C1']})
        self.cli('start', '--owner', self.owner, '--new-round', fail=True)
        self.cli('start', '--owner', 'other', '--new-round', fail=True)
        self.cli('start', '--owner', self.owner, '--resume', self.run, '--new-round', fail=True)

    def test_regression_inventory_lists_unreviewed_existing_code(self):
        spec, cov = wf.assets(self.project)
        cov['cases']['C2']['review'] = 'pending'
        self.write('test/plans/coverage.json', cov)
        self.start()
        result = self.check_sources()
        self.assertEqual('all_existing_automation', result['regression_policy'])
        self.assertFalse(result['regression_inventory']['C2']['ready'])
        self.assertIn('review_not_approved', result['regression_inventory']['C2']['blocked_by'])

    def test_changed_source_selective_work_and_effective_baseline(self):
        self.start()
        self.check_sources('S2')
        self.record({'type': 'source', 'source_id': 'S1', 'freshness_marker': self.marker, 'status': 'changed',
                     'observed_revision': '2', 'observed_fingerprint': 'f2', 'business_change': True, 'evidence': ['test/evidence.txt']})
        self.assertEqual('1', wf.assets(self.project)[1]['sources']['S1']['revision'])
        result = self.cli('status', '--run', self.run)
        self.assertEqual(['C2'], result['regression_ready'])
        self.record(self.execution('E2', 'C2'))
        self.record(self.execution('E1'), fail=True)
        self.record([{'type': 'task', 'task_id': 'impact', 'phase': 'impact', 'status': 'complete', 'owner': self.owner,
                      'source_id': 'S1', 'affected_case_ids': ['C1']},
                     {'type': 'task', 'task_id': 'review', 'phase': 'case_review', 'status': 'approved', 'owner': self.owner, 'source_id': 'S1'}])
        cov = wf.assets(self.project)[1]
        case = dict(cov['cases']['C1'], acceptance_version='2')
        req = dict(cov['requirements']['R1'], expected_version='2')
        self.record({'type': 'case', 'case_id': 'C1', 'action': 'updated', 'code': 'complete', 'code_action': 'modified',
                     'exploration': 'complete', 'exploration_action': 'executed', 'coverage': case, 'requirements': {'R1': req},
                     'evidence': ['test/evidence.txt']})
        self.record({'type': 'baseline', 'source_id': 'S1', 'approved': True, 'revision': '2', 'fingerprint': 'f2',
                     'baseline_path': 'test/source-a.txt'})
        result = self.record(self.execution('E1'))
        self.assertEqual(['C1', 'C2'], result['regression_ready'])

    def test_same_source_unaffected_case_remains_ready_after_impact(self):
        spec, cov = wf.assets(self.project)
        spec['source'] = [{'id': 'S1', 'scopes': ['a', 'b'], 'location': 'test/source-a.txt'}]
        cov['sources'] = {'S1': dict(cov['sources']['S1'], scopes=['a', 'b'])}
        cov['cases']['C2']['source_id'] = 'S1'
        cov['requirements']['R2']['source_id'] = 'S1'
        self.write('test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```\n')
        self.write('test/plans/coverage.json', cov)
        self.start()
        self.record({'type': 'source', 'source_id': 'S1', 'freshness_marker': self.marker, 'status': 'changed',
                     'observed_revision': '2', 'observed_fingerprint': 'f2', 'business_change': True, 'evidence': ['test/evidence.txt']})
        result = self.record({'type': 'task', 'task_id': 'impact', 'phase': 'impact', 'status': 'complete', 'owner': self.owner,
                              'source_id': 'S1', 'affected_case_ids': ['C1']})
        self.assertEqual(['C2'], result['regression_ready'])
        self.record(self.execution('unrelated', 'C2'))

    def test_pending_human_and_partial_case_work_survive_resume(self):
        self.start()
        self.check_sources()
        self.record({'type': 'human', 'request_id': 'H1', 'mode': 'explicit_decision', 'status': 'waiting', 'case_ids': ['C1']})
        self.record({'type': 'case', 'case_id': 'C1', 'action': 'needs_decision', 'code': 'pending'})
        oldmarker = self.marker
        result = self.start()
        self.assertNotEqual(oldmarker, self.marker)
        self.assertIn('recovery_review_required', result['blocked_cases']['C1'])
        self.record({'type': 'source', 'source_id': 'S1', 'freshness_marker': oldmarker, 'status': 'unchanged',
                     'observed_revision': '1', 'observed_fingerprint': 'f1', 'evidence': ['test/evidence.txt']}, fail=True)
        self.check_sources()
        result = self.record({'type': 'recovery_review', 'environment_checked': True, 'artifacts_checked': True})
        self.assertIn('human_decision_required', result['blocked_cases']['C1'])
        self.assertEqual(['C2'], result['regression_ready'])
        self.record(self.execution('E1'), fail=True)

    def test_unavailable_old_baseline_regression_pauses_updates(self):
        self.start()
        self.check_sources('S2')
        result = self.record({'type': 'source', 'source_id': 'S1', 'freshness_marker': self.marker,
                              'status': 'unavailable', 'observed_revision': None})
        self.assertEqual(['C1', 'C2'], result['regression_ready'])
        self.assertIn('S1', result['requirements_updates_paused'])
        self.record(self.execution('E1'))
        case = wf.assets(self.project)[1]['cases']['C1']
        self.record({'type': 'case', 'case_id': 'C1', 'action': 'updated', 'coverage': case}, fail=True)
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'}, fail=True)

    def test_uncertain_create_requires_reconcile_and_exact_record_id(self):
        self.start()
        self.check_sources()
        intent = {'type': 'resource', 'resource_id': 'intent-1', 'status': 'intent', 'kind': 'record', 'created_by_run': self.run}
        self.record(intent)
        self.record(dict(intent, status='ambiguous'))
        self.cli('release', '--run', self.run, '--owner', self.owner)
        result = self.start()
        self.assertEqual('reconcile', result['residual_resources'][0]['suggestion'])
        self.assertEqual('reconcile_or_cleanup_recorded_resources', result['next_steps'][0])
        self.record(dict(intent, status='confirmed', owned=True, receipt='synthetic-create-receipt', record_id='server-generated-ULID'))
        self.assertEqual('server-generated-ULID', self.state()['resources']['intent-1']['record_id'])
        self.check_sources()
        self.record({'type': 'recovery_review', 'environment_checked': True, 'artifacts_checked': True})
        self.record([self.execution('E1'), self.execution('E2', 'C2')])
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'}, fail=True)
        self.record(dict(intent, status='absence_verified', evidence=['test/evidence.txt']))
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'})

    def test_human_bounded_wait_never_approves_and_delegation_blocks(self):
        self.start()
        self.check_sources()
        self.record({'type': 'human', 'request_id': 'H1', 'mode': 'explicit_decision', 'status': 'waiting',
                     'case_ids': ['C1'], 'timeout_seconds': 1, 'at': '2020-01-01T00:00:00+00:00'})
        view = self.cli('status', '--run', self.run)
        self.assertEqual('blocked', view['pending_humans']['H1']['status'])
        self.record({'type': 'human', 'request_id': 'H1', 'status': 'resolved'}, fail=True)
        self.record({'type': 'human', 'request_id': 'H1', 'status': 'approved'})
        view = self.record({'type': 'delegation', 'status': 'unavailable', 'case_ids': ['C1'], 'reason': 'no tool'})
        self.assertIn('delegation_unavailable', view['blocked_cases']['C1'])
        self.assertEqual(['C2'], view['regression_ready'])

    def test_counts_setup_reruns_legacy_groups_shared_issues_and_times(self):
        self.start()
        self.check_sources()
        self.record([self.execution('setup', 'setup', setup=True, case_ids=[]), self.execution('E1', result='fail'),
                     self.execution('E2', result='pass', attempt=2),
                     self.execution('group', 'group', case_ids=['C1', 'C2'], legacy_group=True)])
        self.record([{'type': 'issue', 'issue_id': 'BUG1', 'kind': 'product_candidate', 'affected_case_ids': ['C1'], 'status': 'open'},
                     {'type': 'issue', 'issue_id': 'BUG1', 'kind': 'product_candidate', 'affected_case_ids': ['C2'], 'status': 'open'},
                     {'type': 'issue', 'issue_id': 'ENV1', 'kind': 'environment', 'affected_case_ids': ['C2'], 'status': 'open'},
                     {'type': 'phase_time', 'phase': 'exploration', 'task_id': 'T1', 'owner': 'worker1',
                      'started_at': '2026-01-01T00:00:00+00:00', 'ended_at': '2026-01-01T00:00:10+00:00'},
                     {'type': 'phase_time', 'phase': 'code', 'task_id': 'T2', 'owner': 'worker2',
                      'started_at': '2026-01-01T00:00:05+00:00', 'ended_at': '2026-01-01T00:00:15+00:00'}])
        report = self.report()
        self.assertEqual(1, report['counts']['setup_executions'])
        self.assertEqual(3, report['counts']['business_executions'])
        self.assertEqual(2, report['counts']['independent_business_tests'])
        self.assertEqual(1, report['counts']['product_candidates'])
        self.assertEqual(2, report['counts']['issues'])
        self.assertEqual('notrun', report['cases']['C2']['result'])
        self.assertEqual(['C1', 'C2'], report['issues']['BUG1']['affected_case_ids'])
        self.assertEqual(15, report['timing']['phase_union_seconds'])
        self.assertEqual(10, report['timing']['phase_seconds']['exploration'])
        self.assertIsNone(report['usage']['total_tokens'])

    def test_usage_inclusive_no_double_count_and_nulls(self):
        self.start()
        def usage(mid, count, **extra):
            value = {'type': 'usage', 'measurement_id': mid, 'source': 'host', 'aggregation': 'exclusive',
                     'metrics': {'input_tokens': count, 'output_tokens': count, 'total_tokens': None if count is None else count * 2}}
            value.update(extra)
            return value
        self.record([usage('child', 5), usage('parent', 10, aggregation='inclusive', includes=['child'], coverage='complete_run', coverage_description='Synthetic complete main and delegate host snapshot')])
        self.assertEqual(20, self.report()['usage']['total_tokens'])
        self.record(usage('duplicate-parent', 10, aggregation='inclusive', includes=['child']), fail=True)
        self.record(usage('unknown', None))
        self.assertIsNone(self.report()['usage']['total_tokens'])

    def test_batch_atomicity_ownership_and_explicit_reclaim(self):
        self.start()
        before = self.state()
        self.record([{'type': 'task', 'task_id': 'T1', 'phase': 'code', 'status': 'running', 'owner': 'worker'},
                     {'type': 'unknown'}], fail=True)
        self.assertEqual(before, self.state())
        self.cli('start', '--owner', 'other', fail=True)
        self.cli('start', '--owner', 'other', '--reclaim', '--reason', 'requested', fail=True)
        state = self.state()
        state['claim']['renewed_at'] = '2020-01-01T00:00:00+00:00'
        self.write('test/.cache/runs/' + self.run + '/state.json', state)
        self.cli('start', '--owner', 'other', fail=True)
        result = self.cli('start', '--owner', 'other', '--reclaim', '--reason', 'expired claim')
        self.assertEqual(self.run, result['run_id'])

    def test_writer_receipts_cannot_expand_assignment_or_decide_acceptance(self):
        self.start()
        task = {'type': 'task', 'task_id': 'T1', 'phase': 'code', 'status': 'pending', 'owner': 'worker', 'case_ids': ['C1']}
        self.record(task)
        self.record(dict(task, status='complete'), writer='worker')
        self.record(dict(task, case_ids=['C1', 'C2'], status='complete'), writer='worker', fail=True)
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'aborted'}, writer='worker', fail=True)
        receipt = self.project / 'test/.cache/runs' / self.run / 'receipts/2.json'
        self.assertEqual('worker', wf.load(receipt)['writer'])

    def test_old_version_pass_and_empty_inventory_do_not_accept(self):
        self.start()
        self.check_sources()
        self.record(self.execution('E1'))
        case = wf.assets(self.project)[1]['cases']['C1']
        case['acceptance_version'] = '2'
        req = wf.assets(self.project)[1]['requirements']['R1']
        req['expected_version'] = '2'
        self.record({'type': 'case', 'case_id': 'C1', 'action': 'updated', 'coverage': case, 'requirements': {'R1': req}})
        self.assertEqual('notrun', self.report()['cases']['C1']['result'])
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'}, fail=True)
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'completed_with_failures'})
        result = self.start()
        self.assertNotEqual(self.run, '')
        self.assertEqual('in_progress', result['acceptance'])
        self.assertIsNone(self.report()['usage']['total_tokens'])

    def test_missing_old_body_does_not_invent_delta_or_missing_evidence(self):
        self.start()
        (self.project / 'test/source-a.txt').unlink()
        event = {'type': 'source', 'source_id': 'S1', 'freshness_marker': self.marker, 'status': 'changed',
                 'observed_revision': '2', 'business_change': True, 'baseline_available': True,
                 'requirement_delta': {'added': 1, 'updated': 0, 'retired': 0}, 'evidence': ['test/evidence.txt']}
        self.record(event, fail=True)
        event.pop('requirement_delta')
        self.record(event)
        self.record({'type': 'task', 'task_id': 'T1', 'owner': self.owner, 'phase': 'impact', 'status': 'complete',
                     'evidence': ['test/missing.txt']}, fail=True)

    def test_cache_loss_reconstructs_gaps_without_historical_usage(self):
        self.start()
        old = self.run
        self.check_sources()
        self.record(self.execution('E1'))
        import shutil
        shutil.rmtree(self.project / 'test/.cache')
        result = self.start()
        self.assertNotEqual(old, self.run)
        self.assertFalse(result['usage_known'])
        self.check_sources()
        report = self.report()
        self.assertEqual('notrun', report['cases']['C1']['result'])
        self.assertIsNone(report['usage']['total_tokens'])


    def test_partial_end_carries_human_task_and_resource_into_new_round(self):
        self.start()
        self.check_sources()
        oldrun = self.run
        self.record([{'type': 'human', 'request_id': 'H1', 'mode': 'explicit_decision', 'status': 'waiting', 'case_ids': ['C1']},
                     {'type': 'task', 'task_id': 'unfinished', 'owner': 'worker', 'phase': 'code', 'status': 'blocked', 'case_ids': ['C1']},
                     {'type': 'resource', 'resource_id': 'intent-1', 'status': 'intent', 'kind': 'record', 'created_by_run': oldrun},
                     {'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'}])
        self.start()
        self.assertNotEqual(oldrun, self.run)
        self.assertEqual(oldrun, self.state()['inherited_from'])
        self.check_sources()
        view = self.record({'type': 'recovery_review', 'environment_checked': True, 'artifacts_checked': True})
        self.assertIn('human_decision_required', view['blocked_cases']['C1'])
        self.assertEqual(['C2'], view['regression_ready'])
        self.assertEqual('reconcile', view['residual_resources'][0]['suggestion'])
        self.record({'type': 'resource', 'resource_id': 'intent-1', 'status': 'absence_verified',
                     'created_by_run': oldrun, 'evidence': ['test/evidence.txt']})

    def test_empty_requirement_inventory_cannot_fully_accept(self):
        spec, cov = wf.assets(self.project)
        cov['requirements'] = {}
        cov['cases'] = {}
        self.write('test/plans/coverage.json', cov)
        self.start()
        self.check_sources()
        self.assertEqual(['a', 'b'], self.cli('status', '--run', self.run)['planning_needed_scopes'])
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'}, fail=True)
        validation = self.cli('validate', '--run', self.run, fail=True)
        self.assertTrue(any('requirement inventory' in error for error in validation['errors']))

    def test_only_missing_case_implementation_needs_work(self):
        spec, cov = wf.assets(self.project)
        cov['cases']['C1']['implementation'] = 'draft'
        self.write('test/plans/coverage.json', cov)
        self.start()
        view = self.check_sources()
        self.assertEqual(['C1'], view['work_needed'])
        self.assertEqual(['C2'], view['regression_ready'])

    def test_actions_are_classified_separately_and_deduplicated_per_case(self):
        self.start()
        self.check_sources()
        self.record([{'type': 'case', 'case_id': 'C1', 'action': 'added', 'exploration': 'complete',
                      'exploration_action': 'executed', 'code': 'complete', 'code_action': 'added'},
                     {'type': 'case', 'case_id': 'C1', 'action': 'updated', 'exploration': 'complete',
                      'exploration_action': 'reused', 'code': 'complete', 'code_action': 'modified'},
                     {'type': 'case', 'case_id': 'C2', 'action': 'needs_decision', 'exploration_action': 'blocked', 'code_action': 'missing'}])
        counts = self.report()['counts']
        self.assertEqual(1, counts['planned_case_actions']['added'])
        self.assertEqual(0, counts['planned_case_actions']['updated'])
        self.assertEqual(0, counts['planned_case_actions']['retired'])
        self.assertEqual(1, counts['exploration_actions']['reused'])
        self.assertEqual(1, counts['code_actions']['modified'])

    def test_approvals_for_old_observed_revision_cannot_approve_new_revision(self):
        self.start()
        self.check_sources()
        self.record([{'type': 'task', 'task_id': 'impact', 'owner': self.owner, 'phase': 'impact', 'status': 'complete', 'source_id': 'S1'},
                     {'type': 'task', 'task_id': 'review', 'owner': self.owner, 'phase': 'case_review', 'status': 'approved', 'source_id': 'S1'}])
        self.record({'type': 'source', 'source_id': 'S1', 'freshness_marker': self.marker, 'status': 'changed',
                     'observed_revision': '2', 'observed_fingerprint': 'f2', 'business_change': True, 'evidence': ['test/evidence.txt']})
        self.record({'type': 'baseline', 'source_id': 'S1', 'approved': True, 'revision': '2', 'fingerprint': 'f2'}, fail=True)

    def test_legacy_step_evidence_controls_per_case_results(self):
        self.start()
        self.check_sources()
        self.record(self.execution('group', 'group', case_ids=['C1', 'C2'], legacy_group=True,
                                   steps={'C1': {'result': 'pass', 'evidence': ['test/evidence.txt']},
                                          'C2': {'result': 'fail', 'evidence': ['test/evidence.txt']}}))
        results = self.report()['counts']['case_results']
        self.assertEqual(1, results['pass'])
        self.assertEqual(1, results['fail'])
        self.record(self.execution('bad-step', 'group', attempt=2, case_ids=['C1', 'C2'], legacy_group=True,
                                   steps={'C1': {'result': 'pass'}}), fail=True)

    def test_resume_review_invalidates_only_named_case_and_old_result_stays_invalid(self):
        self.start()
        self.check_sources()
        self.record([self.execution('E1'), self.execution('E2', 'C2')])
        self.start()
        self.check_sources()
        view = self.record({'type': 'recovery_review', 'environment_checked': True, 'artifacts_checked': True,
                            'invalidate_case_ids': ['C1']})
        self.assertEqual(['C2'], view['regression_ready'])
        self.record({'type': 'case', 'case_id': 'C1', 'action': 'unchanged', 'code': 'complete', 'code_action': 'reused'})
        report = self.report()
        self.assertEqual('notrun', report['cases']['C1']['result'])
        self.assertEqual('pass', report['cases']['C2']['result'])
        self.record(self.execution('E3', attempt=2))
        self.assertEqual('pass', self.report()['cases']['C1']['result'])

    def test_manual_hardware_deferred_dispositions_are_not_code_coverage(self):
        spec, cov = wf.assets(self.project)
        cov['cases']['C1']['execution'] = 'manual'
        self.write('test/plans/coverage.json', cov)
        self.start()
        self.check_sources()
        self.record({'type': 'task', 'task_id': 'manual-disposition', 'owner': self.owner, 'phase': 'case_review',
                     'status': 'approved', 'case_ids': ['C1'], 'dispositions': {'C1': {'mode': 'manual', 'status': 'approved', 'acceptance_version': '1', 'reason': 'Synthetic reviewed manual run', 'evidence': ['test/evidence.txt']}}})
        self.record([self.execution('manual', method='manual'), self.execution('auto', 'C2')])
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'})
        self.assertFalse(self.report()['cases']['C1']['covered'])

    def test_credentials_fields_rejected_without_echoing_values(self):
        self.start()
        result = self.record({'type': 'task', 'password': 'synthetic-sensitive-value'}, fail=True)
        self.assertNotIn('synthetic-sensitive-value', result['error'])


    def test_init_preserves_legacy_spec_and_only_fills_missing_assets(self):
        (self.project / 'test/plans/coverage.json').unlink()
        self.write('test/spec.md', '# Legacy reviewed spec without workflow configuration')
        before = (self.project / 'test/spec.md').read_text(encoding='utf-8')
        result = self.cli('init')
        self.assertEqual(['test/plans/coverage.json'], result['created'])
        self.assertEqual(['test/spec.md'], result['preserved'])
        self.assertEqual(before, (self.project / 'test/spec.md').read_text(encoding='utf-8'))
        self.assertIn('spec.workflow-config', result['missing_configuration'])
        with self.assertRaises(ValueError):
            wf.atomic(self.project / 'test/spec.md', 'replace', exclusive=True)


    def test_retirement_preserves_case_record_and_active_requirement_cannot_map_retired(self):
        self.start()
        self.check_sources()
        req = dict(wf.assets(self.project)[1]['requirements']['R1'], status='retired', case_ids=[])
        self.record({'type': 'case', 'case_id': 'C1', 'action': 'retired', 'code_action': 'retired', 'requirements': {'R1': req}})
        self.assertEqual('retired', wf.assets(self.project)[1]['cases']['C1']['implementation'])
        report = self.report()
        self.assertEqual('retired', report['cases']['C1']['result'])
        self.assertEqual(1, report['counts']['planned_case_actions']['retired'])
        self.assertEqual(0, report['counts']['case_results']['pass'])

    def test_partial_host_measurements_never_become_run_token_totals(self):
        self.start()
        self.record({'type': 'usage', 'measurement_id': 'worker-only', 'source': 'host', 'aggregation': 'exclusive',
                     'metrics': {'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 5}})
        usage = self.report()['usage']
        self.assertIsNone(usage['total_tokens'])
        self.assertEqual(5, usage['recorded_subtotals']['total_tokens'])
        self.assertFalse(usage['complete_run_coverage'])

    def test_changing_module_subset_cannot_bypass_old_pending_human(self):
        self.start('--scopes', 'a')
        self.check_sources('S1')
        self.record({'type': 'human', 'request_id': 'H1', 'mode': 'explicit_decision', 'status': 'waiting', 'case_ids': ['C1']})
        self.cli('start', '--owner', self.owner, '--scopes', 'a,b', fail=True)
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'})
        self.start('--scopes', 'a,b')
        self.check_sources()
        view = self.record({'type': 'recovery_review', 'environment_checked': True, 'artifacts_checked': True})
        self.assertIn('human_decision_required', view['blocked_cases']['C1'])
        self.assertEqual(['C2'], view['regression_ready'])

    def test_parallel_cli_writes_keep_all_receipts_and_valid_json(self):
        import subprocess
        import sys
        self.start()
        processes = []
        for number in range(4):
            path = 'input-' + str(number) + '.json'
            self.write(path, {'type': 'task', 'task_id': 'T' + str(number), 'owner': self.owner, 'phase': 'code', 'status': 'pending', 'case_ids': ['C1']})
            processes.append(subprocess.Popen([sys.executable, str(MODULE), 'record', '--project', str(self.project),
                                               '--run', self.run, '--owner', self.owner, '--input', str(self.project / path)],
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for process in processes:
            out, err = process.communicate(timeout=10)
            self.assertEqual(0, process.returncode, out + err)
        self.assertEqual(4, len(self.state()['tasks']))
        self.assertEqual(4, len(self.state()['events']))


    def test_other_scope_resource_is_visible_as_global_link_and_not_auto_cleaned(self):
        self.start('--scopes', 'a')
        origin = self.run
        self.record({'type': 'resource', 'resource_id': 'outside-scope-intent', 'status': 'intent', 'kind': 'record', 'created_by_run': origin})
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'})
        view = self.start('--scopes', 'b')
        self.assertEqual([], view['residual_resources'])
        self.assertEqual(origin, view['global_residual_runs'][0]['run_id'])
        self.assertEqual('owner_reconcile', view['global_residual_runs'][0]['suggestion'])
        self.assertEqual('intent', wf.load(self.project / 'test/.cache/runs' / origin / 'state.json')['resources']['outside-scope-intent']['status'])


    def shared_source(self):
        spec, cov = wf.assets(self.project)
        spec['source'] = [{'id': 'S1', 'scopes': ['a', 'b'], 'location': 'test/source-a.txt'}]
        cov['sources'] = {'S1': dict(cov['sources']['S1'], scopes=['a', 'b'])}
        cov['cases']['C2']['source_id'] = 'S1'
        cov['requirements']['R2']['source_id'] = 'S1'
        self.write('test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```\n')
        self.write('test/plans/coverage.json', cov)

    def test_human_explicit_case_scope_overrides_source_context_for_all_gates(self):
        self.shared_source()
        self.start()
        self.check_sources('S1')
        view = self.record({'type': 'human', 'request_id': 'H-case', 'mode': 'explicit_decision',
                            'status': 'waiting', 'case_ids': ['C2'], 'source_id': 'S1'})
        self.assertEqual(['C1'], view['regression_ready'])
        self.assertIn('human_decision_required', view['blocked_cases']['C2'])
        cov = wf.assets(self.project)[1]
        self.record({'type': 'case', 'case_id': 'C1', 'action': 'unchanged', 'coverage': cov['cases']['C1']})
        self.record({'type': 'case', 'case_id': 'C2', 'action': 'unchanged', 'coverage': cov['cases']['C2']}, fail=True)
        self.record([{'type': 'task', 'task_id': 'impact', 'phase': 'impact', 'status': 'complete', 'owner': self.owner,
                      'source_id': 'S1', 'affected_case_ids': ['C1']},
                     {'type': 'task', 'task_id': 'review', 'phase': 'case_review', 'status': 'approved', 'owner': self.owner, 'source_id': 'S1'}])
        self.record({'type': 'baseline', 'source_id': 'S1', 'approved': True, 'revision': '1', 'fingerprint': 'f1'})
        self.record({'type': 'human', 'request_id': 'H-case', 'status': 'waiting', 'case_ids': ['C1']})
        self.record({'type': 'baseline', 'source_id': 'S1', 'approved': True, 'revision': '1', 'fingerprint': 'f1'}, fail=True)

    def test_human_scope_update_replaces_both_scope_fields_and_status_only_retains(self):
        self.shared_source()
        self.start()
        self.check_sources('S1')
        self.record({'type': 'human', 'request_id': 'H1', 'mode': 'explicit_decision', 'status': 'waiting', 'source_id': 'S1'})
        view = self.record({'type': 'human', 'request_id': 'H1', 'status': 'waiting', 'case_ids': ['C2']})
        self.assertEqual(['C1'], view['regression_ready'])
        self.assertIsNone(self.state()['humans']['H1']['source_id'])
        self.record({'type': 'human', 'request_id': 'H1', 'status': 'blocked'})
        self.assertEqual(['C2'], self.state()['humans']['H1']['case_ids'])
        self.record({'type': 'human', 'request_id': 'H1', 'status': 'waiting', 'case_ids': []}, fail=True)
        view = self.record({'type': 'human', 'request_id': 'H1', 'status': 'waiting', 'source_id': 'S1'})
        self.assertEqual([], self.state()['humans']['H1']['case_ids'])
        self.assertEqual({'C1', 'C2'}, set(view['blocked_cases']))

    def test_next_steps_include_affected_work_even_when_another_case_is_ready(self):
        self.shared_source()
        self.start()
        self.record({'type': 'source', 'source_id': 'S1', 'freshness_marker': self.marker, 'status': 'changed',
                     'observed_revision': '2', 'observed_fingerprint': 'f2', 'business_change': True, 'evidence': ['test/evidence.txt']})
        view = self.record([{'type': 'task', 'task_id': 'impact', 'phase': 'impact', 'status': 'complete',
                             'owner': self.owner, 'source_id': 'S1', 'affected_case_ids': ['C2']},
                            {'type': 'task', 'task_id': 'clarify', 'phase': 'clarify', 'status': 'running',
                             'owner': self.owner, 'case_ids': ['C2']},
                            {'type': 'case', 'case_id': 'C2', 'action': 'needs_decision', 'code': 'pending'}])
        self.assertEqual(['C1'], view['regression_ready'])
        self.assertIn('run_independent_stable_regression', view['next_steps'])
        for action in ['clarify_affected_requirements', 'review_affected_cases', 'approve_and_update_effective_baseline',
                       'revalidate_or_repair_invalidated_case_artifacts', 'complete_pending_case_exploration_or_code']:
            self.assertIn(action, view['next_steps'])
        self.assertNotIn('analyze_requirement_impact', view['next_steps'])
        view = self.record({'type': 'source', 'source_id': 'S1', 'freshness_marker': self.marker, 'status': 'changed',
                            'observed_revision': '3', 'observed_fingerprint': 'f3', 'business_change': True, 'evidence': ['test/evidence.txt']})
        self.assertIn('analyze_requirement_impact', view['next_steps'])

    def test_execution_missing_timestamps_names_both_required_fields(self):
        self.start()
        self.check_sources()
        event = self.execution('E1')
        event.pop('started_at')
        result = self.record(event, fail=True)
        self.assertEqual('execution requires started_at and ended_at timestamps', result['error'])
        event.pop('ended_at')
        self.assertEqual(result, self.record(event, fail=True))


if __name__ == '__main__':
    unittest.main()
