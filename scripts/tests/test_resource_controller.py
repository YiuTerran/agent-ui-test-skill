"""Dynamic resource policy, host bridge and disposable subprocess checks."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_suite_catalog

SPEC = importlib.util.spec_from_file_location(
    'resource_controller', Path(__file__).resolve().parents[1] / 'resource_controller.py')
rc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rc)
FIXTURE = Path(__file__).parent / 'fixtures/resource_adapter.py'


def pool(count=3, **extra):
    start = datetime.now(timezone.utc) - timedelta(seconds=count)
    return {'workers': [{'id': 'worker-' + str(index), 'state': 'active',
                         'started_at': (start + timedelta(seconds=index)).isoformat()}
                        for index in range(count)],
            'pending_tasks': 100, 'available_slots': 32, **extra}


def measurement(cpu=10, memory=20):
    return {'sampled_at': rc.scheduling.timestamp(), 'host': rc.HOST,
            'cpu_percent': cpu, 'memory_percent': memory, 'source': 'synthetic'}


class PolicyTest(unittest.TestCase):
    def decide(self, cpu=10, memory=20, current=None, **settings):
        return rc.decide(measurement(cpu, memory), current or pool(), rc.settings(settings))

    def test_pressure_removes_newest_one_and_preserves_minimum(self):
        for cpu, memory in ((80, 10), (10, 80), (99, 99)):
            result = self.decide(cpu, memory)
            self.assertEqual((result['action'], result['worker_id'], result['desired_subagents']),
                             ('drain', 'worker-2', 2))
        result = self.decide(99, 99, current=pool(1))
        self.assertEqual('hold', result['action'])
        self.assertTrue(result['admission_paused'])
        self.assertEqual(1, result['desired_subagents'])

    def test_hysteresis_slot_ceiling_and_backlog(self):
        self.assertEqual('hold', self.decide(60, 20)['action'])
        self.assertEqual('hold', self.decide(79, 79)['action'])
        self.assertEqual('spawn', self.decide(59, 59)['action'])
        for current in (pool(3, pending_tasks=0), pool(3, available_slots=0), pool(8)):
            self.assertEqual('hold', self.decide(current=current)['action'])

    def test_draining_workers_are_not_replaced_and_do_not_hide_active_floor(self):
        current = pool()
        current['workers'][-1]['state'] = 'draining'
        self.assertEqual('hold', self.decide(current=current)['action'])
        result = self.decide(99, 99, current=current)
        self.assertEqual('worker-1', result['worker_id'])
        self.assertEqual(1, result['desired_subagents'])

    def test_unknown_samples_pause_dispatch_and_never_expand(self):
        for value in (None, float('nan'), True, -1, 101):
            result = self.decide(cpu=value)
            self.assertTrue(result['admission_paused'])
            self.assertNotEqual('spawn', result['action'])
        self.assertEqual('drain', self.decide(cpu=None, memory=90)['action'])

    def test_large_pool_converges_to_floor_and_expands_one_at_a_time(self):
        current = pool(32)
        while len(current['workers']) > 1:
            decision = self.decide(90, 90, current=current, max_subagents=32)
            current['workers'] = [worker for worker in current['workers']
                                  if worker['id'] != decision['worker_id']]
        self.assertEqual(1, self.decide(90, 90, current=current)['desired_subagents'])
        for count in range(1, 32):
            decision = self.decide(current=pool(count), max_subagents=32)
            self.assertEqual(count + 1, decision['desired_subagents'])

    def test_settings_and_inventory_reject_ambiguous_values(self):
        for settings in ({'high_percent': 60, 'low_percent': 60},
                         {'interval_seconds': 0}, {'max_subagents': True},
                         {'sample_seconds': float('nan')}):
            with self.assertRaises(ValueError):
                rc.settings(settings)
        current = pool()
        current['workers'][1]['id'] = current['workers'][0]['id']
        with self.assertRaises(ValueError):
            self.decide(current=current)

    def test_real_sampling_api_and_partial_failure(self):
        fake = SimpleNamespace(cpu_percent=lambda interval: 21,
                               virtual_memory=lambda: SimpleNamespace(available=75, total=100))
        with patch.dict(sys.modules, psutil=fake), patch.object(rc, 'cgroup_samples', return_value={}):
            result = rc.sample(.01)
        self.assertEqual((21, 25), (result['cpu_percent'], result['memory_percent']))
        with patch.dict(sys.modules, psutil=None):
            result = rc.sample(.01)
        self.assertIsNone(result['cpu_percent'])
        self.assertIn('psutil_unavailable', result['errors'])

    def test_cgroup_pressure_overrides_idle_host(self):
        fake = SimpleNamespace(cpu_percent=lambda interval: 1,
                               virtual_memory=lambda: SimpleNamespace(available=90, total=100))
        before = {'group': {'cpu_capacity': 1, 'cpu_seconds': 1}}
        after = {'group': {'cpu_capacity': 1, 'cpu_seconds': 2, 'memory_percent': 95}}
        with patch.dict(sys.modules, psutil=fake), patch.object(rc, 'cgroup_samples',
                                                              side_effect=[before, after]), \
                patch.object(rc.time, 'monotonic', side_effect=[0, 1]):
            result = rc.sample(.01)
        self.assertEqual((100, 95), (result['cpu_percent'], result['memory_percent']))


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='adaptive-isolated-')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.root = self.project / 'fixture'
        self.command = [sys.executable, str(FIXTURE), str(self.root)]
        self.config = rc.settings({'interval_seconds': .05, 'sample_seconds': .01,
                                   'max_subagents': 32})
        self.addCleanup(self.drain_fixture)

    def call(self, action, **extra):
        return rc.adapter_call(self.command, self.project, 'fixture', action, 10, **extra)

    def drain_fixture(self):
        path = self.root / 'pool.json'
        if not path.exists():
            return
        rows = json.loads(path.read_text())['workers']
        for row in rows:
            (self.root / (row['id'] + '.drain')).touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all((self.root / (row['id'] + '.cleanup')).exists() for row in rows):
                return
            time.sleep(.02)
        self.fail('disposable worker did not complete cleanup')

    def test_watch_grows_then_drains_real_subprocesses(self):
        outputs = []
        with patch.object(rc, 'sample', return_value=measurement()):
            rc.watch(self.project, 'fixture', self.config, self.command, cycles=8,
                     emit=lambda line, **options: outputs.append(json.loads(line)))
        self.assertEqual(8, len(self.call('inspect')['workers']))
        self.assertEqual(list(range(1, 9)), [row['decision']['desired_subagents'] for row in outputs])
        with patch.object(rc, 'sample', return_value=measurement(90, 90)):
            rc.watch(self.project, 'fixture', self.config, self.command, cycles=7,
                     emit=lambda *args, **options: None)
        deadline = time.monotonic() + 3
        while len(self.call('inspect')['workers']) > 1 and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(1, len(self.call('inspect')['workers']))
        self.assertEqual(7, len(list(self.root.glob('*.cleanup'))))
        self.assertEqual('stopped', json.loads(rc.paths(self.project)[0].read_text())['status'])

    @unittest.skipUnless(importlib.util.find_spec('psutil'), 'optional psutil is unavailable')
    def test_low_threshold_drains_using_actual_host_samples(self):
        for index in range(8):
            self.call('spawn', action_id='seed-' + str(index))
        config = dict(self.config, high_percent=.000001, low_percent=0)
        outputs = []
        rc.watch(self.project, 'fixture', config, self.command, cycles=7,
                 emit=lambda line, **options: outputs.append(json.loads(line)))
        self.assertTrue(all(row['decision']['action'] == 'drain' for row in outputs))
        self.assertEqual(1, outputs[-1]['decision']['desired_subagents'])
        self.assertTrue(all(row['measurement']['source'] == 'psutil' for row in outputs))

    def test_pending_action_replay_is_idempotent_and_adapter_bound(self):
        path, _ = rc.paths(self.project)
        fingerprint = rc.hashlib.sha256(json.dumps(
            [str(self.project), 'fixture', self.command], ensure_ascii=True).encode()).hexdigest()
        pending = {'action': 'spawn', 'action_id': 'interrupted', 'before_worker_ids': []}
        self.call('spawn', action_id='interrupted')
        rc.write_state(path, 'fixture', self.config, measurement(), {},
                       pending_action=pending, adapter_fingerprint=fingerprint, status='stopped')
        with patch.object(rc, 'sample', return_value=measurement(70, 70)):
            rc.watch(self.project, 'fixture', self.config, self.command, cycles=1,
                     emit=lambda *args, **options: None)
        self.assertEqual(1, len(self.call('inspect')['workers']))
        state = json.loads(path.read_text())
        self.assertIsNone(state['pending_action'])
        state.update(pending_action=pending, adapter_fingerprint='different')
        rc.scheduling.atomic_json(path, state)
        with self.assertRaisesRegex(ValueError, 'original adapter'):
            rc.watch(self.project, 'fixture', self.config, self.command, cycles=1)

    def test_adapter_error_preserves_pending_action_and_closes_admission(self):
        original = rc.adapter_call

        def fail_spawn(command, project, controller_id, action, timeout, **extra):
            if action == 'spawn':
                raise subprocess.TimeoutExpired(command, timeout)
            return original(command, project, controller_id, action, timeout, **extra)

        with patch.object(rc, 'adapter_call', side_effect=fail_spawn), \
                patch.object(rc, 'sample', return_value=measurement()):
            with self.assertRaises(subprocess.TimeoutExpired):
                rc.watch(self.project, 'fixture', self.config, self.command, cycles=1)
        state = json.loads(rc.paths(self.project)[0].read_text())
        self.assertEqual('spawn', state['pending_action']['action'])
        with self.assertRaisesRegex(ValueError, 'not running'):
            rc.read_admission(self.project, 'fixture')

    def test_acknowledgement_without_real_worker_state_remains_pending(self):
        original = rc.adapter_call

        def ignore_action(command, project, controller_id, action, timeout, **extra):
            if action == 'spawn':
                return {'controller_id': controller_id, 'ok': True, 'action_id': extra['action_id']}
            return original(command, project, controller_id, action, timeout, **extra)

        with patch.object(rc, 'adapter_call', side_effect=ignore_action), \
                patch.object(rc, 'sample', return_value=measurement()):
            with self.assertRaisesRegex(ValueError, 'registered worker'):
                rc.watch(self.project, 'fixture', self.config, self.command, cycles=1)
        state = json.loads(rc.paths(self.project)[0].read_text())
        self.assertIsNotNone(state['pending_action'])
        self.assertEqual('stopped', state['status'])

    def test_failed_first_inspection_invalidates_previous_running_state(self):
        path, _ = rc.paths(self.project)
        rc.write_state(path, 'fixture', self.config, measurement(),
                       rc.decide(measurement(), pool(), self.config),
                       status='running', pending_action=None)
        with patch.object(rc, 'adapter_call', side_effect=ValueError('inspection failed')):
            with self.assertRaisesRegex(ValueError, 'inspection failed'):
                rc.watch(self.project, 'fixture', self.config, self.command, cycles=1)
        with self.assertRaisesRegex(ValueError, 'not running'):
            rc.read_admission(self.project, 'fixture')

    def test_drain_ack_requires_stopping_assignments(self):
        self.call('spawn', action_id='first')
        self.call('spawn', action_id='second')
        original = rc.adapter_call

        def ignore_drain(command, project, controller_id, action, timeout, **extra):
            if action == 'drain':
                return {'controller_id': controller_id, 'ok': True, 'action_id': extra['action_id']}
            return original(command, project, controller_id, action, timeout, **extra)

        with patch.object(rc, 'adapter_call', side_effect=ignore_drain), \
                patch.object(rc, 'sample', return_value=measurement(90, 90)):
            with self.assertRaisesRegex(ValueError, 'stopping new assignments'):
                rc.watch(self.project, 'fixture', self.config, self.command, cycles=1)
        state = json.loads(rc.paths(self.project)[0].read_text())
        self.assertEqual('drain', state['pending_action']['action'])
        self.assertEqual('stopped', state['status'])


class AdmissionTest(unittest.TestCase):
    setUp = test_suite_catalog.ApiSchedulingTest.setUp
    request = test_suite_catalog.ApiSchedulingTest.request
    def publish(self, decision=None, **extra):
        project = self.projects[0]
        config = rc.settings({})
        value = decision or rc.decide(measurement(70, 70), pool(2), config)
        return rc.write_state(rc.paths(project)[0], 'fixture', config, measurement(70, 70),
                              value, status='running', pending_action=None, **extra)

    def test_actual_resource_mode_shares_executor_capacity_across_kinds(self):
        self.publish()
        for index, kind in enumerate(('ui', 'api')):
            request = self.request(kind, conflict=kind)
            request['resource_controller'] = 'fixture'
            request['resources'].update(available_cpus=1, available_memory_bytes=1)
            self.assertTrue(test_suite_catalog.sc.execute('acquire', self.projects[index], request)['acquired'])
        request = self.request('api', conflict='extra')
        request.update(run_id='extra', resource_controller='fixture')
        self.assertFalse(test_suite_catalog.sc.execute('acquire', self.projects[1], request)['acquired'])

    def test_high_pressure_does_not_revoke_leases_but_blocks_new_claims(self):
        self.publish()
        request = self.request('ui', conflict='ui')
        request['resource_controller'] = 'fixture'
        first = test_suite_catalog.sc.execute('acquire', self.projects[0], request)
        self.assertTrue(first['acquired'])
        self.publish(rc.decide(measurement(95, 95), pool(2), rc.settings({})))
        self.assertTrue(test_suite_catalog.sc.execute('acquire', self.projects[0], request)['idempotent'])
        request.update(run_id='extra')
        self.assertFalse(test_suite_catalog.sc.execute('acquire', self.projects[0], request)['acquired'])
        self.assertTrue(test_suite_catalog.sc.execute('release', self.projects[0], {
            'lease_id': first['lease']['lease_id'],
            'release_token': first['lease']['release_token']})['released'])

    def test_stale_and_stopped_controller_fail_closed(self):
        state = self.publish()
        path = rc.paths(self.projects[0])[0]
        for key, value in (('updated_at', (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()),
                           ('status', 'stopped')):
            rc.scheduling.atomic_json(path, dict(state, **{key: value}))
            request = self.request('api')
            request['resource_controller'] = 'fixture'
            with self.assertRaises(ValueError):
                test_suite_catalog.sc.execute('acquire', self.projects[1], request)

    def test_pending_action_and_republished_old_sample_cannot_admit(self):
        state = self.publish()
        path = rc.paths(self.projects[0])[0]
        rc.scheduling.atomic_json(path, dict(state, pending_action={'action': 'spawn'}))
        with self.assertRaisesRegex(ValueError, 'pending'):
            rc.read_admission(self.projects[0], 'fixture')
        state['measurement']['sampled_at'] = (
            datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        rc.scheduling.atomic_json(path, state)
        with self.assertRaisesRegex(ValueError, 'stale'):
            rc.read_admission(self.projects[0], 'fixture')

    def test_mixed_ready_does_not_spend_shared_capacity_twice(self):
        decision = rc.decide(measurement(70, 70), pool(1), rc.settings({}))
        self.publish(decision)
        request = self.request('ui', conflict='ui')
        request['resource_controller'] = 'fixture'
        request['tasks']['API'] = dict(self.request('api', conflict='api')['tasks']['T1'], test_kind='api')
        self.assertEqual(['T1'], test_suite_catalog.sc.execute('ready', self.projects[0], request)['ready'])

class LegacyCapacityTest(unittest.TestCase):
    setUp = test_suite_catalog.ApiSchedulingTest.setUp
    request = test_suite_catalog.ApiSchedulingTest.request

    def test_cpu_capacity_is_shared_across_ui_and_api(self):
        for first_index, second_index in ((0, 1), (1, 0)):
            first_kind, second_kind = ('ui', 'api')[first_index], ('ui', 'api')[second_index]
            first = self.request(first_kind, conflict=first_kind)
            second = self.request(second_kind, conflict=second_kind)
            for request in (first, second):
                request['resources']['available_cpus'] = 2
            lease = test_suite_catalog.sc.execute('acquire', self.projects[first_index], first)['lease']
            self.assertFalse(test_suite_catalog.sc.execute(
                'acquire', self.projects[second_index], second)['acquired'])
            test_suite_catalog.sc.execute('release', self.projects[first_index], {
                'lease_id': lease['lease_id'], 'release_token': lease['release_token']})

    def test_unreserved_other_kind_consumes_memory_and_shared_cpu(self):
        for running_kind, pending_kind, project in (
                ('ui', 'api', self.projects[1]), ('api', 'ui', self.projects[0])):
            request = self.request(pending_kind, conflict=pending_kind)
            running = self.request(running_kind, conflict=running_kind)['tasks']['T1']
            request['tasks']['running'] = dict(running, status='running', test_kind=running_kind)
            request['resources']['available_memory_bytes'] = test_suite_catalog.sc.RESERVE_BYTES
            result = test_suite_catalog.sc.execute('acquire', project, request)
            self.assertFalse(result['acquired'])
            request['resources'].update(available_memory_bytes=16 * test_suite_catalog.sc.GIB,
                                        available_cpus=2)
            result = test_suite_catalog.sc.execute('acquire', project, request)
            self.assertFalse(result['acquired'])


if __name__ == '__main__':
    unittest.main()
