#!/usr/bin/env python3
"""Prepare execution candidates from a registered bundle, without recording them.

API: import_results(project, run_id, bundle_id, pending_bundle=None) -> candidates.
pending_bundle is an in-memory validated registration for workflow collect-results.
CLI exits 0 for a candidate batch (including unresolved slots), 2 on invalid input.
Pass only the events array to workflow record; its current approval gates still apply.
"""
import argparse
from collections import Counter
from datetime import timedelta
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys

_spec = importlib.util.spec_from_file_location(
    'import_report_artifacts', Path(__file__).with_name('report_artifacts.py'))
reports = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reports)

RESULTS = {'passed': 'pass', 'failed': 'fail', 'timedOut': 'fail',
           'interrupted': 'fail', 'skipped': 'skip'}
OUTCOMES = {'expected', 'unexpected', 'flaky', 'skipped'}
REDACTED = '[REDACTED]'
SENSITIVE = re.compile(
    r'password|passwd|secret|token|cookie|authorization|bearer|credential|'
    r'api[_ -]?key|authentication|oauth|invalid_grant|access.denied|'
    r'login|log.in|sign.in|session|\b(?:401|403)\b|密码|口令|令牌|凭据|认证', re.I)
OPAQUE = re.compile(r'\b[A-Za-z0-9_+/=-]{24,}\b')
URL = re.compile(r'(?:https?|wss?)://\S+', re.I)


class ImportError(ValueError):
    """A static, safe diagnostic; never includes raw runner error text."""


def require(condition, code):
    if not condition:
        raise ImportError(code)


def digest(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()[:32]


def at_pointer(raw, pointer):
    value = raw
    for part in pointer.strip('/').split('/'):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def private_values(value):
    """Also redact labelled secrets if echoed without labels in matcher values."""
    found = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if SENSITIVE.search(str(key)):
                def strings(item):
                    if isinstance(item, str) and item:
                        found.add(item)
                    elif isinstance(item, dict):
                        for sub in item.values():
                            strings(sub)
                    elif isinstance(item, list):
                        for sub in item:
                            strings(sub)
                strings(child)
            found.update(private_values(child))
    elif isinstance(value, list):
        for child in value:
            found.update(private_values(child))
    return found


def safe_value(value, secrets, depth=0):
    if depth > 6:
        return '[TRUNCATED]'
    if isinstance(value, str):
        if SENSITIVE.search(value) or any(secret in value for secret in secrets):
            return REDACTED
        value = URL.sub('[URL REDACTED]', value)
        value = OPAQUE.sub(REDACTED, value)
        return ''.join(c for c in value[:1000] if c >= ' ' or c in '\n\t')
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else '[NONFINITE]'
    if isinstance(value, list):
        return [safe_value(v, secrets, depth + 1) for v in value[:20]]
    if isinstance(value, dict):
        # Do not emit even redacted credential keys: workflow prohibits those keys.
        if any(SENSITIVE.search(str(k)) for k in value):
            return REDACTED
        return {safe_value(str(k), secrets): safe_value(v, secrets, depth + 1)
                for k, v in list(value.items())[:20]
                if str(k).lower() not in {'body', 'response', 'headers', 'message', 'stack', 'value'}} or REDACTED
    return REDACTED


def safe_identity(value, secrets, *, coverage_mapped=False):
    # Coverage-authored IDs may be Unicode words; underscores delimit semantic parts.
    return (isinstance(value, str) and 0 < len(value) <= 256
            and not URL.search(value) and not any(s in value for s in secrets)
            and all(c.isprintable() for c in value)
            and ((not coverage_mapped and not SENSITIVE.search(value))
                 or (coverage_mapped
                     and re.fullmatch(r'\w[\w.-]*', value) is not None
                     and not re.search(r'(?:^|[._-])[A-Za-z0-9]{24,}(?=$|[._-])', value))))


def relative_file(project, value, root=None):
    if not isinstance(value, str) or not value or any(c in value for c in '\n\r\0?#'):
        return None
    value = value.replace('\\', '/')
    path = Path(value)
    path = (path if path.is_absolute() else (root or project) / path).resolve()
    return path.relative_to(project).as_posix() if path.is_relative_to(project) else None


def repeated_spec_identity(raw, locations, project_name):
    """Accept split repeat specs only with one source declaration and distinct repeats."""
    config = raw.get('config', {})
    projects = [project for project in config.get('projects', [])
                if project.get('name') == project_name]
    if len(projects) > 1:
        return False
    repeat_each = (projects[0]['repeatEach'] if projects and 'repeatEach' in projects[0]
                   else config.get('repeatEach'))
    if type(repeat_each) is not int or not 1 < len(locations) <= repeat_each:
        return False
    declarations, repeat_ids, spec_ids = set(), [], []
    for location in locations:
        spec = at_pointer(raw, location)
        line, column = spec.get('line'), spec.get('column')
        if type(line) is not int or line <= 0 or type(column) is not int or column <= 0:
            return False
        # JSONReporter may emit separate specs for repeats, but their describe ancestry stays the same.
        parts, ancestry = location.strip('/').split('/'), []
        offset = 0
        while offset < len(parts) and parts[offset] == 'suites':
            offset += 2
            suite = at_pointer(raw, '/' + '/'.join(parts[:offset]))
            ancestry.append(tuple(suite.get(key) for key in ('title', 'file', 'line', 'column')))
        declarations.add((tuple(ancestry), line, column))
        tests = [test for test in spec.get('tests', [])
                 if test.get('projectName', '') == project_name]
        if len(tests) != 1:
            return False
        repeat_ids.append(tests[0].get('repeatEachIndex'))
        spec_ids.append(spec.get('id'))
    if len(declarations) != 1:
        return False
    if any(index is not None for index in repeat_ids):
        return (all(type(index) is int and 0 <= index < repeat_each for index in repeat_ids)
                and len(set(repeat_ids)) == len(repeat_ids))
    # Some reporter versions omit repeatEachIndex; never invent indices for their events.
    return (all(isinstance(value, str) and value for value in spec_ids)
            and len(set(spec_ids)) == len(spec_ids))


def mapping(project, raw, cov, state, bundle):
    """Index exact identities; never use title substrings, case IDs or basenames."""
    root_value = raw.get('config', {}).get('rootDir')
    root = project
    if root_value:
        root = Path(root_value)
        root = (root if root.is_absolute() else project / root).resolve()
        require(root.is_relative_to(project), 'runner_root_outside_project')
    identities = {}
    projects, spec_locations = {}, {}
    for pointer, _ in reports.result_slots(raw):
        test = at_pointer(raw, pointer)
        spec = at_pointer(raw, pointer.rsplit('/tests/', 1)[0])
        key = (relative_file(project, spec.get('file'), root), spec.get('title'))
        identities[pointer] = (*key, test.get('projectName', ''))
        projects.setdefault(key, set()).add(test.get('projectName', ''))
        spec_locations.setdefault(identities[pointer], set()).add(pointer.rsplit('/tests/', 1)[0])
    ambiguous_specs = {identity for identity, locations in spec_locations.items()
                       if len(locations) > 1
                       and not repeated_spec_identity(raw, locations, identity[2])}
    indexed, coverage_index = {}, {}
    ids = Counter(c.get('test_id') for c in cov['cases'].values()
                  if c.get('implementation') != 'retired')
    for cid, case in cov['cases'].items():
        case_file = relative_file(project, case.get('file', case.get('spec')))
        coverage_index.setdefault((case_file, case.get('title')), []).append((cid, case))
    for pointer, (file, title, name) in identities.items():
        matches = []
        for cid, case in coverage_index.get((file, title), []) if file else []:
            names = [case[k] for k in ('project', 'projectName') if k in case]
            if names and any(n != name for n in names):
                continue
            matches.append((cid, case, bool(names)))
        reason = None
        if not matches:
            reason = 'no_exact_coverage_match'
        elif len(matches) != 1:
            reason = 'ambiguous_coverage_match'
        elif (file, title, name) in ambiguous_specs:
            reason = 'ambiguous_runner_spec_identity'
        else:
            cid, case, explicit_project = matches[0]
            if case.get('legacy_group'):
                reason = 'legacy_group_requires_explicit_review'
            elif not explicit_project and len(projects[(file, title)]) != 1:
                reason = 'coverage_project_required'
            elif case.get('implementation') == 'retired':
                reason = 'retired_case'
            elif case.get('scope') not in state['scopes'] or case.get('scope') not in bundle['scope']:
                reason = 'case_outside_bundle_scope'
            elif not case.get('test_id') or ids[case['test_id']] != 1:
                reason = 'missing_or_shared_test_id'
        indexed[pointer] = (reason, matches[0][:2] if len(matches) == 1 else None)
    return indexed, root


def failure_details(project, raw_spec, test, result, event, secrets, root):
    errors = result.get('errors') or ([result['error']] if result.get('error') else [])
    errors = [e for e in errors if isinstance(e, dict)]
    auth = event['setup'] or bool(SENSITIVE.search(json.dumps(errors, ensure_ascii=False)))
    matcher = next((e['matcherResult'] for e in errors if isinstance(e.get('matcherResult'), dict)), None)
    if matcher is None:
        matcher = next((e for e in errors if 'expected' in e or 'actual' in e), result)
    expected = REDACTED if auth else safe_value(matcher.get('expected', test.get('expectedStatus', 'passed')), secrets)
    actual = REDACTED if auth else safe_value(matcher.get('actual', result['status']), secrets)
    location = event['runner_test_pointer']
    source = next((e['location'] for e in errors if isinstance(e.get('location'), dict)),
                  result.get('location') if isinstance(result.get('location'), dict) else raw_spec)
    file = relative_file(project, source.get('file'), root)
    if file and safe_value(file, secrets) == file:
        location = file
        for key in ('line', 'column'):
            if type(source.get(key)) is int and source[key] > 0:
                location += ':' + str(source[key])
            else:
                break
    def display(value):
        if value is None or value == '':
            return '[not supplied]'
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
    return {'expected': display(expected), 'actual': display(actual),
            'phase': 'setup' if event['setup'] else 'runner_assertion',
            'classification': 'needs_decision', 'location': location,
            'cleanup': 'not_verified', 'evidence': event['evidence']}


def dependency_projects(raw, project_name):
    """Resolve runner project names/IDs only when each reference is unique."""
    configs = raw.get('config', {}).get('projects', [])
    def resolve(reference):
        hits = [i for i, c in enumerate(configs)
                if reference in (c.get('name'), c.get('id'))]
        return hits[0] if len(hits) == 1 else None
    start = resolve(project_name)
    seen, names = set(), set()
    def visit(index):
        if index in seen:
            return
        seen.add(index)
        deps = configs[index].get('dependencies', [])
        if not isinstance(deps, list):
            return
        for dep in deps:
            target = resolve(dep)
            if target is not None and target != start:
                names.add(configs[target].get('name', ''))
                visit(target)
    if start is not None:
        visit(start)
    return names


def apply_dependencies(raw, bundle, events, registered, slots):
    available = {(e['runner_test_pointer'], e.get('runner_result_index')): e
                 for e in registered + events}
    failures = {}
    for pointer, index in slots:
        test = at_pointer(raw, pointer)
        results = test.get('results', [])
        if (test.get('projectName') in bundle['setup_projects'] and results
                and test['status'] == 'unexpected' and index == len(results) - 1
                and RESULTS.get(results[index]['status']) == 'fail'):
            failures[(pointer, index)] = test['projectName']
    rejected = {}
    for event in events:
        if event['setup'] or event['result'] != 'skip':
            continue
        pointer, index = event['runner_test_pointer'], event['runner_result_index']
        test = at_pointer(raw, pointer)
        spec = at_pointer(raw, pointer.rsplit('/tests/', 1)[0])
        annotations = test.get('annotations', []) + spec.get('annotations', [])
        if any(a.get('type') in {'skip', 'fixme'} for a in annotations):
            continue
        bindings = [b for b in bundle.get('dependency_bindings', [])
                    if b.get('runner_test_pointer') == pointer and b.get('runner_result_index') == index]
        proof = [bundle['execution_result']]
        if bindings:
            binding = bindings[0]
            target = (binding.get('setup_test_pointer'), binding.get('setup_result_index'))
            proof = binding.get('evidence', [])
            if (len(bindings) != 1 or target not in failures or not proof
                    or not isinstance(proof, list)
                    or any(p not in bundle['artifact_sha256'] for p in proof)):
                rejected[(pointer, index)] = 'invalid_dependency_binding'
                continue
            targets = [target]
        else:
            deps = dependency_projects(raw, test.get('projectName', ''))
            targets = [s for s, name in failures.items() if name in deps]
        if targets:
            # The setup event must precede its dependent candidate in record input.
            target = next((s for s in sorted(targets) if s in available), None)
            if target is None:
                rejected[(pointer, index)] = 'dependency_blocker_unresolved'
                continue
            event.update(result='notrun', runner_result='skip',
                         blocked_by=available[target]['execution_id'],
                         reason='Runner project dependency or evidenced slot binding to failed Setup',
                         dependency_evidence=proof)
    return rejected


def import_results(project, run_id, bundle_id, pending_bundle=None):
    """Read immutable inputs and prepare candidates; do not acquire a writing lock."""
    project = Path(project).resolve()
    require(project.is_dir(), 'project_missing')
    require(all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]+', v)
                for v in (run_id, bundle_id)), 'invalid_run_or_bundle_id')
    state_path = project / 'test/.cache/runs' / run_id / 'state.json'
    cov_path = project / 'test/plans/coverage.json'
    try:
        require(state_path.resolve().is_relative_to(project)
                and cov_path.resolve().is_relative_to(project), 'input_path_outside_project')
        state_bytes, cov_bytes = state_path.read_bytes(), cov_path.read_bytes()
        state, cov = json.loads(state_bytes), json.loads(cov_bytes)
        require(state['run_id'] == run_id, 'state_run_id_mismatch')
        if pending_bundle is not None:
            require(isinstance(pending_bundle, dict) and pending_bundle.get('bundle_id') == bundle_id,
                    'pending_bundle_id_mismatch')
            existing = state.get('report_bundles', {}).get(bundle_id)
            require(existing is None or existing == pending_bundle, 'pending_bundle_conflicts_with_registration')
            state.setdefault('report_bundles', {})[bundle_id] = pending_bundle
        bundle = state.get('report_bundles', {}).get(bundle_id)
        require(bundle is not None, 'bundle_not_registered')
        require(bundle['bundle_id'] == bundle_id, 'bundle_id_mismatch')
        require(set(bundle['scope']) <= set(state['scopes']), 'bundle_scope_outside_run')
        reports.recheck_bundle(project, run_id, bundle, require_plan=state.get('native_merge_required', False))
        raw_bytes = reports.path_in(project, bundle['execution_result']).read_bytes()
        require(hashlib.sha256(raw_bytes).hexdigest() == bundle['artifact_sha256'][bundle['execution_result']],
                'bundle_changed_during_import')
        raw = json.loads(raw_bytes)
        slots = reports.result_slots(raw)
        registered = [e for e in state.get('executions', []) if e.get('bundle_id') == bundle_id]
        occupied = set()
        for event in registered:
            slot = reports.check_execution(project, bundle, event)
            require(slot in slots and slot not in occupied, 'invalid_registered_slot')
            occupied.add(slot)
        matches, root = mapping(project, raw, cov, state, bundle)
        secrets = private_values(raw) | private_values(bundle)
        events, unresolved = [], []
        counters = {}
        existing_ids = {e['execution_id'] for e in state.get('executions', [])}
        for e in state.get('executions', []):
            key = (e['setup'], e['test_id'])
            counters[key] = max(counters.get(key, 0), e['attempt'])
        counts = {'raw_slots': len(slots), 'mapped': 0, 'unresolved': 0,
                  'already_registered': len(occupied), 'dependency_blocked': 0,
                  'runner_counts': {s: raw['stats'].get(s, 0) for s in sorted(OUTCOMES)}}
        for kind in ('setup', 'business'):
            counts[kind] = dict(raw_slots=0, mapped=0, unresolved=0, already_registered=0)

        def reject(pointer, index, setup, reason):
            unresolved.append({'bundle_id': bundle_id, 'runner_test_pointer': pointer,
                               'runner_result_index': index, 'setup': setup, 'reason': reason,
                               'evidence': [bundle['execution_result']]})

        # Numeric pointer order keeps retries/repeats deterministic, not lexicographic 10-before-2.
        ordered = sorted(slots, key=lambda s: ([int(p) for p in s[0].split('/') if p.isdigit()],
                                               -1 if s[1] is None else s[1]))
        for pointer, index in ordered:
            test = at_pointer(raw, pointer)
            spec = at_pointer(raw, pointer.rsplit('/tests/', 1)[0])
            setup = test.get('projectName') in bundle['setup_projects']
            kind = 'setup' if setup else 'business'
            counts[kind]['raw_slots'] += 1
            if (pointer, index) in occupied:
                counts[kind]['already_registered'] += 1
                continue
            if test.get('status') not in OUTCOMES:
                reject(pointer, index, setup, 'unsupported_runner_test_status')
                continue
            reason, match = matches[pointer]
            if reason and not setup:
                reject(pointer, index, setup, reason)
                continue
            tid = ('setup-' + digest(spec.get('file'), spec.get('title'), test.get('projectName'))
                   if setup else match[1]['test_id'])
            if not setup and not all(safe_identity(v, secrets, coverage_mapped=True)
                                     for v in (match[0], tid)):
                reject(pointer, index, setup, 'unsafe_coverage_identity')
                continue
            event = {'type': 'execution', 'execution_id': 'import-' + digest(run_id, bundle_id, pointer, index),
                     'test_id': tid, 'case_ids': [] if setup else [match[0]], 'setup': setup,
                     'method': 'automation', 'bundle_id': bundle_id,
                     'runner_test_pointer': pointer, 'runner_result_index': index,
                     'runner_test_status': test['status'], 'evidence': [bundle['execution_result']]}
            if event['execution_id'] in existing_ids:
                reject(pointer, index, setup, 'execution_id_conflict')
                continue
            result = test['results'][index] if index is not None else None
            if result is None:
                if test['status'] != 'skipped':
                    reject(pointer, index, setup, 'no_result_for_non_skipped_target')
                    continue
                event.update(result='skip', runner_status=None, runner_retry=None,
                             runner_attempt=None,
                             started_at=bundle['started_at'], ended_at=bundle['ended_at'],
                             timing_source='invocation_window', reason='Runner target has no result')
            else:
                if result.get('status') not in RESULTS:
                    reject(pointer, index, setup, 'unsupported_runner_status')
                    continue
                try:
                    start = reports.timestamp(result['startTime'])
                    duration = result['duration']
                    require(type(duration) in (int, float) and math.isfinite(duration)
                            and duration >= 0, 'invalid_result_timing')
                    derived_end = start + timedelta(milliseconds=duration)
                    end = reports.timestamp(result['endTime']) if 'endTime' in result else derived_end
                    require(abs((end - derived_end).total_seconds()) <= .001, 'conflicting_result_end_time')
                    require(reports.timestamp(bundle['started_at']) <= start <= end
                            and end.timestamp() <= reports.timestamp(bundle['ended_at']).timestamp() + 1,
                            'result_outside_invocation')
                    retry = result.get('retry', 0)
                    require(type(retry) is int and retry >= 0, 'invalid_retry')
                    attempt = result.get('attempt', retry + 1)
                    require(type(attempt) is int and attempt >= 1, 'invalid_raw_attempt')
                except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                    reject(pointer, index, setup, 'invalid_result_timing_or_retry')
                    continue
                event.update(result=RESULTS[result['status']], runner_status=result['status'],
                             runner_retry=retry, runner_attempt=attempt,
                             duration_ms=duration, started_at=result['startTime'],
                             ended_at=result.get('endTime', end.isoformat()), timing_source='runner_result')
                if event['result'] == 'fail':
                    event['failure_details'] = failure_details(project, spec, test, result, event, secrets, root)
            if 'repeatEachIndex' in test:
                if type(test['repeatEachIndex']) is not int or test['repeatEachIndex'] < 0:
                    reject(pointer, index, setup, 'invalid_repeat_index')
                    continue
                event['runner_repeat_each_index'] = test['repeatEachIndex']
            key = (setup, tid)
            counters[key] = max(counters.get(key, 0) + 1, event['runner_attempt'] or 1)
            event['attempt'] = counters[key]
            reports.check_execution(project, bundle, event, None if setup else match[1]['title'])
            events.append(event)
        rejected = apply_dependencies(raw, bundle, events, registered, slots)
        kept = []
        for event in events:
            slot = (event['runner_test_pointer'], event['runner_result_index'])
            if slot in rejected:
                reject(*slot, event['setup'], rejected[slot])
            else:
                reports.check_execution(project, bundle, event)
                kept.append(event)
        events = sorted(kept, key=lambda e: not e['setup'])
        for items, field in ((events, 'mapped'), (unresolved, 'unresolved')):
            counts[field] = len(items)
            for e in items:
                counts['setup' if e['setup'] else 'business'][field] += 1
        counts['dependency_blocked'] = sum(bool(e.get('blocked_by')) for e in events)
        require(counts['raw_slots'] == counts['mapped'] + counts['unresolved'] + counts['already_registered'],
                'slot_accounting_mismatch')
        reports.recheck_bundle(project, run_id, bundle, require_plan=state.get('native_merge_required', False))
        require(state_bytes == state_path.read_bytes() and cov_bytes == cov_path.read_bytes(),
                'inputs_changed_during_import')
        return {'events': events, 'unresolved': unresolved, 'counts': counts}
    except ImportError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, OSError, OverflowError):
        # Validator exceptions may embed artifact paths or arbitrary runner content.
        raise ImportError('invalid_input_or_bundle_integrity') from None


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ImportError('invalid_cli_arguments')


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--output')
    try:
        args = parser.parse_args(argv)
        result = import_results(args.project, args.run, args.bundle)
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if args.output:
            # Exclusive creation prevents --output from clobbering ANY existing input.
            with Path(args.output).open('x', encoding='utf-8') as stream:
                stream.write(text)
        print(text, end='')
        return 0
    except (ImportError, OSError) as error:
        print(json.dumps({'error': str(error) if isinstance(error, ImportError) else 'output_not_writable_or_exists'}))
        return 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
