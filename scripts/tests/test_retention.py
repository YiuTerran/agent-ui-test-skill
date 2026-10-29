"""Synthetic tmp-only retention tests; never execute tests against a product."""
import test_platform_support as platform_tests
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / 'retention.py'
SPEC = importlib.util.spec_from_file_location('retention_under_test', MODULE)
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='retention-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.spec = {'scope': {'fixed': ['a', 'b']}}
        self.cov = {'scopes': ['a', 'b'], 'sources': {}, 'cases': {}, 'requirements': {}}
        self.write('test/spec.md', '```json workflow-config\n' + json.dumps(self.spec) + '\n```\n')
        self.write('test/plans/coverage.json', self.cov)
        self.run = 'old'
        self.prefix = r.RUNS + '/old'
        self.state = self.new_state('old')
        self.raw_path = self.prefix + '/attempts/B/results/result.json'
        self.html = self.prefix + '/attempts/B/report/playwright/index.html'
        self.trace = self.prefix + '/attempts/B/results/output/trace.zip'
        self.noise = self.prefix + '/attempts/B/results/output/unrelated.png'
        self.raw = {'stats': {'expected': 1, 'unexpected': 0}, 'errors': [], 'suites': [
            {'specs': [{'tests': [{'status': 'expected', 'results': [
                {'status': 'passed', 'attachments': [{'path': self.trace}]}]}]}]}]}
        self.write(self.raw_path, self.raw)
        self.write(self.html, '<html>Synthetic report</html>')
        self.write(self.trace, 'synthetic trace')
        self.write(self.noise, 'synthetic unrelated attachment')
        self.write(self.prefix + '/attempts/B/command.json', {'exit_code': 0})
        self.write(self.prefix + '/summary.json', {'counts': {'pass': 1}})
        self.write(self.prefix + '/report.md', 'Synthetic summary')
        self.bundle = dict(bundle_id='B', run_id='old', scope=['a'], report_consistency='passed',
                           execution_result=self.raw_path, html_report=self.html,
                           runner_counts={'expected': 1, 'unexpected': 0, 'flaky': 0, 'skipped': 0},
                           setup_targets=0, business_targets=1, business_attempts=1, raw_business_skipped=0)
        self.refresh_bundle()
        self.execution = dict(execution_id='E1', bundle_id='B', result='pass', case_ids=['C1'],
                              runner_test_pointer='/suites/0/specs/0/tests/0', runner_result_index=0,
                              evidence=[self.trace])
        self.state['executions'] = [self.execution]
        self.state['events'] = [dict(type='report_bundle', bundle=self.bundle),
                                dict(self.execution, type='execution')]
        self.save_state()

    def new_state(self, rid):
        return dict(run_id=rid, scopes=['a'], acceptance='fully_accepted', claim=None,
                    recovery_required=False, tasks={}, humans={}, issues={}, resources={},
                    case_actions={}, source_checks={}, events=[], executions=[], report_bundles={})

    def write(self, path, value):
        target = self.project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf-8')

    def save_state(self):
        self.write(self.prefix + '/state.json', self.state)

    def refresh_bundle(self):
        self.bundle['artifact_sha256'] = {p: hashlib.sha256((self.project / p).read_bytes()).hexdigest()
                                          for p in (self.raw_path, self.html, self.trace)}
        self.state['report_bundles'] = {'B': self.bundle}

    def plan(self, scopes=None, current=None):
        self.save_state()
        return r.make_plan(self.project, scopes or ['a'], current)

    def paths(self, plan):
        return {f['path'] for f in plan['files']}

    def failure(self):
        self.execution['result'] = 'fail'
        self.raw['suites'][0]['specs'][0]['tests'][0]['results'][0]['status'] = 'failed'
        self.raw['stats'] = {'expected': 0, 'unexpected': 1}
        self.bundle['runner_counts'].update(expected=0, unexpected=1)
        self.write(self.raw_path, self.raw)
        self.refresh_bundle()
        self.state['acceptance'] = 'completed_with_failures'

    def issue(self, status='open'):
        self.state['issues']['I'] = dict(issue_id='I', status=status, affected_case_ids=['C1'])

    def test_full_recycle_keeps_state_summary_command_and_counts(self):
        plan = self.plan()
        before = (self.project / (self.prefix + '/state.json')).read_bytes()
        result = r.apply_plan(self.project, plan)
        self.assertEqual(4, result['files'])
        for path in self.paths(plan):
            self.assertFalse((self.project / path).exists())
            self.assertTrue(r.is_recycled(self.project, path))
        self.assertEqual(before, (self.project / (self.prefix + '/state.json')).read_bytes())
        self.assertTrue((self.project / (self.prefix + '/summary.json')).exists())
        self.assertTrue((self.project / (self.prefix + '/report.md')).exists())
        self.assertTrue((self.project / (self.prefix + '/attempts/B/command.json')).exists())
        marker = r.recycled_bundle(self.project, 'old', 'B')
        self.assertTrue(marker['recycled'])
        self.assertEqual(self.bundle['runner_counts'], marker['runner_counts'])
        self.assertEqual('passed', marker['report_consistency'])
        manifest = json.loads((self.project / result['manifest']).read_text(encoding='utf-8'))
        for path, item in manifest['files'].items():
            self.assertEqual(path, item['original']['path'])
            self.assertEqual(64, len(item['original']['sha256']))
            self.assertTrue(item['recycled_at'])
            self.assertEqual(['a'], item['original']['scopes'])

    def pool_ledger(self, status='available'):
        baseline = self.prefix + '/attempts/B/results/output/pool-baseline.json'
        recovery = self.prefix + '/attempts/B/results/output/pool-quarantine.json'
        self.write(baseline, {'roles': ['reader']})
        self.write(recovery, {'unexpected_role': 'writer'})
        account = {'pool_ref': 'user-01', 'status': status, 'origin': ['old', 'a1'],
                   'baseline_evidence': [baseline]}
        if status == 'quarantined':
            account['history'] = [{'action': 'returned_quarantined', 'evidence': [recovery]}]
        ledger = {'schema_version': 1, 'pools': {'pool-a': {'accounts': {'user-01': account}}}}
        self.state['resources']['a1'] = {
            'resource_id': 'a1', 'status': 'retained_verified', 'retention': 'pooled',
            'pool': 'pool-a', 'pool_ref': 'user-01', 'evidence': [baseline]}
        self.write(r.POOL_LEDGER, ledger)
        return ledger, baseline, recovery

    def test_pool_baseline_and_quarantine_evidence_survive_history_cleanup(self):
        ledger, baseline, recovery = self.pool_ledger('quarantined')
        plan = self.plan()
        self.assertNotIn(baseline, self.paths(plan))
        self.assertNotIn(recovery, self.paths(plan))
        self.assertIn(self.noise, self.paths(plan))
        r.apply_plan(self.project, plan)
        self.assertTrue((self.project / baseline).exists())
        self.assertTrue((self.project / recovery).exists())
        self.assertFalse((self.project / self.noise).exists())

    def test_pool_ledger_changes_after_plan_prevent_deletion(self):
        ledger, baseline, recovery = self.pool_ledger()
        plan = self.plan()
        ledger['pools']['pool-a']['accounts']['user-01']['baseline_evidence'] = [recovery]
        self.write(r.POOL_LEDGER, ledger)
        with self.assertRaisesRegex(ValueError, 'identity pool ledger changed'):
            r.apply_plan(self.project, plan)
        self.assertTrue((self.project / self.noise).exists())

    def test_pool_ledger_created_after_plan_prevents_deletion(self):
        plan = self.plan()
        self.write(r.POOL_LEDGER, {'schema_version': 1, 'pools': {}})
        with self.assertRaisesRegex(ValueError, 'identity pool ledger changed'):
            r.apply_plan(self.project, plan)

    def test_pool_ledger_deleted_after_plan_prevents_deletion(self):
        self.pool_ledger()
        plan = self.plan()
        (self.project / r.POOL_LEDGER).unlink()
        with self.assertRaisesRegex(ValueError, 'identity pool ledger changed'):
            r.apply_plan(self.project, plan)
        self.assertTrue((self.project / self.noise).exists())

    def test_pool_current_evidence_is_protected_without_pinning_all_old_history(self):
        ledger, baseline, recovery = self.pool_ledger('quarantined')
        account = ledger['pools']['pool-a']['accounts']['user-01']
        account['quarantine'] = {'evidence': [recovery]}
        account['history'] = [
            {'action': 'returned_pooled', 'evidence': [self.noise]},
            {'action': 'returned_quarantined', 'evidence': [recovery]}]
        self.write(r.POOL_LEDGER, ledger)
        plan = self.plan()
        self.assertNotIn(baseline, self.paths(plan))
        self.assertNotIn(recovery, self.paths(plan))
        self.assertIn(self.noise, self.paths(plan))
        reasons = {item['reason'] for item in plan['diagnostics']['protections']}
        self.assertIn('identity_pool_reference', reasons)

    def test_disabled_pool_account_retains_its_baseline(self):
        ledger, baseline, recovery = self.pool_ledger('disabled')
        self.assertNotIn(baseline, self.paths(self.plan()))

    def test_batch_recycling_validates_journals_once_and_has_no_cross_call_cache(self):
        r.apply_plan(self.project, self.plan())
        paths = [self.raw_path, self.html, self.trace, self.noise]
        absolute = str(self.project / self.html)
        with mock.patch.object(r, 'manifests', wraps=r.manifests) as scans:
            result = r.recycled_paths(self.project, paths * 20 + [absolute, '../unsafe', '/tmp/outside'])
        self.assertEqual(set(paths + [absolute]), result)
        self.assertEqual(1, scans.call_count)
        self.write(self.html, 'recreated')
        self.assertEqual(set(paths) - {self.html}, r.recycled_paths(self.project, paths))
        self.assertFalse(r.is_recycled(self.project, absolute))

    def test_batch_recycling_rejects_damaged_journals_and_symlinks(self):
        applied = r.apply_plan(self.project, self.plan())
        platform_tests.symlink(self, self.project / self.html, self.project / (self.prefix + '/state.json'))
        self.assertEqual(set(), r.recycled_paths(self.project, [self.html]))
        (self.project / self.html).unlink()
        manifest = json.loads((self.project / applied['manifest']).read_text(encoding='utf-8'))
        manifest['project'] = '/different-project'
        self.write(applied['manifest'], manifest)
        self.assertEqual(set(), r.recycled_paths(self.project, [self.html, self.raw_path]))

    def test_apply_is_idempotent_and_replan_understands_manifest(self):
        plan = self.plan()
        first = r.apply_plan(self.project, plan)
        repeated = r.apply_plan(self.project, plan)
        self.assertFalse(first['replayed'])
        self.assertTrue(repeated['replayed'])
        self.assertEqual(dict(first, replayed=True), repeated)
        fresh = r.make_plan(self.project, ['a'])
        self.assertEqual([], fresh['files'])
        r.apply_plan(self.project, fresh)
        self.assertTrue(r.recycled_bundle(self.project, 'old', 'B'))

    def test_cleanup_diagnostics_reconcile_candidates_and_measured_timing(self):
        plan = self.plan()
        detail = plan['diagnostics']
        self.assertEqual(4, detail['candidate_files'])
        self.assertEqual(4, detail['selected_files'])
        self.assertEqual(0, detail['protected_files'])
        self.assertIsNone(detail['zero_reason'])
        result = r.apply_plan(self.project, plan)
        self.assertEqual(detail, result['diagnostics'])
        self.assertEqual(plan['cleanup_scan_seconds'], result['timing']['cleanup_scan_seconds'])
        self.assertGreaterEqual(result['timing']['cleanup_apply_seconds'], 0)
        self.assertGreaterEqual(plan['cleanup_scan_seconds'], 0)
        manifest = json.loads((self.project / result['manifest']).read_text(encoding='utf-8'))
        self.assertEqual(result['timing'], manifest['timing'])
        self.assertEqual(result['timing'], r.apply_plan(self.project, plan)['timing'])

    def test_zero_candidates_explain_no_eligible_history_without_error(self):
        plan = self.plan(current='old')
        self.assertEqual(0, plan['diagnostics']['candidate_files'])
        self.assertEqual('no_eligible_history', plan['diagnostics']['zero_reason'])
        self.assertGreater(plan['diagnostics']['excluded_files_by_reason'][
            'current_active_or_unfinished_run'], 0)
        result = r.apply_plan(self.project, plan)
        self.assertTrue(result['completed'])
        self.assertEqual(0, result['files'])

    def test_zero_selection_explains_exact_approval_field(self):
        self.state['humans']['H'] = dict(status='approved', evidence=['test/.cache/'])
        plan = self.plan()
        detail = plan['diagnostics']
        self.assertEqual(4, detail['candidate_files'])
        self.assertEqual(4, detail['protected_files'])
        self.assertEqual('protected_references', detail['zero_reason'])
        protection = detail['protections'][0]
        self.assertEqual('test/.cache', protection['path'])
        self.assertEqual(self.prefix + '/state.json', protection['source_file'])
        self.assertEqual('/humans/H/evidence/0', protection['field'])
        self.assertEqual('effective_approval', protection['reason'])
        self.assertEqual(4, protection['files'])
        self.assertEqual(3, len(protection['sample_paths']))
        self.assertEqual(0, r.apply_plan(self.project, plan)['files'])

    def test_diagnostics_locate_config_and_relative_plan_link(self):
        self.cov['sources']['S'] = {'baseline_path': self.html}
        self.write('test/plans/coverage.json', self.cov)
        self.write('test/plans/approval.md', '[trace](../.cache/runs/old/attempts/B/results/output/trace.zip)')
        plan = self.plan()
        detail = plan['diagnostics']
        self.assertEqual(2, detail['selected_files'])
        self.assertEqual(2, detail['protected_files'])
        by_path = {item['path']: item for item in detail['protections']}
        self.assertEqual('/sources/S/baseline_path', by_path[self.html]['field'])
        self.assertEqual('test/plans/coverage.json', by_path[self.html]['source_file'])
        self.assertEqual('test/plans/approval.md', by_path[self.trace]['source_file'])
        self.assertEqual('explicit_plan_link', by_path[self.trace]['reason'])

    def test_unexplained_candidate_fails_with_structured_evidence(self):
        with self.assertRaises(r.CleanupCandidateMismatch) as raised:
            r.candidate_diagnostics({self.raw_path}, set(), {}, {})
        self.assertEqual('cleanup_candidate_mismatch', str(raised.exception))
        self.assertEqual([self.raw_path], raised.exception.diagnostics['unexplained_paths'])
        self.assertEqual(1, raised.exception.diagnostics['candidate_files'])
        self.assertTrue((self.project / self.raw_path).exists())

    def test_legacy_registered_plan_without_diagnostics_remains_applicable(self):
        plan = self.plan()
        plan.pop('diagnostics')
        plan.pop('cleanup_scan_seconds')
        plan['plan_id'] = r.digest(r.plan_body(plan))
        with r.locked(self.project) as tree:
            tree.write(r.STORE + '/plans/' + plan['plan_id'] + '.json', plan, exclusive=True)
        result = r.apply_plan(self.project, plan)
        self.assertTrue(result['completed'])
        self.assertIsNone(result['diagnostics'])
        self.assertIsNone(result['timing']['cleanup_scan_seconds'])

    def test_stage_is_hashed_once_after_move_and_once_before_delete(self):
        plan = self.plan()
        stage_reads = []
        original = r.Tree.read

        def read(tree, path):
            if path.startswith(r.STORE + '/quarantine/'):
                stage_reads.append(path)
            return original(tree, path)

        with mock.patch.object(r.Tree, 'read', read):
            r.apply_plan(self.project, plan)
        for index, item in enumerate(plan['files']):
            path = r.STORE + '/quarantine/' + plan['plan_id'] + '/' + str(index)
            # Windows verifies deletion through its pinned handle, not Tree.read.
            self.assertEqual(1 if os.name == 'nt' else 2, stage_reads.count(path))
            self.assertTrue(r.is_recycled(self.project, item['path']))

    def test_current_active_nonterminal_and_recovery_runs_are_protected(self):
        for kind in ('current', 'lease', 'unfinished', 'recovery'):
            with self.subTest(kind=kind):
                self.state.update(acceptance='fully_accepted', recovery_required=False, claim=None)
                if kind == 'lease':
                    self.state['claim'] = dict(owner='running', renewed_at=r.now(), lease_seconds=1)
                elif kind == 'unfinished':
                    self.state['acceptance'] = 'blocked'
                elif kind == 'recovery':
                    self.state['recovery_required'] = True
                self.assertEqual([], self.plan(current='old' if kind == 'current' else None)['files'])

    def test_expired_claim_is_not_an_active_run(self):
        self.state['claim'] = dict(owner='old', renewed_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
        self.assertIn(self.raw_path, self.paths(self.plan()))

    def test_long_workflow_claim_stays_active_and_legacy_claim_is_not_extended(self):
        self.state['claim'] = dict(owner='running', lease_seconds=3600,
                                   renewed_at=(datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat())
        self.assertEqual([], self.plan()['files'])
        self.state['claim']['lease_seconds'] = 300
        self.assertIn(self.raw_path, self.paths(self.plan()))

    def test_mixed_bundle_requires_every_scope(self):
        self.state['scopes'] = ['a', 'b']
        self.bundle['scope'] = ['a', 'b']
        self.assertEqual([], self.plan(['a'])['files'])
        self.assertIn(self.raw_path, self.paths(self.plan(['a', 'b'])))

    def test_completed_provenance_does_not_pin_temporary_sources(self):
        paths = [self.prefix + '/sources/old.md', self.prefix + '/handoffs/finished.json',
                 self.prefix + '/discovery/old.txt']
        for path in paths:
            self.write(path, 'obsolete')
        self.state['source_checks']['S'] = {'evidence': paths}
        self.state['tasks']['T'] = dict(phase='impact', status='complete', evidence=paths)
        self.state['events'] += [dict(type='source', evidence=paths),
                                 dict(type='task', phase='code', status='complete', evidence=paths)]
        self.execution['evidence'] += paths
        self.assertTrue(set(paths) <= self.paths(self.plan()))

    def test_effective_baseline_and_transitive_source_manifest_protected(self):
        source = self.prefix + '/sources/effective.md'
        manifest = self.prefix + '/sources/effective.json'
        obsolete = self.prefix + '/sources/obsolete.md'
        self.write(source, 'effective baseline')
        self.write(obsolete, 'old baseline')
        self.write(manifest, {'files': {source: 'hash'}})
        self.cov['sources']['S'] = {'baseline_path': manifest}
        self.write('test/plans/coverage.json', self.cov)
        self.state['events'].append(dict(type='baseline', baseline_path=obsolete, approved=True))
        selected = self.paths(self.plan())
        self.assertNotIn(manifest, selected)
        self.assertNotIn(source, selected)
        self.assertIn(obsolete, selected)

    def test_approval_decisions_plan_links_and_dispositions_protected(self):
        paths = [self.prefix + '/handoffs/' + p for p in ('human.txt', 'review.txt', 'disposition.txt', 'plan.txt')]
        for path in paths:
            self.write(path, 'decision evidence')
        self.state['humans']['H'] = dict(status='approved', case_ids=['C1'], evidence=[paths[0]])
        self.state['tasks']['T'] = dict(phase='case_review', status='approved', case_ids=['C1'], evidence=[paths[1]])
        self.state['tasks']['D'] = dict(phase='code', status='complete', dispositions={'C1': {'evidence': [paths[2]]}})
        self.write('test/plans/review.md', '[decision](../.cache/runs/old/handoffs/plan.txt)')
        selected = self.paths(self.plan())
        self.assertFalse(set(paths) & selected)
        # Case review approval alone is not a reference to every later execution.
        self.assertIn(self.raw_path, selected)

    def test_latest_absence_and_closed_issue_release_old_events(self):
        source = self.prefix + '/handoffs/cleanup.txt'
        self.write(source, 'old cleanup evidence')
        self.state['resources']['R'] = dict(status='absence_verified', evidence=[source])
        self.state['issues']['I'] = dict(status='closed', evidence=[source], affected_case_ids=['C1'])
        self.state['events'] += [dict(type='resource', status='confirmed', evidence=[source]),
                                 dict(type='issue', status='open', evidence=[source])]
        self.assertIn(source, self.paths(self.plan()))

    def test_only_live_or_effective_reuse_inventory_is_protected(self):
        paths = [self.prefix + '/sources/' + name for name in ('reuse.md', 'hit.md')]
        for path in paths:
            self.write(path, 'reusable')
        self.state['source_reuse'] = [paths[0]]
        self.state['source_checks']['S'] = {'cache_hit': True, 'action': 'reuse', 'files': [paths[1]]}
        self.assertTrue(set(paths) <= self.paths(self.plan()))
        self.state['acceptance'] = 'in_progress'
        self.assertFalse(set(paths) & self.paths(self.plan()))
        self.state['acceptance'] = 'fully_accepted'
        self.cov['sources']['S'] = {'baseline_path': paths[0]}
        self.write('test/plans/coverage.json', self.cov)
        selected = self.paths(self.plan())
        self.assertNotIn(paths[0], selected)
        self.assertIn(paths[1], selected)

    def test_active_command_quota_protects_ended_run_until_release(self):
        path = 'test/.cache/scheduling/quota.json'
        ledger = {'schema_version': 1, 'leases': {'lease': {'run_id': 'old', 'status': 'active'}}}
        self.write(path, ledger)
        self.assertEqual([], self.plan()['files'])
        ledger['leases']['lease']['status'] = 'released'
        self.write(path, ledger)
        self.assertIn(self.raw_path, self.paths(self.plan()))

    def test_new_reservation_invalidates_existing_cleanup_plan(self):
        plan = self.plan()
        self.write('test/.cache/scheduling/quota.json', {'schema_version': 1, 'leases': {
            'lease': {'run_id': 'old', 'status': 'active'}}})
        with self.assertRaisesRegex(ValueError, 'scheduling reservations changed'):
            r.apply_plan(self.project, plan)
        self.assertTrue((self.project / self.raw_path).is_file())

    def test_incomplete_legacy_bundle_is_recycled_with_limitations(self):
        self.state['report_bundles']['B'] = {'execution_result': self.raw_path}
        plan = self.plan()
        self.assertIn(self.raw_path, self.paths(plan))
        self.assertIn('historical_bundle_identity_incomplete', {item['reason'] for item in plan['limitations']})
        self.assertTrue(r.apply_plan(self.project, plan)['completed'])

    def test_active_cross_run_references_protect_history(self):
        other = self.new_state('live')
        other['acceptance'] = 'in_progress'
        other['events'] = [dict(type='source', evidence=[self.raw_path])]
        self.write(r.RUNS + '/live/state.json', other)
        self.assertNotIn(self.raw_path, self.paths(self.plan()))

    def test_descriptive_cache_roots_do_not_pin_historical_reports(self):
        spec = (self.project / 'test/spec.md').read_text(encoding='utf-8')
        for location in ('test/spec.md', 'test/plans/notes.md'):
            for root in ('test/.cache/', 'test/.cache/runs/'):
                with self.subTest(location=location, root=root):
                    self.write('test/spec.md', spec)
                    self.write('test/plans/notes.md', '')
                    self.write(location, (spec if location == 'test/spec.md' else '')
                               + '\nReports are stored in `' + root + '`.\n')
                    self.assertTrue({self.raw_path, self.html, self.trace, self.noise}
                                    <= self.paths(self.plan()))

    def test_concrete_prose_baseline_file_and_export_directory_stay_protected(self):
        baseline = self.prefix + '/sources/baseline.md'
        export = self.prefix + '/sources/export/body.md'
        self.write(baseline, 'baseline')
        self.write(export, 'exported body')
        self.write('test/plans/notes.md',
                   'Cache: `test/.cache/`; baseline: `' + baseline
                   + '`; exports: `' + self.prefix + '/sources/export/`.')
        selected = self.paths(self.plan())
        self.assertNotIn(baseline, selected)
        self.assertNotIn(export, selected)
        self.assertIn(self.html, selected)

    def test_explicit_cache_root_approval_and_recovery_references_stay_protected(self):
        for field, record in (
                ('humans', dict(status='approved', evidence=['test/.cache/'])),
                ('resources', dict(status='ambiguous', evidence=['test/.cache/runs/']))):
            with self.subTest(field=field):
                self.state['humans'] = {}
                self.state['resources'] = {}
                self.state[field]['R'] = record
                self.assertEqual(set(), self.paths(self.plan()))

    def test_explicit_plan_link_and_structured_config_cache_roots_stay_protected(self):
        self.write('test/plans/decision.md', '[approval evidence](../.cache/runs/)')
        self.assertEqual(set(), self.paths(self.plan()))
        self.write('test/plans/decision.md', '')
        config = dict(self.spec, source_cache='test/.cache/')
        self.write('test/spec.md',
                   '```json workflow-config\n' + json.dumps(config) + '\n```\n')
        self.assertEqual(set(), self.paths(self.plan()))

    def test_live_inherited_issue_does_not_pin_history_or_release_source_cache(self):
        manifest = self.prefix + '/sources/reuse.json'
        payload = self.prefix + '/sources/reuse/body.md'
        self.write(payload, 'effective source cache')
        self.write(manifest, {'files': {payload: 'snapshot'}})
        other = self.new_state('live')
        other['acceptance'] = 'in_progress'
        other['issues']['I'] = dict(status='open', origin_run='old',
                                    evidence=[self.html, self.trace, self.raw_path])
        other['source_cache'] = {'S': {'manifest': manifest}}
        live_html = r.RUNS + '/live/attempts/N/report/playwright/index.html'
        self.write(live_html, 'current report')
        self.write(r.RUNS + '/live/state.json', other)
        plan = self.plan(current='live')
        selected = self.paths(plan)
        self.assertTrue({self.html, self.trace, self.raw_path} <= selected)
        self.assertFalse({manifest, payload, live_html} & selected)
        r.apply_plan(self.project, plan)
        self.assertFalse((self.project / self.html).exists())
        for path in (manifest, payload, live_html):
            self.assertTrue((self.project / path).exists())

    def test_inherited_issue_does_not_override_explicit_approval_or_resource(self):
        other = self.new_state('live')
        other['acceptance'] = 'in_progress'
        other['issues']['I'] = dict(status='open', origin_run='old',
                                    evidence=[self.html, self.trace, self.raw_path])
        other['humans']['H'] = dict(status='approved', evidence=[self.trace])
        other['resources']['R'] = dict(status='ambiguous', evidence=[self.raw_path])
        self.write(r.RUNS + '/live/state.json', other)
        selected = self.paths(self.plan(current='live'))
        self.assertIn(self.html, selected)
        self.assertNotIn(self.trace, selected)
        self.assertNotIn(self.raw_path, selected)

    def test_current_issue_cross_run_reference_stays_protected(self):
        other = self.new_state('live')
        other['acceptance'] = 'in_progress'
        other['issues']['I'] = dict(status='open', origin_run='live',
                                    evidence=[self.trace])
        self.write(r.RUNS + '/live/state.json', other)
        self.assertNotIn(self.trace, self.paths(self.plan(current='live')))

    def test_diagnostic_ignore_output_is_not_an_explicit_cache_root_reference(self):
        diagnostic = self.prefix + '/handoffs/checks.json'
        self.write(diagnostic, {'gitignore': '.gitignore:30:/test/.cache/'})
        other = self.new_state('live')
        other['acceptance'] = 'in_progress'
        other['source_checks'] = {'S': {'evidence': [diagnostic]}}
        self.write(r.RUNS + '/live/state.json', other)
        selected = self.paths(self.plan(current='live'))
        self.assertIn(self.html, selected)
        self.assertNotIn(diagnostic, selected)

    def test_ended_cross_run_only_effective_references_protect(self):
        other = self.new_state('other')
        other['events'] = [dict(type='source', evidence=[self.raw_path])]
        self.write(r.RUNS + '/other/state.json', other)
        self.assertIn(self.raw_path, self.paths(self.plan()))
        other['humans']['H'] = dict(status='approved', evidence=[self.raw_path])
        self.write(r.RUNS + '/other/state.json', other)
        self.assertNotIn(self.raw_path, self.paths(self.plan()))

    def test_historical_open_failure_and_unbound_receipt_do_not_pin_reports(self):
        self.failure()
        self.issue()
        context = self.prefix + '/attempts/B/results/output/error-context.md'
        self.write(context, 'error context')
        self.execution['failure_details'] = {'actual': 'assertion failed', 'evidence': [context]}
        plan = self.plan()
        selected = self.paths(plan)
        self.assertIn(self.raw_path, selected)
        self.assertIn(self.trace, selected)
        self.assertIn(context, selected)
        self.assertIn(self.html, selected)
        self.assertIn(self.noise, selected)
        r.apply_plan(self.project, plan)
        self.assertTrue(r.is_recycled(self.project, self.html))
        self.assertTrue(r.is_recycled(self.project, self.raw_path))
        self.assertTrue(r.recycled_bundle(self.project, 'old', 'B')['recycled'])
        self.assertEqual('fail', json.loads((self.project / (self.prefix + '/state.json')).read_text(encoding='utf-8'))['executions'][0]['result'])

    def test_closed_failure_can_be_completely_recycled(self):
        self.failure()
        self.issue('closed')
        plan = self.plan()
        self.assertIn(self.raw_path, self.paths(plan))
        r.apply_plan(self.project, plan)
        self.assertEqual(1, r.recycled_bundle(self.project, 'old', 'B')['runner_counts']['unexpected'])

    def test_historical_report_mapping_anomalies_are_limitations_not_retention_claims(self):
        self.failure()
        self.state['issues']['other'] = dict(status='closed', affected_case_ids=['C2'])
        first = self.plan()
        self.assertIn(self.raw_path, self.paths(first))
        self.issue('closed')
        self.execution['runner_result_index'] = 99
        second = self.plan()
        self.assertIn(self.raw_path, self.paths(second))

    def test_resource_without_usable_evidence_keeps_recovery_context(self):
        self.state['resources']['pending'] = {'status': 'ambiguous'}
        self.assertEqual([], self.plan()['files'])

    def test_unresolved_historical_resource_preserves_only_explicit_evidence(self):
        self.state['resources']['R'] = dict(status='ambiguous', evidence=[self.trace])
        selected = self.paths(self.plan())
        self.assertNotIn(self.trace, selected)
        self.assertIn(self.raw_path, selected)
        self.assertIn(self.html, selected)

    def test_legacy_wildcard_and_noncanonical_historical_references_do_not_abort_report_recycle(self):
        self.failure()
        self.issue()
        stale = self.prefix + '/handoffs/unused-authorization-note.md'
        self.write(stale, 'stale handoff')
        self.state['issues']['I']['evidence'] = [self.prefix + '/handoffs/source-authorized-*']
        first = self.plan()
        self.assertIn(self.raw_path, self.paths(first))
        self.assertTrue(any(item['reason'] == 'historical_wildcard_reference_not_protected'
                            for item in first['limitations']))
        self.state['issues']['I']['evidence'] = [self.prefix + '/handoffs/old/../source-authorized.txt']
        second = self.plan()
        self.assertIn(self.raw_path, self.paths(second))
        self.assertTrue(any(item['reason'] == 'noncanonical_historical_reference'
                            for item in second['limitations']))

    def test_historical_malformed_report_paths_and_hashes_do_not_pin_safe_payloads(self):
        for path in (self.prefix + '/attempts/B/results/*.json',
                     self.prefix + '/attempts/bad.id/*.json',
                     self.prefix + '/attempts/B/../results/*.json'):
            with self.subTest(path=path):
                self.bundle['execution_result'] = path
                self.bundle['artifact_sha256'] = {path: 'invalid-hash', self.html: '0' * 64}
                plan = self.plan()
                self.assertIn(self.raw_path, self.paths(plan))
                self.assertIn(self.html, self.paths(plan))
                self.assertTrue(plan['limitations'])
        r.apply_plan(self.project, plan)
        self.assertTrue(r.is_recycled(self.project, self.html))

    def test_finished_recovery_review_and_old_observable_wait_do_not_pin_history(self):
        self.state['events'].append(dict(type='recovery_review', evidence=[self.html]))
        self.state['humans']['H'] = dict(status='waiting', mode='observable', evidence=[self.trace])
        self.assertIn(self.html, self.paths(self.plan()))
        self.assertIn(self.trace, self.paths(self.plan()))

    def test_explicit_resource_execution_reference_keeps_recovery_context(self):
        self.state['resources']['R'] = dict(status='ambiguous', execution_id='E1')
        plan = self.plan()
        self.assertNotIn(self.raw_path, self.paths(plan))
        self.assertNotIn(self.trace, self.paths(plan))
        self.assertIn(self.noise, self.paths(plan))
        self.assertIn(self.html, self.paths(plan))

    def test_unregistered_attempt_blob_and_merged_html_are_recycled_but_summary_kept(self):
        blob = self.prefix + '/attempts/orphan/blob/report.zip'
        merged = self.prefix + '/merged/playwright/report.html'
        review = self.prefix + '/report.html'
        self.write(blob, 'old blob')
        self.write(merged, 'old native merged HTML')
        self.write(review, 'small summary index')
        self.state['scopes'] = ['a', 'b']
        partial = self.plan(['a'])
        self.assertNotIn(blob, self.paths(partial))
        self.assertNotIn(merged, self.paths(partial))
        plan = self.plan(['a', 'b'])
        self.assertIn(blob, self.paths(plan))
        self.assertIn(merged, self.paths(plan))
        self.assertNotIn(review, self.paths(plan))
        r.apply_plan(self.project, plan)
        self.assertTrue((self.project / review).is_file())
        self.assertEqual([], self.plan(['a', 'b'])['files'])

    def test_merged_malformed_registration_recycles_only_selected_run_payloads(self):
        merged = self.prefix + '/merged/playwright/index.html'
        self.write(merged, 'old merged HTML')
        self.state['merged_report'] = {'bundle_ids': None, 'html_report': merged,
                                       'artifact_sha256': {'test/.cache/runs/old/merged/*.html': 'bad'},
                                       'inputs': {'bad': {self.raw_path: 'bad'}}}
        plan = self.plan()
        self.assertIn(merged, self.paths(plan))
        self.assertIn(self.raw_path, self.paths(plan))
        r.apply_plan(self.project, plan)
        self.assertTrue(r.is_recycled(self.project, merged))

    def test_mixed_module_and_protected_resource_keep_only_linked_bundle_and_merged_report(self):
        self.state['scopes'] = ['a', 'b']
        self.cov['scopes'] = ['a', 'b']
        merged = self.prefix + '/merged/playwright/index.html'
        self.write(merged, '<html>merged</html>')
        self.state['merged_report'] = {'bundle_ids': ['B'], 'html_report': merged,
                                       'artifact_sha256': {merged: hashlib.sha256(
                                           (self.project / merged).read_bytes()).hexdigest()},
                                       'inputs': {'B': {}}}
        self.refresh_bundle()
        self.state['report_bundles']['B']['scope'] = ['a', 'b']
        self.state['resources']['R'] = {'status': 'ambiguous', 'case_ids': ['C1'],
                                        'evidence': [self.trace]}
        selected_scope = self.plan(['a'])
        self.assertNotIn(self.raw_path, self.paths(selected_scope))
        self.assertNotIn(merged, self.paths(selected_scope))
        full_scope = self.plan(['a', 'b'])
        self.assertNotIn(self.trace, self.paths(full_scope))
        self.assertNotIn(self.raw_path, self.paths(full_scope))
        self.assertIn(merged, self.paths(full_scope))

    def test_missing_or_unreadable_any_run_fails_closed(self):
        self.write(r.RUNS + '/bad/state.json', '{broken')
        with self.assertRaises(ValueError):
            self.plan()
        (self.project / (r.RUNS + '/bad/state.json')).unlink()
        with self.assertRaises(OSError):
            self.plan()
        self.assertTrue((self.project / self.raw_path).exists())

    def test_mutated_plan_rehash_does_not_grant_delete_capability(self):
        plan = self.plan()
        forged = copy.deepcopy(plan)
        forged['files'].append(dict(forged['files'][0], path='test/plans/coverage.json'))
        forged['plan_id'] = r.digest(r.plan_body(forged))
        with self.assertRaises((ValueError, OSError)):
            r.apply_plan(self.project, forged)
        self.assertTrue((self.project / self.raw_path).exists())

    def test_changed_protection_file_state_or_inventory_aborts_before_delete(self):
        for kind in ('state', 'coverage', 'approval', 'inventory', 'artifact'):
            with self.subTest(kind=kind):
                plan = self.plan()
                path = {'state': self.prefix + '/state.json', 'coverage': 'test/plans/coverage.json',
                        'approval': 'test/plans/new-decision.md', 'inventory': self.prefix + '/sources/new.txt',
                        'artifact': self.html}[kind]
                existing = (self.project / path).read_bytes() if (self.project / path).exists() else None
                self.write(path, 'changed')
                with self.assertRaises((ValueError, OSError)):
                    r.apply_plan(self.project, plan)
                self.assertTrue((self.project / self.raw_path).exists())
                if existing is None:
                    (self.project / path).unlink()
                else:
                    (self.project / path).write_bytes(existing)

    def test_unexpected_missing_is_not_recycled(self):
        plan = self.plan()
        (self.project / self.html).unlink()
        self.assertFalse(r.is_recycled(self.project, self.html))
        with self.assertRaises((ValueError, OSError)):
            r.apply_plan(self.project, plan)
        fresh = r.make_plan(self.project, ['a'])
        self.assertIn(self.raw_path, self.paths(fresh))
        self.assertIn('historical_artifact_unexpectedly_missing', {item['reason'] for item in fresh['limitations']})
        r.apply_plan(self.project, fresh)
        self.assertFalse(r.is_recycled(self.project, self.html))
        self.assertIsNone(r.recycled_bundle(self.project, 'old', 'B'))

    def test_recreated_paths_and_symlinks_never_count_as_recycled(self):
        plan = self.plan()
        r.apply_plan(self.project, plan)
        self.write(self.html, 'replacement')
        self.assertFalse(r.is_recycled(self.project, self.html))
        self.assertIsNone(r.recycled_bundle(self.project, 'old', 'B'))
        with self.assertRaises(ValueError):
            r.apply_plan(self.project, plan)
        (self.project / self.html).unlink()
        platform_tests.symlink(self, self.project / self.html, self.project / (self.prefix + '/state.json'))
        self.assertFalse(r.is_recycled(self.project, self.html))

    def test_symlink_and_hardlink_files_rejected(self):
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                path = self.project / (self.prefix + '/sources/link')
                path.parent.mkdir(exist_ok=True)
                if kind == 'symlink':
                    platform_tests.symlink(self, path, self.project / 'test/spec.md')
                else:
                    os.link(self.project / 'test/spec.md', path)
                with self.assertRaises((OSError, ValueError)):
                    self.plan()
                path.unlink()

    def test_component_symlink_rejected_and_target_untouched(self):
        plan = self.plan()
        directory = self.project / (self.prefix + '/attempts/B/results/output')
        saved = directory.with_name('saved')
        directory.rename(saved)
        platform_tests.symlink(self, directory, saved, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            r.apply_plan(self.project, plan)
        self.assertTrue((saved / 'trace.zip').exists())

    def test_traversal_unsafe_whitelist_and_project_mismatch(self):
        for path in ('../x', '/tmp/x', 'test/.cache/runs/old/../../x',
                     'test/.cache/runs/old/state.json', 'test/src/test.ts',
                     'test/.cache/runs/old/resources/record', 'test//.cache/runs/old/sources/x'):
            with self.subTest(path=path):
                self.assertFalse(r.is_recycled(self.project, path))
        plan = self.plan()
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaises(ValueError):
                r.apply_plan(Path(other).resolve(), plan)

    def test_read_cache_hashes_each_file_once_per_selection(self):
        calls = []
        original = r.Tree.read
        def counted(tree, path):
            calls.append(path)
            return original(tree, path)
        self.save_state()
        with r.locked(self.project) as tree, mock.patch.object(r.Tree, 'read', counted):
            r.selection(tree, ['a'])
        self.assertEqual(1, calls.count(self.raw_path))
        self.assertEqual(1, calls.count(self.prefix + '/state.json'))

    def test_crash_after_staging_resumes_same_plan(self):
        plan = self.plan()
        original = r.quarantine
        def crash(tree, item, stage):
            original(tree, item, stage)
            raise SystemExit('simulated interruption')
        with mock.patch.object(r, 'quarantine', crash):
            with self.assertRaises(SystemExit):
                r.apply_plan(self.project, plan)
        self.assertTrue(r.apply_plan(self.project, plan)['completed'])
        self.assertTrue(r.is_recycled(self.project, self.html))

    def test_crash_after_journal_commit_resumes_and_drains_quarantine(self):
        plan = self.plan()
        original = r.Tree.write
        crashed = []
        def crash(tree, path, value, exclusive=False):
            original(tree, path, value, exclusive)
            if path.startswith(r.STORE + '/manifests/') and any(
                    f['status'] == 'deleted' for f in value['files'].values()) and not crashed:
                crashed.append(True)
                raise SystemExit('simulated journal interruption')
        with mock.patch.object(r.Tree, 'write', crash):
            with self.assertRaises(SystemExit):
                r.apply_plan(self.project, plan)
        self.assertTrue(r.apply_plan(self.project, plan)['completed'])
        with r.locked(self.project) as tree:
            self.assertFalse(any(v is None for v in tree.walk(r.STORE + '/quarantine').values()))

    def test_reference_race_during_staging_restores_original_bytes(self):
        plan = self.plan()
        original = r.quarantine
        called = []
        def race(tree, item, stage):
            original(tree, item, stage)
            if not called:
                called.append(True)
                self.write('test/plans/racing-approval.md', '[evidence](' + self.html + ')')
        with mock.patch.object(r, 'quarantine', race):
            with self.assertRaises(ValueError):
                r.apply_plan(self.project, plan)
        for item in plan['files']:
            self.assertEqual(item['sha256'], hashlib.sha256((self.project / item['path']).read_bytes()).hexdigest())

    def test_cli_plan_apply_json_exit_and_immutable_export(self):
        self.save_state()
        output = self.project / 'plan.json'
        args = [sys.executable, '-B', str(MODULE)]
        planned = subprocess.run(args + ['plan', '--project', str(self.project), '--scopes', 'a',
                                         '--output', str(output)], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(0, planned.returncode, planned.stdout + planned.stderr)
        self.assertEqual(4, json.loads(planned.stdout)['files'])
        applied = subprocess.run(args + ['apply', '--project', str(self.project), '--input', str(output)],
                                 capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(0, applied.returncode, applied.stdout + applied.stderr)
        self.assertTrue(json.loads(applied.stdout)['completed'])
        invalid = subprocess.run(args + ['plan', '--project', str(self.project), '--scopes', 'outside',
                                         '--output', str(output)], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(2, invalid.returncode)
        self.assertIn('error', json.loads(invalid.stdout))

    def test_manifest_tampering_fails_closed(self):
        plan = self.plan()
        result = r.apply_plan(self.project, plan)
        path = self.project / result['manifest']
        value = json.loads(path.read_text(encoding='utf-8'))
        value['files'][self.html]['original']['sha256'] = '0' * 64
        path.write_text(json.dumps(value))
        self.assertFalse(r.is_recycled(self.project, self.html))
        self.assertIsNone(r.recycled_bundle(self.project, 'old', 'B'))

    def test_obsolete_cache_hit_events_do_not_pin_old_snapshot(self):
        source = self.prefix + '/sources/obsolete-cache.md'
        self.write(source, 'old cache')
        self.state['events'].append(dict(type='source', cache_hit=True, action='reuse', files=[source]))
        self.assertIn(source, self.paths(self.plan()))

    def test_resolved_observable_human_is_not_permanent_approval(self):
        source = self.prefix + '/handoffs/old-observation.txt'
        self.write(source, 'observed and resolved')
        self.state['humans']['H'] = dict(status='resolved', mode='observable', evidence=[source])
        self.state['events'] += [dict(type='human', status='waiting', evidence=[source]),
                                 dict(type='human', status='resolved', mode='observable', evidence=[source])]
        self.assertIn(source, self.paths(self.plan()))
        self.state['events'].append(dict(type='human', status='approved', evidence=[source]))
        self.assertNotIn(source, self.paths(self.plan()))

    def test_root_alias_cli_supported_but_direct_api_requires_canonical_root(self):
        alias = self.project / 'alias'
        target = self.project / 'isolated-output'
        target.mkdir()
        platform_tests.symlink(self, alias, target, target_is_directory=True)
        plan = self.plan()
        exported = alias / 'plan.json'
        r.export_plan(exported, plan)
        self.assertEqual(plan, r.external_read(exported))
        with self.assertRaises((OSError, ValueError)):
            r.make_plan(alias, ['a'])
        platform_tests.symlink(self, alias / 'input-link.json', target / 'plan.json')
        with self.assertRaises((OSError, ValueError)):
            r.external_read(alias / 'input-link.json')

    def test_malformed_claim_and_ambiguous_approval_fail_closed(self):
        for claim in ({}, [], '', False):
            with self.subTest(claim=claim):
                self.state['claim'] = claim
                with self.assertRaises(ValueError):
                    self.plan()
        self.state['claim'] = None
        self.state['humans']['H'] = dict(status='approved', evidence=['unknown-relative-evidence'])
        self.assertEqual([], self.plan()['files'])

    def test_same_bytes_replaced_inode_after_plan_rejected(self):
        plan = self.plan()
        target = self.project / self.html
        replacement = target.with_name('replacement')
        replacement.write_bytes(target.read_bytes())
        os.replace(replacement, target)
        with self.assertRaises(ValueError):
            r.apply_plan(self.project, plan)
        self.assertTrue((self.project / self.raw_path).exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX rename interception; Windows uses verified handles')
    def test_file_substitution_during_rename_is_restored_not_deleted(self):
        plan = self.plan()
        original = os.rename
        victim = plan['files'][0]['path']
        def race(src, dst, **kwargs):
            if 'quarantine' not in str(dst):
                target = self.project / victim
                replacement = target.with_name('attacker-replacement')
                replacement.write_text('unrelated replacement')
                os.replace(replacement, target)
            return original(src, dst, **kwargs)
        with mock.patch.object(r.os, 'rename', race):
            with self.assertRaises(ValueError):
                r.apply_plan(self.project, plan)
        self.assertEqual('unrelated replacement', (self.project / victim).read_text(encoding='utf-8'))
        self.assertFalse(r.is_recycled(self.project, victim))

    @unittest.skipUnless(os.name == 'nt', 'native Windows verified-handle staging')
    def test_windows_replacement_before_handle_open_is_not_staged_or_deleted(self):
        plan = self.plan()
        move = r.WindowsTree.move
        victim = plan['files'][0]['path']
        def substitute(tree, source, target, expected=None):
            if source == victim:
                path = self.project / source
                replacement = path.with_name('replacement')
                replacement.write_bytes(b'unrelated replacement')
                os.replace(replacement, path)
            return move(tree, source, target, expected)
        with mock.patch.object(r.WindowsTree, 'move', substitute):
            with self.assertRaises(ValueError):
                r.apply_plan(self.project, plan)
        self.assertEqual(b'unrelated replacement', (self.project / victim).read_bytes())
        self.assertFalse(r.is_recycled(self.project, victim))

    def test_windows_style_approval_references_and_transitive_cache_are_protected(self):
        payload = self.prefix + '/sources/payload.md'
        manifest = self.prefix + '/sources/snapshot.json'
        self.write(payload, 'baseline body')
        self.write(manifest, {'files': {payload.upper().replace('/', '\\'): 'fixture'}})
        self.cov['sources']['S1'] = {'baseline_path': manifest.upper().replace('/', '\\')}
        self.write('test/plans/coverage.json', self.cov)
        with mock.patch.object(r, 'CASE_INSENSITIVE', True):
            paths = self.paths(self.plan())
        self.assertNotIn(payload, paths)
        self.assertNotIn(manifest, paths)
        self.assertIn(self.html, paths)

    def test_failure_location_line_suffix_is_not_an_ntfs_stream(self):
        self.state['issues']['I'] = {'status': 'open', 'affected_case_ids': ['C1'],
                                    'location': 'test/src/C1.spec.ts:121:5'}
        self.assertIn('test/src/C1.spec.ts', list(r.references(self.state, str(self.project))))
        self.plan()

    def test_relative_historical_failure_attachment_is_recycled_with_html(self):
        self.failure()
        self.issue()
        self.raw['suites'][0]['specs'][0]['tests'][0]['results'][0]['attachments'][0]['path'] = 'output/trace.zip'
        self.write(self.raw_path, self.raw)
        self.refresh_bundle()
        self.execution['evidence'] = []
        plan = self.plan()
        self.assertIn(self.trace, self.paths(plan))
        self.assertIn(self.html, self.paths(plan))

    def test_complete_marker_refused_if_untracked_payload_is_protected(self):
        self.state['humans']['H'] = dict(status='approved', evidence=[self.noise])
        plan = self.plan()
        self.assertNotIn(self.noise, self.paths(plan))
        r.apply_plan(self.project, plan)
        self.assertIsNone(r.recycled_bundle(self.project, 'old', 'B'))

    def test_failed_replay_with_directory_symlink_is_read_only(self):
        plan = self.plan()
        r.apply_plan(self.project, plan)
        directory = self.project / (self.prefix + '/attempts/B/report')
        moved = directory.with_name('moved-report')
        directory.rename(moved)
        platform_tests.symlink(self, directory, moved, target_is_directory=True)
        self.assertFalse(r.is_recycled(self.project, self.html))
        with self.assertRaises((ValueError, OSError)):
            r.apply_plan(self.project, plan)


if __name__ == '__main__':
    unittest.main()
