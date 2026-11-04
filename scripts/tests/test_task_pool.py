"""Real process locks, dynamic admission and shared leaf-I/O budgets."""
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'task_pool.py'
SPEC = importlib.util.spec_from_file_location('task_pool', SCRIPT)
pool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pool)


def measurement(cpu=10, memory=20):
    return dict(cpu_percent=cpu, memory_percent=memory, source='offline_synthetic')


class PolicyTest(unittest.TestCase):
    def test_start_uses_reserved_capacity_and_thresholds_have_hysteresis(self):
        self.assertEqual((4, False), pool.policy(1, 4, measurement(65, 65), True)[:2])
        self.assertEqual((2, False), pool.policy(2, 4, measurement(60, 79))[:2])
        self.assertEqual((3, False), pool.policy(2, 4, measurement(59, 59))[:2])
        self.assertEqual((1, True), pool.policy(2, 4, measurement(80, 20))[:2])

    def test_unknown_and_invalid_samples_never_expand(self):
        for value in (None, True, float('nan'), float('inf'), -1, 101):
            self.assertEqual((2, True), pool.policy(2, 4, measurement(value))[:2])
        self.assertEqual((1, True), pool.policy(2, 4, measurement(None, 90))[:2])

    def test_configuration_rejects_unbounded_and_ambiguous_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            for fields in (dict(maximum_tasks=0), dict(maximum_io=1),
                           dict(progress_slots=True), dict(stage='../other'),
                           dict(stale_seconds=60), dict(low_percent=80)):
                arguments = dict(project=directory, run='run', stage='cli',
                                 maximum_tasks=4, maximum_io=8)
                with self.assertRaises(ValueError):
                    pool.configuration(**dict(arguments, **fields))


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.measurement = measurement()
        config = pool.configuration(self.root, 'run-1', 'cli-01', 3, 4,
                                    interval_seconds=3600, stale_seconds=7201)
        self.monitor = self.stack.enter_context(pool.Monitor(
            self.root / 'pool', config, sampler=lambda _: self.measurement))
        self.pool = self.monitor.pool
        self.processes = []

    def tearDown(self):
        for process, _ in self.processes:
            if process.poll() is None:
                process.stdin.close()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if not process.stdin.closed:
                process.stdin.close()
            process.stdout.close()
            process.stderr.close()
        self.stack.close()

    def child(self, cleanup=False):
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), 'hold', '--directory', str(self.monitor.directory),
             '--project', str(self.root), '--run', 'run-1', '--stage', 'cli-01',
             '--wait-seconds', '5', *(['--cleanup'] if cleanup else [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        events = queue.Queue()

        def read():
            for line in process.stdout:
                events.put(json.loads(line))
            events.put(None)

        threading.Thread(target=read, daemon=True).start()
        self.processes.append((process, events))
        return process, events

    def event(self, child, event, identifier=None):
        result = child[1].get(timeout=5)
        self.assertIsNotNone(result, child[0].stderr.read() if child[0].poll() is not None else '')
        self.assertEqual(event, result['event'])
        if identifier is not None:
            self.assertEqual(identifier, result['id'])

    def quiet(self, child):
        with self.assertRaises(queue.Empty):
            child[1].get(timeout=.15)

    def send(self, child, identifier, action='acquire', progress=False):
        child[0].stdin.write(json.dumps(dict(id=identifier, action=action, progress=progress)) + '\n')
        child[0].stdin.flush()

    def test_task_slots_cover_entire_lifetime_and_waiter_exits_on_eof(self):
        children = [self.child() for _ in range(4)]
        admitted = []
        for child in children:
            try:
                event = child[1].get(timeout=.4)
                if event:
                    admitted.append(child)
            except queue.Empty:
                pass
        self.assertEqual(3, len(admitted))
        waiter = next(child for child in children if child not in admitted)
        self.quiet(waiter)
        admitted[0][0].stdin.close()
        admitted[0][0].wait(timeout=3)
        self.event(waiter, 'admitted')
        extra = self.child()
        self.quiet(extra)
        extra[0].stdin.close()
        self.assertEqual(0, extra[0].wait(timeout=3))

    def test_io_budget_is_shared_and_progress_survives_saturation(self):
        first, second = self.child(), self.child()
        self.event(first, 'admitted')
        self.event(second, 'admitted')
        for identifier in ('1', '2', '3'):
            self.send(first, identifier)
            self.event(first, 'acquired', identifier)
        self.send(second, '4')
        self.quiet(second)
        self.send(second, 'reply', progress=True)
        self.event(second, 'acquired', 'reply')
        self.send(first, '1', 'release')
        self.event(first, 'released', '1')
        self.event(second, 'acquired', '4')

    def test_shrink_drains_high_slots_and_does_not_replace_or_kill(self):
        slots = [self.pool.try_claim('task') for _ in range(3)]
        try:
            self.measurement = measurement(90)
            self.monitor.refresh()
            self.assertEqual(2, self.pool.state()['task_capacity'])
            self.assertIsNone(self.pool.try_claim('task'))
            slots[0].close()
            self.assertIsNone(self.pool.try_claim('task', cleanup=True))
            self.measurement = measurement()
            self.monitor.refresh()
            self.assertEqual('waiting_for_drain', self.pool.state()['reason'])
            slots[2].close()
            self.monitor.refresh()
            self.assertEqual(3, self.pool.state()['task_capacity'])
            with self.pool.try_claim('task'):
                pass
        finally:
            for slot in slots:
                slot.close()

    def test_unknown_allows_inflight_io_but_quarantine_only_allows_explicit_progress(self):
        self.measurement = measurement(None)
        self.monitor.refresh()
        cleanup = self.pool.try_claim('task', cleanup=True)
        ordinary, progress = self.pool.try_claim('io'), self.pool.try_claim('progress')
        try:
            self.assertIsNotNone(cleanup)
            self.assertIsNotNone(ordinary)
            self.assertIsNotNone(progress)
            self.assertIsNone(self.pool.try_claim('task', cleanup=True))
            self.assertIsNone(self.pool.try_claim('io'))
            self.assertIsNone(self.pool.try_claim('progress'))
            (self.monitor.directory / 'dispatch-blocked.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'dispatch_blocked'):
                self.pool.try_claim('task')
            with self.assertRaisesRegex(ValueError, 'dispatch_blocked'):
                self.pool.try_claim('io')
            ordinary.close()
            progress.close()
            with self.pool.try_claim('progress'):
                pass
        finally:
            for slot in (cleanup, ordinary, progress):
                slot.close()

    def test_ordinary_io_drain_does_not_starve_progress(self):
        slots = [self.pool.try_claim('io') for _ in range(3)]
        try:
            self.measurement = measurement(90)
            self.monitor.refresh()
            slots[0].close()
            self.assertIsNone(self.pool.try_claim('io'))
            with self.pool.try_claim('progress'):
                pass
            self.measurement = measurement()
            self.monitor.refresh()
            self.assertEqual('waiting_for_drain', self.pool.state()['reason'])
        finally:
            for slot in slots:
                slot.close()

    def test_stale_stopped_and_mismatched_contexts_fail_closed(self):
        path = self.monitor.directory / 'state.json'
        original = self.pool.state()
        for update in (dict(stopped=True), dict(sampled_at=0),
                       dict(sampled_at=time.time() + 100), dict(binding='foreign'),
                       dict(task_capacity=True)):
            pool.save(path, dict(original, **update))
            for kind in ('task', 'io', 'progress'):
                with self.assertRaises(ValueError):
                    self.pool.try_claim(kind, cleanup=True)
        pool.save(path, original)
        with self.assertRaisesRegex(ValueError, 'context_mismatch'):
            pool.Pool(self.monitor.directory, self.root, 'other-run', 'cli-01')

    def test_crashed_helper_unlocks_but_quarantines_unknown_io_and_progress(self):
        child = self.child()
        self.event(child, 'admitted')
        for identifier in ('1', '2', '3'):
            self.send(child, identifier)
            self.event(child, 'acquired', identifier)
        self.send(child, 'reply', progress=True)
        self.event(child, 'acquired', 'reply')
        child[0].kill()
        child[0].wait(timeout=3)
        self.assertEqual(0, self.pool.occupied('task', 0, 3))
        self.assertEqual(0, self.pool.occupied('io', 0, 3))
        self.assertEqual(0, self.pool.occupied('progress', 0, 1))
        with self.assertRaisesRegex(ValueError, 'dispatch_blocked'):
            self.pool.try_claim('io')
        with self.assertRaisesRegex(ValueError, 'dispatch_blocked'):
            self.pool.try_claim('task')
        self.assertIsNone(self.pool.try_claim('progress'))
        self.assertEqual(4, len(list(self.monitor.directory.glob('*.claim.json'))))
        with self.pool.try_claim('task', cleanup=True):
            pass

    def test_orphan_detection_blocks_io_before_client_marker_without_killing_live_helper(self):
        first, second = self.child(), self.child()
        self.event(first, 'admitted')
        self.event(second, 'admitted')
        self.send(first, 'lost')
        self.event(first, 'acquired', 'lost')
        self.send(second, 'still-running')
        self.event(second, 'acquired', 'still-running')
        first[0].kill()
        first[0].wait(timeout=3)
        self.assertFalse((self.monitor.directory / 'dispatch-blocked.json').exists())
        self.send(second, 'new-business')
        self.event(second, 'rejected', 'new-business')
        self.send(second, 'cleanup', progress=True)
        self.event(second, 'acquired', 'cleanup')
        self.send(second, 'still-running', 'release')
        self.event(second, 'released', 'still-running')
        self.send(second, 'cleanup', 'release')
        self.event(second, 'released', 'cleanup')
        second[0].stdin.close()
        self.assertEqual(0, second[0].wait(timeout=3))
        self.assertEqual(1, len(list(self.monitor.directory.glob('*.claim.json'))))
        marker = json.loads((self.monitor.directory / 'dispatch-blocked.json').read_text())
        self.assertEqual('task_pool_orphaned_io', marker['reason'])

    def test_eof_with_unreleased_io_preserves_unknown_occupancy(self):
        child = self.child()
        self.event(child, 'admitted')
        self.send(child, 'unknown-result')
        self.event(child, 'acquired', 'unknown-result')
        child[0].stdin.close()
        self.assertEqual(2, child[0].wait(timeout=3))
        with self.assertRaisesRegex(ValueError, 'dispatch_blocked'):
            self.pool.try_claim('io')
        self.assertEqual(1, len(list(self.monitor.directory.glob('*.claim.json'))))

    def test_foreign_directory_copy_cannot_reuse_health_state(self):
        import shutil
        copy = self.root / 'other-stage'
        shutil.copytree(self.monitor.directory, copy)
        copied = pool.Pool(copy, self.root, 'run-1', 'cli-01')
        with self.assertRaisesRegex(ValueError, 'foreign'):
            copied.try_claim('task')

    def test_stage_is_single_use_and_keeps_history(self):
        with self.assertRaisesRegex(ValueError, 'already_active'):
            with pool.Monitor(self.monitor.directory, self.monitor.config):
                pass
        self.monitor.__exit__()
        history = (self.monitor.directory / 'samples.jsonl').read_bytes()
        with self.assertRaisesRegex(ValueError, 'already_used'):
            with pool.Monitor(self.monitor.directory, self.monitor.config):
                pass
        self.assertEqual(history, (self.monitor.directory / 'samples.jsonl').read_bytes())

    def test_background_monitor_refreshes_and_stops_without_replacing_history(self):
        config = pool.configuration(self.root, 'run-1', 'sweep-01', 2, 3,
                                    interval_seconds=.05, sample_seconds=.01, stale_seconds=.2)
        current = measurement()
        with pool.Monitor(self.root / 'sweep', config, sampler=lambda _: current) as monitor:
            self.assertEqual(2, monitor.pool.state()['task_capacity'])
            current = measurement(90)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not monitor.pool.state()['paused']:
                time.sleep(.01)
            self.assertTrue(monitor.pool.state()['paused'])
            self.assertEqual(1, monitor.pool.state()['task_capacity'])
        state = json.loads((monitor.directory / 'state.json').read_text())
        self.assertTrue(state['stopped'])
        history = [json.loads(line) for line in (monitor.directory / 'samples.jsonl').read_text().splitlines()]
        self.assertGreaterEqual(len(history), 2)
        self.assertFalse(history[0]['paused'])
        self.assertTrue(history[-1]['paused'])

    def test_broken_sampler_pauses_without_guessing_healthy_capacity(self):
        def broken(_):
            raise RuntimeError('synthetic_sampler_failure')
        self.monitor.sampler = broken
        self.monitor.refresh()
        self.assertTrue(self.pool.state()['paused'])
        self.assertEqual('measurement_unknown', self.pool.state()['reason'])
        self.assertIsNone(self.pool.try_claim('task'))

    def test_io_slots_are_released_after_operation_failure_and_protocol_error(self):
        child = self.child()
        self.event(child, 'admitted')
        self.send(child, 'failing-operation')
        self.event(child, 'acquired', 'failing-operation')
        self.send(child, 'failing-operation', 'release')
        self.event(child, 'released', 'failing-operation')
        self.send(child, 'next-operation')
        self.event(child, 'acquired', 'next-operation')
        self.send(child, 'unknown-operation', 'release')
        self.assertEqual(2, child[0].wait(timeout=3))
        self.assertEqual(0, self.pool.occupied('io', 0, 3))

    def test_watch_cli_readiness_and_eof_shutdown(self):
        directory = self.root / 'watch'
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), 'watch', '--directory', str(directory),
             '--project', str(self.root), '--run', 'run-1', '--stage', 'watch',
             '--maximum-tasks', '2', '--maximum-io', '3'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        try:
            output, error = process.communicate(input='', timeout=5)
            self.assertEqual(0, process.returncode, error)
            self.assertEqual('monitor_ready', json.loads(output)['event'])
            self.assertTrue(json.loads((directory / 'state.json').read_text())['stopped'])
            self.assertEqual(1, len((directory / 'samples.jsonl').read_text().splitlines()))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)


if __name__ == '__main__':
    unittest.main()
