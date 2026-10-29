"""Resources that cannot be deleted close only in a verified pooled or disabled state."""
import json
import unittest

import test_workflow as base

wf = base.wf


class RetainedResourceTest(unittest.TestCase):
    for _name in ('setUp', 'tearDown', 'write', 'cli', 'start', 'record', 'check_sources', 'state',
                  'execution', 'mark_legacy_run'):
        locals()[_name] = getattr(base.WorkflowTest, _name)

    def with_pool(self, limit=2):
        spec = wf.parse_spec(self.project / 'test/spec.md')
        spec['identity_pools'] = {'pool-a': {'max_accounts': limit}}
        self.write('test/spec.md', '# Synthetic\n\n```json workflow-config\n' + json.dumps(spec) + '\n```\n')

    def confirmed(self, rid='acct-1', pool='pool-a'):
        if pool:
            self.with_pool()
        self.start()
        self.mark_legacy_run()
        self.check_sources()
        intent = {'type': 'resource', 'resource_id': rid, 'status': 'intent', 'kind': 'account',
                  'created_by_run': self.run}
        if pool:
            intent['pool'] = pool
        self.record(intent)
        self.record(dict(intent, status='confirmed', owned=True, receipt='synthetic-create-receipt'))
        return intent

    def test_pooled_account_closes_without_blocking_full_acceptance(self):
        intent = self.confirmed()
        self.record([self.execution('E1'), self.execution('E2', 'C2')])
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'}, fail=True)
        self.record(dict(intent, status='retained_verified', retention='pooled', pool_ref='user-01',
                         evidence=['test/evidence.txt']))
        view = self.cli('status', '--run', self.run, '--full')
        self.assertEqual([], view['residual_resources'])
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'})
        counts = self.cli('report', '--run', self.run, '--owner', self.owner)['counts']
        self.assertEqual({'disabled': 0, 'pooled': 1}, counts['retained_resources'])
        self.assertEqual(1, counts['resources']['retained_verified'])

    def test_disabled_account_needs_evidence_and_is_final(self):
        intent = self.confirmed(pool=None)
        self.assertIn('readback evidence', self.record(dict(intent, status='retained_verified', retention='disabled'),
                                                       fail=True)['error'])
        self.record(dict(intent, status='retained_verified', retention='disabled', evidence=['test/evidence.txt']))
        self.assertIn('invalid resource transition', self.record(dict(intent, status='absence_verified',
                                                                      evidence=['test/evidence.txt']), fail=True)['error'])

    def test_retention_mode_and_pool_ref_are_validated(self):
        intent = self.confirmed()
        for extra, message in (({'retention': 'kept'}, 'retention=pooled or disabled'),
                               ({}, 'retention=pooled or disabled'),
                               ({'retention': 'pooled'}, 'pool_ref'),
                               ({'retention': 'pooled', 'pool_ref': ''}, 'pool_ref')):
            with self.subTest(extra=extra):
                error = self.record(dict(intent, status='retained_verified', evidence=['test/evidence.txt'], **extra),
                                    fail=True)['error']
                self.assertIn(message, error)
        self.assertIn('retained_verified only', self.record(dict(intent, status='absence_verified', retention='pooled',
                                                                 evidence=['test/evidence.txt']), fail=True)['error'])
        self.assertIn('credential field', self.record(dict(intent, status='retained_verified', retention='pooled',
                                                           pool_ref='user-01', password='x',
                                                           evidence=['test/evidence.txt']), fail=True)['error'])
        self.assertEqual('confirmed', self.state()['resources']['acct-1']['status'])

    def test_unconfirmed_resource_cannot_be_retained(self):
        self.start()
        self.mark_legacy_run()
        self.check_sources()
        intent = {'type': 'resource', 'resource_id': 'acct-1', 'status': 'intent', 'kind': 'account',
                  'created_by_run': self.run}
        self.record(intent)
        self.assertIn('invalid resource transition', self.record(dict(intent, status='retained_verified',
                                                                      retention='disabled',
                                                                      evidence=['test/evidence.txt']), fail=True)['error'])

    def test_shared_config_cannot_close_as_retained(self):
        self.start()
        self.mark_legacy_run()
        self.check_sources()
        original = f'test/.cache/runs/{self.run}/evidence/original.json'
        self.write(original, {'theme': 'blue'})
        event = {'type': 'resource', 'resource_id': 'cfg-1', 'kind': 'shared_config', 'config_key': 'env/scope/theme',
                 'created_by_run': self.run}
        self.record(dict(event, status='intent', original_snapshot=original))
        self.record(dict(event, status='confirmed', owned=True, receipt='synthetic-update-response'))
        self.assertIn('restored readback', self.record(dict(event, status='retained_verified', retention='disabled',
                                                            evidence=['test/evidence.txt']), fail=True)['error'])

    def test_inherited_account_can_be_retained_by_next_run(self):
        intent = self.confirmed()
        oldrun = self.run
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'})
        self.start()
        self.check_sources()
        self.record({'type': 'recovery_review', 'environment_checked': True, 'artifacts_checked': True})
        self.assertEqual('cleanup', self.cli('status', '--run', self.run, '--full')['residual_resources'][0]['suggestion'])
        self.record(dict(intent, created_by_run=oldrun, status='retained_verified', retention='pooled',
                         pool_ref='user-01', evidence=['test/evidence.txt']))
        view = self.cli('status', '--run', self.run, '--full')
        self.assertEqual([], view['residual_resources'])
        self.assertEqual([], view['global_residual_runs'])


if __name__ == '__main__':
    unittest.main()
