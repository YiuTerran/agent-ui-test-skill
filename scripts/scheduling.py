#!/usr/bin/env python3
"""Advisory E2E scheduling and atomic project reservations; never starts work."""
import argparse
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import socket
import subprocess
import sys
import tempfile

GIB = 1024 ** 3
BROWSER_BYTES = 2 * GIB
RESERVE_BYTES = 2 * GIB
QUOTAS = ('agents', 'exploration_browsers', 'playwright_workers', 'browsers')
DONE = {'approved', 'complete', 'completed', 'succeeded'}
TERMINAL = DONE | {'failed', 'cancelled', 'canceled', 'aborted'}
CONFIG_DEFAULTS = {'max_parallel_agents': 'auto', 'auto_parallel_cap': 4,
                   'browser_parallel_cap': 4, 'playwright_workers': 4}
HOST = socket.gethostname()
SYNCHRONIZE = 0x00100000
ERROR_INVALID_PARAMETER = 87
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258

_PLATFORM_SPEC = importlib.util.spec_from_file_location(
    'e2e_scheduling_platform_support', Path(__file__).resolve().with_name('platform_support.py'))
platform_support = importlib.util.module_from_spec(_PLATFORM_SPEC)
_PLATFORM_SPEC.loader.exec_module(platform_support)


class MemoryStatusEx(ctypes.Structure):
    # Fixed-width fields also make API mocks independent of the test host ABI.
    _fields_ = [('dwLength', ctypes.c_uint32), ('dwMemoryLoad', ctypes.c_uint32),
                ('ullTotalPhys', ctypes.c_uint64), ('ullAvailPhys', ctypes.c_uint64),
                ('ullTotalPageFile', ctypes.c_uint64), ('ullAvailPageFile', ctypes.c_uint64),
                ('ullTotalVirtual', ctypes.c_uint64), ('ullAvailVirtual', ctypes.c_uint64),
                ('ullAvailExtendedVirtual', ctypes.c_uint64)]


def windows_kernel32():
    return ctypes.WinDLL('kernel32', use_last_error=True)


def windows_resources():
    """Read current-process CPU affinity and available (not total) physical RAM."""
    cpus, memory = None, None
    try:
        kernel = windows_kernel32()
    except (AttributeError, OSError):
        return {'available_cpus': cpus, 'available_memory_bytes': memory}
    try:
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.GetProcessAffinityMask.argtypes = [ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_size_t),
                                                 ctypes.POINTER(ctypes.c_size_t)]
        kernel.GetProcessAffinityMask.restype = ctypes.c_int
        process_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
        if kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(), ctypes.byref(process_mask),
                                         ctypes.byref(system_mask)):
            mask = process_mask.value & system_mask.value
            cpus = bin(mask).count('1') if mask else None
    except (AttributeError, OSError, ValueError):
        pass
    try:
        kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatusEx)]
        kernel.GlobalMemoryStatusEx.restype = ctypes.c_int
        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
            memory = status.ullAvailPhys
    except (AttributeError, OSError, ValueError):
        pass
    return {'available_cpus': cpus, 'available_memory_bytes': memory}


def check(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, name, minimum=0):
    check(type(value) is int and value >= minimum, name + ' must be an integer >= ' + str(minimum))
    return value


def identifier(value, name):
    check(isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_.-]+', value))
          and value not in {'.', '..'}, 'invalid ' + name)
    return value


def read_json(path):
    with path.open(encoding='utf-8') as stream:
        return json.load(stream)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def configuration(project, overrides):
    spec_path = project / 'test/spec.md'
    spec = {}
    if spec_path.exists():
        blocks = re.findall(r'```json workflow-config\s*\n(.*?)\n```', spec_path.read_text(encoding='utf-8'), re.S)
        check(len(blocks) == 1, 'spec.md requires exactly one json workflow-config block')
        spec = json.loads(blocks[0])
        check(isinstance(spec, dict), 'workflow-config must be an object')
    configured = spec.get('concurrency', {})
    check(isinstance(configured, dict) and isinstance(overrides, dict), 'concurrency must be an object')
    check(not (set(overrides) - set(CONFIG_DEFAULTS)), 'unknown concurrency override')
    values, sources = {}, {}
    for key, default in CONFIG_DEFAULTS.items():
        values[key] = overrides.get(key, configured.get(key, default))
        sources[key] = 'input' if key in overrides else ('spec' if key in configured else 'default')
        if key == 'max_parallel_agents' and values[key] == 'auto':
            continue
        integer(values[key], key, 1)
    return values, sources


def cgroup_directories():
    """Inspect the process cgroup and its ancestors, not unrelated host limits."""
    root = Path('/sys/fs/cgroup')
    paths = []
    try:
        for line in Path('/proc/self/cgroup').read_text().splitlines():
            _, controllers, relative = line.split(':', 2)
            bases = [root] if not controllers else [root / c for c in controllers.split(',')]
            for base in bases:
                child = base / relative.lstrip('/')
                if child.is_relative_to(base) and '..' not in child.parts:
                    paths.extend([child, *[p for p in child.parents if p.is_relative_to(base)]])
    except (OSError, ValueError):
        pass
    return list(dict.fromkeys(paths))


def mac_available_memory():
    """Only count free/reclaimable pages; compressed/active RAM is not capacity."""
    try:
        output = subprocess.run(['/usr/bin/vm_stat'], capture_output=True, text=True,
                                check=True, timeout=2).stdout
        page_size = int(re.search(r'page size of (\d+) bytes', output)[1])
        pages = []
        for key in ('free', 'inactive', 'speculative'):
            pages.append(int(re.search(r'^Pages ' + key + r':\s+(\d+)\.', output, re.M)[1]))
        return sum(pages) * page_size
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None


def probe_resources():
    if sys.platform == 'win32':
        # Never fall through to POSIX probes or host-total RAM on Windows.
        return windows_resources()
    try:
        cpus = getattr(os, 'process_cpu_count', os.cpu_count)()
    except (OSError, NotImplementedError):
        cpus = None
    if hasattr(os, 'sched_getaffinity'):
        try:
            cpus = min(cpus or math.inf, len(os.sched_getaffinity(0)))
        except (OSError, NotImplementedError):
            pass
    memory = None
    try:
        text = Path('/proc/meminfo').read_text()
        memory = int(re.search(r'^MemAvailable:\s+(\d+) kB$', text, re.M)[1]) * 1024
    except (OSError, TypeError):
        try:
            memory = os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
        except (AttributeError, ValueError, OSError):
            pass
    if memory is None and sys.platform == 'darwin':
        memory = mac_available_memory()
    for directory in cgroup_directories():
        try:
            quota, period = (directory / 'cpu.max').read_text().split()
            if quota != 'max':
                cpus = min(cpus or math.inf, int(quota) / int(period))
        except (OSError, ValueError, ZeroDivisionError):
            try:
                quota = int((directory / 'cpu.cfs_quota_us').read_text())
                period = int((directory / 'cpu.cfs_period_us').read_text())
                if quota > 0:
                    cpus = min(cpus or math.inf, quota / period)
            except (OSError, ValueError, ZeroDivisionError):
                pass
        for limit_name, current_name in [('memory.max', 'memory.current'),
                                          ('memory.limit_in_bytes', 'memory.usage_in_bytes')]:
            try:
                limit = int((directory / limit_name).read_text())
                free = max(0, limit - int((directory / current_name).read_text()))
                memory = min(memory, free) if memory is not None else free
            except (OSError, ValueError):
                pass
    return {'available_cpus': cpus, 'available_memory_bytes': memory}


def resource_facts(data):
    supplied = data.get('resources', {})
    check(isinstance(supplied, dict), 'resources must be an object')
    allowed = {'available_cpus', 'available_memory_bytes', 'available_agent_slots', 'independent_tasks', 'pressure'}
    check(not (set(supplied) - allowed), 'unknown resource field')
    observed = probe_resources()
    facts, sources = {}, {}
    for key in allowed - {'pressure'}:
        value = supplied.get(key, observed.get(key))
        if value is not None:
            if key == 'available_cpus':
                check(type(value) in {int, float} and math.isfinite(value) and value >= 0,
                      'available_cpus must be finite and nonnegative')
            else:
                integer(value, key)
        facts[key] = value
        sources[key] = 'provided' if key in supplied else ('observed' if value is not None else 'unknown')
    facts['pressure'] = supplied.get('pressure', False)
    check(type(facts['pressure']) is bool, 'pressure must be a boolean')
    sources['pressure'] = 'provided' if 'pressure' in supplied else 'not_reported'
    return facts, sources


def empty_ledger():
    return {'schema_version': 1, 'leases': {}, 'peak': dict.fromkeys(QUOTAS, 0)}


def counts(leases):
    result = dict.fromkeys(QUOTAS, 0)
    for lease in leases:
        if lease['status'] == 'active':
            for key in QUOTAS:
                result[key] += lease['quota'][key]
    return result


def quota_view(ledger):
    active = counts(ledger['leases'].values())
    return {'active': active, 'peak': dict(ledger['peak']),
            'count_basis': 'observed_reservations_not_running_processes',
            'estimated_active_browser_memory_bytes': active['browsers'] * BROWSER_BYTES,
            'estimated_peak_browser_memory_bytes': ledger['peak']['browsers'] * BROWSER_BYTES,
            'measured_peak_memory_bytes': None}


def budget(project, data, ledger, independent=None):
    config, config_sources = configuration(project, data.get('concurrency', {}))
    facts, fact_sources = resource_facts(data)
    active = counts(ledger['leases'].values())
    requested_agents = config['auto_parallel_cap'] if config['max_parallel_agents'] == 'auto' else config['max_parallel_agents']
    cpus = facts['available_cpus']
    cpu_agents = max(1, math.floor(cpus / 2)) if cpus else (0 if cpus == 0 else 1)
    cpu_browsers = max(1, math.floor(cpus)) if cpus else (0 if cpus == 0 else 1)
    slots = facts['available_agent_slots']
    slots = 1 if slots is None else slots
    lanes = facts['independent_tasks']
    agent_lanes, browser_lanes = independent if independent is not None else (1, 1)
    if lanes is not None:
        agent_lanes, browser_lanes = min(agent_lanes, lanes), min(browser_lanes, lanes)
        if independent is None:
            agent_lanes = browser_lanes = lanes
    memory = facts['available_memory_bytes']
    # Reservations may not have materialized as processes yet. Charge all active
    # reservations conservatively; repeated snapshots must not mint capacity.
    memory_cap = max(0, (memory - RESERVE_BYTES) // BROWSER_BYTES) if memory is not None else 1
    limits = {
        'agents': {'requested': requested_agents, 'cpu': cpu_agents,
                   'slots': active['agents'] + slots, 'independent_tasks': active['agents'] + agent_lanes},
        'browsers': {'requested': config['browser_parallel_cap'], 'cpu': cpu_browsers, 'memory': memory_cap,
                     'independent_tasks': active['browsers'] + browser_lanes},
    }
    # Unknown CPU or memory starts at ONE total browser, not one per invocation.
    if cpus is None:
        limits['browsers']['unknown_cpu'] = 1
    effective = {key: min(values.values()) for key, values in limits.items()}
    effective['playwright_workers'] = min(config['playwright_workers'], effective['browsers'])
    if facts['pressure']:
        effective = dict.fromkeys(effective, 0)
    available = {key: max(0, value - active[key]) for key, value in effective.items()}
    return {'requested': {'agents': requested_agents, 'browsers': config['browser_parallel_cap'],
                          'playwright_workers': config['playwright_workers']},
            'configuration': config, 'configuration_sources': config_sources,
            'effective': effective, 'available': available, 'limits': limits,
            'resource_facts': facts, 'resource_sources': fact_sources,
            'admission_paused': facts['pressure'], 'existing_leases_revoked': False,
            'planning_estimate': {'bytes_per_browser': BROWSER_BYTES, 'reserve_bytes': RESERVE_BYTES,
                                  'basis': 'estimate_not_measurement'}, 'quota': quota_view(ledger)}


def normalize_task(project, task_id, task):
    check(isinstance(task, dict), 'task must be an object')
    identifier(task_id, 'task_id')
    check(task.get('task_id', task_id) == task_id, 'task key and task_id differ')
    result = dict(task, task_id=task_id)
    for field in ('dependencies', 'writable_paths', 'readable_paths', 'conflict_keys', 'case_ids'):
        values = task.get(field, [])
        check(isinstance(values, list) and all(isinstance(v, str) and v for v in values), field + ' must be a string array')
        result[field] = list(dict.fromkeys(values))
    for dependency in result['dependencies']:
        identifier(dependency, 'dependency')
        check(dependency != task_id, 'task cannot depend on itself')
    for field in ('writable_paths', 'readable_paths'):
        paths = []
        for value in result[field]:
            check(not Path(value).anchor and not PureWindowsPath(value).anchor,
                  field + ' must be project-relative')
            check(not any(c in value for c in '*?[]'), field + ' must be literal paths, not globs')
            platform_support.relative_parts(value.replace('\\', '/'))
            path = (project / value).resolve()
            check(path.is_relative_to(project), field + ' path escapes project')
            paths.append(str(path))
        result[field] = paths
    isolation = task.get('isolation', 'unknown')
    check(isolation in {'verified', 'unknown'}, 'isolation must be verified or unknown')
    # Missing write declarations remain unknown even if isolation was asserted.
    result['isolation'] = isolation if 'writable_paths' in task else 'unknown'
    if task.get('isolation_group'):
        check(isinstance(task['isolation_group'], str), 'isolation_group must be a string')
        groups = ['group:' + task['isolation_group']]
    else:
        groups = [key + ':' + task[key] for key in ('source_id', 'scope') if isinstance(task.get(key), str) and task[key]]
        groups += ['case:' + cid for cid in result['case_ids']]
    result['isolation_groups'] = groups or ['unscoped']
    phase = task.get('phase')
    default = {'agents': 1, 'exploration_browsers': int(phase == 'exploration'),
               'playwright_workers': int(phase == 'execution')}
    units = task.get('quota', default)
    check(isinstance(units, dict) and not (set(units) - set(default)), 'invalid task quota fields')
    result['quota'] = {key: integer(units.get(key, 0), key) for key in default}
    result['quota']['browsers'] = result['quota']['exploration_browsers'] + result['quota']['playwright_workers']
    check(any(result['quota'].values()), 'task must reserve at least one resource')
    result['status'] = task.get('status', 'pending')
    check(isinstance(result['status'], str), 'status must be a string')
    return result


def tasks_from_input(project, data):
    raw = data.get('tasks', {})
    check(isinstance(raw, (dict, list)), 'tasks must be an object or array')
    if isinstance(raw, list):
        check(all(isinstance(t, dict) and 'task_id' in t for t in raw), 'task array requires task_id')
        pairs = [(t['task_id'], t) for t in raw]
        check(len({k for k, _ in pairs}) == len(pairs), 'duplicate task_id')
    else:
        pairs = list(raw.items())
    return {key: normalize_task(project, key, task) for key, task in pairs}


def conflicts(left, right):
    reasons = []
    shared = sorted(set(left['conflict_keys']) & set(right['conflict_keys']))
    if shared:
        reasons.append({'kind': 'conflict_keys', 'keys': shared})
    for a in left['writable_paths']:
        for b in right['writable_paths']:
            if Path(a).is_relative_to(b) or Path(b).is_relative_to(a):
                reasons.append({'kind': 'writable_paths', 'paths': [a, b]})
    # Readers can share stable inputs, but writers must wait for their readers.
    for writer, reader in ((left, right), (right, left)):
        for a in writer['writable_paths']:
            for b in reader.get('readable_paths', []):
                if Path(a).is_relative_to(b) or Path(b).is_relative_to(a):
                    reasons.append({'kind': 'read_write_paths', 'paths': [a, b]})
    groups = sorted(set(left['isolation_groups']) & set(right['isolation_groups']))
    if groups and 'unknown' in {left['isolation'], right['isolation']}:
        reasons.append({'kind': 'isolation_unknown', 'groups': groups})
    return reasons


def eligibility(task, tasks, occupied, run_id):
    reasons = []
    if task['status'] not in {'pending', 'ready'}:
        reasons.append({'kind': 'status', 'status': task['status']})
    for dependency in task['dependencies']:
        dep = tasks.get(dependency)
        if dep is None or dep['status'] not in DONE:
            reasons.append({'kind': 'dependency', 'task_id': dependency,
                            'status': dep['status'] if dep else 'missing'})
        elif any(item['run_id'] == run_id and item['task']['task_id'] == dependency for item in occupied):
            reasons.append({'kind': 'dependency_still_reserved', 'task_id': dependency})
    for item in occupied:
        other = item['task']
        if item['run_id'] == run_id and other['task_id'] == task['task_id']:
            reasons.append({'kind': 'already_running', 'task_id': other['task_id']})
        else:
            for reason in conflicts(task, other):
                reasons.append(dict(reason, run_id=item['run_id'], task_id=other['task_id']))
    return reasons


def context(project, data, ledger):
    run_id = identifier(data.get('run_id'), 'run_id')
    tasks = tasks_from_input(project, data)
    occupied = [{'run_id': lease['run_id'], 'task': lease['task']}
                for lease in ledger['leases'].values() if lease['status'] == 'active']
    unreserved = []
    for task in tasks.values():
        if task['status'] == 'running' and not any(i['run_id'] == run_id and i['task']['task_id'] == task['task_id'] for i in occupied):
            occupied.append({'run_id': run_id, 'task': task})
            unreserved.append(task['task_id'])
    candidates = [t for t in tasks.values() if not eligibility(t, tasks, occupied, run_id)]
    independent_tasks = []
    for task in candidates:
        if not any(conflicts(task, prior) for prior in independent_tasks):
            independent_tasks.append(task)
    unreserved_usage = {key: sum(tasks[tid]['quota'][key] for tid in unreserved) for key in QUOTAS}
    # A greedy alternative with smaller demand must not prevent acquiring a
    # different, individually eligible multi-worker task.
    independent = tuple(max(sum(t['quota'][key] for t in independent_tasks),
                            max((t['quota'][key] for t in candidates), default=0)) + unreserved_usage[key]
                        for key in ('agents', 'browsers'))
    result = budget(project, data, ledger, independent)
    result['unreserved_running_tasks'] = unreserved
    result['unreserved_running_quota_estimate'] = unreserved_usage
    result['available'] = {key: max(0, value - unreserved_usage[key])
                           for key, value in result['available'].items()}
    return run_id, tasks, occupied, result


def capacity_reasons(task, remaining):
    return [{'kind': 'quota', 'resource': key, 'requested': task['quota'][key], 'available': remaining[key]}
            for key in ('agents', 'browsers', 'playwright_workers') if task['quota'][key] > remaining[key]]


def ready(project, data, ledger):
    run_id, tasks, occupied, result = context(project, data, ledger)
    remaining = dict(result['available'])
    selected, blocked = [], {}
    for tid, task in tasks.items():
        reasons = eligibility(task, tasks, occupied, run_id) + capacity_reasons(task, remaining)
        if reasons:
            blocked[tid] = reasons
        else:
            selected.append(tid)
            occupied.append({'run_id': run_id, 'task': task})
            for key in remaining:
                remaining[key] -= task['quota'][key]
    return {'run_id': run_id, 'ready': selected, 'blocked': blocked, 'budget': result,
            'selection': 'input_order_greedy_advisory_acquire_rechecks'}


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=True, allow_nan=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        platform_support.sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked_ledger(project):
    directory = project / 'test/.cache/scheduling'
    check(directory.resolve().is_relative_to(project), 'scheduling directory escapes project')
    directory.mkdir(parents=True, exist_ok=True)
    # Keep the lock inode stable; only the separate JSON file is replaced.
    with (directory / 'quota.lock').open('a') as stream:
        with platform_support.file_lock(stream.fileno()):
            path = directory / 'quota.json'
            ledger = read_json(path) if path.exists() else empty_ledger()
            check(ledger.get('schema_version') == 1 and isinstance(ledger.get('leases'), dict), 'invalid quota ledger')
            yield ledger, path


def acquisition(project, data, ledger):
    run_id, tasks, occupied, result = context(project, data, ledger)
    tid = identifier(data.get('task_id'), 'task_id')
    check(tid in tasks, 'task_id not found in tasks')
    task = tasks[tid]
    owner = data.get('owner')
    check(isinstance(owner, str) and owner, 'owner is required')
    check(task.get('owner', owner) == owner, 'task owner differs from reservation owner')
    pid = integer(data.get('owner_pid'), 'owner_pid', 1)
    host = data.get('host', HOST)
    check(isinstance(host, str) and host, 'host must be a nonempty string')
    # Preserve pre-read-set signatures when the optional read set is empty.
    signature_task = {key: value for key, value in task.items()
                      if key != 'readable_paths' or value}
    signature = hashlib.sha256(json.dumps([run_id, signature_task, owner, pid, host], sort_keys=True).encode()).hexdigest()
    for lease in ledger['leases'].values():
        if lease['status'] == 'active' and lease['run_id'] == run_id and lease['task']['task_id'] == tid:
            check(lease['signature'] == signature, 'active task reservation differs; release it before changing')
            return {'acquired': True, 'idempotent': True, 'lease': lease, 'budget': result}, False
    reasons = eligibility(task, tasks, occupied, run_id) + capacity_reasons(task, result['available'])
    if reasons:
        return {'acquired': False, 'reasons': reasons, 'budget': result}, False
    lid = secrets.token_hex(16)
    lease = {'lease_id': lid, 'release_token': secrets.token_hex(24), 'run_id': run_id,
             'task': task, 'owner': owner, 'owner_pid': pid, 'host': host,
             'quota': task['quota'], 'status': 'active', 'created_at': timestamp(), 'signature': signature}
    ledger['leases'][lid] = lease
    active = counts(ledger['leases'].values())
    for key in QUOTAS:
        ledger['peak'][key] = max(ledger['peak'][key], active[key])
    result['quota'] = quota_view(ledger)
    result['available'] = {key: max(0, value - active[key] - result['unreserved_running_quota_estimate'][key])
                           for key, value in result['effective'].items()}
    return {'acquired': True, 'idempotent': False, 'lease': lease, 'budget': result}, True


def windows_pid_state(pid):
    """A zero-time wait observes liveness without any process mutation rights."""
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        return 'unknown'
    handle = None
    try:
        kernel = windows_kernel32()
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            # Access denied and every unclassified API failure are NOT absence.
            return 'dead' if ctypes.get_last_error() == ERROR_INVALID_PARAMETER else 'unknown'
        result = kernel.WaitForSingleObject(handle, 0)
        if result == WAIT_OBJECT_0:
            return 'dead'
        if result == WAIT_TIMEOUT:
            return 'alive'
        return 'unknown'
    except (AttributeError, OSError, ValueError, OverflowError):
        return 'unknown'
    finally:
        if handle:
            try:
                kernel.CloseHandle(handle)
            except (AttributeError, OSError):
                pass


def pid_state(pid):
    if sys.platform == 'win32':
        # os.kill(pid, 0) can terminate a Windows process; never use it here.
        return windows_pid_state(pid)
    try:
        os.kill(pid, 0)
        return 'alive'
    except ProcessLookupError:
        return 'dead'
    except (PermissionError, OSError):
        return 'unknown'


def terminal_evidence(project, lease):
    # Trust only the canonical workflow task record, never a caller's boolean,
    # lease age, run acceptance, or an arbitrary evidence path.
    path = project / 'test/.cache/runs' / lease['run_id'] / 'state.json'
    try:
        check(path.resolve().is_relative_to(project), 'terminal evidence escapes project')
        raw = path.read_bytes()
        state = json.loads(raw)
        task = state.get('tasks', {}).get(lease['task']['task_id'], {})
        at = datetime.fromisoformat(task.get('at', '').replace('Z', '+00:00'))
        created_at = datetime.fromisoformat(lease['created_at'])
        if (state.get('run_id') == lease['run_id'] and task.get('owner') == lease['owner']
                and task.get('status') in TERMINAL and at.tzinfo is not None
                and created_at <= at <= datetime.now(timezone.utc)):
            return {'path': path.relative_to(project).as_posix(), 'task_status': task['status'],
                    'sha256': hashlib.sha256(raw).hexdigest()}
    except (OSError, ValueError, AttributeError):
        pass
    return None


def finish_lease(project, data, ledger, command):
    lid = identifier(data.get('lease_id'), 'lease_id')
    check(lid in ledger['leases'], 'unknown lease_id')
    lease = ledger['leases'][lid]
    if command == 'release':
        token = data.get('release_token')
        check(isinstance(token, str) and secrets.compare_digest(token, lease['release_token']), 'invalid release_token')
    if lease['status'] != 'active':
        return {'released' if command == 'release' else 'reaped': False,
                'lease_id': lid, 'status': lease['status'], 'idempotent': True, 'quota': quota_view(ledger)}, False
    evidence = None
    if command == 'reap':
        evidence = terminal_evidence(project, lease)
        if evidence:
            reason = 'trusted_task_terminal'
        elif lease['host'] != HOST:
            return {'reaped': False, 'lease_id': lid, 'reason': 'remote_owner_liveness_unknown', 'quota': quota_view(ledger)}, False
        else:
            state = pid_state(lease['owner_pid'])
            if state != 'dead':
                return {'reaped': False, 'lease_id': lid, 'reason': 'owner_pid_' + state, 'quota': quota_view(ledger)}, False
            reason = 'same_host_owner_pid_dead'
    else:
        reason = 'owner_released'
    lease.update(status='released' if command == 'release' else 'reaped', ended_at=timestamp(), end_reason=reason)
    if evidence:
        lease['terminal_evidence'] = evidence
    return {'released' if command == 'release' else 'reaped': True, 'lease_id': lid,
            'reason': reason, 'quota': quota_view(ledger)}, True


def execute(command, project, data):
    project = Path(project).resolve()
    check(project.is_dir(), 'project must be an existing directory')
    check(isinstance(data, dict), 'input must be a JSON object')
    with locked_ledger(project) as (ledger, path):
        dirty = False
        if command == 'budget':
            result = budget(project, data, ledger)
        elif command == 'ready':
            result = ready(project, data, ledger)
        elif command == 'acquire':
            result, dirty = acquisition(project, data, ledger)
        else:
            result, dirty = finish_lease(project, data, ledger, command)
        if dirty:
            atomic_json(path, ledger)
        return dict(result, command=command, schema_version=1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['budget', 'ready', 'acquire', 'release', 'reap'])
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--input', required=True, type=Path, help='structured JSON request file')
    args = parser.parse_args(argv)
    try:
        result = execute(args.command, args.project, read_json(args.input))
    except (ValueError, OSError, TypeError, KeyError, OverflowError) as error:
        print(json.dumps({'error': str(error), 'command': args.command, 'schema_version': 1}))
        return 2
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
