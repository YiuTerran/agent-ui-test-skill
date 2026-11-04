#!/usr/bin/env python3
"""Cooperative task and leaf-I/O admission inside an already reserved stage."""
import argparse
from contextlib import contextmanager, ExitStack
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import re
import stat
import sys
import threading
import time


_SPEC = importlib.util.spec_from_file_location(
    'task_pool_resources', Path(__file__).with_name('resource_controller.py'))
resources = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(resources)
platform = resources.platform_support
save = resources.scheduling.atomic_json


def require(condition, message):
    if not condition:
        raise ValueError(message)


def percentage(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100


class DispatchBlocked(ValueError):
    pass


def policy(current, maximum, measurement, initial=False, high=80, low=60):
    values = [measurement.get(key) for key in ('cpu_percent', 'memory_percent')]
    if any(percentage(value) and value >= high for value in values):
        return max(1, current - 1), True, 'high_utilization'
    if not all(percentage(value) for value in values):
        return current, True, 'measurement_unknown'
    if initial:
        return maximum, False, 'reserved_capacity_available'
    if all(value < low for value in values):
        return min(maximum, current + 1), False, 'low_utilization'
    return current, False, 'within_band'


def configuration(project, run, stage, maximum_tasks, maximum_io, progress_slots=1,
                  interval_seconds=30, sample_seconds=1, stale_seconds=75,
                  high_percent=80, low_percent=60):
    project = Path(project).resolve(strict=True)
    require(project.is_dir(), 'project_directory_required')
    for name, value in (('run', run), ('stage', stage)):
        require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value),
                'invalid_' + name)
    for value in (maximum_tasks, maximum_io, progress_slots):
        require(type(value) is int, 'integer_capacities_required')
    require(maximum_tasks >= 1 and 0 <= progress_slots < maximum_io, 'invalid_capacities')
    for value in (interval_seconds, sample_seconds, stale_seconds, high_percent, low_percent):
        require(type(value) in (int, float) and math.isfinite(value), 'finite_settings_required')
    require(.05 <= interval_seconds <= 3600 and .01 <= sample_seconds <= 10,
            'invalid_sampling_interval')
    require(stale_seconds >= 2 * interval_seconds + sample_seconds, 'stale_window_too_short')
    require(0 <= low_percent < high_percent <= 100, 'invalid_thresholds')
    return dict(schema_version=1, project=str(project), run=run, stage=stage,
                maximum_tasks=maximum_tasks, maximum_io=maximum_io, progress_slots=progress_slots,
                interval_seconds=interval_seconds, sample_seconds=sample_seconds,
                stale_seconds=stale_seconds, high_percent=high_percent, low_percent=low_percent)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


@contextmanager
def lock(path, timeout=0):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    stack = ExitStack()
    try:
        require(stat.S_ISREG(os.fstat(descriptor).st_mode), 'regular_lock_file_required')
        try:
            stack.enter_context(platform.file_lock(descriptor, timeout=timeout))
        except TimeoutError:
            yield None
            return
        yield descriptor
    finally:
        stack.close()
        os.close(descriptor)


class Pool:
    def __init__(self, directory, project, run, stage):
        self.directory = Path(directory).resolve(strict=True)
        config = json.loads((self.directory / 'pool.json').read_text(encoding='utf-8'))
        require(config == configuration(**{key: value for key, value in config.items()
                                          if key != 'schema_version'}), 'pool_config_invalid')
        require((config['project'], config['run'], config['stage']) ==
                (str(Path(project).resolve(strict=True)), run, stage), 'pool_context_mismatch')
        self.config = config
        self.binding = digest(dict(config, directory=str(self.directory)))

    def state(self):
        state = json.loads((self.directory / 'state.json').read_text(encoding='utf-8'))
        require(type(state.get('schema_version')) is int and state['schema_version'] == 1 and
                state.get('binding') == self.binding and
                state.get('stopped') is False,
                'pool_stopped_or_foreign')
        sampled = state.get('sampled_at')
        require(type(sampled) in (int, float) and math.isfinite(sampled) and
                0 <= time.time() - sampled <= self.config['stale_seconds'], 'pool_stale')
        require(type(state.get('paused')) is bool, 'pool_state_invalid')
        for key, maximum in (('task_capacity', self.config['maximum_tasks']),
                             ('ordinary_io_capacity', self.config['maximum_io'] -
                              self.config['progress_slots'])):
            require(type(state.get(key)) is int and 1 <= state[key] <= maximum, 'pool_state_invalid')
        expected_io = 1 if state['paused'] else max(1, math.ceil(
            (self.config['maximum_io'] - self.config['progress_slots']) *
            state['task_capacity'] / self.config['maximum_tasks']))
        require(state['ordinary_io_capacity'] == expected_io and
                state.get('progress_io_capacity') == self.config['progress_slots'] and
                state.get('io_capacity') == expected_io + self.config['progress_slots'],
                'pool_state_invalid')
        return state

    def occupied(self, prefix, start, stop):
        count = 0
        for index in range(start, stop):
            with lock(self.directory / f'{prefix}-{index}.lock') as acquired:
                count += acquired is None
        return count

    def quarantined(self):
        marker = self.directory / 'dispatch-blocked.json'
        for prefix, maximum in (
                ('io', self.config['maximum_io'] - self.config['progress_slots']),
                ('progress', self.config['progress_slots'])):
            for index in range(maximum):
                if not (self.directory / f'{prefix}-{index}.claim.json').exists():
                    continue
                with lock(self.directory / f'{prefix}-{index}.lock') as acquired:
                    if acquired is not None and not marker.exists():
                        save(marker, dict(reason='task_pool_orphaned_io', slot=f'{prefix}-{index}'))
        return marker.exists()

    def try_claim(self, kind, cleanup=False):
        require(kind in {'task', 'io', 'progress'}, 'invalid_slot_kind')
        with lock(self.directory / 'dispatch.lock', timeout=1) as dispatch:
            require(dispatch is not None, 'pool_dispatch_lock_timeout')
            state = self.state()
            blocked = self.quarantined()
            if blocked and kind == 'io':
                raise DispatchBlocked('pool_dispatch_blocked')
            if kind == 'task':
                if not cleanup and blocked:
                    raise DispatchBlocked('pool_dispatch_blocked')
                if state['paused'] and not cleanup:
                    return None
                capacity = 1 if state['paused'] else state['task_capacity']
                maximum, prefix = self.config['maximum_tasks'], 'task'
            elif kind == 'io':
                capacity = 1 if state['paused'] else state['ordinary_io_capacity']
                maximum = self.config['maximum_io'] - self.config['progress_slots']
                prefix = 'io'
            else:
                capacity = maximum = self.config['progress_slots']
                require(capacity > 0, 'progress_slots_not_reserved')
                prefix = 'progress'
            if self.occupied(prefix, capacity, maximum):
                return None
            for index in range(capacity):
                receipt = self.directory / f'{prefix}-{index}.claim.json'
                if kind != 'task' and receipt.exists():
                    continue
                stack = ExitStack()
                if stack.enter_context(lock(self.directory / f'{prefix}-{index}.lock')) is not None:
                    if kind != 'task':
                        try:
                            save(receipt, dict(schema_version=1, helper_pid=os.getpid(),
                                               binding=self.binding, kind=kind))
                        except BaseException:
                            stack.close()
                            raise
                        return IOClaim(self, stack, receipt)
                    return stack
                stack.close()
        return None


class IOClaim:
    def __init__(self, pool, stack, receipt):
        self.pool, self.stack, self.receipt = pool, stack, receipt
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.closed:
            return
        with lock(self.pool.directory / 'dispatch.lock', timeout=1) as dispatch:
            require(dispatch is not None, 'pool_dispatch_lock_timeout')
            self.receipt.unlink()
            self.stack.close()
            self.closed = True

    def abandon(self):
        self.stack.close()
        self.closed = True


class Monitor:
    def __init__(self, directory, config, sampler=None):
        self.directory = Path(directory).resolve()
        self.config = configuration(**{key: value for key, value in config.items()
                                       if key != 'schema_version'})
        self.sampler = sampler or resources.sample
        self.capacity = 1
        self.stop = threading.Event()
        self.thread = None
        self.pool = None

    def refresh(self, initial=False):
        try:
            measurement = self.sampler(self.config['sample_seconds'])
            require(isinstance(measurement, dict), 'invalid_measurement')
        except Exception:
            measurement = dict(cpu_percent=None, memory_percent=None, errors=['sampler_failed'])
        measurement = dict(measurement)
        for key in ('cpu_percent', 'memory_percent'):
            if not percentage(measurement.get(key)):
                measurement[key] = None
        with lock(self.directory / 'dispatch.lock', timeout=1) as dispatch:
            require(dispatch is not None, 'pool_dispatch_lock_timeout')
            if self.stop.is_set():
                return
            capacity, paused, reason = policy(
                self.capacity, self.config['maximum_tasks'], measurement, initial,
                self.config['high_percent'], self.config['low_percent'])
            ordinary_maximum = self.config['maximum_io'] - self.config['progress_slots']
            previous_io = (self.pool.state()['ordinary_io_capacity'] if not initial else 1)
            if capacity > self.capacity and not initial and (
                    self.pool.occupied('task', self.capacity, self.config['maximum_tasks']) or
                    self.pool.occupied('io', previous_io, ordinary_maximum)):
                capacity, reason = self.capacity, 'waiting_for_drain'
            self.capacity = capacity
            ordinary = 1 if paused else max(1, math.ceil(
                ordinary_maximum * capacity / self.config['maximum_tasks']))
            state = dict(schema_version=1, binding=self.pool.binding, sampled_at=time.time(),
                         task_capacity=capacity, ordinary_io_capacity=ordinary,
                         progress_io_capacity=self.config['progress_slots'],
                         io_capacity=ordinary + self.config['progress_slots'],
                         paused=paused, stopped=False, reason=reason, measurement=measurement,
                         active_tasks=self.pool.occupied('task', 0, self.config['maximum_tasks']),
                         active_io=self.pool.occupied('io', 0, ordinary_maximum) +
                         self.pool.occupied('progress', 0, self.config['progress_slots']))
            save(self.directory / 'state.json', state)
            with (self.directory / 'samples.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(state, allow_nan=False) + '\n')

    def loop(self):
        try:
            while not self.stop.wait(self.config['interval_seconds']):
                self.refresh()
        except Exception:
            self.stop.set()
            self.mark_stopped()

    def mark_stopped(self):
        with lock(self.directory / 'dispatch.lock', timeout=1) as dispatch:
            require(dispatch is not None, 'pool_dispatch_lock_timeout')
            path = self.directory / 'state.json'
            if path.exists():
                state = json.loads(path.read_text(encoding='utf-8'))
                state.update(stopped=True, paused=True)
                save(path, state)

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner = ExitStack()
        try:
            require(self.owner.enter_context(lock(self.directory / 'monitor.lock')) is not None,
                    'pool_monitor_already_active')
            require(not (self.directory / 'pool.json').exists(), 'pool_stage_already_used')
            save(self.directory / 'pool.json', self.config)
            self.pool = Pool(self.directory, self.config['project'], self.config['run'], self.config['stage'])
            self.refresh(initial=True)
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.owner.close()
            raise

    def __exit__(self, *errors):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=self.config['sample_seconds'] + 2)
        try:
            self.mark_stopped()
        finally:
            self.owner.close()


def emit(**value):
    print(json.dumps(value), flush=True)


def serve(pool, cleanup=False, wait_seconds=900):
    messages = queue.Queue()

    def reader():
        try:
            for line in sys.stdin:
                value = json.loads(line)
                require(isinstance(value, dict), 'message_object_required')
                messages.put(value)
        except Exception:
            messages.put({'invalid': True})
        finally:
            messages.put(None)

    threading.Thread(target=reader, daemon=True).start()
    pending, held = {}, {}
    task = None
    started = time.monotonic()
    try:
        while True:
            try:
                message = messages.get(timeout=.025)
            except queue.Empty:
                message = {}
            if message is None:
                require(not held, 'unreleased_io_on_eof')
                return
            if message:
                require(task is not None, 'task_not_admitted')
                identifier = message.get('id')
                require(isinstance(identifier, str) and identifier, 'message_id_required')
                action = message.get('action')
                if action == 'acquire' and identifier not in pending and identifier not in held:
                    progress = message.get('progress', False)
                    require(type(progress) is bool, 'progress_boolean_required')
                    pending[identifier] = (time.monotonic(), 'progress' if progress else 'io')
                elif action == 'release' and identifier in held:
                    held.pop(identifier).close()
                    emit(id=identifier, event='released')
                else:
                    raise ValueError('invalid_message')
            if task is None:
                require(time.monotonic() - started <= wait_seconds, 'task_wait_timeout')
                task = pool.try_claim('task', cleanup)
                if task is not None:
                    emit(event='admitted')
                continue
            for identifier, (requested, kind) in list(pending.items()):
                require(time.monotonic() - requested <= wait_seconds, 'io_wait_timeout')
                try:
                    slot = pool.try_claim(kind)
                except DispatchBlocked:
                    del pending[identifier]
                    emit(id=identifier, event='rejected', error='pool_dispatch_blocked')
                    continue
                if slot is not None:
                    held[identifier] = slot
                    del pending[identifier]
                    emit(id=identifier, event='acquired')
    finally:
        for slot in held.values():
            slot.abandon()
        if task is not None:
            task.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('watch', 'hold'):
        command = commands.add_parser(name)
        command.add_argument('--directory', required=True, type=Path)
        command.add_argument('--project', required=True, type=Path)
        command.add_argument('--run', required=True)
        command.add_argument('--stage', required=True)
        if name == 'watch':
            command.add_argument('--maximum-tasks', required=True, type=int)
            command.add_argument('--maximum-io', required=True, type=int)
            command.add_argument('--progress-slots', type=int, default=1)
        else:
            command.add_argument('--cleanup', action='store_true')
            command.add_argument('--wait-seconds', type=float, default=900)
    args = parser.parse_args(argv)
    if args.command == 'watch':
        config = configuration(args.project, args.run, args.stage, args.maximum_tasks,
                               args.maximum_io, args.progress_slots)
        with Monitor(args.directory, config) as monitor:
            emit(event='monitor_ready', directory=str(monitor.directory))
            sys.stdin.read()
    else:
        require(math.isfinite(args.wait_seconds) and args.wait_seconds > 0, 'invalid_wait_seconds')
        serve(Pool(args.directory, args.project, args.run, args.stage),
              args.cleanup, args.wait_seconds)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print('task_pool:' + type(error).__name__ + ':' + str(error), file=sys.stderr)
        sys.exit(2)
