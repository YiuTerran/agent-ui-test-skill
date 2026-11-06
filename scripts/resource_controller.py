#!/usr/bin/env python3
"""Sample actual utilization and reconcile an owned, cooperatively drained worker pool."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time


def sibling(name):
    spec = importlib.util.spec_from_file_location(
        'auto_test_resources_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


platform_support = sibling('platform_support')
scheduling = sibling('scheduling')
HOST = socket.gethostname()
DEFAULTS = {'high_percent': 80, 'low_percent': 60, 'interval_seconds': 30,
            'sample_seconds': 1, 'max_subagents': 8, 'adapter_timeout_seconds': 10}


def check(condition, message):
    if not condition:
        raise ValueError(message)


def settings(value):
    check(isinstance(value, dict) and not (set(value) - set(DEFAULTS)),
          'unknown resource controller setting')
    result = dict(DEFAULTS, **value)
    for key, number in result.items():
        check(type(number) in (int, float) and math.isfinite(number), 'invalid ' + key)
    check(0 <= result['low_percent'] < result['high_percent'] <= 100,
          'require 0 <= low_percent < high_percent <= 100')
    check(.05 <= result['interval_seconds'] <= 3600, 'invalid interval_seconds')
    check(.01 <= result['sample_seconds'] <= 10, 'invalid sample_seconds')
    check(.1 <= result['adapter_timeout_seconds'] <= 60, 'invalid adapter_timeout_seconds')
    check(type(result['max_subagents']) is int and result['max_subagents'] >= 1,
          'max_subagents must be an integer >= 1')
    return result


def cgroup_samples():
    result = {}
    if sys.platform != 'linux':
        return result
    for directory in scheduling.cgroup_directories():
        try:
            quota, period = (directory / 'cpu.max').read_text().split()
            usage = dict(line.split() for line in (directory / 'cpu.stat').read_text().splitlines())
            if quota != 'max':
                result[str(directory)] = {'cpu_capacity': int(quota) / int(period),
                                          'cpu_seconds': int(usage['usage_usec']) / 1_000_000}
        except (OSError, ValueError, KeyError, ZeroDivisionError):
            pass
        try:
            maximum = int((directory / 'memory.max').read_text())
            used = int((directory / 'memory.current').read_text())
            if maximum > 0:
                result.setdefault(str(directory), {})['memory_percent'] = 100 * used / maximum
        except (OSError, ValueError):
            pass
    return result


def sample(seconds=1):
    result = {'sampled_at': scheduling.timestamp(), 'host': HOST,
              'cpu_percent': None, 'memory_percent': None, 'errors': [],
              'source': 'psutil', 'scope': 'local_host'}
    try:
        import psutil
    except ImportError:
        result['errors'].append('psutil_unavailable')
        return result
    before = cgroup_samples()
    started = time.monotonic()

    def memory_usage():
        memory = psutil.virtual_memory()
        return 100 * (1 - memory.available / memory.total)

    for field, read in (
            ('cpu_percent', lambda: psutil.cpu_percent(interval=seconds)),
            ('memory_percent', memory_usage)):
        try:
            value = read()
            check(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100,
                  'invalid measured percentage')
            result[field] = value
        except (OSError, ValueError, ZeroDivisionError, RuntimeError) as error:
            result['errors'].append(field + ':' + type(error).__name__)
    elapsed = time.monotonic() - started
    for group, current in cgroup_samples().items():
        if result['memory_percent'] is not None and 'memory_percent' in current:
            result['memory_percent'] = max(result['memory_percent'], min(100, current['memory_percent']))
            result['scope'] = 'local_host_and_cgroup_v2'
        prior = before.get(group, {})
        if (result['cpu_percent'] is not None and 'cpu_seconds' in current
                and 'cpu_seconds' in prior and elapsed > 0 and current['cpu_capacity'] > 0):
            used = max(0, current['cpu_seconds'] - prior['cpu_seconds'])
            result['cpu_percent'] = max(result['cpu_percent'], min(
                100, used / elapsed / current['cpu_capacity'] * 100))
            result['scope'] = 'local_host_and_cgroup_v2'
    result['sampled_at'] = scheduling.timestamp()
    return result


def inventory(value):
    check(isinstance(value, dict), 'adapter inventory must be an object')
    workers = value.get('workers')
    check(isinstance(workers, list), 'inventory requires workers')
    seen = set()
    for worker in workers:
        check(isinstance(worker, dict), 'invalid worker')
        identifier = worker.get('id')
        check(isinstance(identifier, str) and identifier and identifier not in seen,
              'worker IDs must be nonempty and unique')
        seen.add(identifier)
        check(worker.get('state') in {'active', 'draining'}, 'invalid worker state')
        check(isinstance(worker.get('started_at'), str), 'worker started_at must be a timestamp')
        started = datetime.fromisoformat(worker['started_at'].replace('Z', '+00:00'))
        check(started.tzinfo is not None, 'worker started_at requires timezone')
    for key in ('pending_tasks', 'available_slots'):
        check(type(value.get(key)) is int and value[key] >= 0, key + ' must be nonnegative')
    return value


def decide(measurement, pool, config):
    pool = inventory(pool)
    workers = pool['workers']
    active = [worker for worker in workers if worker['state'] == 'active']
    draining = len(workers) - len(active)
    percentages = [measurement.get(key) for key in ('cpu_percent', 'memory_percent')]
    known = all(type(value) in (int, float) and math.isfinite(value)
                and 0 <= value <= 100 for value in percentages)
    high = any(type(value) in (int, float) and math.isfinite(value)
               and value >= config['high_percent'] for value in percentages)
    result = {'action': 'hold', 'reason': 'within_band', 'worker_id': None,
              'subagents': len(workers), 'active_subagents': len(active),
              'draining_subagents': draining, 'desired_subagents': len(active),
              'minimum_total_agents': 2,
              'admission_paused': high or not known or len(active) > config['max_subagents'],
              'pending_tasks': pool['pending_tasks']}
    if (high or len(active) > config['max_subagents']) and len(active) > 1:
        newest = max(active, key=lambda worker: datetime.fromisoformat(
            worker['started_at'].replace('Z', '+00:00')))
        result.update(action='drain', worker_id=newest['id'],
                      desired_subagents=min(config['max_subagents'], len(active) - 1),
                      reason='high_utilization' if high else 'configured_ceiling')
    elif high:
        result['reason'] = 'high_utilization_at_floor'
    elif not known:
        result['reason'] = 'measurement_unknown'
    elif draining:
        result['reason'] = 'waiting_for_cleanup'
    elif not pool['pending_tasks']:
        result['reason'] = 'no_pending_tasks'
    elif not pool['available_slots']:
        result['reason'] = 'no_host_slots'
    elif len(active) >= config['max_subagents']:
        result['reason'] = 'configured_ceiling'
    elif not active or all(value < config['low_percent'] for value in percentages):
        result.update(action='spawn', reason='initial_worker' if not active else 'low_utilization',
                      desired_subagents=len(active) + 1)
    return result


def paths(project):
    root = scheduling.suite_catalog.bind_coordination(project)
    directory = root / 'test/.cache/scheduling/resource-controllers' / hashlib.sha256(
        HOST.encode()).hexdigest()[:16]
    lexical = directory
    for parent in [lexical / 'state.json', lexical / 'controller.lock', lexical, *lexical.parents]:
        check(not parent.is_symlink()
              and not (parent.exists() and getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400),
              'resource controller path contains symlink or reparse point')
    platform_support.ensure_directory(directory, parents=True)
    check(directory.resolve().is_relative_to(root), 'resource controller path escapes root')
    return directory / 'state.json', directory / 'controller.lock'


@contextmanager
def controller_lock(project):
    state, lock = paths(project)
    with lock.open('a') as stream:
        with platform_support.file_lock(stream.fileno(), timeout=.1):
            yield state


def write_state(path, controller_id, config, measurement, decision, **extra):
    state = {'schema_version': 1, 'controller_id': controller_id, 'host': HOST,
             'updated_at': scheduling.timestamp(), 'config': config,
             'measurement': measurement, 'decision': decision, **extra}
    scheduling.atomic_json(path, state)
    return state


def read_admission(project, controller_id):
    path, _ = paths(project)
    check(path.is_file(), 'resource controller has not sampled')
    state = scheduling.read_json(path)
    check(state.get('schema_version') == 1 and state.get('controller_id') == controller_id
          and state.get('host') == HOST, 'resource controller binding differs')
    config = settings(state['config'])
    at = datetime.fromisoformat(state['updated_at'].replace('Z', '+00:00'))
    age = (datetime.now(timezone.utc) - at).total_seconds()
    sampled = datetime.fromisoformat(state['measurement']['sampled_at'].replace('Z', '+00:00'))
    sampled_age = (datetime.now(timezone.utc) - sampled).total_seconds()
    maximum_age = 2 * config['interval_seconds'] + config['sample_seconds'] + config['adapter_timeout_seconds']
    check(0 <= age <= maximum_age and 0 <= sampled_age <= maximum_age,
          'resource controller sample is stale')
    decision = state['decision']
    check(type(decision.get('admission_paused')) is bool
          and type(decision.get('desired_subagents')) is int
          and decision['desired_subagents'] >= 0,
          'invalid controller decision')
    check(state.get('status') == 'running', 'resource controller is not running')
    check(not state.get('pending_action'), 'resource controller action is pending')
    return state


def adapter_call(command, project, controller_id, action, timeout, **extra):
    request = {'schema_version': 1, 'controller_id': controller_id, 'action': action, **extra}
    result = subprocess.run(command, input=json.dumps(request), cwd=project,
                            capture_output=True, text=True, timeout=timeout)
    check(result.returncode == 0, 'adapter failed; inspect its private logs')
    try:
        response = json.loads(result.stdout)
    except ValueError as error:
        raise ValueError('adapter returned invalid JSON') from error
    check(isinstance(response, dict) and response.get('controller_id') == controller_id,
          'adapter controller binding differs')
    if action == 'inspect':
        return inventory(response)
    check(response.get('ok') is True and response.get('action_id') == extra['action_id'],
          'adapter did not acknowledge action')
    return response


def apply_action(command, project, controller_id, pending, timeout):
    request = {key: value for key, value in pending.items() if key != 'before_worker_ids'}
    adapter_call(command, project, controller_id, timeout=timeout, **request)
    observed = adapter_call(command, project, controller_id, 'inspect', timeout)
    if pending['action'] == 'drain':
        remaining = [worker for worker in observed['workers'] if worker['id'] == pending['worker_id']]
        check(not remaining or remaining[0]['state'] == 'draining',
              'adapter acknowledged drain without stopping new assignments')
    else:
        before = set(pending['before_worker_ids'])
        added = [worker for worker in observed['workers'] if worker['id'] not in before]
        check(len(added) == 1 and added[0]['state'] == 'active',
              'adapter acknowledged spawn without one registered worker')
    return observed


def watch(project, controller_id, config, command, cycles=None, emit=print):
    check(isinstance(command, list) and command and all(
        isinstance(value, str) and value for value in command), 'adapter must be an argv array')
    timeout = config['adapter_timeout_seconds']
    adapter_fingerprint = hashlib.sha256(json.dumps(
        [str(project), controller_id, command], ensure_ascii=True).encode()).hexdigest()
    with controller_lock(project) as path:
        previous = scheduling.read_json(path) if path.exists() else {}
        check(not previous.get('pending_action') or previous.get('controller_id') == controller_id,
              'recover previous controller pending action before changing owner')
        check(not previous.get('pending_action')
              or previous.get('adapter_fingerprint') == adapter_fingerprint,
              'pending action requires the original adapter')
        pending = previous.get('pending_action')
        count = 0
        last_state = write_state(
            path, controller_id, config,
            {'sampled_at': scheduling.timestamp(), 'host': HOST,
             'cpu_percent': None, 'memory_percent': None, 'errors': ['initializing']},
            {'admission_paused': True, 'desired_subagents': 0},
            status='starting', pending_action=pending, adapter_fingerprint=adapter_fingerprint)
        try:
            while cycles is None or count < cycles:
                started = time.monotonic()
                if pending:
                    apply_action(command, project, controller_id, pending, timeout)
                    pending = None
                pool = adapter_call(command, project, controller_id, 'inspect', timeout)
                measurement = sample(config['sample_seconds'])
                decision = decide(measurement, pool, config)
                if decision['action'] != 'hold':
                    pending = {'action': decision['action'], 'action_id': secrets.token_hex(16),
                               'worker_id': decision['worker_id'],
                               'admission_paused': decision['admission_paused'],
                               'before_worker_ids': [worker['id'] for worker in pool['workers']]}
                last_state = write_state(path, controller_id, config, measurement, decision,
                                         status='running', pending_action=pending,
                                         adapter_fingerprint=adapter_fingerprint)
                if pending:
                    observed = apply_action(command, project, controller_id, pending, timeout)
                    pending = None
                    last_state = write_state(path, controller_id, config, measurement, decision,
                                             status='running', pending_action=None,
                                             adapter_fingerprint=adapter_fingerprint,
                                             observed_subagents=len(observed['workers']))
                emit(json.dumps(last_state, ensure_ascii=True, allow_nan=False), flush=True)
                count += 1
                if cycles is None or count < cycles:
                    remaining = config['interval_seconds'] - (time.monotonic() - started)
                    while remaining > 0:
                        time.sleep(min(remaining, 1))
                        remaining = config['interval_seconds'] - (time.monotonic() - started)
        finally:
            if last_state is not None:
                last_state.update(status='stopped', pending_action=pending,
                                  updated_at=scheduling.timestamp())
                scheduling.atomic_json(path, last_state)
        return last_state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['sample', 'step', 'watch'])
    parser.add_argument('--project', type=Path)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--cycles', type=int)
    args = parser.parse_args(argv)
    try:
        request = scheduling.read_json(args.input) if args.input else {}
        check(isinstance(request, dict), 'input must be an object')
        config = settings(request.get('settings', {}))
        if args.command == 'sample':
            result = sample(config['sample_seconds'])
        else:
            check(args.project and args.project.is_dir(), '--project must exist')
            project = args.project.resolve()
            controller_id = scheduling.identifier(request.get('controller_id'), 'controller_id')
            if args.command == 'step':
                with controller_lock(project) as path:
                    previous = scheduling.read_json(path) if path.exists() else {}
                    check(not previous.get('pending_action'),
                          'recover pending adapter action with watch before using step')
                    measurement = sample(config['sample_seconds'])
                    decision = decide(measurement, request['inventory'], config)
                    result = write_state(path, controller_id, config, measurement, decision,
                                         status='running', pending_action=None)
            else:
                check(args.cycles is None or args.cycles >= 1, '--cycles must be >= 1')
                watch(project, controller_id, config, request['adapter'], args.cycles)
                return 0
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'error': type(error).__name__, 'detail': str(error)
                          if isinstance(error, ValueError) else 'resource controller failed'}))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
