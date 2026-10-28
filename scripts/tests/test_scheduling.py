"""Synthetic scheduler checks only: no agents, browsers, or business execution."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

MODULE = Path(__file__).resolve().parents[1] / 'scheduling.py'
SPEC = importlib.util.spec_from_file_location('scheduling', MODULE)
sc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sc)


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='scheduling-synthetic-')
        self.project = Path(self.temp.name).resolve()
        self.resources = {'available_cpus': 32, 'available_memory_bytes': 32 * sc.GIB,
                          'available_agent_slots': 32, 'independent_tasks': 32}
        self.probe = patch.object(sc, 'probe_resources', return_value={
            'available_cpus': 32, 'available_memory_bytes': 32 * sc.GIB})
        self.probe.start()
        self.addCleanup(self.probe.stop)
        self.addCleanup(self.temp.cleanup)

    def write(self, relative, value):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf-8')
        return path

    def config(self, concurrency):
        self.write('test/spec.md', '# Synthetic\n```json workflow-config\n' + json.dumps({
            'schema_version': 1, 'concurrency': concurrency}) + '\n```\n')

    def call(self, command, **data):
        return sc.execute(command, self.project, data)

    def budget(self, resources=None, **extra):
        return self.call('budget', resources=self.resources if resources is None else resources, **extra)

    def task(self, task_id='T1', **extra):
        result = {'task_id': task_id, 'status': 'pending', 'phase': 'exploration',
                  'dependencies': [], 'writable_paths': ['test/src/' + task_id + '.spec.ts'],
                  'conflict_keys': [], 'isolation': 'verified', 'isolation_group': task_id}
        result.update(extra)
        return result

    def request(self, tasks=None, **extra):
        result = {'run_id': 'run-1', 'resources': dict(self.resources), 'tasks': tasks or [self.task()],
                  'task_id': 'T1', 'owner': 'worker-1', 'owner_pid': os.getpid()}
        result.update(extra)
        return result

    def acquire(self, **extra):
        return self.call('acquire', **self.request(**extra))

    def release(self, lease):
        return self.call('release', lease_id=lease['lease_id'], release_token=lease['release_token'])

    def ledger(self):
        return sc.read_json(self.project / 'test/.cache/scheduling/quota.json')

    def cli(self, command, data, index='input'):
        path = self.write(index + '.json', data)
        process = subprocess.run([sys.executable, str(MODULE), command, '--project', str(self.project),
                                  '--input', str(path)], capture_output=True, text=True, timeout=20)
        return process.returncode, json.loads(process.stdout)

    def terminal(self, lease, **extra):
        task = {'status': 'complete', 'owner': lease['owner'], 'at': sc.timestamp()}
        task.update(extra)
        self.write('test/.cache/runs/' + lease['run_id'] + '/state.json', {
            'run_id': lease['run_id'], 'tasks': {lease['task']['task_id']: task}})

    def test_default_caps_four(self):
        result = self.budget()
        self.assertEqual({'agents': 4, 'browsers': 4, 'playwright_workers': 4}, result['requested'])
        self.assertEqual(result['requested'], result['effective'])
        self.assertEqual('default', result['configuration_sources']['browser_parallel_cap'])

    def test_mac_probe_only_counts_free_and_reclaimable_pages(self):
        output = ('Mach Virtual Memory Statistics: (page size of 16384 bytes)\n'
                  'Pages free: 100.\nPages active: 900000.\nPages inactive: 200.\n'
                  'Pages speculative: 50.\nPages occupied by compressor: 50000.\n')
        with patch.object(sc.subprocess, 'run', return_value=type('Result', (), {'stdout': output})()) as command:
            self.assertEqual(350 * 16384, sc.mac_available_memory())
        self.assertEqual(['/usr/bin/vm_stat'], command.call_args.args[0])
        self.assertEqual(2, command.call_args.kwargs['timeout'])

    def test_mac_probe_failure_remains_unknown(self):
        for failure in [OSError('unavailable'), sc.subprocess.TimeoutExpired('vm_stat', 2)]:
            with patch.object(sc.subprocess, 'run', side_effect=failure):
                self.assertIsNone(sc.mac_available_memory())
        with patch.object(sc.subprocess, 'run', return_value=type('Result', (), {'stdout': 'unknown format'})()):
            self.assertIsNone(sc.mac_available_memory())

    def test_existing_spec_values_are_preserved(self):
        self.config({'max_parallel_agents': 'auto', 'auto_parallel_cap': 2, 'playwright_workers': 1})
        result = self.budget()
        self.assertEqual({'agents': 2, 'browsers': 4, 'playwright_workers': 1}, result['effective'])
        self.assertEqual('spec', result['configuration_sources']['auto_parallel_cap'])

    def test_explicit_agent_count_can_exceed_soft_cap(self):
        self.config({'max_parallel_agents': 7, 'auto_parallel_cap': 2, 'browser_parallel_cap': 6,
                     'playwright_workers': 5})
        self.assertEqual({'agents': 7, 'browsers': 6, 'playwright_workers': 5}, self.budget()['effective'])

    def test_per_request_override_has_provenance(self):
        self.config({'max_parallel_agents': 2, 'playwright_workers': 1})
        result = self.budget(concurrency={'max_parallel_agents': 3, 'playwright_workers': 2})
        self.assertEqual(3, result['effective']['agents'])
        self.assertEqual(2, result['effective']['playwright_workers'])
        self.assertEqual('input', result['configuration_sources']['max_parallel_agents'])

    def test_cpu_slots_and_independence_tighten(self):
        for fact, value, expected in [('available_cpus', 5, 2), ('available_agent_slots', 0, 0),
                                      ('independent_tasks', 1, 1)]:
            with self.subTest(fact=fact):
                result = self.budget(dict(self.resources, **{fact: value}))
                self.assertEqual(expected, result['effective']['agents'])
        self.assertEqual(0, self.budget(dict(self.resources, available_cpus=0))['effective']['browsers'])

    def test_memory_reserve_and_two_gib_per_browser(self):
        for gib, browsers in [(1, 0), (2, 0), (3, 0), (4, 1), (6, 2), (10, 4)]:
            with self.subTest(gib=gib):
                result = self.budget(dict(self.resources, available_memory_bytes=gib * sc.GIB))
                self.assertEqual(browsers, result['effective']['browsers'])

    def test_unknown_memory_limits_total_to_one(self):
        result = self.budget(dict(self.resources, available_memory_bytes=None))
        self.assertEqual(1, result['effective']['browsers'])
        self.assertEqual('provided', result['resource_sources']['available_memory_bytes'])
        with patch.object(sc, 'probe_resources', return_value={'available_cpus': 32, 'available_memory_bytes': None}):
            result = self.budget({'available_agent_slots': 32, 'independent_tasks': 32})
            self.assertEqual(1, result['effective']['browsers'])
            self.assertEqual('unknown', result['resource_sources']['available_memory_bytes'])

    def test_unknown_cpu_and_no_slots_are_conservative(self):
        result = self.budget(dict(self.resources, available_cpus=None, available_agent_slots=None))
        self.assertEqual(1, result['effective']['agents'])
        self.assertEqual(1, result['effective']['browsers'])

    def test_unavailable_windows_resources_downgrade_budget(self):
        self.probe.stop()
        with patch.object(sc.sys, 'platform', 'win32'), \
                patch.object(sc, 'windows_kernel32', side_effect=OSError('API unavailable')):
            result = self.budget({'available_agent_slots': 32, 'independent_tasks': 32})
        self.assertEqual({'agents': 1, 'browsers': 1, 'playwright_workers': 1}, result['effective'])
        self.assertEqual('unknown', result['resource_sources']['available_cpus'])
        self.assertEqual('unknown', result['resource_sources']['available_memory_bytes'])

    def test_invalid_resource_values_are_rejected(self):
        for field, value in [('available_cpus', float('nan')), ('available_cpus', True),
                              ('available_memory_bytes', -1), ('available_agent_slots', 1.5),
                              ('independent_tasks', False), ('pressure', 'yes')]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.budget(dict(self.resources, **{field: value}))

    def test_unknown_config_override_and_invalid_spec_fail_closed(self):
        with self.assertRaises(ValueError):
            self.budget(concurrency={'browser_cap': 4})
        self.write('test/spec.md', '# No config block')
        with self.assertRaises(ValueError):
            self.budget()

    def test_legacy_task_defaults_without_new_fields(self):
        tasks = {'a': {'phase': 'code', 'status': 'pending', 'source_id': 's1'},
                 'b': {'phase': 'code', 'status': 'pending', 'source_id': 's1'},
                 'c': {'phase': 'code', 'status': 'pending', 'source_id': 's2'}}
        result = self.call('ready', **self.request(tasks=tasks))
        self.assertEqual(['a', 'c'], result['ready'])
        self.assertEqual('isolation_unknown', result['blocked']['b'][0]['kind'])

    def test_dependency_gate_is_local_not_phase_barrier(self):
        tasks = [self.task('review-a', phase='case_review', status='blocked'),
                 self.task('flow-a', dependencies=['review-a']),
                 self.task('review-b', phase='case_review', status='complete'),
                 self.task('flow-b', dependencies=['review-b'])]
        result = self.call('ready', **self.request(tasks=tasks))
        self.assertEqual(['flow-b'], result['ready'])
        self.assertEqual('dependency', result['blocked']['flow-a'][0]['kind'])

    def mixed_tasks(self, **execution):
        return [
            self.task('explore-new', writable_paths=['test/src/new.spec.ts']),
            self.task('stable-regression', phase='execution',
                      writable_paths=['test/.cache/attempts/stable'],
                      readable_paths=['test/src/stable.spec.ts', 'test/src/fixtures', 'playwright.config.ts'],
                      quota={'agents': 1, 'playwright_workers': 3}, **execution)]

    def test_stable_regression_and_exploration_share_four_browser_budget(self):
        tasks = self.mixed_tasks()
        self.assertEqual(['explore-new', 'stable-regression'],
                         self.call('ready', **self.request(tasks=tasks))['ready'])
        exploring = self.acquire(tasks=tasks, task_id='explore-new')
        running = self.acquire(tasks=tasks, task_id='stable-regression', owner='runner')
        self.assertTrue(exploring['acquired'])
        self.assertTrue(running['acquired'])
        self.assertEqual(4, sc.counts(self.ledger()['leases'].values())['browsers'])
        denied = self.acquire(tasks=[self.task('extra')], task_id='extra')
        self.assertFalse(denied['acquired'])
        self.release(running['lease'])
        self.release(exploring['lease'])
        self.assertEqual(0, sc.counts(self.ledger()['leases'].values())['browsers'])

    def test_mixed_pipeline_shared_server_state_still_serializes(self):
        tasks = self.mixed_tasks(conflict_keys=['organization:settings'])
        tasks[0]['conflict_keys'] = ['organization:settings']
        result = self.call('ready', **self.request(tasks=tasks))
        self.assertEqual(['explore-new'], result['ready'])
        self.assertIn('conflict_keys', [r['kind'] for r in result['blocked']['stable-regression']])

    def test_read_write_conflicts_block_both_dispatch_orders(self):
        for input_path in ('test/src/fixtures', 'test/src/fixtures/auth.ts', 'playwright.config.ts'):
            for reverse in (False, True):
                with self.subTest(input_path=input_path, reverse=reverse):
                    writer = self.task('writer', phase='code', writable_paths=[input_path])
                    reader = self.mixed_tasks()[1]
                    tasks = [reader, writer] if reverse else [writer, reader]
                    result = self.call('ready', **self.request(tasks=tasks))
                    self.assertEqual([tasks[0]['task_id']], result['ready'])
                    self.assertIn('read_write_paths', [r['kind'] for r in result['blocked'][tasks[1]['task_id']]])

    def test_readers_can_share_inputs_without_a_global_fixture_lock(self):
        tasks = [self.task(tid, phase='execution', writable_paths=['test/.cache/' + tid],
                           readable_paths=['test/src/fixtures', 'playwright.config.ts'])
                 for tid in ('run-a', 'run-b')]
        self.assertEqual(['run-a', 'run-b'], self.call('ready', **self.request(tasks=tasks))['ready'])

    def test_active_reader_blocks_later_writer_until_teardown_release(self):
        reader = self.mixed_tasks()[1]
        lease = self.acquire(tasks=[reader], task_id='stable-regression')
        self.assertTrue(lease['acquired'])
        writer = self.task('writer', phase='code', writable_paths=['test/src/fixtures/auth.ts'])
        denied = self.acquire(tasks=[writer], task_id='writer')
        self.assertFalse(denied['acquired'])
        self.release(lease['lease'])
        allowed = self.acquire(tasks=[writer], task_id='writer')
        self.assertTrue(allowed['acquired'])
        self.release(allowed['lease'])

    def test_read_paths_reject_escape_globs_and_invalid_types(self):
        for paths in (['../outside'], ['/outside'], ['C:\\outside'], ['test/*.ts'], 'test/src'):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.call('ready', **self.request(tasks=[self.task(readable_paths=paths)]))

    def test_read_path_symlinks_use_same_identity_and_reject_escape(self):
        (self.project / 'actual').mkdir()
        try:
            (self.project / 'alias').symlink_to(self.project / 'actual', target_is_directory=True)
            (self.project / 'outside').symlink_to(self.project.parent, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest('symlink unavailable: ' + str(error))
        tasks = [self.task('reader', writable_paths=[], readable_paths=['alias/input.ts']),
                 self.task('writer', writable_paths=['actual'])]
        result = self.call('ready', **self.request(tasks=tasks))
        self.assertEqual(['reader'], result['ready'])
        self.assertEqual('read_write_paths', result['blocked']['writer'][0]['kind'])
        with self.assertRaises(ValueError):
            self.call('ready', **self.request(tasks=[self.task(readable_paths=['outside/input.ts'])]))

    def test_old_active_lease_without_read_paths_remains_compatible(self):
        lease = self.acquire()['lease']
        ledger = self.ledger()
        old = ledger['leases'][lease['lease_id']]
        del old['task']['readable_paths']
        old['signature'] = sc.hashlib.sha256(json.dumps(
            [old['run_id'], old['task'], old['owner'], old['owner_pid'], old['host']],
            sort_keys=True).encode()).hexdigest()
        self.write('test/.cache/scheduling/quota.json', ledger)
        self.assertTrue(self.acquire()['idempotent'])
        with self.assertRaises(ValueError):
            self.acquire(tasks=[self.task(readable_paths=['playwright.config.ts'])])
        tasks = [self.task('independent', readable_paths=['playwright.config.ts'])]
        self.assertEqual(['independent'], self.call('ready', **self.request(tasks=tasks))['ready'])
        self.release(lease)

    def test_missing_failed_and_cyclic_dependencies_do_not_block_unrelated(self):
        tasks = [self.task('a', dependencies=['b']), self.task('b', dependencies=['a']),
                 self.task('c', dependencies=['missing']), self.task('failed', status='failed'),
                 self.task('d', dependencies=['failed']), self.task('e')]
        result = self.call('ready', **self.request(tasks=tasks))
        self.assertEqual(['e'], result['ready'])
        self.assertEqual('missing', result['blocked']['c'][0]['status'])

    def test_unknown_isolation_only_serializes_same_local_group(self):
        tasks = [self.task('a', isolation='unknown', isolation_group='account-a'),
                 self.task('b', isolation='verified', isolation_group='account-a'),
                 self.task('c', isolation='unknown', isolation_group='account-b'),
                 self.task('d', isolation='unknown', isolation_group='account-c')]
        result = self.call('ready', **self.request(tasks=tasks))
        self.assertEqual(['a', 'c', 'd'], result['ready'])
        self.assertTrue(any(r['kind'] == 'isolation_unknown' for r in result['blocked']['b']))

    def test_verified_same_group_can_run_together(self):
        tasks = [self.task('a', isolation_group='same'), self.task('b', isolation_group='same')]
        self.assertEqual(['a', 'b'], self.call('ready', **self.request(tasks=tasks))['ready'])

    def test_missing_writable_declaration_remains_unknown(self):
        a, b = self.task('a', isolation_group='same'), self.task('b', isolation_group='same')
        del a['writable_paths']
        result = self.call('ready', **self.request(tasks=[a, b]))
        self.assertEqual(['a'], result['ready'])

    def test_explicit_empty_write_set_is_read_only(self):
        tasks = [self.task('a', writable_paths=[], isolation_group='same'),
                 self.task('b', writable_paths=[], isolation_group='same')]
        self.assertEqual(['a', 'b'], self.call('ready', **self.request(tasks=tasks))['ready'])

    def test_conflict_keys_serialize(self):
        tasks = [self.task('a', conflict_keys=['auth:x']), self.task('b', conflict_keys=['auth:x']),
                 self.task('c', conflict_keys=['auth:y'])]
        result = self.call('ready', **self.request(tasks=tasks))
        self.assertEqual(['a', 'c'], result['ready'])
        self.assertEqual('conflict_keys', result['blocked']['b'][0]['kind'])

    def test_parent_child_equal_and_sibling_write_paths(self):
        tasks = [self.task('a', writable_paths=['test/src']),
                 self.task('b', writable_paths=['test/src/a.spec.ts']),
                 self.task('c', writable_paths=['test/src']),
                 self.task('d', writable_paths=['test/src-other'])]
        result = self.call('ready', **self.request(tasks=tasks))
        self.assertEqual(['a', 'd'], result['ready'])
        self.assertEqual('writable_paths', result['blocked']['b'][0]['kind'])

    def test_symlink_write_aliases_conflict_and_escape_rejected(self):
        (self.project / 'actual').mkdir()
        try:
            (self.project / 'alias').symlink_to(self.project / 'actual', target_is_directory=True)
            (self.project / 'outside').symlink_to(self.project.parent, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('directory symlinks unavailable: ' + str(exc))
        tasks = [self.task('a', writable_paths=['actual']), self.task('b', writable_paths=['alias/x'])]
        self.assertEqual(['a'], self.call('ready', **self.request(tasks=tasks))['ready'])
        with self.assertRaises(ValueError):
            self.call('ready', **self.request(tasks=[self.task(writable_paths=['outside/x'])]))

    def test_symlink_permission_failure_is_skipped(self):
        with patch.object(Path, 'symlink_to', side_effect=PermissionError('symlink privilege unavailable')), \
                self.assertRaises(unittest.SkipTest):
            self.test_symlink_write_aliases_conflict_and_escape_rejected()

    def test_windows_drive_rooted_and_unc_paths_are_not_project_relative(self):
        for value in [r'C:\outside', r'C:outside', r'\outside', r'\\server\share\outside',
                      r'\\?\C:\outside', r'\\.\C:\outside']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.call('ready', **self.request(tasks=[self.task(writable_paths=[value])]))

    @unittest.skipUnless(sys.platform == 'win32', 'native Windows path semantics')
    def test_windows_case_and_separator_aliases_conflict(self):
        tasks = [self.task('a', writable_paths=[r'test\Shared']),
                 self.task('b', writable_paths=['TEST/shared/child.spec.ts']),
                 self.task('c', writable_paths=[r'test\Shared-other'])]
        self.assertEqual(['a', 'c'], self.call('ready', **self.request(tasks=tasks))['ready'])

    def test_windows_path_conflicts_with_pure_path_semantics(self):
        left = {'writable_paths': [r'C:\project\test\Shared'], 'conflict_keys': [],
                'isolation_groups': [], 'isolation': 'verified'}
        right = dict(left, writable_paths=['c:/PROJECT/test/shared/child.spec.ts'])
        with patch.object(sc, 'Path', PureWindowsPath):
            self.assertEqual('writable_paths', sc.conflicts(left, right)[0]['kind'])
            self.assertEqual([], sc.conflicts(left, dict(right, writable_paths=[r'C:\project\test\Shared-other'])))

    def test_windows_read_write_aliases_conflict_with_pure_path_semantics(self):
        reader = {'writable_paths': [], 'readable_paths': [r'C:\project\test\Shared'],
                  'conflict_keys': [], 'isolation_groups': [], 'isolation': 'verified'}
        writer = dict(reader, readable_paths=[], writable_paths=['c:/PROJECT/test/shared/helper.ts'])
        with patch.object(sc, 'Path', PureWindowsPath):
            for left, right in ((reader, writer), (writer, reader)):
                self.assertEqual('read_write_paths', sc.conflicts(left, right)[0]['kind'])
            self.assertEqual([], sc.conflicts(reader, dict(writer, writable_paths=[r'C:\project\test\Shared-other'])))

    def test_invalid_tasks_rejected(self):
        bad = [self.task(writable_paths=['../outside']), self.task(writable_paths=['/tmp/outside']),
               self.task(writable_paths=['test/*.ts']), self.task(dependencies='T2'),
               self.task(dependencies=['T1']), self.task(quota={'agents': -1}),
               self.task(quota={'agents': True}), self.task(isolation='maybe')]
        for task in bad:
            with self.subTest(task=task), self.assertRaises(ValueError):
                self.call('ready', **self.request(tasks=[task]))
        with self.assertRaises(ValueError):
            self.call('ready', **self.request(tasks=[self.task(), self.task()]))

    def test_playwright_worker_demand_is_not_limited_to_one_per_task(self):
        task = self.task(phase='execution', quota={'agents': 1, 'playwright_workers': 4})
        result = self.acquire(tasks=[task])
        self.assertTrue(result['acquired'])
        self.assertEqual(4, result['budget']['quota']['active']['playwright_workers'])

    def test_exploration_and_playwright_share_one_project_cap(self):
        execution = self.task(phase='execution', quota={'agents': 1, 'playwright_workers': 3})
        first = self.acquire(tasks=[execution])
        second = self.acquire(run_id='run-2', tasks=[self.task('T2')], task_id='T2')
        denied = self.acquire(run_id='run-3', tasks=[self.task('T3')], task_id='T3')
        self.assertTrue(first['acquired'] and second['acquired'])
        self.assertFalse(denied['acquired'])
        self.assertEqual(4, denied['budget']['quota']['active']['browsers'])
        self.assertEqual(3, denied['budget']['quota']['active']['playwright_workers'])
        self.assertEqual(1, denied['budget']['quota']['active']['exploration_browsers'])

    def test_acquire_rechecks_conflicts_after_ready(self):
        tasks = [self.task(writable_paths=['test/shared'])]
        request = self.request(tasks=tasks)
        self.assertEqual(['T1'], self.call('ready', **request)['ready'])
        self.acquire(run_id='other-run', tasks=[self.task('other', writable_paths=['test/shared/x'])], task_id='other')
        result = self.call('acquire', **request)
        self.assertFalse(result['acquired'])
        self.assertTrue(any(r['kind'] == 'writable_paths' for r in result['reasons']))

    def test_acquire_rechecks_dependencies(self):
        tasks = [self.task('dep', status='complete'), self.task(dependencies=['dep'])]
        request = self.request(tasks=tasks)
        self.assertEqual(['T1'], self.call('ready', **request)['ready'])
        tasks[0]['status'] = 'failed'
        self.assertFalse(self.call('acquire', **request)['acquired'])

    def test_active_dependency_not_done_until_release(self):
        self.acquire(task_id='dep', tasks=[self.task('dep')])
        tasks = [self.task('dep', status='complete'), self.task(dependencies=['dep'])]
        result = self.call('ready', **self.request(tasks=tasks))
        self.assertTrue(any(r['kind'] == 'dependency_still_reserved' for r in result['blocked']['T1']))

    def test_active_lease_cannot_be_hidden_by_task_status(self):
        self.acquire()
        request = self.request(tasks=[self.task(status='complete'), self.task('next', writable_paths=['test/src'])], task_id='next')
        self.assertFalse(self.call('acquire', **request)['acquired'])

    def test_acquire_is_idempotent_but_rejects_different_owner(self):
        first = self.acquire()
        again = self.acquire()
        self.assertTrue(again['idempotent'])
        self.assertEqual(first['lease']['lease_id'], again['lease']['lease_id'])
        self.assertEqual(1, again['budget']['quota']['active']['agents'])
        with self.assertRaises(ValueError):
            self.acquire(owner='someone-else')

    def test_release_requires_token_is_idempotent_and_retains_peak(self):
        lease = self.acquire()['lease']
        with self.assertRaises(ValueError):
            self.call('release', lease_id=lease['lease_id'], release_token='wrong')
        result = self.release(lease)
        self.assertTrue(result['released'])
        self.assertEqual(0, result['quota']['active']['browsers'])
        self.assertEqual(1, result['quota']['peak']['browsers'])
        self.assertTrue(self.release(lease)['idempotent'])

    def test_low_available_memory_stops_new_without_reclaiming_active(self):
        lease = self.acquire()['lease']
        resources = dict(self.resources, available_memory_bytes=2 * sc.GIB)
        result = self.acquire(run_id='second', task_id='T2', tasks=[self.task('T2')], resources=resources)
        self.assertFalse(result['acquired'])
        self.assertEqual(1, result['budget']['quota']['active']['browsers'])
        self.assertEqual(0, result['budget']['available']['browsers'])
        self.assertEqual('active', self.ledger()['leases'][lease['lease_id']]['status'])
        self.assertFalse(result['budget']['existing_leases_revoked'])

    def test_repeated_free_memory_snapshot_cannot_mint_new_capacity(self):
        self.acquire()
        result = self.acquire(run_id='second', task_id='T2', tasks=[self.task('T2')],
                              resources=dict(self.resources, available_memory_bytes=4 * sc.GIB))
        self.assertFalse(result['acquired'])
        self.assertEqual(1, result['budget']['quota']['active']['browsers'])

    def test_unknown_memory_limit_is_global_not_one_new_per_command(self):
        resources = dict(self.resources, available_memory_bytes=None)
        self.assertTrue(self.acquire(resources=resources)['acquired'])
        result = self.acquire(run_id='second', task_id='T2', tasks=[self.task('T2')], resources=resources)
        self.assertFalse(result['acquired'])

    def test_pressure_pauses_admission_without_revoking_existing(self):
        lease = self.acquire()['lease']
        request = self.request(run_id='second', task_id='T2', tasks=[self.task('T2')],
                               resources=dict(self.resources, pressure=True))
        result = self.call('acquire', **request)
        self.assertFalse(result['acquired'])
        self.assertTrue(result['budget']['admission_paused'])
        self.assertEqual(1, result['budget']['quota']['active']['agents'])
        self.assertEqual('active', self.ledger()['leases'][lease['lease_id']]['status'])
        self.assertTrue(self.release(lease)['released'])

    def test_slots_are_current_free_not_total_double_subtracted(self):
        self.acquire()
        result = self.acquire(run_id='second', task_id='T2', tasks=[self.task('T2')],
                              resources=dict(self.resources, available_agent_slots=1))
        self.assertTrue(result['acquired'])
        self.assertEqual(2, result['budget']['effective']['agents'])

    def test_running_task_without_reservation_is_counted_without_global_barrier(self):
        request = self.request(tasks=[self.task('old', status='running'), self.task()])
        self.assertEqual(['T1'], self.call('ready', **request)['ready'])
        result = self.call('acquire', **request)
        self.assertTrue(result['acquired'])
        self.assertIn('old', result['budget']['unreserved_running_tasks'])
        self.assertEqual(1, result['budget']['quota']['active']['agents'])
        self.assertEqual(1, result['budget']['unreserved_running_quota_estimate']['agents'])

    def test_unreserved_running_quota_tightens_capacity(self):
        running = self.task('old', status='running', quota={'agents': 4, 'exploration_browsers': 4})
        request = self.request(tasks=[running, self.task()])
        self.assertEqual([], self.call('ready', **request)['ready'])
        self.assertFalse(self.call('acquire', **request)['acquired'])

    def test_independent_budget_excludes_conflicting_pending_tasks(self):
        tasks = [self.task('a', conflict_keys=['shared']), self.task('b', conflict_keys=['shared'])]
        result = self.call('ready', **self.request(tasks=tasks))
        self.assertEqual(1, result['budget']['effective']['agents'])
        self.assertEqual(1, result['budget']['effective']['browsers'])

    def test_acquire_can_choose_larger_conflicting_pending_alternative(self):
        tasks = [self.task('small', conflict_keys=['shared']),
                 self.task(conflict_keys=['shared'], quota={'agents': 1, 'playwright_workers': 4})]
        self.assertTrue(self.acquire(tasks=tasks)['acquired'])

    def test_project_playwright_subcap_applies_across_commands(self):
        self.config({'playwright_workers': 1})
        self.assertTrue(self.acquire(tasks=[self.task(phase='execution')])['acquired'])
        result = self.acquire(run_id='other', task_id='T2', tasks=[self.task('T2', phase='execution')])
        self.assertFalse(result['acquired'])
        self.assertTrue(any(r.get('resource') == 'playwright_workers' for r in result['reasons']))
        self.assertTrue(self.acquire(run_id='explore', task_id='T3', tasks=[self.task('T3')])['acquired'])

    def test_ttl_and_age_never_automatically_reclaim_live_pid(self):
        lease = self.acquire()['lease']
        ledger = self.ledger()
        record = ledger['leases'][lease['lease_id']]
        record.update(created_at='2000-01-01T00:00:00+00:00', expires_at='2000-01-01T00:01:00+00:00')
        sc.atomic_json(self.project / 'test/.cache/scheduling/quota.json', ledger)
        result = self.call('reap', lease_id=lease['lease_id'])
        self.assertFalse(result['reaped'])
        self.assertEqual('owner_pid_alive', result['reason'])
        self.assertEqual(1, self.budget()['quota']['active']['agents'])

    def test_reap_verifies_real_same_host_dead_pid(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait(timeout=10)
        lease = self.acquire(owner_pid=child.pid)['lease']
        result = self.call('reap', lease_id=lease['lease_id'])
        self.assertTrue(result['reaped'])
        self.assertEqual('same_host_owner_pid_dead', result['reason'])
        self.assertTrue(self.call('reap', lease_id=lease['lease_id'])['idempotent'])

    def test_reap_refuses_remote_unknown_without_probing_local_pid(self):
        lease = self.acquire(host='remote-synthetic-host')['lease']
        with patch.object(sc, 'pid_state', side_effect=AssertionError('must not probe remote PID locally')):
            result = self.call('reap', lease_id=lease['lease_id'], trusted_terminal=True, expired=True)
        self.assertFalse(result['reaped'])
        self.assertEqual('remote_owner_liveness_unknown', result['reason'])

    def test_reap_accepts_verified_canonical_task_terminal_for_remote_owner(self):
        lease = self.acquire(host='remote-synthetic-host')['lease']
        self.terminal(lease)
        result = self.call('reap', lease_id=lease['lease_id'])
        self.assertTrue(result['reaped'])
        self.assertEqual('trusted_task_terminal', result['reason'])
        self.assertIn('sha256', self.ledger()['leases'][lease['lease_id']]['terminal_evidence'])

    def test_reap_refuses_stale_future_or_wrong_owner_terminal(self):
        lease = self.acquire(host='remote-synthetic-host')['lease']
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for extra in [{'at': '2000-01-01T00:00:00+00:00'}, {'owner': 'other'},
                      {'at': future}, {'at': 'bad'}, {'status': 'running'}, {'at': '2026-01-01'}]:
            with self.subTest(extra=extra):
                self.terminal(lease, **extra)
                self.assertFalse(self.call('reap', lease_id=lease['lease_id'])['reaped'])

    def test_run_terminal_does_not_substitute_for_task_terminal(self):
        lease = self.acquire(host='remote-synthetic-host')['lease']
        self.write('test/.cache/runs/run-1/state.json', {'run_id': 'run-1', 'acceptance': 'fully_accepted', 'tasks': {}})
        self.assertFalse(self.call('reap', lease_id=lease['lease_id'])['reaped'])

    def test_permission_error_is_unknown_not_dead(self):
        lease = self.acquire()['lease']
        with patch.object(sc.os, 'kill', side_effect=PermissionError()), \
                patch.object(sc, 'windows_kernel32', side_effect=OSError('access denied')):
            self.assertEqual('unknown', sc.pid_state(os.getpid()))
            self.assertFalse(self.call('reap', lease_id=lease['lease_id'])['reaped'])

    def test_windows_unknown_pid_does_not_reap_reservation(self):
        lease = self.acquire()['lease']
        with patch.object(sc.sys, 'platform', 'win32'), \
                patch.object(sc, 'windows_kernel32', side_effect=OSError('access denied')), \
                patch.object(sc.os, 'kill', side_effect=AssertionError('unsafe on Windows')):
            result = self.call('reap', lease_id=lease['lease_id'])
        self.assertFalse(result['reaped'])
        self.assertEqual('owner_pid_unknown', result['reason'])
        self.assertEqual('active', self.ledger()['leases'][lease['lease_id']]['status'])

    def test_atomic_json_uses_shared_directory_sync(self):
        path = self.project / 'atomic.json'
        with patch.object(sc.platform_support, 'sync_directory') as sync:
            sc.atomic_json(path, {'written': True})
        sync.assert_called_once_with(self.project)
        self.assertEqual({'written': True}, sc.read_json(path))

    def test_ledger_uses_shared_file_descriptor_lock(self):
        with patch.object(sc.platform_support, 'file_lock', wraps=sc.platform_support.file_lock) as lock:
            self.budget()
        lock.assert_called_once()
        self.assertIs(type(lock.call_args.args[0]), int)

    def test_ledger_initialization_does_not_resolve_missing_directories(self):
        resolve = Path.resolve
        def resolve_existing(path, strict=False):
            if not path.exists():
                raise AssertionError('missing directories can change during Windows resolution')
            return resolve(path, strict=strict)
        with patch.object(Path, 'resolve', resolve_existing):
            self.assertEqual(4, self.budget()['available']['browsers'])
        self.assertTrue((self.project / 'test/.cache/scheduling/quota.lock').is_file())

    def test_ledger_rejects_escaping_directory_before_creating_children(self):
        with tempfile.TemporaryDirectory(prefix='scheduling-outside-') as temporary:
            outside = Path(temporary).resolve()
            for component in ('test', 'test/.cache', 'test/.cache/scheduling'):
                with self.subTest(component=component):
                    link = self.project / component
                    link.parent.mkdir(parents=True, exist_ok=True)
                    junction = os.name == 'nt'
                    if junction:
                        result = subprocess.run(
                            ['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                            capture_output=True, timeout=10)
                        self.assertEqual(0, result.returncode, result.stderr)
                    else:
                        link.symlink_to(outside, target_is_directory=True)
                    try:
                        with self.assertRaisesRegex(ValueError, 'scheduling directory escapes project'):
                            self.budget()
                        self.assertEqual([], list(outside.iterdir()))
                    finally:
                        link.rmdir() if junction else link.unlink()

    @unittest.skipUnless(os.name == 'nt', 'requires Windows final-path resolution')
    def test_ledger_initialization_survives_parent_creation_during_resolution(self):
        import ntpath
        directory = self.project / 'test/.cache/scheduling'
        getfinalpathname = ntpath._getfinalpathname
        def create_parent_after_missing(path):
            try:
                return getfinalpathname(path)
            except FileNotFoundError:
                if Path(path) == directory:
                    directory.parent.mkdir(parents=True, exist_ok=True)
                raise
        with patch.object(ntpath, '_getfinalpathname', create_parent_after_missing):
            self.assertEqual(4, self.budget()['available']['browsers'])
        self.assertTrue((directory / 'quota.lock').is_file())

    def test_counts_are_reservations_and_memory_is_explicitly_estimated(self):
        result = self.acquire()['budget']['quota']
        self.assertEqual('observed_reservations_not_running_processes', result['count_basis'])
        self.assertIsNone(result['measured_peak_memory_bytes'])
        self.assertEqual(2 * sc.GIB, result['estimated_active_browser_memory_bytes'])
        self.assertEqual('estimate_not_measurement', self.budget()['planning_estimate']['basis'])

    def test_cli_contract_and_json_error(self):
        code, result = self.cli('budget', {'resources': self.resources})
        self.assertEqual(0, code)
        self.assertEqual(4, result['requested']['browsers'])
        code, result = self.cli('ready', self.request())
        self.assertEqual(0, code)
        self.assertEqual(['T1'], result['ready'])
        code, result = self.cli('acquire', self.request())
        self.assertEqual(0, code)
        lease = result['lease']
        code, result = self.cli('release', {'lease_id': lease['lease_id'], 'release_token': lease['release_token']})
        self.assertEqual(0, code)
        self.assertTrue(result['released'])
        code, result = self.cli('acquire', self.request(owner_pid=False))
        self.assertEqual(2, code)
        self.assertIn('error', result)

    def test_real_parallel_cli_processes_share_atomic_cross_run_quota(self):
        inputs = []
        for i in range(12):
            task = self.task('T' + str(i), phase='execution' if i % 2 else 'exploration')
            path = self.write('parallel-' + str(i) + '.json', self.request(
                tasks=[task], task_id=task['task_id'], run_id='run-' + str(i)))
            inputs.append(path)

        def invoke(path):
            process = subprocess.run([sys.executable, str(MODULE), 'acquire', '--project', str(self.project),
                                      '--input', str(path)], capture_output=True, text=True, timeout=30)
            return process.returncode, json.loads(process.stdout)

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(invoke, inputs))
        self.assertTrue(all(code == 0 for code, _ in results), results)
        acquired = [value for _, value in results if value['acquired']]
        self.assertEqual(4, len(acquired), results)
        ledger = self.ledger()
        self.assertEqual(4, sc.counts(ledger['leases'].values())['browsers'])
        self.assertEqual(4, ledger['peak']['browsers'])
        for value in acquired:
            self.assertTrue(self.release(value['lease'])['released'])
        self.assertEqual(0, self.budget()['quota']['active']['browsers'])
        self.assertEqual(4, self.budget()['quota']['peak']['browsers'])

    def test_corrupt_ledger_is_not_silently_reset(self):
        self.write('test/.cache/scheduling/quota.json', '{broken')
        with self.assertRaises(ValueError):
            self.budget()


class ResourceProbeTest(unittest.TestCase):
    def test_cgroup_and_host_memory_limits_are_minimized(self):
        data = {Path('/proc/meminfo'): 'MemAvailable:       16777216 kB\n',
                Path('/synthetic/cpu.max'): '300000 100000',
                Path('/synthetic/memory.max'): str(8 * sc.GIB),
                Path('/synthetic/memory.current'): str(2 * sc.GIB)}

        def read_text(path, *args, **kwargs):
            if path in data:
                return data[path]
            raise FileNotFoundError(str(path))

        with patch.object(sc.sys, 'platform', 'linux'), \
                patch.object(sc, 'cgroup_directories', return_value=[Path('/synthetic')]), \
                patch.object(Path, 'read_text', read_text), \
                patch.object(sc.os, 'cpu_count', return_value=16), \
                patch.object(sc.os, 'process_cpu_count', return_value=16, create=True), \
                patch.object(sc.os, 'sched_getaffinity', return_value=set(range(8)), create=True):
            result = sc.probe_resources()
        self.assertEqual(3, result['available_cpus'])
        self.assertEqual(6 * sc.GIB, result['available_memory_bytes'])

    def test_missing_memory_is_not_invented_on_macos_like_platform(self):
        with patch.object(sc.sys, 'platform', 'darwin'), \
                patch.object(Path, 'read_text', side_effect=FileNotFoundError()), \
                patch.object(sc.os, 'sysconf', side_effect=ValueError(), create=True), \
                patch.object(sc, 'cgroup_directories', return_value=[]), \
                patch.object(sc.subprocess, 'run', side_effect=OSError('vm_stat unavailable')):
            self.assertIsNone(sc.probe_resources()['available_memory_bytes'])

    def test_missing_sysconf_remains_unknown(self):
        with patch.dict(sc.os.__dict__), patch.object(sc.sys, 'platform', 'linux'), \
                patch.object(Path, 'read_text', side_effect=FileNotFoundError()), \
                patch.object(sc, 'cgroup_directories', return_value=[]):
            sc.os.__dict__.pop('sysconf', None)
            self.assertIsNone(sc.probe_resources()['available_memory_bytes'])

    def resource_kernel(self, available=6 * sc.GIB, mask=0b1011, system_mask=0b1111):
        kernel = Mock(spec=['GetCurrentProcess', 'GetProcessAffinityMask', 'GlobalMemoryStatusEx'])
        kernel.GetCurrentProcess.return_value = -1

        def affinity(handle, process, system):
            self.assertEqual(-1, handle)
            sc.ctypes.cast(process, sc.ctypes.POINTER(sc.ctypes.c_size_t))[0] = mask
            sc.ctypes.cast(system, sc.ctypes.POINTER(sc.ctypes.c_size_t))[0] = system_mask
            return 1

        def memory(pointer):
            status = sc.ctypes.cast(pointer, sc.ctypes.POINTER(sc.MemoryStatusEx)).contents
            self.assertEqual(64, status.dwLength)
            status.ullTotalPhys = 128 * sc.GIB
            status.ullAvailPhys = available
            return 1

        kernel.GetProcessAffinityMask.side_effect = affinity
        kernel.GlobalMemoryStatusEx.side_effect = memory
        return kernel

    def test_windows_reads_available_memory_and_process_affinity_without_posix_probes(self):
        kernel = self.resource_kernel()
        with patch.object(sc.sys, 'platform', 'win32'), \
                patch.object(sc, 'windows_kernel32', return_value=kernel), \
                patch.object(Path, 'read_text', side_effect=AssertionError('no /proc on Windows')), \
                patch.object(sc.os, 'sysconf', side_effect=AssertionError('no sysconf on Windows'), create=True), \
                patch.object(sc, 'cgroup_directories', side_effect=AssertionError('no cgroups on Windows')):
            result = sc.probe_resources()
        self.assertEqual({'available_cpus': 3, 'available_memory_bytes': 6 * sc.GIB}, result)
        self.assertIs(kernel.GetCurrentProcess.restype, sc.ctypes.c_void_p)

    def test_windows_affinity_is_intersected_with_system_mask(self):
        with patch.object(sc, 'windows_kernel32', return_value=self.resource_kernel(mask=0b1111, system_mask=0b0101)):
            self.assertEqual(2, sc.windows_resources()['available_cpus'])

    def test_windows_resource_api_failure_is_unknown_not_host_total(self):
        for failure in [OSError('unavailable'), AttributeError('WinDLL unavailable')]:
            with self.subTest(failure=failure), \
                    patch.object(sc, 'windows_kernel32', side_effect=failure):
                self.assertEqual({'available_cpus': None, 'available_memory_bytes': None}, sc.windows_resources())
        kernel = self.resource_kernel()
        kernel.GetProcessAffinityMask.side_effect = None
        kernel.GetProcessAffinityMask.return_value = 0
        kernel.GlobalMemoryStatusEx.side_effect = None
        kernel.GlobalMemoryStatusEx.return_value = 0
        with patch.object(sc, 'windows_kernel32', return_value=kernel):
            self.assertEqual({'available_cpus': None, 'available_memory_bytes': None}, sc.windows_resources())

    def test_windows_zero_available_memory_is_not_unknown(self):
        with patch.object(sc, 'windows_kernel32', return_value=self.resource_kernel(available=0)):
            self.assertEqual(0, sc.windows_resources()['available_memory_bytes'])

    def test_windows_zero_affinity_and_partial_probe_failure(self):
        with patch.object(sc, 'windows_kernel32', return_value=self.resource_kernel(mask=0)):
            self.assertEqual({'available_cpus': None, 'available_memory_bytes': 6 * sc.GIB}, sc.windows_resources())
        kernel = self.resource_kernel()
        kernel.GlobalMemoryStatusEx.side_effect = OSError('memory unavailable')
        with patch.object(sc, 'windows_kernel32', return_value=kernel):
            self.assertEqual({'available_cpus': 3, 'available_memory_bytes': None}, sc.windows_resources())
        kernel = self.resource_kernel()
        kernel.GetProcessAffinityMask.side_effect = OSError('affinity unavailable')
        with patch.object(sc, 'windows_kernel32', return_value=kernel):
            self.assertEqual({'available_cpus': None, 'available_memory_bytes': 6 * sc.GIB}, sc.windows_resources())

    @unittest.skipUnless(sys.platform == 'win32', 'native Win32 resource APIs')
    def test_windows_native_resource_probe(self):
        result = sc.probe_resources()
        # Multi-group processes may have no single affinity mask; stay conservative.
        cpus = result['available_cpus']
        self.assertTrue(cpus is None or type(cpus) is int and cpus > 0)
        self.assertIs(type(result['available_memory_bytes']), int)
        self.assertGreaterEqual(result['available_memory_bytes'], 0)


class WindowsPidTest(unittest.TestCase):
    def kernel(self, wait=sc.WAIT_TIMEOUT, handle=0x1234567887654321):
        kernel = Mock(spec=['OpenProcess', 'WaitForSingleObject', 'CloseHandle'])
        kernel.OpenProcess.return_value = handle
        kernel.WaitForSingleObject.return_value = wait
        kernel.CloseHandle.return_value = 1
        return kernel

    def test_windows_zero_time_wait_is_read_only_and_closes_handle(self):
        for wait, expected in [(sc.WAIT_TIMEOUT, 'alive'), (sc.WAIT_OBJECT_0, 'dead'),
                               (0xFFFFFFFF, 'unknown'), (0x80, 'unknown')]:
            kernel = self.kernel(wait)
            with self.subTest(wait=wait), patch.object(sc.sys, 'platform', 'win32'), \
                    patch.object(sc, 'windows_kernel32', return_value=kernel), \
                    patch.object(sc.os, 'kill', side_effect=AssertionError('unsafe on Windows')):
                self.assertEqual(expected, sc.pid_state(1234))
            kernel.OpenProcess.assert_called_once_with(sc.SYNCHRONIZE, False, 1234)
            kernel.WaitForSingleObject.assert_called_once_with(kernel.OpenProcess.return_value, 0)
            kernel.CloseHandle.assert_called_once_with(kernel.OpenProcess.return_value)
            self.assertIs(kernel.OpenProcess.restype, sc.ctypes.c_void_p)

    def test_windows_open_failure_only_proves_absence_for_invalid_parameter(self):
        for error, expected in [(sc.ERROR_INVALID_PARAMETER, 'dead'), (5, 'unknown'), (0, 'unknown'), (8, 'unknown')]:
            kernel = self.kernel(handle=None)
            with self.subTest(error=error), patch.object(sc, 'windows_kernel32', return_value=kernel), \
                    patch.object(sc.ctypes, 'get_last_error', return_value=error, create=True):
                self.assertEqual(expected, sc.windows_pid_state(1234))
            kernel.WaitForSingleObject.assert_not_called()
            kernel.CloseHandle.assert_not_called()

    def test_windows_api_unavailable_never_falls_back_to_kill(self):
        for failure in [OSError('unavailable'), AttributeError('WinDLL unavailable')]:
            with self.subTest(failure=failure), patch.object(sc.sys, 'platform', 'win32'), \
                    patch.object(sc, 'windows_kernel32', side_effect=failure), \
                    patch.object(sc.os, 'kill', side_effect=AssertionError('unsafe on Windows')):
                self.assertEqual('unknown', sc.pid_state(1234))

    def test_windows_wait_exception_still_closes_handle(self):
        kernel = self.kernel()
        kernel.WaitForSingleObject.side_effect = OSError('query failed')
        with patch.object(sc, 'windows_kernel32', return_value=kernel):
            self.assertEqual('unknown', sc.windows_pid_state(1234))
        kernel.CloseHandle.assert_called_once_with(kernel.OpenProcess.return_value)

    def test_windows_invalid_pid_never_wraps_to_another_process(self):
        with patch.object(sc, 'windows_kernel32', side_effect=AssertionError('invalid PID must not reach API')):
            for pid in [0, -1, True, None, '1234', 1.5, 0x100000000]:
                with self.subTest(pid=pid):
                    self.assertEqual('unknown', sc.windows_pid_state(pid))

    @unittest.skipUnless(sys.platform == 'win32', 'native Win32 process wait')
    def test_windows_native_alive_and_exited_process_without_kill(self):
        with patch.object(sc.os, 'kill', side_effect=AssertionError('unsafe on Windows')):
            self.assertEqual('alive', sc.pid_state(os.getpid()))
            child = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(259)'])
            child.wait(timeout=10)
            # Exit code 259 is STILL_ACTIVE for GetExitCodeProcess, not for a wait.
            self.assertEqual('dead', sc.pid_state(child.pid))


if __name__ == '__main__':
    unittest.main()
