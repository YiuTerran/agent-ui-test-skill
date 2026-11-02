#!/usr/bin/env python3
"""Durable scheduling index. Never runs tests, grants permissions, or accepts a run."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from platform_support import file_lock, relative_parts, sync_directory

RESIDUAL_INDEX = 'test/.cache/resource-residuals.json'
RESOLVED = {'absence_verified', 'retained_verified'}
RESOURCE_STATES = {'intent', 'confirmed', 'ambiguous', 'cleanup_failed'} | RESOLVED


@contextmanager
def residual_lock(project):
    path = safe(project, RESIDUAL_INDEX, cache=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a+b') as stream:
        with file_lock(stream.fileno()):
            yield


def residual_index(project):
    path = safe(project, RESIDUAL_INDEX, cache=True)
    value = read(path) if path.exists() else {'schema_version': 1, 'resources': {}, 'receipts': {}}
    check(isinstance(value, dict) and value.get('schema_version') == 1 and isinstance(value.get('resources'), dict)
          and isinstance(value.get('receipts'), dict), 'invalid residual index; reconcile before dispatch')
    for key, item in value['resources'].items():
        check(isinstance(item, dict) and {'resource_environment', 'kind', 'id', 'status', 'first_seen_run'} <= set(item)
              and all(isinstance(item[k], str) for k in ['resource_environment', 'kind', 'id', 'status', 'first_seen_run'])
              and key == digest([item['resource_environment'], item['kind'], item['id']])
              and item['status'] in RESOURCE_STATES, 'invalid residual entry; reconcile before dispatch')
        check('environment_is_stable' not in item or type(item['environment_is_stable']) is bool,
              'invalid residual environment provenance')
    return value


def resource_access(value):
    check(isinstance(value, dict) and set(value) == {'mode', 'kinds', 'dependencies'}
          and value['mode'] in {'readonly', 'write'}, 'invalid resource_access')
    for field in ['kinds', 'dependencies']:
        check(isinstance(value[field], list) and all(isinstance(v, str) for v in value[field])
              and len(value[field]) == len(set(value[field])), 'invalid resource kind list')
        for kind in value[field]:
            identifier(kind)
    check(value['mode'] != 'readonly' or not value['kinds'], 'readonly cannot declare writes')
    return value


def residual_matches(item, environment):
    # Legacy producers used deployment IDs, which cannot prove tenant isolation.
    return (environment is None or item.get('environment_is_stable') is not True
            or item['resource_environment'] == environment)


def residual_kinds(index, environment):
    return {r['kind'] for r in index['resources'].values() if r['status'] not in RESOLVED
            and residual_matches(r, environment)}


def residual_gate(pending, access):
    if access is not None:
        resource_access(access)
    if not pending or (access is not None and access['mode'] == 'readonly'):
        return []
    kinds = None if access is None else set(access['kinds']) | set(access['dependencies'])
    return sorted(pending if kinds is None else pending & kinds)


def residual_summary(index, environment, run_id):
    resources = [r for r in index['resources'].values()
                 if residual_matches(r, environment)]
    pending = [r for r in resources if r['status'] not in RESOLVED]
    current = sum(r['first_seen_run'] == run_id for r in pending)
    return {'current_run_unresolved': current, 'historical_unresolved': len(pending) - current,
            'cumulative_unresolved': len(pending),
            'current_run_absence_verified': sum(r['status'] == 'absence_verified'
                and r.get('resolved_by_run') == run_id for r in resources),
            'current_run_retained_verified': sum(r['status'] == 'retained_verified'
                and r.get('resolved_by_run') == run_id for r in resources)}


def predispatch_gate(project, request):
    check(isinstance(request, dict) and set(request) <= {'resource_environment', 'resource_access'},
          'gate accepts resource_environment and resource_access only')
    if 'resource_environment' in request:
        identifier(request['resource_environment'])
    index = residual_index(project)
    blocked = residual_gate(residual_kinds(index, request.get('resource_environment')),
                            request.get('resource_access'))
    return {'allowed': not blocked, 'blocked_kinds': blocked,
            'reason': 'resource_cleanup_unresolved' if blocked else 'no_residual_conflict',
            'residual_index_sha256': digest(index)}


def config_baseline(project, resource, prior):
    """Pin the original before resolution; a verified flag cannot prove restoration."""
    fields = {'original_snapshot', 'restored_snapshot'}
    if resource['kind'] != 'shared_config':
        check(not fields & set(resource), 'config snapshots belong to shared_config only')
        return {}
    original = resource.get('original_snapshot', prior.get('original_snapshot'))
    if 'original_snapshot' in resource:
        check(isinstance(original, str) and original, 'original_snapshot path required')
    if resource['status'] in RESOLVED:
        check(prior.get('original_sha256'), 'shared_config original must be registered before resolution')
    if prior.get('original_snapshot'):
        check(original == prior['original_snapshot'], 'shared_config original snapshot cannot change')
    if not original:
        check('restored_snapshot' not in resource, 'restored_snapshot belongs to resolution only')
        return {}
    path = safe(project, original)
    check(path.is_file(), 'shared_config original snapshot missing')
    checksum = digest(read(path))
    check(not prior.get('original_sha256') or checksum == prior['original_sha256'],
          'shared_config original snapshot changed after registration')
    if resource['status'] in RESOLVED:
        check(resource['status'] == 'absence_verified', 'shared_config must restore original, not retain')
        restored = safe(project, resource.get('restored_snapshot'))
        check(restored.is_file() and digest(read(restored)) == checksum,
              'shared_config restored readback differs from original')
    else:
        check('restored_snapshot' not in resource, 'restored_snapshot belongs to resolution only')
    return {'original_snapshot': original, 'original_sha256': checksum}


def persist_cleanup(project, journal, spec, event):
    """Commit the safety index first; retry receipts bridge a crash before checkpoint append."""
    check(set(event) == {'type', 'event_id', 'resources'}, 'invalid cleanup_state fields')
    identifier(event['event_id'])
    index = residual_index(project)
    receipt = digest([journal, event['event_id']])
    content = digest(event)
    if receipt in index['receipts']:
        check(index['receipts'][receipt] == content, 'cleanup event_id reused with different content')
        return
    environment = spec.get('resource_environment', spec['environment_id'])
    check(isinstance(event['resources'], list) and event['resources'], 'cleanup resources required')
    entries, seen = [], set()
    for resource in event['resources']:
        check(isinstance(resource, dict) and set(resource) <= {'kind', 'id', 'status', 'evidence', 'retention',
                                                            'original_snapshot', 'restored_snapshot'}
              and {'kind', 'id', 'status', 'evidence'} <= set(resource), 'invalid cleanup resource fields')
        identifier(resource['kind'])
        check(isinstance(resource['id'], str) and resource['id'] and len(resource['id']) <= 512,
              'resource id required')
        check(resource['status'] in RESOURCE_STATES, 'invalid cleanup resource status')
        key = digest([environment, resource['kind'], resource['id']])
        check(key not in seen, 'duplicate cleanup resource identity')
        seen.add(key)
        prior = index['resources'].get(key, {})
        baseline = config_baseline(project, resource, prior)
        check(isinstance(resource['evidence'], list) and resource['evidence'], 'cleanup evidence required')
        proofs = []
        for name in resource['evidence']:
            path = safe(project, name)
            check(path.is_file(), 'cleanup evidence missing')
            if resource['status'] in RESOLVED:
                # Only an explicit resource readback receipt can resolve debt, not a test result.
                proofs.append(read(path))
        if resource['status'] == 'retained_verified':
            check(resource.get('retention') in {'pooled', 'disabled'}, 'verified retention mode required')
            check(resource['kind'] != 'shared_config', 'shared_config requires restored absence proof')
        else:
            check('retention' not in resource, 'retention belongs to retained_verified only')
        if resource['status'] in RESOLVED:
            expected = dict(resource_environment=environment, kind=resource['kind'], id=resource['id'],
                            status=resource['status'], verified=True)
            if 'retention' in resource:
                expected['retention'] = resource['retention']
            if resource['kind'] == 'shared_config':
                expected['restored_snapshot'] = resource['restored_snapshot']
            check(any(isinstance(p, dict) and all(type(p.get(k)) is type(v) and p.get(k) == v
                      for k, v in expected.items()) for p in proofs), 'matching resource readback proof required')
        entries.append((key, dict(resource, **baseline, resource_environment=environment,
            environment_is_stable=('resource_environment' in spec
                                   and (not prior or prior.get('environment_is_stable') is True)))))
    for key, resource in entries:
        prior = index['resources'].get(key, {})
        run_id = spec.get('resource_run_id', journal)
        index['resources'][key] = dict(resource, journal=journal, event_id=event['event_id'],
            first_seen_run=prior.get('first_seen_run', run_id),
            resolved_by_run=run_id if resource['status'] in RESOLVED else None)
    index['receipts'][receipt] = content
    path = safe(project, RESIDUAL_INDEX, cache=True)
    fd, temporary = tempfile.mkstemp(prefix='.resource-residuals-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(index, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check(value, message):
    if not value:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def safe(project, name, cache=False):
    check(isinstance(name, str) and name and not Path(name).is_absolute(), 'project-relative path required')
    relative_parts(name)
    check(not cache or name.startswith('test/.cache/'), 'checkpoint output must be in test/.cache/')
    path = project / name
    check(path.resolve().is_relative_to(project), 'path escapes project')
    for part in [path, *path.parents]:
        check(not part.is_symlink() and not (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400),
              'symlink/reparse path prohibited')
        if part == project:
            break
    return path


def identifier(value):
    check(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', value), 'invalid identifier')
    return value


def manifest(project, request):
    check({'environment_id', 'cases'} <= set(request) <= {'environment_id', 'cases', 'resource_environment', 'resource_run_id'},
          'manifest requires environment_id and cases; optional resource_environment')
    identifier(request['environment_id'])
    if 'resource_environment' in request:
        identifier(request['resource_environment'])
    if 'resource_run_id' in request:
        identifier(request['resource_run_id'])
    check(isinstance(request['cases'], list) and request['cases'], 'nonempty cases required')
    seen, targets = set(), set()
    for case in request['cases']:
        required = {'case_id', 'target', 'inputs', 'dependencies', 'conflicts', 'reviewed'}
        check(required <= set(case) <= required | {'resource_access'}, 'invalid case fields')
        if 'resource_access' in case:
            resource_access(case['resource_access'])
        cid = identifier(case['case_id'])
        check(cid not in seen, 'duplicate case_id')
        seen.add(cid)
        check(case['reviewed'] is True, 'all selected cases must be reviewed before checkpoint initialization')
        check(set(case['target']) == {'file', 'project', 'title_path'}, 'exact file/project/title_path required')
        check(isinstance(case['target']['file'], str) and case['target']['file']
              and isinstance(case['target']['project'], str), 'invalid test file/project target')
        title_path = case['target']['title_path']
        check(isinstance(title_path, list) and title_path and all(isinstance(v, str) for v in title_path)
              and title_path[-1], 'complete reporter titlePath required')
        target = (case['target']['file'], case['target']['project'], tuple(title_path))
        check(target not in targets, 'ambiguous target mapping')
        targets.add(target)
        check(safe(project, case['target']['file']).is_file(), 'test file missing')
        check(isinstance(case['inputs'], list) and case['inputs'] and case['target']['file'] in case['inputs'],
              'inputs must explicitly include the test and its reviewed dependency closure')
        for name in case['inputs']:
            check(safe(project, name).is_file(), 'input file missing')
        for key in ['dependencies', 'conflicts']:
            check(isinstance(case[key], list) and len(case[key]) == len(set(case[key])), 'invalid dependency/conflict list')
            for value in case[key]:
                identifier(value)
    return request


def fingerprints(project, spec):
    # Hash each shared input once, not once per consumer. An incomplete closure cannot certify reuse.
    files = {}
    for case in spec['cases']:
        for name in case['inputs']:
            if name not in files:
                path = safe(project, name)
                check(path.is_file(), 'input missing; review discovery before scheduling')
                files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {c['case_id']: digest({'environment': spec['environment_id'], 'target': c['target'],
                                 'files': {name: files[name] for name in c['inputs']}})
            for c in spec['cases']}


def replay(path):
    rows, previous = [], None
    if not path.exists():
        return rows
    data = path.read_bytes()
    check(not data or data.endswith(b'\n'), 'incomplete journal tail: preserve it; do not retry writes or truncate automatically')
    for line in data.splitlines():
        row = json.loads(line)
        checksum = row.pop('checksum')
        check(row['sequence'] == len(rows) + 1 and row['previous'] == previous and digest(row) == checksum,
              'journal sequence/hash mismatch')
        row['checksum'] = checksum
        rows.append(row)
        previous = checksum
    return rows


def journal_row(rows, event):
    row = {'sequence': len(rows) + 1, 'previous': rows[-1]['checksum'] if rows else None, 'event': event}
    row['checksum'] = digest(row)
    return row


def append(path, rows, event):
    row = journal_row(rows, event)
    with path.open('ab') as stream:
        stream.write((json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())
    rows.append(row)


def state(rows):
    check(rows and rows[0]['event']['type'] == 'init', 'checkpoint not initialized')
    spec = rows[0]['event']['manifest']
    cases = {c['case_id']: {'status': 'pending', 'repairs': 0, 'confirmations': 0} for c in spec['cases']}
    attempts, gates, ids = {}, {}, {}
    for row in rows[1:]:
        e = row['event']; kind = e['type']; cid = e.get('case_id')
        ids[e['event_id']] = e
        if kind == 'claim':
            attempts[e['attempt_id']] = dict(e, closed=False)
            for selected in e['case_ids']:
                cases[selected].update(status='reserved', attempt_id=e['attempt_id'])
        elif kind == 'case_end':
            cases[cid].update(status=e['status'], fingerprint=attempts[e['attempt_id']]['fingerprints'][cid])
        elif kind == 'interruption':
            for selected in e['unfinished_case_ids']:
                cases[selected]['status'] = 'unfinished'
        elif kind == 'close':
            attempts[e['attempt_id']]['closed'] = True
            for selected in attempts[e['attempt_id']]['case_ids']:
                if cases[selected]['status'] == 'unfinished':
                    cases[selected]['status'] = 'pending'
        elif kind in {'repair', 'confirm'}:
            # Keep historical counts for existing journals, without limiting useful work.
            cases[cid]['repairs' if kind == 'repair' else 'confirmations'] += 1
            cases[cid]['status'] = 'pending'
        elif kind in {'block', 'unblock'}:
            if kind == 'block':
                gates[e['dependency']] = e['evidence']
            else:
                gates.pop(e['dependency'], None)
    return spec, cases, attempts, gates, ids


def plan(project, rows, workers=4, journal=None):
    check(isinstance(workers, int) and not isinstance(workers, bool) and 1 <= workers <= 64, 'workers must be 1..64')
    spec, cases, attempts, gates, _ = state(rows)
    residuals = residual_index(project)
    environment = spec.get('resource_environment')
    pending_kinds = residual_kinds(residuals, environment)
    current = fingerprints(project, spec)
    active = [a for a in attempts.values() if not a['closed']]
    held_cases = {cid for a in active for cid in a['case_ids']}
    held_groups = {group for c in spec['cases'] if c['case_id'] in held_cases for group in c['conflicts']}
    slots = max(0, workers - len(held_cases))
    ready, blocked, reused = [], {}, []
    for c in spec['cases']:
        cid = c['case_id']; item = cases[cid]
        if cid in held_cases:
            blocked[cid] = 'inflight_or_cleanup_unverified'
        elif residual_gate(pending_kinds, c.get('resource_access')):
            blocked[cid] = 'resource_cleanup_unresolved'
        elif set(c['dependencies']) & gates.keys():
            blocked[cid] = 'dependency_circuit_open'
        elif item['status'] == 'passed' and item.get('fingerprint') == current[cid]:
            reused.append(cid)
        elif item['status'] not in {'pending', 'passed'}:
            blocked[cid] = 'explicit_diagnosis_or_confirmation_required'
        elif set(c['conflicts']) & held_groups:
            blocked[cid] = 'conflict_group_busy'
        elif len(ready) < slots:
            ready.append(cid)
            held_groups.update(c['conflicts'])
        else:
            blocked[cid] = 'capacity_wait'
    return {'ready': ready, 'reused_observations': reused, 'blocked': blocked,
            'counts': {'ready': len(ready), 'reused_observations': len(reused), 'blocked': len(blocked),
                       'total': len(cases)},
            'formal_regression_completeness': 'not_evaluated',
            'resource_residuals': residual_summary(residuals, environment, spec.get('resource_run_id', journal)),
            'fingerprints': current}


FIELDS = {
    'case_end': {'attempt_id', 'case_id', 'status', 'evidence'},
    'close': {'attempt_id', 'cleanup_verified', 'evidence'},
    'interruption': {'attempt_id', 'unfinished_case_ids', 'process_exited', 'results_reconciled', 'evidence'},
    'repair': {'case_id', 'root_cause_id', 'evidence'},
    'confirm': {'case_id', 'root_cause_id', 'evidence'},
    'block': {'dependency', 'evidence'},
    'unblock': {'dependency', 'evidence'},
}


def record(project, rows, event):
    kind = event.get('type')
    check(kind in FIELDS and set(event) == {'type', 'event_id'} | FIELDS[kind], 'unknown event or fields')
    identifier(event['event_id'])
    spec, cases, attempts, gates, ids = state(rows)
    if event['event_id'] in ids:
        check(ids[event['event_id']] == event, 'event_id reused with different content')
        return False
    check(isinstance(event['evidence'], list) and event['evidence'], 'evidence required')
    for name in event['evidence']:
        check(safe(project, name).is_file(), 'evidence file missing')
    if kind in {'case_end', 'close', 'interruption'}:
        check(event['attempt_id'] in attempts and not attempts[event['attempt_id']]['closed'], 'attempt not active')
    if kind == 'case_end':
        cid = event['case_id']
        check(cid in attempts[event['attempt_id']]['case_ids'] and cases[cid]['status'] == 'reserved', 'case not reserved by attempt')
        check(event['status'] in {'passed', 'failed', 'skipped', 'timedOut', 'interrupted'}, 'invalid case status')
    elif kind == 'close':
        check(event['cleanup_verified'] is True, 'cleanup proof required; interruption does not release conflicts automatically')
        check(not any(cases[cid]['status'] == 'reserved' for cid in attempts[event['attempt_id']]['case_ids']),
              'attempt results incomplete; ingest case_end results before close, or explicitly reconcile a real interruption')
    elif kind == 'interruption':
        check(event['process_exited'] is True and event['results_reconciled'] is True,
              'interruption requires exited process and reconciled available results')
        unfinished = event['unfinished_case_ids']
        check(isinstance(unfinished, list) and unfinished and all(isinstance(cid, str) for cid in unfinished)
              and len(set(unfinished)) == len(unfinished), 'nonempty unique unfinished_case_ids required')
        pending = {cid for cid in attempts[event['attempt_id']]['case_ids'] if cases[cid]['status'] == 'reserved'}
        check(set(unfinished) == pending, 'unfinished_case_ids must exactly match cases without case_end')
    elif kind in {'repair', 'confirm'}:
        cid = event['case_id']; identifier(event['root_cause_id'])
        check(cid in cases and cases[cid]['status'] in {'failed', 'skipped', 'timedOut', 'interrupted'}, 'requires a diagnosed terminal case')
        check(not any(cid in a['case_ids'] for a in attempts.values() if not a['closed']), 'close cleanup obligations first')
    elif kind in {'block', 'unblock'}:
        identifier(event['dependency'])
        check(any(event['dependency'] in c['dependencies'] for c in spec['cases']), 'unknown dependency')
        check(kind != 'unblock' or event['dependency'] in gates, 'dependency not blocked')
    return True


def dispatch(project, journal, command, request, workers=4):
    project = Path(project).resolve()
    if command == 'gate':
        with residual_lock(project):
            return predispatch_gate(project, request)
    path = safe(project, journal, cache=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a+b') as lock:
        with file_lock(lock.fileno()), residual_lock(project):
            rows = replay(path)
            if command == 'init':
                spec = manifest(project, request)
                if rows:
                    check(rows[0]['event']['manifest'] == spec, 'manifest is immutable; create a new checkpoint for a changed target/environment')
                else:
                    append(path, rows, {'type': 'init', 'manifest': spec})
            elif command == 'record':
                if request.get('type') == 'cleanup_state':
                    spec, _, _, _, ids = state(rows)
                    if request.get('event_id') in ids:
                        check(ids[request['event_id']] == request, 'event_id reused with different content')
                    persist_cleanup(project, journal, spec, request)
                    if request['event_id'] not in ids:
                        append(path, rows, request)
                elif record(project, rows, request):
                    append(path, rows, request)
            elif command == 'ingest':
                check(set(request) == {'attempt_id', 'stream'}, 'ingest requires attempt_id and stream')
                source = safe(project, request['stream'], cache=True)
                data = source.read_bytes()
                check(data.endswith(b'\n'), 'reporter stream incomplete; preserve and resume the same process')
                entries = [json.loads(line) for line in data.splitlines()]
                check(all(e.get('type') == 'case_end' and e.get('attempt_id') == request['attempt_id']
                          for e in entries), 'reporter stream has unmapped targets or mismatched attempt')
                # Validate the entire batch before writing any prefix that might look reusable.
                pending, shadow = [], list(rows)
                for entry in entries:
                    event = dict(entry, evidence=[request['stream']])
                    if record(project, shadow, event):
                        shadow.append(journal_row(shadow, event))
                        pending.append(event)
                for event in pending:
                    append(path, rows, event)
            elif command == 'claim':
                check(set(request) == {'attempt_id', 'case_ids'}, 'claim requires attempt_id and case_ids')
                identifier(request['attempt_id'])
                _, _, attempts, _, _ = state(rows)
                check(request['attempt_id'] not in attempts, 'attempt ID cannot be reused')
                selection = plan(project, rows, workers, journal)
                check(isinstance(request['case_ids'], list) and request['case_ids'] and
                      len(request['case_ids']) == len(set(request['case_ids'])) and
                      set(request['case_ids']) <= set(selection['ready']), 'claim only ready cases from conflict-aware plan')
                append(path, rows, dict(request, type='claim', event_id='claim:' + request['attempt_id'],
                                       fingerprints={cid: selection['fingerprints'][cid] for cid in request['case_ids']}))
            elif command != 'plan':
                raise ValueError('unknown command')
            result = plan(project, rows, workers, journal)
            result.pop('fingerprints')
            return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'plan', 'claim', 'record', 'ingest', 'gate'])
    parser.add_argument('--project', required=True)
    parser.add_argument('--journal')
    parser.add_argument('--input')
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args(argv)
    try:
        request = read(Path(args.input)) if args.input else {}
        check(args.command == 'plan' or args.input, '--input required')
        check(args.command == 'gate' or args.journal, '--journal required except for gate')
        result = dispatch(args.project, args.journal, args.command, request, args.workers)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result.get('allowed') is False else 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({'valid': False, 'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
