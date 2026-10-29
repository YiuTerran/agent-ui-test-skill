"""Shared configuration changes close only with a readback equal to the saved original."""
import unittest

import test_workflow as base

wf = base.wf


class SharedConfigTest(unittest.TestCase):
    for _name in ('setUp', 'tearDown', 'write', 'cli', 'start', 'record', 'check_sources', 'state',
                  'execution', 'mark_legacy_run'):
        locals()[_name] = getattr(base.WorkflowTest, _name)

    def prepared(self):
        self.start()
        self.mark_legacy_run()
        self.check_sources()
        self.original = f'test/.cache/runs/{self.run}/evidence/original.json'
        self.write(self.original, {'theme': 'blue', 'limits': {'upload': 10}})
        return {'type': 'resource', 'resource_id': 'cfg-1', 'kind': 'shared_config', 'config_key': 'env-a/scope-a/theme',
                'created_by_run': self.run}

    def changed(self):
        event = self.prepared()
        self.record(dict(event, status='intent', original_snapshot=self.original))
        self.record(dict(event, status='confirmed', owned=True, receipt='synthetic-update-response'))
        return event

    def test_intent_requires_config_key_and_original_snapshot_under_cache(self):
        event = self.prepared()
        self.assertIn('config_key', self.record(dict(event, status='intent', config_key=None,
                                                     original_snapshot=self.original), fail=True)['error'])
        self.assertIn('original_snapshot', self.record(dict(event, status='intent'), fail=True)['error'])
        self.write('test/original.json', {'theme': 'blue'})
        self.assertIn('test/.cache/', self.record(dict(event, status='intent', original_snapshot='test/original.json'),
                                                  fail=True)['error'])
        self.write(f'test/.cache/runs/{self.run}/evidence/bad.json', 'not json')
        self.assertIn('valid JSON', self.record(dict(event, status='intent', original_snapshot=
                                                     f'test/.cache/runs/{self.run}/evidence/bad.json'), fail=True)['error'])
        self.record(dict(event, status='intent', original_snapshot=self.original))
        stored = self.state()['resources']['cfg-1']
        self.assertEqual(wf.fingerprint({'theme': 'blue', 'limits': {'upload': 10}}), stored['original_sha256'])
        self.assertIn(self.original, stored['evidence'])

    def test_unrestored_config_blocks_full_acceptance_and_suggests_restore(self):
        event = self.changed()
        restored = f'test/.cache/runs/{self.run}/evidence/restored.json'
        self.write(restored, {'theme': 'red', 'limits': {'upload': 10}})
        error = self.record(dict(event, status='absence_verified', restored_snapshot=restored,
                                 evidence=['test/evidence.txt']), fail=True)['error']
        self.assertIn('differs from original', error)
        self.assertEqual('confirmed', self.state()['resources']['cfg-1']['status'])
        view = self.cli('status', '--run', self.run, '--full')
        self.assertEqual('restore', view['residual_resources'][0]['suggestion'])
        self.assertEqual('env-a/scope-a/theme', view['residual_resources'][0]['config_key'])
        self.record([self.execution('E1'), self.execution('E2', 'C2')])
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'}, fail=True)
        # Key order is not a configuration difference.
        self.write(restored, {'limits': {'upload': 10}, 'theme': 'blue'})
        self.record(dict(event, status='absence_verified', restored_snapshot=restored, evidence=['test/evidence.txt']))
        self.assertIn(restored, self.state()['resources']['cfg-1']['evidence'])
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'fully_accepted'})

    def test_original_snapshot_cannot_be_replaced_or_edited(self):
        event = self.changed()
        other = f'test/.cache/runs/{self.run}/evidence/other.json'
        self.write(other, {'theme': 'red'})
        self.assertIn('cannot change', self.record(dict(event, status='absence_verified', original_snapshot=other,
                                                        restored_snapshot=other, evidence=['test/evidence.txt']),
                                                   fail=True)['error'])
        self.write(self.original, {'theme': 'red'})
        self.assertIn('changed after intent', self.record(dict(event, status='absence_verified', restored_snapshot=other,
                                                               evidence=['test/evidence.txt']), fail=True)['error'])

    def test_kind_and_key_are_fixed_and_restored_snapshot_only_closes(self):
        event = self.prepared()
        self.record(dict(event, status='intent', original_snapshot=self.original))
        self.assertIn('kind cannot change', self.record(dict(event, status='confirmed', kind='record', owned=True,
                                                             receipt='r'), fail=True)['error'])
        self.assertIn('config_key cannot change', self.record(dict(event, status='confirmed', config_key='other',
                                                                   owned=True, receipt='r'), fail=True)['error'])
        self.assertIn('restored_snapshot', self.record(dict(event, status='confirmed', owned=True, receipt='r',
                                                            restored_snapshot=self.original), fail=True)['error'])
        self.assertIn('computed', self.record(dict(event, resource_id='cfg-2', status='intent', original_sha256='x',
                                                   original_snapshot=self.original), fail=True)['error'])

    def test_ordinary_resources_keep_existing_rules(self):
        self.prepared()
        intent = {'type': 'resource', 'resource_id': 'r1', 'status': 'intent', 'kind': 'record', 'created_by_run': self.run}
        self.record(intent)
        self.record(dict(intent, status='confirmed', owned=True, receipt='synthetic-create-receipt'))
        self.assertEqual('cleanup', self.cli('status', '--run', self.run, '--full')['residual_resources'][0]['suggestion'])
        self.record(dict(intent, status='absence_verified', evidence=['test/evidence.txt']))

    def test_ordinary_resource_kind_can_be_added_when_confirming(self):
        self.prepared()
        intent = {'type': 'resource', 'resource_id': 'late-kind', 'status': 'intent', 'created_by_run': self.run}
        self.record(intent)
        self.record(dict(intent, status='confirmed', kind='record', owned=True, receipt='synthetic-create-receipt'))
        self.assertEqual('record', self.state()['resources']['late-kind']['kind'])
        self.assertEqual('cleanup', self.cli('status', '--run', self.run, '--full')['residual_resources'][0]['suggestion'])
        self.record(dict(intent, status='absence_verified', evidence=['test/evidence.txt']))
        self.assertEqual('record', self.state()['resources']['late-kind']['kind'])

    def test_ordinary_resource_kind_can_be_added_when_verifying_absence(self):
        self.prepared()
        intent = {'type': 'resource', 'resource_id': 'late-kind', 'status': 'intent', 'created_by_run': self.run}
        self.record(intent)
        self.record(dict(intent, status='absence_verified', kind='record', evidence=['test/evidence.txt']))
        self.assertEqual('absence_verified', self.state()['resources']['late-kind']['status'])

    def test_ordinary_resource_cannot_be_promoted_to_shared_config(self):
        self.prepared()
        for initial_kind in (None, 'record'):
            with self.subTest(initial_kind=initial_kind):
                intent = {'type': 'resource', 'resource_id': 'promote-' + str(initial_kind),
                          'status': 'intent', 'created_by_run': self.run}
                if initial_kind is not None:
                    intent['kind'] = initial_kind
                self.record(intent)
                error = self.record(dict(intent, status='confirmed', kind='shared_config', config_key='env/scope/key',
                                         original_snapshot=self.original, owned=True, receipt='synthetic-response'),
                                    fail=True)['error']
                self.assertIn('resource kind cannot change', error)
                resource = self.state()['resources'][intent['resource_id']]
                self.assertEqual('intent', resource['status'])
                self.assertEqual(initial_kind, resource.get('kind'))

    def test_known_ordinary_resource_kind_cannot_change_or_be_cleared(self):
        self.prepared()
        for new_kind in ('account', None):
            with self.subTest(new_kind=new_kind):
                intent = {'type': 'resource', 'resource_id': 'fixed-' + str(new_kind), 'status': 'intent',
                          'kind': 'record', 'created_by_run': self.run}
                self.record(intent)
                error = self.record(dict(intent, status='confirmed', kind=new_kind, owned=True,
                                         receipt='synthetic-response'), fail=True)['error']
                self.assertIn('resource kind cannot change', error)
                self.assertEqual('record', self.state()['resources'][intent['resource_id']]['kind'])


if __name__ == '__main__':
    unittest.main()
