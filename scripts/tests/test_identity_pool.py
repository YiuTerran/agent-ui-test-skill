"""Cross-run identity pool: capacity, exclusive leases and quarantine accounting."""
import json
from unittest import mock
import unittest

import test_workflow as base

wf = base.wf


class IdentityPoolTest(unittest.TestCase):
    for _name in ('setUp', 'tearDown', 'write', 'cli', 'start', 'record', 'check_sources', 'state',
                  'execution', 'mark_legacy_run'):
        locals()[_name] = getattr(base.WorkflowTest, _name)

    def configure(self, limit=2):
        spec = wf.parse_spec(self.project / 'test/spec.md')
        spec['identity_pools'] = {'pool-a': {'max_accounts': limit}}
        self.write('test/spec.md', '# Synthetic\n\n```json workflow-config\n' + json.dumps(spec) + '\n```\n')

    def begin(self, limit=2):
        self.configure(limit)
        self.start()
        self.mark_legacy_run()
        self.check_sources()

    def intent(self, rid):
        return {'type': 'resource', 'resource_id': rid, 'status': 'intent', 'kind': 'account', 'pool': 'pool-a',
                'created_by_run': self.run}

    def create(self, rid, ref, retention='pooled'):
        intent = self.intent(rid)
        self.record(intent)
        self.record(dict(intent, status='confirmed', owned=True, receipt='synthetic-create-receipt'))
        self.record(dict(intent, status='retained_verified', retention=retention, pool_ref=ref,
                         evidence=['test/evidence.txt']))

    def pool(self, command, *args, fail=False):
        if command in {'pool-lease', 'pool-account'} and '--request' not in args:
            args = ('--request', 'runner-1', *args)
        return self.cli(command, '--run', self.run, '--owner', self.owner, '--pool', 'pool-a', *args, fail=fail)

    def give_back(self, *items, command='pool-return', fail=False):
        self.write('pool-input.json', {'accounts': list(items)})
        return self.pool(command, '--input', str(self.project / 'pool-input.json'), fail=fail)

    def ledger(self):
        return wf.load(self.project / wf.POOL_LEDGER)['pools']['pool-a']['accounts']

    def accept(self, fail=False):
        self.record([self.execution('E1'), self.execution('E2', 'C2')])
        return self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'}, fail=fail)

    def test_retained_account_is_registered_and_leased_exclusively(self):
        self.begin()
        self.create('a1', 'user-01')
        self.assertEqual('available', self.ledger()['user-01']['status'])
        result = self.pool('pool-lease', '--count', '1')
        self.assertEqual(['user-01'], result['leased'])
        leases = wf.load(self.project / result['lease_file'])
        self.assertEqual({'pool-a': {'worker_count': 1, 'accounts': ['user-01']}},
                         leases['requests']['runner-1']['pools'])
        exhausted = self.pool('pool-lease', '--request', 'runner-2', '--count', '1', fail=True)
        self.assertEqual('pool_exhausted', exhausted['error'])
        self.assertEqual(1, exhausted['occupied'])
        self.assertEqual(['user-01'], self.cli('status', '--run', self.run, '--full')['identity_pools']['pool-a']
                         ['leased_by_this_run'])

    def test_capacity_counts_intents_disabled_and_quarantined_accounts(self):
        self.begin(limit=2)
        self.create('a1', 'user-01', retention='disabled')
        self.record(self.intent('a2'))
        error = self.record(self.intent('a3'), fail=True)['error']
        self.assertIn('is full: 2/2', error)
        self.assertNotIn('a3', self.state()['resources'])

    def test_pool_requires_spec_limit_and_fixed_membership(self):
        self.start()
        self.mark_legacy_run()
        self.check_sources()
        self.assertIn('max_accounts', self.record(self.intent('a1'), fail=True)['error'])
        plain = {'type': 'resource', 'resource_id': 'p1', 'status': 'intent', 'kind': 'account', 'created_by_run': self.run}
        self.record(plain)
        self.record(dict(plain, status='confirmed', owned=True, receipt='r'))
        self.assertIn('pool is fixed', self.record(dict(plain, status='retained_verified', retention='pooled',
                                                        pool='pool-a', pool_ref='user-01',
                                                        evidence=['test/evidence.txt']), fail=True)['error'])

    def test_pool_ref_cannot_be_reused_or_contain_paths(self):
        self.begin()
        self.create('a1', 'user-01')
        intent = self.intent('a2')
        self.record(intent)
        self.record(dict(intent, status='confirmed', owned=True, receipt='r'))
        retained = dict(intent, status='retained_verified', retention='pooled', evidence=['test/evidence.txt'])
        self.assertIn('already belongs', self.record(dict(retained, pool_ref='user-01'), fail=True)['error'])
        self.assertIn('reference name', self.record(dict(retained, pool_ref='a/b'), fail=True)['error'])

    def test_return_requires_lease_evidence_and_open_issue_for_quarantine(self):
        self.begin()
        self.create('a1', 'user-01')
        self.assertIn('not leased', self.give_back({'pool_ref': 'user-01', 'result': 'pooled',
                                                     'evidence': ['test/evidence.txt']}, fail=True)['error'])
        self.pool('pool-lease', '--count', '1')
        self.assertIn('readback evidence', self.give_back({'pool_ref': 'user-01', 'result': 'pooled'},
                                                          fail=True)['error'])
        self.assertIn('open environment issue', self.give_back(
            {'pool_ref': 'user-01', 'result': 'quarantined', 'issue_id': 'I1', 'evidence': ['test/evidence.txt']},
            fail=True)['error'])
        self.assertIn('credential field', self.give_back(
            {'pool_ref': 'user-01', 'result': 'pooled', 'password': 'x', 'evidence': ['test/evidence.txt']},
            fail=True)['error'])
        self.assertEqual('leased', self.ledger()['user-01']['status'])
        self.give_back({'pool_ref': 'user-01', 'result': 'pooled', 'evidence': ['test/evidence.txt']})
        self.assertEqual('available', self.ledger()['user-01']['status'])
        self.assertNotIn('leased_by_run', self.ledger()['user-01'])

    def test_leased_account_blocks_acceptance_finalize_and_new_round(self):
        self.begin()
        self.create('a1', 'user-01')
        self.pool('pool-lease', '--count', '1')
        self.assertIn('must be returned', self.accept(fail=True)['error'])
        self.write('finalize.json', {'acceptance': {'status': 'partially_blocked', 'reason': 'Synthetic'},
                                    'require_full': False})
        result = self.cli('finalize', '--run', self.run, '--owner', self.owner,
                          '--input', str(self.project / 'finalize.json'), fail=True)
        self.assertIn('returned pool accounts', result['error'])
        self.assertIn('must be returned', self.record({'type': 'acceptance', 'owner': self.owner,
                                                       'status': 'completed_with_failures'}, fail=True)['error'])
        error = self.cli('start', '--owner', self.owner, '--new-round', fail=True)['error']
        self.assertIn('return leased pool accounts', error)

    def test_quarantine_blocks_only_its_run_and_resolution_reopens_account(self):
        self.begin()
        self.create('a1', 'user-01')
        self.create('a2', 'user-02')
        self.pool('pool-lease', '--count', '2')
        self.record({'type': 'issue', 'issue_id': 'I1', 'status': 'open', 'kind': 'environment'})
        self.give_back({'pool_ref': 'user-01', 'result': 'quarantined', 'issue_id': 'I1', 'reason': 'role not revoked',
                        'evidence': ['test/evidence.txt']},
                       {'pool_ref': 'user-02', 'result': 'pooled', 'evidence': ['test/evidence.txt']})
        self.assertIn('quarantined by this run', self.accept(fail=True)['error'])
        view = self.cli('status', '--run', self.run, '--full')
        self.assertIn('resolve_quarantined_pool_accounts', view['next_steps'])
        self.assertEqual(1, view['identity_pools']['pool-a']['quarantined'])
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'completed_with_failures'})
        self.start()
        self.mark_legacy_run()
        self.check_sources()
        self.record({'type': 'recovery_review', 'environment_checked': True, 'artifacts_checked': True})
        # Later runs skip the quarantined account instead of inheriting a block.
        self.assertEqual(['user-02'], self.pool('pool-lease', '--count', '1')['leased'])
        self.give_back({'pool_ref': 'user-02', 'result': 'pooled', 'evidence': ['test/evidence.txt']})
        self.assertIn('reason', self.give_back({'pool_ref': 'user-01', 'result': 'available',
                                                'evidence': ['test/evidence.txt']},
                                               command='pool-resolve', fail=True)['error'])
        self.give_back({'pool_ref': 'user-01', 'result': 'available', 'reason': 'role revoked and verified',
                        'evidence': ['test/evidence.txt']}, command='pool-resolve')
        account = self.ledger()['user-01']
        self.assertEqual('available', account['status'])
        self.assertEqual('I1', account['resolved_quarantine']['issue_id'])
        self.accept()

    def test_runner_assignments_are_disjoint_and_slots_do_not_wrap(self):
        self.begin(limit=4)
        for i in range(4):
            self.create('a' + str(i), 'user-' + str(i))
        first = self.pool('pool-lease', '--count', '2')
        second = self.pool('pool-lease', '--request', 'runner-2', '--count', '2')
        self.assertFalse(set(first['leased']) & set(second['leased']))
        for request, expected in [('runner-1', first['leased']), ('runner-2', second['leased'])]:
            for slot, ref in enumerate(expected):
                resolved = self.pool('pool-account', '--request', request, '--workers', '2', '--slot', str(slot))
                self.assertEqual(ref, resolved['pool_ref'])
            self.assertIn('concurrent slot count', self.pool(
                'pool-account', '--request', request, '--workers', '4', '--slot', '0', fail=True)['error'])
            self.assertIn('outside leased', self.pool(
                'pool-account', '--request', request, '--workers', '2', '--slot', '2', fail=True)['error'])

    def test_lease_retry_after_assignment_write_failure_replays_exact_allocation(self):
        self.begin()
        self.create('a1', 'user-01')
        self.create('a2', 'user-02')
        atomic = wf.atomic

        def interrupted(path, *args, **kwargs):
            if path.name == 'identity-leases.json':
                raise OSError('synthetic assignment write interruption')
            return atomic(path, *args, **kwargs)

        with mock.patch.object(wf, 'atomic', side_effect=interrupted):
            self.assertIn('interruption', self.pool('pool-lease', '--count', '1', fail=True)['error'])
        before = self.ledger()
        result = self.pool('pool-lease', '--count', '1')
        self.assertEqual(['user-01'], result['leased'])
        self.assertTrue(result['replayed'])
        self.assertEqual(before, self.ledger())
        self.assertEqual('available', self.ledger()['user-02']['status'])
        manifest = wf.load(self.project / result['lease_file'])
        self.assertEqual(['user-01'], manifest['requests']['runner-1']['pools']['pool-a']['accounts'])
        self.assertIn('count cannot change', self.pool('pool-lease', '--count', '2', fail=True)['error'])

    def test_lease_failure_before_ledger_commit_leaves_no_request_or_allocation(self):
        self.begin()
        self.create('a1', 'user-01')
        self.create('a2', 'user-02')
        before = wf.load(self.project / wf.POOL_LEDGER)
        atomic = wf.atomic

        def interrupted(path, *args, **kwargs):
            if path.name == 'identity-pools.json':
                raise OSError('synthetic ledger write interruption')
            return atomic(path, *args, **kwargs)

        with mock.patch.object(wf, 'atomic', side_effect=interrupted):
            self.assertIn('interruption', self.pool('pool-lease', '--count', '1', fail=True)['error'])
        self.assertEqual(before, wf.load(self.project / wf.POOL_LEDGER))
        self.assertEqual(['user-01'], self.pool('pool-lease', '--count', '1')['leased'])

    def test_lease_requires_a_valid_runner_request_before_mutating_accounts(self):
        self.begin()
        self.create('a1', 'user-01')
        before = wf.load(self.project / wf.POOL_LEDGER)
        for request in ([], ['--request', ''], ['--request', '../other'], ['--request', 'x' * 65]):
            result = self.cli('pool-lease', '--run', self.run, '--owner', self.owner,
                              '--pool', 'pool-a', '--count', '1', *request, fail=True)
            self.assertIn('--request must identify', result['error'])
            self.assertEqual(before, wf.load(self.project / wf.POOL_LEDGER))

    def test_partial_return_keeps_slot_numbers_and_prevents_stale_assignment_use(self):
        self.begin()
        self.create('a1', 'user-01')
        self.create('a2', 'user-02')
        lease = self.pool('pool-lease', '--count', '2')
        self.give_back({'pool_ref': 'user-01', 'result': 'pooled', 'evidence': ['test/evidence.txt']})
        manifest = wf.load(self.project / lease['lease_file'])
        self.assertEqual([None, 'user-02'], manifest['requests']['runner-1']['pools']['pool-a']['accounts'])
        self.assertIn('no longer leased', self.pool(
            'pool-account', '--workers', '2', '--slot', '0', fail=True)['error'])
        self.assertEqual('user-02', self.pool('pool-account', '--workers', '2', '--slot', '1')['pool_ref'])
        self.assertIn('returned accounts', self.pool('pool-lease', '--count', '2', fail=True)['error'])
        self.assertEqual(['user-01'], self.pool('pool-lease', '--request', 'runner-2', '--count', '1')['leased'])
        self.assertIn('no longer leased', self.pool(
            'pool-account', '--workers', '2', '--slot', '0', fail=True)['error'])

    def test_return_retry_after_assignment_write_failure_does_not_reapply(self):
        self.begin()
        self.create('a1', 'user-01')
        self.pool('pool-lease', '--count', '1')
        item = {'pool_ref': 'user-01', 'result': 'pooled', 'evidence': ['test/evidence.txt']}
        atomic = wf.atomic

        def interrupted(path, *args, **kwargs):
            if path.name == 'identity-leases.json':
                raise OSError('synthetic return write interruption')
            return atomic(path, *args, **kwargs)

        with mock.patch.object(wf, 'atomic', side_effect=interrupted):
            self.assertIn('interruption', self.give_back(item, fail=True)['error'])
        before = self.ledger()
        self.give_back(item)
        self.assertEqual(before, self.ledger())
        self.pool('pool-lease', '--request', 'runner-2', '--count', '1')
        self.give_back(item)
        self.assertEqual('available', self.ledger()['user-01']['status'])
        self.assertEqual(2, sum(h['action'] == 'returned_pooled' for h in self.ledger()['user-01']['history']))

    def test_unrelated_records_ignore_corrupt_historical_run_without_pool_sync(self):
        self.start()
        self.mark_legacy_run()
        self.check_sources()
        self.write('test/.cache/runs/broken/state.json', '{')
        before = len(self.state()['events'])
        at = wf.now()
        with mock.patch.object(wf, 'sync_pool_ledger', wraps=wf.sync_pool_ledger) as sync:
            self.record({'type': 'issue', 'issue_id': 'I1', 'kind': 'environment', 'status': 'open'})
            self.record({'type': 'phase_time', 'phase': 'exploration', 'started_at': at, 'ended_at': at})
        sync.assert_not_called()
        self.assertEqual(before + 2, len(self.state()['events']))
        self.assertEqual(1, len(self.state()['phase_times']))

    def test_retained_sync_failure_precedes_state_commit_and_retry_records_once(self):
        self.begin()
        intent = self.intent('a1')
        self.record(intent)
        self.record(dict(intent, status='confirmed', owned=True, receipt='synthetic-create'))
        retained = {'type': 'resource', 'resource_id': 'a1', 'created_by_run': self.run,
                    'status': 'retained_verified', 'retention': 'pooled', 'pool_ref': 'user-01',
                    'evidence': ['test/evidence.txt']}
        before = self.state()
        with mock.patch.object(wf, 'sync_pool_ledger', side_effect=OSError('synthetic sync failure')) as sync:
            self.assertIn('sync failure', self.record(retained, fail=True)['error'])
        self.assertEqual(before, self.state())
        self.assertEqual(True, sync.call_args.kwargs['current_only'])
        self.record(retained)
        self.assertEqual(len(before['events']) + 1, len(self.state()['events']))
        self.assertEqual('available', self.ledger()['user-01']['status'])

    def test_state_write_failure_after_pool_sync_can_retry_without_duplicate_registration(self):
        self.begin()
        intent = self.intent('a1')
        self.record(intent)
        self.record(dict(intent, status='confirmed', owned=True, receipt='synthetic-create'))
        retained = dict(intent, status='retained_verified', retention='pooled', pool_ref='user-01',
                        evidence=['test/evidence.txt'])
        atomic = wf.atomic
        before = self.state()

        def interrupted(path, *args, **kwargs):
            if path.name == 'state.json':
                raise OSError('synthetic state write failure')
            return atomic(path, *args, **kwargs)

        with mock.patch.object(wf, 'atomic', side_effect=interrupted):
            self.assertIn('state write failure', self.record(retained, fail=True)['error'])
        self.assertEqual(before, self.state())
        self.assertEqual(['user-01'], list(self.ledger()))
        self.record(retained)
        self.assertEqual(len(before['events']) + 1, len(self.state()['events']))
        self.assertEqual(['user-01'], list(self.ledger()))

    def test_current_run_sync_never_scans_corrupt_history_but_pool_operations_still_do(self):
        self.begin()
        self.create('a1', 'user-01')
        (self.project / wf.POOL_LEDGER).unlink()
        self.write('test/.cache/runs/broken/state.json', '{')
        wf.sync_pool_ledger(self.project, self.state(), current_only=True)
        self.assertEqual('available', self.ledger()['user-01']['status'])
        self.assertIn('unreadable run state', self.pool('pool-lease', '--count', '1', fail=True)['error'])

    def test_readonly_slot_lookup_needs_active_claim_but_no_owner_or_writes(self):
        self.begin()
        self.create('a1', 'user-01')
        self.pool('pool-lease', '--count', '1')
        before = self.state()
        ledger = wf.load(self.project / wf.POOL_LEDGER)
        args = ['--run', self.run, '--pool', 'pool-a', '--request', 'runner-1', '--workers', '1', '--slot', '0']
        with mock.patch.object(wf, 'atomic', side_effect=AssertionError('read-only lookup')):
            self.assertEqual('user-01', self.cli('pool-account', *args)['pool_ref'])
        self.assertEqual(before, self.state())
        self.assertEqual(ledger, wf.load(self.project / wf.POOL_LEDGER))
        before['claim']['renewed_at'] = '2000-01-01T00:00:00+00:00'
        self.write(f'test/.cache/runs/{self.run}/state.json', before)
        self.assertIn('expired', self.cli('pool-account', *args, fail=True)['error'])
        before['claim']['renewed_at'] = wf.now()
        before['acceptance'] = 'partially_blocked'
        self.write(f'test/.cache/runs/{self.run}/state.json', before)
        self.assertIn('active round', self.cli('pool-account', *args, fail=True)['error'])
        before['claim'] = None
        self.write(f'test/.cache/runs/{self.run}/state.json', before)
        self.assertIn('released', self.cli('pool-account', *args, fail=True)['error'])

    def test_closed_requests_leave_global_ledger_and_cannot_reallocate_on_replay(self):
        self.begin()
        self.create('a1', 'user-01')
        self.create('a2', 'user-02')
        lease = self.pool('pool-lease', '--count', '1')
        self.pool('pool-lease', '--request', 'runner-2', '--count', '1')
        self.give_back({'pool_ref': 'user-01', 'result': 'pooled', 'evidence': ['test/evidence.txt']})
        self.assertIn(self.run, wf.load(self.project / wf.POOL_LEDGER)['pools']['pool-a']['requests'])
        self.give_back({'pool_ref': 'user-02', 'result': 'pooled', 'evidence': ['test/evidence.txt']})
        self.assertNotIn('requests', wf.load(self.project / wf.POOL_LEDGER)['pools']['pool-a'])
        self.assertEqual(['runner-1', 'runner-2'], self.state()['closed_pool_requests']['pool-a'])
        self.assertEqual({}, wf.load(self.project / lease['lease_file'])['requests'])
        self.assertIn('returned accounts', self.pool('pool-lease', '--count', '1', fail=True)['error'])
        self.assertEqual(['user-01'], self.pool('pool-lease', '--request', 'runner-3', '--count', '1')['leased'])

    def test_return_before_tombstone_write_failure_preserves_original_request_for_retry(self):
        self.begin()
        self.create('a1', 'user-01')
        self.pool('pool-lease', '--count', '1')
        before = self.state()
        item = {'pool_ref': 'user-01', 'result': 'pooled', 'evidence': ['test/evidence.txt']}
        atomic = wf.atomic

        def interrupted(path, *args, **kwargs):
            if path.name == 'state.json':
                raise OSError('synthetic tombstone interruption')
            return atomic(path, *args, **kwargs)

        with mock.patch.object(wf, 'atomic', side_effect=interrupted):
            self.assertIn('tombstone interruption', self.give_back(item, fail=True)['error'])
        self.assertEqual(before, self.state())
        self.assertIn(self.run, wf.load(self.project / wf.POOL_LEDGER)['pools']['pool-a']['requests'])
        self.assertIn('returned accounts', self.pool('pool-lease', '--count', '1', fail=True)['error'])
        self.give_back(item)
        self.assertNotIn('requests', wf.load(self.project / wf.POOL_LEDGER)['pools']['pool-a'])
        self.assertEqual(['runner-1'], self.state()['closed_pool_requests']['pool-a'])

    def test_request_prune_interruption_keeps_replay_guard_and_return_can_resume(self):
        self.begin()
        self.create('a1', 'user-01')
        self.pool('pool-lease', '--count', '1')
        item = {'pool_ref': 'user-01', 'result': 'pooled', 'evidence': ['test/evidence.txt']}
        atomic = wf.atomic

        def interrupted(path, data, *args, **kwargs):
            if path.name == 'identity-pools.json' and 'requests' not in data['pools']['pool-a']:
                raise OSError('synthetic pruning interruption')
            return atomic(path, data, *args, **kwargs)

        with mock.patch.object(wf, 'atomic', side_effect=interrupted):
            self.assertIn('pruning interruption', self.give_back(item, fail=True)['error'])
        self.assertEqual(['runner-1'], self.state()['closed_pool_requests']['pool-a'])
        self.assertIn('returned accounts', self.pool('pool-lease', '--count', '1', fail=True)['error'])
        self.give_back(item)
        self.assertNotIn('requests', wf.load(self.project / wf.POOL_LEDGER)['pools']['pool-a'])
        self.assertEqual(1, sum(h['action'] == 'returned_pooled' for h in self.ledger()['user-01']['history']))


if __name__ == '__main__':
    unittest.main()
