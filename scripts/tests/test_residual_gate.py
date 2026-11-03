"""Offline cross-run cleanup safety; no browser, network or business fixtures."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checkpoint as cp
import work_packages as wp


class ResidualGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'test').mkdir()
        (self.root / 'test/a.ts').write_text('offline')
        (self.root / 'proof.json').write_text('{}')
        self.spec = {'environment_id': 'deployment-1', 'resource_environment': 'tenant-dev',
                     'resource_run_id': 'run-1', 'cases': [
                         self.case('category', ['category']), self.case('product', ['product'], ['category']),
                         self.case('other', ['device']), self.case('read', [], mode='readonly'),
                         self.case('independent', [])]}
        self.journal = 'test/.cache/run-1.jsonl'
        self.call('init', self.spec)

    def case(self, cid, kinds, dependencies=None, mode='write'):
        return {'case_id': cid, 'target': {'file': 'test/a.ts', 'project': 'offline', 'title_path': [cid]},
                'inputs': ['test/a.ts'], 'dependencies': [], 'conflicts': [], 'reviewed': True,
                'resource_access': {'mode': mode, 'kinds': kinds, 'dependencies': dependencies or []}}

    def call(self, command, request=None, journal=None):
        return cp.dispatch(self.root, journal or self.journal, command, request or {}, workers=8)

    def resource(self, rid='70', status='cleanup_failed', kind='category', evidence='proof.json', **extra):
        return dict(kind=kind, id=rid, status=status, evidence=[evidence], **extra)

    def cleanup(self, *resources, eid='cleanup-1', journal=None):
        return self.call('record', dict(type='cleanup_state', event_id=eid,
                                      resources=list(resources or [self.resource()])), journal)

    def proof(self, rid='70', status='absence_verified', kind='category', **extra):
        value = dict(resource_environment='tenant-dev', kind=kind, id=rid, status=status, verified=True)
        value.update(extra)
        (self.root / 'readback.json').write_text(json.dumps(value))

    def new_run(self, environment='tenant-dev', run_id='run-2', legacy=False):
        spec = copy.deepcopy(self.spec)
        spec.update(environment_id='deployment-2', resource_environment=environment, resource_run_id=run_id)
        if legacy:
            spec.pop('resource_environment')
        path = f'test/.cache/{run_id}.jsonl'
        self.call('init', spec, path)
        return path

    def test_failure_blocks_only_related_writes_and_dependencies(self):
        result = self.cleanup()
        self.assertEqual(set(result['ready']), {'other', 'read', 'independent'})
        self.assertEqual(result['blocked'], {'category': 'resource_cleanup_unresolved',
                                             'product': 'resource_cleanup_unresolved'})
        with self.assertRaisesRegex(ValueError, 'ready'):
            self.call('claim', {'attempt_id': 'denied', 'case_ids': ['category']})
        self.call('claim', {'attempt_id': 'allowed', 'case_ids': ['other', 'read', 'independent']})

    def test_new_checkpoint_and_process_keep_history_gate(self):
        self.cleanup()
        journal = self.new_run()
        program = "import sys,json; sys.path.insert(0,sys.argv[1]); import checkpoint as c; print(json.dumps(c.dispatch(sys.argv[2],sys.argv[3],'plan',{},8)))"
        result = subprocess.run([sys.executable, '-B', '-c', program, str(Path(cp.__file__).parent),
                                 str(self.root), journal], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['blocked']['category'], 'resource_cleanup_unresolved')

    def test_environment_isolation_and_missing_environment_fail_closed(self):
        self.cleanup()
        isolated = self.new_run('other-tenant', 'other-run')
        self.assertIn('category', self.call('plan', journal=isolated)['ready'])
        legacy = self.new_run(run_id='legacy-run', legacy=True)
        self.assertNotIn('category', self.call('plan', journal=legacy)['ready'])

    def test_legacy_producer_blocks_stable_consumers_until_resolved(self):
        legacy = self.new_run(run_id='legacy-producer', legacy=True)
        self.cleanup(journal=legacy)
        scope = {'resource_environment': 'tenant-dev', 'resource_access':
                 {'mode': 'write', 'kinds': ['category'], 'dependencies': []}}
        self.assertFalse(cp.dispatch(self.root, None, 'gate', scope)['allowed'])
        self.assertNotIn('category', self.call('plan')['ready'])
        item = dict(case_id='A', category='script', evidence=['proof.json'])
        packages = wp.build(self.root, {'resource_environment': 'tenant-dev', 'items': [item]})
        self.assertEqual(packages['packages'][0]['action'], 'cleanup_review_no_blind_retry')
        self.assertEqual(self.call('plan')['resource_residuals']['cumulative_unresolved'], 1)
        self.proof(resource_environment='deployment-2')
        self.cleanup(self.resource(status='absence_verified', evidence='readback.json'),
                     eid='resolve-legacy', journal=legacy)
        self.assertTrue(cp.dispatch(self.root, None, 'gate', scope)['allowed'])

    def test_old_index_without_environment_provenance_is_conservative(self):
        self.cleanup()
        path = self.root / cp.RESIDUAL_INDEX
        index = json.loads(path.read_text())
        for item in index['resources'].values():
            item.pop('environment_is_stable', None)
        path.write_text(json.dumps(index))
        other = self.new_run('other-tenant', 'other-run')
        self.assertNotIn('category', self.call('plan', journal=other)['ready'])

    def test_redeclaring_legacy_namespace_cannot_upgrade_environment_provenance(self):
        legacy = self.new_run(run_id='legacy-producer', legacy=True)
        self.cleanup(journal=legacy)
        renamed = self.new_run('deployment-2', 'renamed')
        self.cleanup(journal=renamed)
        other = self.new_run('other-tenant', 'other-run')
        self.assertNotIn('category', self.call('plan', journal=other)['ready'])

    def test_environment_provenance_must_be_boolean(self):
        self.cleanup()
        path = self.root / cp.RESIDUAL_INDEX
        index = json.loads(path.read_text())
        entry = next(iter(index['resources'].values()))
        for value in [None, 0, 1, 'true']:
            entry['environment_is_stable'] = value
            path.write_text(json.dumps(index))
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.call('plan')

    def shared_config(self, **extra):
        return self.resource(kind='shared_config', **extra)

    def register_config_baseline(self):
        (self.root / 'original.json').write_text(json.dumps({'enabled': False, 'limits': [1, 2]}))
        self.cleanup(self.shared_config(original_snapshot='original.json'))

    def config_resolution(self):
        self.proof(kind='shared_config', restored_snapshot='restored.json')
        return self.shared_config(status='absence_verified', evidence='readback.json',
                                  restored_snapshot='restored.json')

    def test_shared_config_generic_verified_receipt_cannot_clear_gate(self):
        self.cleanup(self.shared_config())
        self.proof(kind='shared_config')
        with self.assertRaises(ValueError):
            self.cleanup(self.shared_config(status='absence_verified', evidence='readback.json'), eid='bad-close')
        self.assertIn('shared_config', cp.residual_kinds(cp.residual_index(self.root), 'tenant-dev'))

    def test_shared_config_restoration_requires_previously_pinned_original(self):
        self.cleanup(self.shared_config())
        (self.root / 'original.json').write_text('{"enabled": false}')
        (self.root / 'restored.json').write_text('{"enabled": false}')
        resource = dict(self.config_resolution(), original_snapshot='original.json')
        with self.assertRaises(ValueError):
            self.cleanup(resource, eid='invent-baseline-at-close')

    def test_shared_config_closes_only_matching_original_and_restored_readback(self):
        self.register_config_baseline()
        resource = self.config_resolution()
        (self.root / 'restored.json').write_text('{"enabled": true, "limits": [1, 2]}')
        before = (self.root / cp.RESIDUAL_INDEX).read_bytes()
        with self.assertRaises(ValueError):
            self.cleanup(resource, eid='mismatch')
        self.assertEqual(before, (self.root / cp.RESIDUAL_INDEX).read_bytes())
        (self.root / 'restored.json').write_text('{"limits": [1, 2], "enabled": false}')
        self.cleanup(resource, eid='restored')
        self.assertNotIn('shared_config', cp.residual_kinds(cp.residual_index(self.root), 'tenant-dev'))
        entry = next(iter(cp.residual_index(self.root)['resources'].values()))
        self.assertEqual(entry['original_snapshot'], 'original.json')

    def test_shared_config_original_cannot_change_across_runs_or_on_disk(self):
        self.register_config_baseline()
        journal = self.new_run()
        (self.root / 'other-original.json').write_text('{"enabled": true}')
        with self.assertRaises(ValueError):
            self.cleanup(self.shared_config(original_snapshot='other-original.json'), eid='replace', journal=journal)
        (self.root / 'original.json').write_text('{"enabled": true}')
        (self.root / 'restored.json').write_text('{"enabled": true}')
        with self.assertRaises(ValueError):
            self.cleanup(self.config_resolution(), eid='tamper', journal=journal)

    def test_shared_config_generic_absence_and_retention_fail_with_pinned_baseline(self):
        self.register_config_baseline()
        for status, extra in [('absence_verified', {}), ('retained_verified', {'retention': 'disabled'})]:
            self.proof(kind='shared_config', status=status, **extra)
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.cleanup(self.shared_config(status=status, evidence='readback.json', **extra), eid=status)

    def test_shared_config_baseline_cannot_be_dropped_and_survives_reopen(self):
        self.register_config_baseline()
        for value in [None, '', False]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.cleanup(self.shared_config(original_snapshot=value), eid='drop-baseline')
        (self.root / 'restored.json').write_bytes((self.root / 'original.json').read_bytes())
        self.cleanup(self.config_resolution(), eid='restored')
        self.cleanup(self.shared_config(), eid='reopened')
        entry = next(iter(cp.residual_index(self.root)['resources'].values()))
        self.assertEqual(entry['original_snapshot'], 'original.json')
        self.assertIn('shared_config', cp.residual_kinds(cp.residual_index(self.root), 'tenant-dev'))

    def test_shared_config_receipt_must_reference_the_verified_snapshot(self):
        self.register_config_baseline()
        resource = self.config_resolution()
        (self.root / 'restored.json').write_bytes((self.root / 'original.json').read_bytes())
        self.proof(kind='shared_config', restored_snapshot='another.json')
        with self.assertRaisesRegex(ValueError, 'readback proof'):
            self.cleanup(resource, eid='wrong-reference')

    def test_config_snapshot_fields_are_rejected_for_other_resource_kinds(self):
        for field in ['original_snapshot', 'restored_snapshot']:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.cleanup(self.resource(**{field: 'proof.json'}))

    def test_same_environment_kind_id_deduplicates_across_runs(self):
        self.cleanup()
        journal = self.new_run()
        self.cleanup(eid='again', journal=journal)
        self.assertEqual(len(cp.residual_index(self.root)['resources']), 1)
        self.cleanup(self.resource(kind='device'), eid='other-kind', journal=journal)
        isolated = self.new_run('other-tenant', 'isolated')
        self.cleanup(eid='other-env', journal=isolated)
        self.assertEqual(len(cp.residual_index(self.root)['resources']), 3)

    def test_71_unique_resources_not_70_delete_failures_or_attempt_counts(self):
        resources = [self.resource(str(i)) for i in range(71)]
        result = self.cleanup(*resources)
        self.cleanup(*resources, eid='retry-same-71')
        self.assertEqual(result['resource_residuals']['current_run_unresolved'], 71)
        self.assertEqual(self.call('plan')['resource_residuals']['cumulative_unresolved'], 71)

    def test_current_historical_and_this_run_cleanup_counts_are_separate(self):
        self.cleanup(self.resource('old'))
        journal = self.new_run()
        result = self.cleanup(self.resource('new'), eid='new', journal=journal)
        self.assertEqual(result['resource_residuals']['current_run_unresolved'], 1)
        self.assertEqual(result['resource_residuals']['historical_unresolved'], 1)
        self.assertEqual(result['resource_residuals']['cumulative_unresolved'], 2)
        self.proof('old')
        result = self.cleanup(self.resource('old', 'absence_verified', evidence='readback.json'), eid='resolve', journal=journal)
        self.assertEqual(result['resource_residuals'], dict(current_run_unresolved=1,
            historical_unresolved=0, cumulative_unresolved=1, current_run_absence_verified=1,
            current_run_retained_verified=0))

    def test_pass_close_and_unblock_never_resolve_resource_debt(self):
        self.call('claim', {'attempt_id': 'attempt', 'case_ids': ['category']})
        self.cleanup()
        for event in [dict(type='case_end', event_id='end', attempt_id='attempt', case_id='category', status='passed'),
                      dict(type='close', event_id='close', attempt_id='attempt', cleanup_verified=True)]:
            self.call('record', dict(event, evidence=['proof.json']))
        with self.assertRaises(ValueError):
            self.call('record', dict(type='unblock', event_id='unblock', dependency='category', evidence=['proof.json']))
        self.assertEqual(self.call('plan')['blocked']['category'], 'resource_cleanup_unresolved')
        self.assertEqual(len(cp.residual_index(self.root)['resources']), 1)

    def test_only_matching_absence_readback_resolves_and_stale_replay_does_not_reopen(self):
        self.cleanup()
        self.proof()
        resolved = self.resource(status='absence_verified', evidence='readback.json')
        self.assertIn('category', self.cleanup(resolved, eid='resolve')['ready'])
        self.cleanup()  # Original event replay is idempotent even after resolution.
        self.assertIn('category', self.call('plan')['ready'])
        self.cleanup(eid='new-ambiguity')
        self.assertNotIn('category', self.call('plan')['ready'])
        (self.root / 'readback.json').unlink()
        self.cleanup(resolved, eid='resolve')  # Old success cannot clear a newer failure.
        self.assertNotIn('category', self.call('plan')['ready'])

    def test_retained_verified_requires_mode_and_matching_readback(self):
        self.cleanup()
        self.proof(status='retained_verified', retention='disabled')
        resource = self.resource(status='retained_verified', evidence='readback.json', retention='disabled')
        result = self.cleanup(resource, eid='retain')
        self.assertIn('category', result['ready'])
        self.assertEqual(result['resource_residuals']['current_run_retained_verified'], 1)
        for retention in ['pooled', 'unknown', None]:
            bad = dict(resource, retention=retention)
            with self.subTest(retention=retention), self.assertRaises(ValueError):
                self.cleanup(bad, eid='bad-' + str(retention))

    def test_resolving_one_id_does_not_clear_other_resources_of_same_kind(self):
        self.cleanup(self.resource('70'), self.resource('71'))
        self.proof('70')
        result = self.cleanup(self.resource('70', 'absence_verified', evidence='readback.json'), eid='resolve-one')
        self.assertNotIn('category', result['ready'])
        self.assertEqual(result['resource_residuals']['cumulative_unresolved'], 1)
        index = cp.residual_index(self.root)
        statuses = {r['id']: r['status'] for r in index['resources'].values()}
        self.assertEqual(statuses, {'70': 'absence_verified', '71': 'cleanup_failed'})

    def test_cli_rejects_raw_msg_without_echo_and_accepts_normalized_cleanup(self):
        request = self.root / 'request.json'
        event = dict(type='cleanup_state', event_id='cli-cleanup', resources=[self.resource()])
        event['resources'][0]['msg'] = 'credential-must-not-echo'
        request.write_text(json.dumps(event))
        args = [sys.executable, '-B', str(Path(cp.__file__)), 'record', '--project', str(self.root),
                '--journal', self.journal, '--input', str(request)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('credential-must-not-echo', result.stdout + result.stderr)
        del event['resources'][0]['msg']
        request.write_text(json.dumps(event))
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['blocked']['category'], 'resource_cleanup_unresolved')

    def test_pass_missing_mismatched_and_unverified_proofs_cannot_clear_gate(self):
        self.cleanup()
        base = dict(resource_environment='tenant-dev', kind='category', id='70', status='absence_verified', verified=True)
        for delta in [{'status': 'passed'}, {'id': 'other'}, {'id': 70}, {'kind': 'device'},
                      {'resource_environment': 'other'}, {'verified': False}, {'verified': 1},
                      {'verified': 'true'}]:
            with self.subTest(delta=delta):
                (self.root / 'readback.json').write_text(json.dumps(dict(base, **delta)))
                with self.assertRaisesRegex(ValueError, 'readback proof'):
                    self.cleanup(self.resource(status='absence_verified', evidence='readback.json'), eid='bad-proof')
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.cleanup(self.resource(status='absence_verified', evidence='missing.json'), eid='missing')
        self.assertNotIn('category', self.call('plan')['ready'])

    def test_nested_index_and_array_cannot_replace_flat_readback_proof(self):
        self.cleanup()
        proof = dict(resource_environment='tenant-dev', kind='category', id='70',
                     status='absence_verified', verified=True)
        for value in [{'resources': [proof]}, {'resources': {'category:70': proof}}, [proof]]:
            with self.subTest(value=value):
                (self.root / 'readback.json').write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, 'readback proof'):
                    self.cleanup(self.resource(status='absence_verified', evidence='readback.json'),
                                 eid='nested-proof')
                self.assertNotIn('category', self.call('plan')['ready'])
        self.proof()
        self.cleanup(self.resource(status='absence_verified', evidence='readback.json'), eid='flat-proof')
        self.assertIn('category', self.call('plan')['ready'])

    def test_bad_batch_commits_no_prefix(self):
        self.proof()
        before = (self.root / self.journal).read_bytes()
        with self.assertRaises(ValueError):
            self.cleanup(self.resource(), self.resource('bad', status='passed'))
        self.assertEqual((self.root / self.journal).read_bytes(), before)
        self.assertFalse((self.root / cp.RESIDUAL_INDEX).exists())

    def test_crash_after_index_commit_before_journal_append_still_blocks_and_retries(self):
        original = cp.append
        with mock.patch.object(cp, 'append', side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError):
                self.cleanup()
        self.assertNotIn('category', self.call('plan')['ready'])
        with mock.patch.object(cp, 'append', wraps=original) as append:
            self.cleanup()
            self.assertEqual(append.call_count, 1)
        self.assertEqual(len(cp.residual_index(self.root)['resources']), 1)

    def test_index_write_failure_never_commits_a_cleanup_journal_event(self):
        before = (self.root / self.journal).read_bytes()
        with mock.patch.object(cp.os, 'replace', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):
                self.cleanup()
        self.assertEqual((self.root / self.journal).read_bytes(), before)

    def test_cleanup_event_id_cannot_change_content(self):
        self.cleanup()
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.cleanup(self.resource('other'))

    def test_corrupt_index_stops_claim_and_work_packages(self):
        (self.root / cp.RESIDUAL_INDEX).write_text('{broken')
        with self.assertRaises(ValueError):
            self.call('claim', {'attempt_id': 'denied', 'case_ids': ['other']})
        with self.assertRaises(ValueError):
            wp.build(self.root, {'items': []})

    def test_unknown_access_is_conservative_but_explicit_independent_is_allowed(self):
        spec = copy.deepcopy(self.spec)
        del spec['cases'][0]['resource_access']
        journal = 'test/.cache/unannotated.jsonl'
        self.call('init', spec, journal)
        self.cleanup()
        result = self.call('plan', journal=journal)
        self.assertNotIn('category', result['ready'])
        self.assertIn('independent', result['ready'])
        with self.assertRaises(ValueError):
            cp.resource_access({'mode': 'readonly', 'kinds': ['category'], 'dependencies': []})

    def test_work_package_gates_use_index_preserve_permissions_and_do_not_mutate_input(self):
        self.cleanup()
        items = [dict(case_id=c['case_id'], category='script', evidence=['proof.json'],
                      resource_access=c['resource_access']) for c in self.spec['cases']]
        items.append(dict(items[0], case_id='permission', permission_required=True))
        request = {'resource_environment': 'tenant-dev', 'items': items}
        before = copy.deepcopy(request)
        result = wp.build(self.root, request)
        self.assertEqual(request, before)
        routes = {cid: p['action'] for p in result['packages'] for cid in p['case_ids']}
        self.assertEqual(routes['category'], 'cleanup_review_no_blind_retry')
        self.assertEqual(routes['product'], 'cleanup_review_no_blind_retry')
        self.assertEqual(routes['permission'], 'awaiting_authorization')
        for cid in ['other', 'read', 'independent']:
            self.assertEqual(routes[cid], 'repair_then_affected')
        self.assertEqual(result['schema_version'], 2)

    def test_work_packages_legacy_cannot_bypass_and_other_environment_can_proceed(self):
        self.cleanup()
        item = dict(case_id='A', category='script', evidence=['proof.json'])
        for environment, expected in [(None, 'cleanup_review_no_blind_retry'), ('other', 'repair_then_affected')]:
            request = {'items': [item]}
            if environment is not None:
                request['resource_environment'] = environment
            self.assertEqual(wp.build(self.root, request)['packages'][0]['action'], expected)

    def test_non_checkpoint_runner_gate_rejects_unknown_writes_and_allows_readonly(self):
        self.cleanup()
        request = self.root / 'scope.json'
        args = [sys.executable, '-B', str(Path(cp.__file__)), 'gate', '--project', str(self.root),
                '--input', str(request)]
        for access, expected in [(None, 2),
                ({'mode': 'write', 'kinds': ['product'], 'dependencies': ['category']}, 2),
                ({'mode': 'readonly', 'kinds': [], 'dependencies': []}, 0),
                ({'mode': 'write', 'kinds': ['unrelated'], 'dependencies': []}, 0)]:
            value = {'resource_environment': 'tenant-dev'}
            if access is not None:
                value['resource_access'] = access
            request.write_text(json.dumps(value))
            result = subprocess.run(args, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(json.loads(result.stdout)['allowed'], expected == 0)

    def test_raw_error_message_fields_are_rejected_without_persisting_secrets(self):
        resource = self.resource(msg='credential-must-not-persist', http_status=400, business_status=10)
        with self.assertRaisesRegex(ValueError, '^invalid cleanup resource fields$'):
            self.cleanup(resource)
        self.assertNotIn('credential-must-not-persist', (self.root / self.journal).read_text())
        self.assertFalse((self.root / cp.RESIDUAL_INDEX).exists())

    def test_parallel_journals_do_not_lose_incremental_updates(self):
        journals = [self.new_run(run_id='parallel-' + str(i)) for i in range(2)]
        program = """import sys
sys.path.insert(0,sys.argv[1]); import checkpoint as c
c.dispatch(sys.argv[2],sys.argv[3],'record',{'type':'cleanup_state','event_id':'cleanup',
'resources':[{'kind':'category','id':sys.argv[4],'status':'cleanup_failed','evidence':['proof.json']}]})
"""
        children = [subprocess.Popen([sys.executable, '-B', '-c', program, str(Path(cp.__file__).parent),
                    str(self.root), journal, str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for i, journal in enumerate(journals)]
        for child in children:
            out, err = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, (out, err))
        self.assertEqual(len(cp.residual_index(self.root)['resources']), 2)


if __name__ == '__main__':
    unittest.main()
