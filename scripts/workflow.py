#!/usr/bin/env python3
"""Local, evidence-indexed E2E workflow recorder. No external execution."""
import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile

_platform_spec = importlib.util.spec_from_file_location(
    'e2e_workflow_platform', Path(__file__).with_name('platform_support.py'))
platform_support = importlib.util.module_from_spec(_platform_spec)
_platform_spec.loader.exec_module(platform_support)

_report_spec = importlib.util.spec_from_file_location('e2e_report_artifacts', Path(__file__).with_name('report_artifacts.py'))
report_artifacts = importlib.util.module_from_spec(_report_spec)
_report_spec.loader.exec_module(report_artifacts)

_retention_spec = importlib.util.spec_from_file_location('e2e_retention', Path(__file__).with_name('retention.py'))
retention = importlib.util.module_from_spec(_retention_spec)
_retention_spec.loader.exec_module(retention)

SCHEMA = 1
LEASE_SECONDS = platform_support.DEFAULT_RUN_LEASE_SECONDS
TERMINAL = {'fully_accepted', 'completed_with_failures', 'partially_blocked', 'aborted'}
DONE = {'approved', 'complete'}
RESULTS = {'pass', 'fail', 'skip', 'notrun'}
CASE_ACTIONS = {'added', 'updated', 'retired', 'unchanged', 'needs_decision'}
SECRET_KEYS = {'password', 'passwd', 'secret', 'token', 'access_token', 'refresh_token', 'cookie', 'authorization', 'api_key'}


def now():
    return datetime.now(timezone.utc).isoformat()


def dt(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError):
        raise ValueError('timestamp must be timezone-aware ISO8601')


def check(condition, message):
    if not condition:
        raise ValueError(message)


def load(path):
    with path.open(encoding='utf-8') as stream:
        return json.load(stream)


def atomic(path, data, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    check(not exclusive or not path.exists(), 'refusing to overwrite existing file: ' + str(path))
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2) + '\n'
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            if os.name == 'nt':
                os.rename(temp, path)
            else:
                os.link(temp, path)
                os.unlink(temp)
        else:
            os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def safe_path(project, value):
    check(isinstance(value, str) and value and not Path(value).is_absolute(), 'path must be project-relative')
    platform_support.relative_parts(value)
    project = Path(project).resolve()
    target = (project / value).resolve()
    check(target.is_relative_to(project), 'path escapes project')
    return target


def no_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            check(key.lower() not in SECRET_KEYS, 'credential field is prohibited: ' + key)
            no_secrets(item)
    elif isinstance(value, list):
        for item in value:
            no_secrets(item)


@contextmanager
def project_lock(project):
    directory = project / 'test/.cache'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'workflow.lock').open('a+b') as stream:
        with platform_support.file_lock(stream.fileno()):
            yield


def parse_spec(path):
    text = path.read_text(encoding='utf-8')
    matches = re.findall(r'```json workflow-config\s*\n(.*?)\n```', text, flags=re.S)
    check(len(matches) == 1, 'spec.md must contain exactly one json workflow-config block')
    return json.loads(matches[0])


def assets(project):
    try:
        spec, cov = parse_spec(project / 'test/spec.md'), load(project / 'test/plans/coverage.json')
        check(isinstance(spec, dict) and isinstance(cov, dict), 'asset roots must be objects')
        return spec, cov
    except FileNotFoundError as error:
        raise ValueError('missing workflow assets; run workflow.py init --project "' + str(project)
                         + '" first, then fill confirmed configuration; use doctor for details') from error
    except (ValueError, UnicodeError) as error:
        raise ValueError('invalid workflow assets; run workflow.py doctor --project "' + str(project)
                         + '" and repair the indicated configuration') from error


def config_missing(spec):
    missing = []
    for key in ['env', 'authref', 'data-policy', 'commands']:
        if not spec.get(key):
            missing.append(key)
    if not spec.get('scope', {}).get('fixed'):
        missing.append('scope.fixed')
    if not spec.get('source'):
        missing.append('source')
    return missing


def asset_errors(project, spec, cov):
    errors = []
    if spec.get('schema_version') != SCHEMA or cov.get('schema_version') != SCHEMA:
        errors.append('schema_version must be 1')
    if set(spec.get('scope', {}).get('fixed', [])) != set(cov.get('scopes', [])):
        errors.append('coverage scopes must equal scope.fixed')
    source_ids = {s['id'] for s in spec.get('source', [])}
    for source in spec.get('source', []):
        if not isinstance(source.get('scopes', [source.get('scope')]), list):
            errors.append('source scopes must be array: ' + source['id'])
    for sid, source in cov.get('sources', {}).items():
        if sid not in source_ids or not set(source.get('scopes', [source.get('scope')])) <= set(cov.get('scopes', [])):
            errors.append('invalid source mapping: ' + sid)
    for scope in cov.get('scopes', []):
        if not any(r.get('scope') == scope for r in cov.get('requirements', {}).values()):
            errors.append('scope has no reviewed requirement inventory: ' + scope)
    for rid, req in cov.get('requirements', {}).items():
        if req.get('source_id') not in cov.get('sources', {}):
            errors.append('unknown requirement source: ' + rid)
        if req.get('scope') not in cov.get('scopes', []) or req.get('expected_version') is None:
            errors.append('requirement scope or expected_version missing: ' + rid)
        if not req.get('case_ids') and req.get('status') != 'retired':
            errors.append('requirement has no case mapping: ' + rid)
        for cid in req.get('case_ids', []):
            if cid not in cov.get('cases', {}) or rid not in cov['cases'][cid].get('requirement_ids', []) or (req.get('status') != 'retired' and cov['cases'][cid].get('implementation') == 'retired'):
                errors.append('broken requirement/case mapping: ' + rid + '/' + cid)
    for test_id, ids in test_id_index(cov).items():
        if len(ids) > 1:
            errors.append('duplicate test_id: ' + '/'.join(ids) + ' (' + test_id + ')')
    for cid, case in cov.get('cases', {}).items():
        if case.get('scope') not in cov.get('scopes', []):
            errors.append('unknown case scope: ' + cid)
        if case.get('execution') not in {'automation', 'manual', 'hardware', 'deferred'}:
            errors.append('invalid execution mode: ' + cid)
        for rid in ([] if case.get('implementation') == 'retired' else case.get('requirement_ids', [])):
            if rid not in cov.get('requirements', {}) or cid not in cov['requirements'][rid].get('case_ids', []):
                errors.append('broken case/requirement mapping: ' + cid + '/' + rid)
            elif case.get('scope') != cov['requirements'][rid].get('scope') or case.get('source_id') != cov['requirements'][rid].get('source_id'):
                errors.append('case requirement scope/source mismatch: ' + cid + '/' + rid)
        for field in ['plan', 'spec']:
            if case.get(field):
                try:
                    safe_path(project, case[field])
                except ValueError as error:
                    errors.append(str(error))
    return errors


def test_id_index(cov):
    index = {}
    for cid, case in cov.get('cases', {}).items():
        if case.get('implementation') != 'retired' and case.get('test_id') and not case.get('legacy_group'):
            index.setdefault(case['test_id'], []).append(cid)
    return index


def coverage_reasons(project, cov, cid, context=None):
    """Explain eligibility, sharing indexes and file checks within one stable view/batch."""
    context = context if context is not None else {}
    memo = context.setdefault('coverage', {})
    if cid in memo:
        return memo[cid]
    case = cov['cases'][cid]
    reasons = []
    for field, expected in [('execution', 'automation'), ('review', 'approved'), ('implementation', 'valid')]:
        if case.get(field) != expected:
            reasons.append(field + '_not_' + expected)
    if case.get('legacy_group'):
        reasons.append('legacy_group_requires_independent_test')
    for field in ['test_id', 'title']:
        if not case.get(field):
            reasons.append('missing_' + field)
    index = context.get('test_ids')
    if index is None:
        index = context['test_ids'] = test_id_index(cov)
    if len(index.get(case.get('test_id'), [])) > 1:
        reasons.append('duplicate_test_id:' + '/'.join(index[case['test_id']]))
    if not case.get('requirement_ids'):
        reasons.append('missing_requirement_ids')
    for rid in case.get('requirement_ids', []):
        req = cov.get('requirements', {}).get(rid)
        if req is None:
            reasons.append('missing_requirement:' + rid)
        else:
            if req.get('expected_version') is None or case.get('acceptance_version') != req.get('expected_version'):
                reasons.append('acceptance_version_mismatch:' + rid)
            if req.get('review', 'approved') != 'approved':
                reasons.append('requirement_review_not_approved:' + rid)
    source = cov.get('sources', {}).get(case.get('source_id'), {})
    if not (source.get('revision') or source.get('fingerprint')):
        reasons.append('no_confirmed_baseline')
    files = context.setdefault('files', {})
    for field in ['plan', 'spec']:
        value = case.get(field)
        if not value:
            reasons.append('missing_' + field + '_path')
        else:
            try:
                if value not in files:
                    files[value] = safe_path(project, value).is_file()
                if not files[value]:
                    reasons.append('missing_' + field + '_file:' + value)
            except ValueError:
                reasons.append('unsafe_' + field + '_path')
    memo[cid] = reasons
    return reasons


def covered(project, cov, cid, context=None):
    return not coverage_reasons(project, cov, cid, context)


def run_path(project, run_id):
    check(bool(re.fullmatch(r'[A-Za-z0-9_-]+', run_id or '')), 'invalid run ID')
    return project / 'test/.cache/runs' / run_id


def owner_check(state, owner):
    claim = state.get('claim')
    check(claim and owner and claim['owner'] == owner, 'run is owned by another task or released')
    lease = max(platform_support.MIN_RUN_LEASE_SECONDS,
                claim.get('lease_seconds', platform_support.MIN_RUN_LEASE_SECONDS))
    check((dt(now()) - dt(claim['renewed_at'])).total_seconds() < lease, 'claim expired; use start --reclaim --reason')


def renew(project, state, spec, cov, owner):
    """A heartbeat is not a recovery or a new source-observation round."""
    owner_check(state, owner)
    check(state['acceptance'] not in TERMINAL, 'ended run requires explicit start --resume or --new-round')
    check(state.get('config_fingerprint') == fingerprint(spec),
          'configuration changed or legacy run; start --resume and review inputs')
    check(state['asset_fingerprint'] == fingerprint(cov), 'coverage changed; start --resume and review artifacts')
    state['claim']['renewed_at'] = now()
    atomic(run_path(project, state['run_id']) / 'state.json', state)
    return status(project, state, spec, cov)


def scoped_cases(state, cov):
    return {cid: case for cid, case in cov.get('cases', {}).items()
            if case.get('scope') in state['scopes'] and case.get('implementation') != 'retired'}


def needed_sources(state, spec):
    return [s['id'] for s in spec.get('source', []) if set(s.get('scopes', [s.get('scope')])) & set(state['scopes'])]


def task_blocks(state, cid, sid, context=None):
    affected = cached_source_affected(state, sid, context)
    return [tid for tid, task in state['tasks'].items()
            if task['status'] not in DONE and (cid in task.get('case_ids', [])
                or (task.get('source_id') == sid and not task.get('case_ids') and (affected is None or cid in affected))
                or (not task.get('case_ids') and not task.get('source_id')))]


def human_pending(human):
    return human['status'] in {'pending_notification', 'waiting', 'blocked', 'rejected'}


def human_applies(human, cid, sid):
    if human.get('case_ids'):
        return cid in human['case_ids']
    return human.get('source_id') == sid if human.get('source_id') else True


def human_view(human):
    result = copy.deepcopy(human)
    if human_pending(human) and (dt(now()) - dt(human['opened_at'])).total_seconds() >= human.get('timeout_seconds', 300):
        result['status'] = 'blocked'
        result['timeout_reached'] = True
    return result


def source_affected(state, sid):
    current = source_version(state['source_checks'].get(sid, {}))
    impacts = [t for t in state['tasks'].values() if t.get('source_id') == sid
               and t.get('phase') == 'impact' and t.get('status') in DONE and t.get('source_version') == current]
    if not impacts or 'affected_case_ids' not in impacts[-1]:
        return None
    return set(impacts[-1]['affected_case_ids'])


def source_version(source):
    return {'revision': source.get('observed_revision'), 'fingerprint': source.get('observed_fingerprint')}


def source_work_done(state, sid):
    current = source_version(state['source_checks'].get(sid, {}))
    return all(any(t.get('source_id') == sid and t['phase'] == phase and t['status'] in DONE and t.get('source_version') == current
                   for t in state['tasks'].values()) for phase in ['impact', 'case_review'])


def disposition_approved(state, cid, case):
    for task in state['tasks'].values():
        value = task.get('dispositions', {}).get(cid, {})
        if (task['phase'] == 'case_review' and task['status'] in DONE and case.get('review') == 'approved'
                and value.get('mode') == case.get('execution') and value.get('status') == 'approved'
                and value.get('acceptance_version') == case.get('acceptance_version')
                and value.get('reason') and value.get('evidence')):
            return True
    return False


def global_residuals(project, state):
    latest, unreadable = {}, []
    paths = (project / 'test/.cache/runs').glob('*/state.json')
    others = []
    for path in paths:
        if path.parent.name == state['run_id']:
            continue
        try:
            others.append(load(path))
        except (ValueError, OSError):
            unreadable.append({'run_id': path.parent.name, 'status': 'unknown', 'suggestion': 'reconcile',
                               'state_path': path.relative_to(project).as_posix()})
    for other in others + [state]:
        for rid, resource in other['resources'].items():
            key = (resource.get('created_by_run', other['run_id']), resource.get('origin_resource_id', rid))
            if key not in latest or resource.get('at', '') >= latest[key][1].get('at', ''):
                latest[key] = (other['run_id'], resource)
    current = {(r.get('created_by_run', state['run_id']), r.get('origin_resource_id', rid)) for rid, r in state['resources'].items()}
    result = list(unreadable)
    for key, (run_id, resource) in sorted(latest.items()):
        if key not in current and resource['status'] != 'absence_verified':
            result.append({'run_id': run_id, 'resource_id': resource['resource_id'], 'record_id': resource.get('record_id'),
                           'status': resource['status'], 'suggestion': 'owner_cleanup' if resource['status'] == 'confirmed' else 'owner_reconcile',
                           'state_path': 'test/.cache/runs/' + run_id + '/state.json'})
    return result


def cached_source_affected(state, sid, context=None):
    if context is None:
        return source_affected(state, sid)
    cache = context.setdefault('affected', {})
    if sid not in cache:
        cache[sid] = source_affected(state, sid)
    return cache[sid]


def case_block_reasons(state, cov, cid, config_drift=False, context=None):
    case = cov['cases'][cid]
    sid = case.get('source_id')
    source = state['source_checks'].get(sid, {'status': 'unknown'})
    pending = (h for h in state['humans'].values() if human_pending(h))
    reasons = []
    if config_drift and state['acceptance'] not in TERMINAL:
        reasons.append('configuration_review_required')
    if source['status'] == 'unknown':
        reasons.append('source_check_required')
    if state.get('recovery_required'):
        reasons.append('recovery_review_required')
    if task_blocks(state, cid, sid, context):
        reasons.append('incomplete_task')
    if any(human_applies(h, cid, sid) for h in pending):
        reasons.append('human_decision_required')
    if any(cid in item.get('case_ids', []) or not item.get('case_ids') for item in state['delegation_blocks']):
        reasons.append('delegation_unavailable')
    if cid in state['invalidated']:
        reasons.append('artifact_invalidated')
    baseline = cov.get('sources', {}).get(sid, {})
    if source['status'] == 'changed' and source.get('business_change') and (cached_source_affected(state, sid, context) is None or cid in cached_source_affected(state, sid, context)):
        if not source_work_done(state, sid):
            reasons.append('impact_and_case_review_required')
        if source.get('observed_revision') != baseline.get('revision') or source.get('observed_fingerprint') != baseline.get('fingerprint'):
            reasons.append('effective_baseline_update_required')
    if source['status'] == 'unavailable' and not (baseline.get('revision') or baseline.get('fingerprint')):
        reasons.append('no_confirmed_baseline')
    action = state['case_actions'].get(cid, {})
    if action.get('action') == 'needs_decision' or action.get('exploration') in {'pending', 'running', 'blocked'} or action.get('code') in {'pending', 'running', 'blocked'}:
        reasons.append('incomplete_case_work')
    return reasons

def status(project, state, spec, cov, full=True, include_history=True):
    context = {}
    selected = scoped_cases(state, cov)
    config_drift = state.get('config_fingerprint', fingerprint(spec)) != fingerprint(spec)
    checks = {sid: state['source_checks'].get(sid, {'status': 'unknown'}) for sid in needed_sources(state, spec)}
    pending = {hid: human_view(h) for hid, h in state['humans'].items() if human_pending(h)}
    residual = []
    for rid, resource in state['resources'].items():
        if resource['status'] != 'absence_verified':
            suggestion = 'cleanup' if resource['status'] == 'confirmed' and resource.get('owned') and resource.get('receipt') else 'reconcile'
            residual.append(dict({key: resource.get(key) for key in ['resource_id', 'status', 'created_by_run', 'origin_run', 'record_id', 'identifying_fields', 'receipt', 'owned']}, suggestion=suggestion))
    blocked, ready, work, dispositions, work_reasons = {}, [], [], {}, {}
    for cid, case in selected.items():
        sid = case.get('source_id')
        source = checks.get(sid, {'status': 'unknown'})
        reasons = case_block_reasons(state, cov, cid, config_drift, context)
        if reasons:
            blocked[cid] = reasons
        elif covered(project, cov, cid, context):
            ready.append(cid)
        elif case.get('execution') in {'manual', 'hardware', 'deferred'}:
            reviewed = disposition_approved(state, cid, case)
            dispositions[cid] = {'execution': case['execution'], 'reviewed': reviewed}
            if not reviewed:
                work.append(cid)
                work_reasons[cid] = ['reviewed_disposition_required']
        else:
            work.append(cid)
            work_reasons[cid] = coverage_reasons(project, cov, cid, context)
    global_resources = global_residuals(project, state) if include_history else []
    missing = config_missing(spec)
    unmapped = [rid for rid, req in cov.get('requirements', {}).items() if req.get('scope') in state['scopes']
                and req.get('status') != 'retired' and (not req.get('case_ids') or any(cid not in selected for cid in req.get('case_ids', [])))]
    planning = [scope for scope in state['scopes'] if not any(r.get('scope') == scope for r in cov.get('requirements', {}).values())]
    steps = []
    if config_drift and state['acceptance'] not in TERMINAL:
        steps.append('resume_and_review_changed_configuration')
    if residual or global_resources:
        steps.append('reconcile_or_cleanup_recorded_resources')
    if any(s['status'] == 'unknown' for s in checks.values()):
        steps.append('record_current_source_checks')
    if state.get('recovery_required'):
        steps.append('review_environment_and_artifacts')
    if pending:
        steps.append('main_agent_notify_or_resolve_human_requests')
    reasons = {reason for values in blocked.values() for reason in values}
    phase_steps = {'impact': 'analyze_requirement_impact', 'clarify': 'clarify_affected_requirements',
                   'case_review': 'review_affected_cases', 'exploration': 'complete_pending_case_exploration',
                   'code': 'complete_pending_case_code', 'execution': 'resume_pending_executions',
                   'acceptance': 'complete_main_agent_acceptance'}
    pending_phases = {t['phase'] for t in state['tasks'].values() if t['status'] not in DONE}
    for sid, source in checks.items():
        if source.get('business_change') and not source_work_done(state, sid):
            for phase in ['impact', 'case_review']:
                if not any(t.get('source_id') == sid and t['phase'] == phase and t['status'] in DONE
                           and t.get('source_version') == source_version(source) for t in state['tasks'].values()):
                    pending_phases.add(phase)
    for phase, action in phase_steps.items():
        if phase in pending_phases:
            steps.append(action)
    if 'effective_baseline_update_required' in reasons:
        steps.append('approve_and_update_effective_baseline')
    if 'artifact_invalidated' in reasons:
        steps.append('revalidate_or_repair_invalidated_case_artifacts')
    if 'incomplete_case_work' in reasons:
        steps.append('complete_pending_case_exploration_or_code')
    if 'no_confirmed_baseline' in reasons:
        steps.append('obtain_readable_source_or_confirmed_baseline')
    if 'delegation_unavailable' in reasons:
        steps.append('restore_delegation_or_record_explicit_alternative')
    if work or unmapped or planning:
        steps.append('fill_only_mapping_review_or_code_gaps')
    if ready:
        steps.append('run_independent_stable_regression')
    if missing:
        steps.append('complete_confirmed_configuration')
    inventory = {}
    ready_set = set(ready)
    for cid, case in (selected.items() if full else []):
        if case.get('execution') != 'automation' or not case.get('spec'):
            continue
        reasons = list(blocked.get(cid, []))
        if not covered(project, cov, cid, context):
            reasons.extend(coverage_reasons(project, cov, cid, context))
        inventory[cid] = {'spec': case['spec'], 'title': case.get('title'),
                          'ready': cid in ready_set, 'blocked_by': reasons}
    result = {'regression_inventory': inventory,
            'configuration_drift': config_drift,
            'regression_policy': spec.get('regression', {}).get('final', 'all_existing_automation'),
            'run_id': state['run_id'], 'freshness_marker': state['freshness_marker'], 'acceptance': state['acceptance'], 'claim': state.get('claim'),
            'source_checks': checks, 'residual_resources': residual, 'global_residual_runs': global_resources, 'pending_humans': pending,
            'blocked_cases': blocked, 'regression_ready': ready, 'work_needed': work, 'work_needed_reasons': work_reasons, 'nonautomated_cases': dispositions,
            'requirements_updates_paused': [sid for sid, value in checks.items() if value['status'] in {'unknown', 'unavailable'}],
            'missing_configuration': missing, 'unmapped_requirements': unmapped, 'planning_needed_scopes': planning, 'next_steps': steps, 'usage_known': bool(state['usage']), 'coverage_asset_drift': state['asset_fingerprint'] != fingerprint(cov)}

    result['global_residuals_checked'] = include_history
    result['counts'] = {'selected_cases': len(selected), 'ready': len(ready),
                        'blocked': len(blocked), 'work_needed': len(work),
                        'executions': len(state['executions'])}
    if full:
        return result
    keys = ['run_id', 'freshness_marker', 'claim', 'acceptance', 'counts', 'blocked_cases',
            'regression_ready', 'work_needed', 'work_needed_reasons', 'next_steps',
            'missing_configuration', 'configuration_drift', 'coverage_asset_drift',
            'global_residuals_checked']
    return {key: result[key] for key in keys}


def require_evidence(project, event, allow_recycled=False):
    evidence = event.get('evidence', [])
    check(isinstance(evidence, list), 'evidence must be a list')
    for item in evidence:
        check(safe_path(project, item).is_file() or (allow_recycled and retention.is_recycled(project, item)),
              'evidence file missing: ' + item)


def record_event(project, state, spec, cov, event, context=None):
    """A shared context is valid only across consecutive execution receipts."""
    check(isinstance(event, dict), 'event must be an object')
    no_secrets(event)
    require_evidence(project, event)
    event = copy.deepcopy(event)
    event['at'] = event.get('at', now())
    check(dt(event['at']) <= dt(now()), 'event timestamp cannot be in the future')
    event['seq'] = len(state['events']) + 1
    kind = event.get('type')
    context = context if context is not None else {}
    if kind != 'execution':
        context.clear()
        selected = scoped_cases(state, cov)
    else:
        if 'selected' not in context:
            context['selected'] = scoped_cases(state, cov)
        selected = context['selected']
    for field in ['case_ids', 'invalidate_case_ids', 'affected_case_ids']:
        for cid in event.get(field, []):
            check(cid in selected, 'unknown or out-of-scope case: ' + cid)
    if kind == 'source':
        sid = event['source_id']
        check(sid in needed_sources(state, spec), 'source outside selected scopes')
        check(event.get('freshness_marker') == state['freshness_marker'], 'stale source freshness marker')
        check(event.get('status') in {'changed', 'unchanged', 'unavailable'}, 'invalid source status')
        baseline = cov.get('sources', {}).get(sid, {})
        if event['status'] == 'unavailable':
            check(event.get('observed_revision') is None and event.get('observed_fingerprint') is None, 'unavailable source cannot assert observed version')
        elif event['status'] == 'unchanged':
            check(bool(baseline.get('revision') or baseline.get('fingerprint')), 'unchanged requires a confirmed baseline')
            check(event.get('observed_revision') == baseline.get('revision') and event.get('observed_fingerprint') == baseline.get('fingerprint'), 'unchanged does not match effective baseline')
        else:
            check(isinstance(event.get('business_change'), bool), 'changed requires explicit business_change')
            check(event.get('observed_revision') or event.get('observed_fingerprint'), 'changed requires observed version')
        if 'requirement_delta' in event:
            check(event.get('baseline_available') and baseline.get('baseline_path') and safe_path(project, baseline['baseline_path']).is_file(), 'requirement delta requires actual old baseline text')
            check(all(isinstance(event['requirement_delta'].get(k), int) and event['requirement_delta'][k] >= 0 for k in ['added', 'updated', 'retired']), 'invalid requirement delta')
        if event['status'] in {'changed', 'unchanged'}:
            check(event.get('evidence'), 'readable source check requires evidence')
        state['source_checks'][sid] = event
    elif kind == 'recovery_review':
        check(event.get('environment_checked') is True and event.get('artifacts_checked') is True, 'recovery requires environment and artifact review')
        state['recovery_required'] = False
        state['asset_fingerprint'] = fingerprint(cov)
        state['invalidated'] = sorted(set(state['invalidated']) | set(event.get('invalidate_case_ids', [])))
        for cid in event.get('invalidate_case_ids', []):
            state.setdefault('invalidation_seq', {})[cid] = event['seq']
    elif kind == 'task':
        check(event.get('phase') in {'impact', 'clarify', 'case_review', 'exploration', 'code', 'execution', 'acceptance'}, 'invalid task phase')
        check(event.get('status') in {'pending', 'running', 'blocked', 'approved', 'complete'}, 'invalid task status')
        check(event.get('owner'), 'task owner required')
        if event.get('source_id'):
            check(event['source_id'] in needed_sources(state, spec), 'task source outside selected scopes')
            event['source_version'] = source_version(state['source_checks'].get(event['source_id'], {}))
        for cid, value in event.get('dispositions', {}).items():
            check(event['phase'] == 'case_review' and cid in selected and cid in event.get('case_ids', []), 'disposition must belong to assigned case review')
            check(value.get('mode') in {'manual', 'hardware', 'deferred'} and value['mode'] == selected[cid].get('execution'), 'invalid disposition mode')
            check(value.get('status') == 'approved' and value.get('reason') and value.get('evidence') and value.get('acceptance_version') == selected[cid].get('acceptance_version'), 'disposition needs explicit approval, version, reason and evidence')
            require_evidence(project, value)
        prior = state['tasks'].get(event['task_id'])
        if prior and event.get('phase') != prior['phase']:
            raise ValueError('task phase cannot change')
        state['tasks'][event['task_id']] = dict(prior or {}, **event)
        if event['phase'] == 'impact' and event['status'] in DONE:
            sid = event.get('source_id')
            if sid and state['source_checks'].get(sid, {}).get('business_change'):
                affected = set(event.get('affected_case_ids', []))
                for rid in event.get('affected_requirement_ids', []):
                    check(rid in cov['requirements'], 'unknown affected requirement: ' + rid)
                    affected.update(cov['requirements'][rid]['case_ids'])
                if 'affected_case_ids' not in event and 'affected_requirement_ids' not in event:
                    affected = {cid for cid, case in selected.items() if case.get('source_id') == sid}
                event['affected_case_ids'] = sorted(affected)
                state['tasks'][event['task_id']]['affected_case_ids'] = event['affected_case_ids']
            for cid in event.get('affected_case_ids', []):
                state['invalidated'] = sorted(set(state['invalidated']) | {cid})
                state.setdefault('invalidation_seq', {})[cid] = event['seq']
    elif kind == 'case':
        check(event.get('action') in CASE_ACTIONS, 'invalid case action')
        check(event.get('exploration_action', 'not_needed') in {'executed', 'reused', 'not_needed', 'blocked'}, 'invalid exploration action')
        check(event.get('code_action', 'missing') in {'added', 'modified', 'reused', 'retired', 'missing'}, 'invalid code action')
        cid = event['case_id']
        for field in ['exploration', 'code']:
            check(event.get(field, 'none') in {'none', 'pending', 'running', 'complete', 'blocked'}, 'invalid case work state')
        if 'coverage' in event or 'requirements' in event:
            sid = event.get('coverage', cov['cases'].get(cid, {})).get('source_id')
            src = state['source_checks'].get(sid, {})
            check(src.get('status') in {'changed', 'unchanged'}, 'source unavailable or unchecked; requirements updates paused')
            if src.get('business_change'):
                check(source_work_done(state, sid), 'impact and case review required before coverage change')
            check(not any(human_pending(h) and human_applies(h, cid, sid) for h in state['humans'].values()), 'pending human decision blocks case updates')
            if 'coverage' in event:
                check(event['coverage'].get('scope') in state['scopes'], 'case outside selected scopes')
                cov['cases'][cid] = event['coverage']
            cov['requirements'].update(event.get('requirements', {}))
        check(cid in cov['cases'], 'case must exist or supply coverage')
        if event['action'] == 'retired':
            sid = cov['cases'][cid].get('source_id')
            check(state['source_checks'].get(sid, {}).get('status') in {'changed', 'unchanged'}, 'retirement requires current readable source')
            if state['source_checks'][sid].get('business_change'):
                check(source_work_done(state, sid), 'retirement requires impact and case review')
            cov['cases'][cid]['implementation'] = 'retired'
            state['invalidated'] = [item for item in state['invalidated'] if item != cid]
        prior_action = state['case_actions'].get(cid, {})
        # A case added then modified remains one addition in this run.
        if prior_action.get('action') == 'added' and event['action'] in {'updated', 'unchanged'}:
            event['action'] = 'added'
        elif prior_action.get('action') == 'updated' and event['action'] == 'unchanged':
            event['action'] = 'updated'
        state['case_actions'][cid] = dict(prior_action, **event)
        if event.get('code') == 'complete' and covered(project, cov, cid):
            state['invalidated'] = [item for item in state['invalidated'] if item != cid]
    elif kind == 'baseline':
        sid = event['source_id']
        src = state['source_checks'].get(sid, {})
        check(src.get('status') in {'changed', 'unchanged'}, 'baseline requires readable current source')
        check(event.get('approved') is True and source_work_done(state, sid), 'baseline requires explicit approval, impact and case review')
        affected = source_affected(state, sid)
        target_cases = [cid for cid, case in selected.items() if case.get('source_id') == sid and (affected is None or cid in affected)]
        check(not any(human_pending(h) and any(human_applies(h, cid, sid) for cid in (target_cases or [None])) for h in state['humans'].values()), 'pending human decision blocks baseline')
        check(event.get('revision') == src.get('observed_revision') and event.get('fingerprint') == src.get('observed_fingerprint'), 'baseline must match observed version')
        if event.get('baseline_path'):
            check(safe_path(project, event['baseline_path']).is_file(), 'baseline file missing')
        cov['sources'][sid].update({key: event.get(key) for key in ['revision', 'fingerprint', 'baseline_path']})
    elif kind == 'resource':
        rid, value = event['resource_id'], event['status']
        prior = state['resources'].get(rid)
        check(value in {'intent', 'confirmed', 'ambiguous', 'absence_verified'}, 'invalid resource state')
        check(prior is not None or value == 'intent', 'resource must record intent before create result')
        origin = prior.get('created_by_run') if prior and prior.get('origin_run') else state['run_id']
        check(event.get('created_by_run') == origin, 'resource must belong to this run or its explicitly inherited ledger')
        if prior:
            allowed = {'intent': {'confirmed', 'ambiguous', 'absence_verified'}, 'ambiguous': {'confirmed', 'absence_verified'}, 'confirmed': {'absence_verified'}, 'absence_verified': set()}
            check(value in allowed[prior['status']], 'invalid resource transition')
        if value == 'confirmed':
            check(event.get('owned') is True and bool(event.get('receipt')), 'confirmed resource requires ownership and creation receipt')
        if value == 'absence_verified':
            check(event.get('evidence'), 'absence verification requires evidence')
        state['resources'][rid] = dict(prior or {}, **event)
    elif kind == 'human':
        hid = event['request_id']
        prior = state['humans'].get(hid)
        check(event.get('status') in {'pending_notification', 'waiting', 'blocked', 'approved', 'rejected', 'resolved'}, 'invalid human status')
        mode = event.get('mode', prior.get('mode') if prior else None)
        check(mode in {'explicit_decision', 'observable'}, 'invalid human mode')
        check(not prior or mode == prior['mode'], 'human request mode cannot change')
        check(not (mode == 'explicit_decision' and event['status'] == 'resolved'), 'explicit decision requires approved or rejected')
        if event['status'] in {'approved', 'rejected', 'resolved'}:
            check(prior is not None, 'human resolution requires existing request')
            if mode == 'observable':
                check(event.get('evidence'), 'observable resolution requires evidence')
        timeout = event.get('timeout_seconds', prior.get('timeout_seconds', spec.get('wait', {}).get('timeout_seconds', 300)) if prior else spec.get('wait', {}).get('timeout_seconds', 300))
        check(isinstance(timeout, int) and 0 < timeout <= spec.get('wait', {}).get('timeout_seconds', 300), 'human timeout must be positive and within configured wait bound')
        event['timeout_seconds'] = timeout
        merged = dict(prior or {}, **event)
        if 'case_ids' in event or 'source_id' in event:
            merged['case_ids'] = event.get('case_ids', [])
            merged['source_id'] = event.get('source_id')
            if prior:
                check(merged.get('case_ids') or merged.get('source_id'), 'scope update requires nonempty case_ids or source_id; create a new request for run scope')
        merged.update(mode=mode, opened_at=prior['opened_at'] if prior else event['at'])
        state['humans'][hid] = merged
    elif kind == 'delegation':
        check(event.get('status') in {'unavailable', 'available'}, 'invalid delegation status')
        check(event.get('reason'), 'delegation reason required')
        if event['status'] == 'unavailable':
            state['delegation_blocks'].append(event)
        else:
            ids = set(event.get('case_ids', []))
            state['delegation_blocks'] = [item for item in state['delegation_blocks'] if set(item.get('case_ids', [])) != ids]
    elif kind == 'execution':
        eid = event['execution_id']
        check(eid not in {item['execution_id'] for item in state['executions']}, 'duplicate execution ID')
        check(event.get('result') in RESULTS and event.get('test_id'), 'invalid execution result or test ID')
        check(isinstance(event.get('setup'), bool), 'execution setup must be explicit boolean')
        check(isinstance(event.get('attempt'), int) and event['attempt'] >= 1, 'attempt must be positive integer')
        previous = [item['attempt'] for item in state['executions'] if item['test_id'] == event['test_id'] and item['setup'] == event['setup']]
        check(not previous or event['attempt'] > max(previous), 'rerun attempt must increase')
        ids = event.get('case_ids', [])
        check(event['setup'] or ids, 'business execution requires case IDs')
        check(len(ids) <= 1 or event.get('legacy_group') is True, 'new business tests must map one case per test')
        check(event.get('started_at') and event.get('ended_at'), 'execution requires started_at and ended_at timestamps')
        check(dt(event['ended_at']) >= dt(event['started_at']), 'execution ends before start')
        if event.get('blocked_by'):
            blocker = next((e for e in state['executions'] if e['execution_id'] == event['blocked_by']), None)
            check(event['result'] == 'notrun' and event.get('runner_result') == 'skip' and event.get('reason'),
                  'dependency skip requires notrun, raw runner_result=skip and reason')
            check(blocker and blocker['setup'] and blocker['result'] == 'fail', 'blocked_by must reference a failed Setup execution')
        if 'failure_details' in event:
            detail = event['failure_details']
            check(isinstance(detail, dict) and all(detail.get(k) for k in ['expected', 'actual', 'phase', 'classification', 'location', 'cleanup']),
                  'failure_details requires expected, actual, phase, classification, location and cleanup')
            check(detail['classification'] in {'product_candidate', 'script', 'environment', 'needs_decision', 'deferred'},
                  'invalid failure classification')
            require_evidence(project, detail)
        if not event['setup']:
            check(not config_missing(spec), 'execution requires confirmed configuration')
            if 'config_drift' not in context:
                context['config_drift'] = state.get('config_fingerprint', fingerprint(spec)) != fingerprint(spec)
            for cid in ids:
                reasons = case_block_reasons(state, cov, cid, context['config_drift'], context)
                check(not reasons, 'execution includes blocked case: ' + cid + ': ' + ','.join(reasons))
            for cid in ids:
                check(event.get('legacy_group') or event['test_id'] == cov['cases'][cid].get('test_id'), 'test ID differs from reviewed case mapping')
                method = event.get('method', 'automation')
                check(method in {'automation', 'manual', 'hardware'}, 'invalid execution method')
                eligible = covered(project, cov, cid, context) if method == 'automation' else (cov['cases'][cid].get('execution') == method and cov['cases'][cid].get('review') == 'approved')
                check(eligible or (event.get('legacy_group') and cov['cases'][cid].get('review') == 'approved'), 'business execution requires current reviewed code or reviewed manual/hardware case')
        for cid, step in event.get('steps', {}).items():
            check(cid in ids and step.get('result') in RESULTS, 'invalid legacy step')
            require_evidence(project, step)
            check(step.get('evidence'), 'legacy step requires evidence')
        if event.get('bundle_id'):
            bundle = state.get('report_bundles', {}).get(event['bundle_id'])
            check(bundle is not None, 'execution requires a registered report bundle')
            expected_title = cov['cases'][ids[0]].get('title') if not event['setup'] and not event.get('legacy_group') else None
            slot = report_artifacts.check_execution(project, bundle, event, expected_title)
            check(not any(e.get('bundle_id') == event['bundle_id']
                          and (e.get('runner_test_pointer'), e.get('runner_result_index')) == slot
                          for e in state['executions']), 'raw runner result already registered')
        event['acceptance_versions'] = {cid: cov['cases'][cid].get('acceptance_version') for cid in ids}
        if 'asset_fingerprint' not in context:
            context['asset_fingerprint'] = fingerprint(cov)
        event['asset_fingerprint'] = context['asset_fingerprint']
        state['executions'].append(event)
    elif kind == 'report_bundle':
        bundle = event.get('bundle', {})
        bid = bundle.get('bundle_id')
        check(bid not in state.get('report_bundles', {}), 'report bundle ID already registered; create a new invocation')
        check(isinstance(bundle.get('scope'), list) and set(bundle['scope']) <= set(state['scopes']), 'report scope outside run')
        if state.get('native_merge_required', False):
            check(isinstance(bundle.get('blob_reports'), list) and bundle['blob_reports'],
                  'current invocation requires at least one native Playwright blob report')
        checked = report_artifacts.inspect_bundle(project, state['run_id'], bundle)
        state.setdefault('report_bundles', {})[bid] = checked
    elif kind == 'merged_report':
        state['merged_report'] = report_artifacts.inspect_merged_report(
            project, state['run_id'], state.get('report_bundles', {}), event.get('report'))
    elif kind == 'issue':
        check(event.get('status') in {'open', 'closed'}, 'invalid issue status')
        check(event.get('kind') in {'product_candidate', 'script', 'environment', 'needs_decision', 'deferred'}, 'invalid issue kind')
        issue = state['issues'].get(event['issue_id'], {})
        event['affected_case_ids'] = sorted(set(issue.get('affected_case_ids', [])) | set(event.get('affected_case_ids', [])))
        state['issues'][event['issue_id']] = event
    elif kind == 'usage':
        mid = event['measurement_id']
        check(mid not in state['usage'], 'duplicate measurement ID')
        check(event.get('source') == 'host' and event.get('aggregation') in {'inclusive', 'exclusive'}, 'usage must be host actual measurement')
        check(event.get('coverage', 'partial') in {'partial', 'complete_run'}, 'invalid usage coverage')
        if event.get('coverage') == 'complete_run':
            check(event.get('coverage_description'), 'complete run usage requires explicit actor/phase coverage description')
        metrics = event.get('metrics', {})
        check(all(k in metrics and (metrics[k] is None or (isinstance(metrics[k], int) and not isinstance(metrics[k], bool) and metrics[k] >= 0)) for k in ['input_tokens', 'output_tokens', 'total_tokens']), 'usage metrics must be actual nonnegative integers or null')
        included = event.get('includes', [])
        check(len(set(included)) == len(included) and mid not in included, 'invalid usage inclusion')
        check(event['aggregation'] == 'inclusive' or not included, 'exclusive measurement cannot include children')
        existing_children = {c for u in state['usage'].values() for c in u.get('includes', [])}
        check(not (set(included) & existing_children), 'measurement already included by another parent')
        check(all(c in state['usage'] for c in included), 'included measurements must be recorded first')
        check(not any(c in existing_children for c in included), 'overlapping inclusive metrics')
        state['usage'][mid] = event
    elif kind == 'phase_time':
        check(dt(event['ended_at']) >= dt(event['started_at']), 'phase ends before start')
        state['phase_times'].append(event)
    elif kind == 'acceptance':
        check(event.get('owner') == state['claim']['owner'], 'acceptance must be recorded by main owner')
        check(event.get('status') in {'in_progress', 'blocked', 'partially_accepted', 'fully_accepted', 'completed_with_failures', 'partially_blocked', 'aborted'}, 'invalid acceptance state')
        if event['status'] == 'fully_accepted':
            if state.get('report_bundles'):
                check(report_consistency(project, state)['status'] == 'checked', 'full acceptance requires consistent complete report receipts')
            view = status(project, state, spec, cov)
            check(not view['missing_configuration'] and not asset_errors(project, spec, cov), 'configuration or coverage invalid')
            check(not view['residual_resources'] and not view['global_residual_runs'] and not view['pending_humans'] and not view['blocked_cases'] and not view['work_needed'], 'unfinished work or resources prevent full acceptance')
            check(all(s['status'] not in {'unknown', 'unavailable'} for s in view['source_checks'].values()), 'full acceptance requires readable current sources')
            check(not any(t['status'] not in DONE for t in state['tasks'].values()), 'unfinished tasks prevent full acceptance')
            results = case_results(project, state, cov)
            for cid, case in selected.items():
                if case['execution'] == 'automation':
                    check(results[cid] == 'pass', 'full acceptance requires current per-case pass: ' + cid)
                else:
                    check(disposition_approved(state, cid, case), 'manual/hardware/deferred case requires explicit reviewed disposition')
                    if case['execution'] in {'manual', 'hardware'}:
                        check(results[cid] == 'pass', 'manual/hardware acceptance requires current recorded pass')
        state['acceptance'] = event['status']
    else:
        raise ValueError('unknown event type: ' + str(kind))
    state['events'].append(event)


def union_seconds(intervals):
    merged = []
    for start, end in sorted((dt(a), dt(b)) for a, b in intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return sum((b - a).total_seconds() for a, b in merged)


def case_results(project, state, cov):
    results = {cid: 'notrun' for cid in scoped_cases(state, cov)}
    for execution in state['executions']:
        if execution['setup']:
            continue
        for cid in execution['case_ids']:
            if cid not in results:
                continue
            if execution.get('seq', 0) <= state.get('invalidation_seq', {}).get(cid, 0) or execution.get('acceptance_versions', {}).get(cid) != cov['cases'][cid].get('acceptance_version') or cid in state['invalidated']:
                continue
            if execution.get('legacy_group'):
                step = execution.get('steps', {}).get(cid)
                if step and step.get('evidence'):
                    results[cid] = step['result']
            else:
                results[cid] = execution['result']
    return results


def report_consistency(project, state):
    project = Path(project).resolve()
    bundles = state.get('report_bundles', {})
    errors, recycled, recycled_merged = [], [], []
    for bid, bundle in bundles.items():
        try:
            if state.get('native_merge_required', False):
                check(bundle.get('blob_reports'), 'native blob report missing for current invocation')
            recovered = [p for p in bundle.get('artifact_sha256', {}) if retention.is_recycled(project, p)]
            if recovered:
                check(state['acceptance'] in TERMINAL, 'active run cannot use recycled report evidence')
                for path, expected_hash in bundle['artifact_sha256'].items():
                    target = safe_path(project, path)
                    if path not in recovered:
                        check(target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected_hash,
                              'retained report artifact changed or unexpectedly missing: ' + path)
                recycled.append({'bundle_id': bid, 'paths': recovered,
                                 'original_artifact_consistency': bundle.get('report_consistency'),
                                 'runner_counts': bundle.get('runner_counts'),
                                 'receipt_revalidation': 'not_claimed_after_policy_recycling'})
                continue
            report_artifacts.recheck_bundle(project, state['run_id'], bundle)
            raw = load(safe_path(project, bundle['execution_result']))
            expected = report_artifacts.result_slots(raw)
            receipts = [e for e in state['executions'] if e.get('bundle_id') == bid]
            observed = set()
            for receipt in receipts:
                observed.add(report_artifacts.check_execution(project, bundle, receipt))
            check(observed == expected, 'runner results are not fully mapped to execution receipts')
        except (ValueError, KeyError, TypeError, OSError) as error:
            errors.append(bid + ': ' + str(error))
    merged = state.get('merged_report')
    if merged:
        try:
            recovered = [p for p in merged['artifact_sha256'] if retention.is_recycled(project, p)]
            if recovered:
                check(state['acceptance'] in TERMINAL, 'active run cannot use recycled merged report')
                for path, expected_hash in merged['artifact_sha256'].items():
                    if path not in recovered:
                        target = safe_path(project, path)
                        check(target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected_hash,
                              'retained merged artifact changed or unexpectedly missing: ' + path)
                recycled_merged.append({'paths': recovered,
                                        'original_report_consistency': merged.get('report_consistency')})
            else:
                report_artifacts.recheck_merged_report(project, state['run_id'], bundles, merged)
        except (ValueError, KeyError, TypeError, OSError) as error:
            errors.append('merged_report: ' + str(error))
    elif ((state.get('native_merge_required', False) and bundles)
          or any(bundle.get('blob_reports') for bundle in bundles.values())):
        errors.append('merged_report: native merge required before final report')
    unbound = any(not e.get('bundle_id') for e in state['executions'])
    return {'status': 'failed' if errors else ('recycled' if recycled or recycled_merged else ('partial' if bundles and unbound else ('checked' if bundles else 'unregistered'))),
            'errors': errors,
            'recycled_bundles': recycled,
            'recycled_merged_reports': recycled_merged,
            'unbound_execution_ids': [e['execution_id'] for e in state['executions'] if not e.get('bundle_id')],
            'note': 'Artifact consistency does not prove business expectations or actual command provenance.'}


def failure_markdown(state, cov, project):
    lines = ['', '## 失败与未执行详情', '']
    problem = [e for e in state['executions'] if e['result'] in {'fail', 'notrun', 'skip'}]
    if not problem:
        return lines + ['本轮没有登记失败或未执行回执。']
    for execution in problem:
        ids = execution.get('case_ids', [])
        names = [cov['cases'].get(cid, {}).get('title', cid) for cid in ids]
        lines += ['### ' + ', '.join(ids or [execution['test_id']]) + ' · ' + execution['execution_id'], '',
                  '- 标题：' + ('；'.join(names) or execution['test_id']),
                  '- 结果：' + execution['result'] + '；第' + str(execution['attempt']) + '次尝试']
        detail = execution.get('failure_details', {})
        for key, label in [('expected', '预期'), ('actual', '实际表现'), ('phase', '发生阶段'),
                           ('classification', '问题分类'), ('location', '文件与位置'),
                           ('readback', '回读数据'), ('cleanup', '清理状态')]:
            value = detail.get(key, '未提供，待补充')
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            lines += ['- ' + label + '：' + value.replace('\n', '\n  ')]
        if execution.get('blocked_by'):
            lines += ['- 阻塞源：' + execution['blocked_by'] + '；' + execution.get('reason', '')]
        elif execution.get('reason'):
            lines += ['- 原因：' + execution['reason']]
        evidence = sorted(set(execution.get('evidence', []) + detail.get('evidence', [])))
        links = [('已按保留策略回收：' + value if retention.is_recycled(project, value)
                  else '[' + Path(value).name + '](<' + str(safe_path(project, value)) + '>)') for value in evidence]
        lines += ['- 证据：' + (', '.join(links) if links else '未提供，待补充'), '']
    return lines


def summary(project, state, spec, cov):
    generated = now()
    business = [item for item in state['executions'] if not item['setup']]
    setup = [item for item in state['executions'] if item['setup']]
    results = case_results(project, state, cov)
    phases = {}
    for item in state['phase_times']:
        phases.setdefault(item['phase'], []).append((item['started_at'], item['ended_at']))
    human_intervals = []
    for human in state['humans'].values():
        start_at = max(dt(human['opened_at']), dt(state['created_at'])).isoformat()
        end_at = generated if human_pending(human) else human['at']
        if dt(end_at) >= dt(start_at):
            human_intervals.append((start_at, end_at))
    usage = state['usage']
    children = {cid for item in usage.values() for cid in item.get('includes', [])}
    roots = [item for mid, item in usage.items() if mid not in children]
    totals, subtotals = {}, {}
    complete = len(roots) == 1 and roots[0].get('coverage') == 'complete_run'
    for key in ['input_tokens', 'output_tokens', 'total_tokens']:
        vals = [item['metrics'][key] for item in roots]
        subtotals[key] = sum(vals) if vals and all(v is not None for v in vals) else None
        totals[key] = subtotals[key] if complete else None
    counts = {'planned_case_actions': {a: sum(e.get('action') == a for e in state['case_actions'].values()) for a in sorted(CASE_ACTIONS)},
              'exploration_actions': {value: sum(e.get('exploration_action', 'not_needed') == value for e in state['case_actions'].values()) for value in ['executed', 'reused', 'not_needed', 'blocked']},
              'code_actions': {value: sum(e.get('code_action', 'missing') == value for e in state['case_actions'].values()) for value in ['added', 'modified', 'reused', 'retired', 'missing']},
              'business_executions': len(business), 'setup_executions': len(setup),
              'business_actual_attempts': sum(e['result'] not in {'notrun', 'skip'} for e in business),
              'dependency_blocked': sum(bool(e.get('blocked_by')) for e in business),
              'independent_business_tests': len({e['test_id'] for e in business}),
              'execution_results': {r: sum(e['result'] == r for e in business) for r in sorted(RESULTS)},
              'setup_results': {r: sum(e['result'] == r for e in setup) for r in sorted(RESULTS)},
              'issues_by_kind': {kind: sum(e.get('kind') == kind for e in state['issues'].values()) for kind in ['product_candidate', 'script', 'environment', 'needs_decision', 'deferred']},
              'case_results': {r: sum(v == r for v in results.values()) for r in sorted(RESULTS)},
              'issues': len(state['issues']), 'product_candidates': sum(e.get('kind') == 'product_candidate' for e in state['issues'].values()), 'resources': {r: sum(e['status'] == r for e in state['resources'].values()) for r in ['intent', 'confirmed', 'ambiguous', 'absence_verified']}}
    cases = {cid: {'result': value, 'covered': covered(project, cov, cid), 'action': state['case_actions'].get(cid),
                   'executions': [e for e in business if cid in e['case_ids']]} for cid, value in results.items()}
    for cid, action in state['case_actions'].items():
        if action.get('action') == 'retired' and cid not in cases:
            cases[cid] = {'result': 'retired', 'covered': False, 'action': action,
                          'executions': [e for e in business if cid in e['case_ids']]}
    for cid, value in cases.items():
        value['resource_ids'] = [rid for rid, r in state['resources'].items() if cid in r.get('case_ids', [])]
        value['phase_records'] = [p for p in state['phase_times'] if cid in p.get('case_ids', []) or cid in state['tasks'].get(p.get('task_id'), {}).get('case_ids', [])]
    return {'schema_version': SCHEMA, 'run_id': state['run_id'], 'generated_at': generated,
            'counts': counts, 'cases': cases, 'issues': state['issues'], 'resources': state['resources'],
            'source_observations': state['source_checks'],
            'report_bundles': state.get('report_bundles', {}),
            'merged_report': state.get('merged_report'),
            'native_merge_required': state.get('native_merge_required', False),
            'report_paths': {'human_report': 'test/.cache/runs/' + state['run_id'] + '/report.md',
                             'machine_report': 'test/.cache/runs/' + state['run_id'] + '/summary.json',
                             'review_html': 'test/.cache/runs/' + state['run_id'] + '/report.html',
                             'merged_html': (state['merged_report']['html_report'] if state.get('merged_report') and
                                             not retention.is_recycled(project, state['merged_report']['html_report']) else None)},
            'report_consistency': report_consistency(project, state),
            'performance': {'source_cache_hits': sum(v.get('cache_hit') is True for v in state['source_checks'].values()),
                            'source_observations': len(state['source_checks']),
                            'regression_preflight': state.get('regression_preflight')},
            'timing': {'wall_seconds': (dt(generated) - dt(state['created_at'])).total_seconds(),
                       'phase_seconds': {p: union_seconds(v) for p, v in phases.items()},
                       'phase_union_seconds': union_seconds([i for values in phases.values() for i in values]),
                       'phase_records': state['phase_times'],
                       'human_wait_seconds': union_seconds(human_intervals)},
            'usage': dict(totals, complete_run_coverage=complete, recorded_subtotals=subtotals, measurements=usage), 'status': status(project, state, spec, cov),
            'evidence': sorted({p for e in state['events'] for p in e.get('evidence', [])}),
            'limits': ['File existence and state constraints do not prove semantic coverage.', 'Missing host usage remains null; no historical counts are inferred.']}


def initialize(project):
    specpath, covpath = project / 'test/spec.md', project / 'test/plans/coverage.json'
    root = Path(__file__).resolve().parent.parent
    spec_text = (root / 'assets/spec.template.md').read_text(encoding='utf-8')
    spec = parse_spec(root / 'assets/spec.template.md')
    cov = load(root / 'assets/coverage.template.json')
    files = []
    commands = {}
    for name in ['package.json', 'playwright.config.ts', 'playwright.config.js', 'pyproject.toml', 'pom.xml', 'test/package.json', 'test/playwright.config.ts']:
        path = project / name
        if path.is_file():
            files.append(name)
            if name.endswith('package.json'):
                try:
                    commands[name] = {key: 'npm run ' + key for key in load(path).get('scripts', {}) if re.fullmatch(r'[A-Za-z0-9:_-]+', key)}
                except (ValueError, OSError):
                    commands[name] = {'error': 'package file unreadable'}
    created, preserved = [], []
    for path, data in [(specpath, spec_text), (covpath, cov)]:
        relative = path.relative_to(project).as_posix()
        if path.exists():
            preserved.append(relative)
        else:
            atomic(path, data, exclusive=True)
            created.append(relative)
    try:
        spec = parse_spec(specpath)
        missing = config_missing(spec)
    except (ValueError, OSError):
        missing = ['spec.workflow-config']
    return {'created': created, 'preserved': preserved, 'discovered_files': files,
            'discovered_commands': commands, 'missing_configuration': missing}


def start(project, args, spec, cov):
    check(args.owner, '--owner required')
    check(not args.reclaim or args.reason, '--reclaim requires --reason')
    scopes = sorted(set(args.scopes.split(','))) if args.scopes else sorted(spec.get('scope', {}).get('fixed', []))
    check(set(scopes) <= set(spec.get('scope', {}).get('fixed', [])), 'selected scope is outside fixed project scope')
    runs = project / 'test/.cache/runs'
    runs.mkdir(parents=True, exist_ok=True)
    candidates = []
    prior_finished, overlapping = [], []
    superseded = {}
    check(not (args.resume and args.new_round), '--resume and --new-round are mutually exclusive')
    if args.resume:
        candidates = [run_path(project, args.resume) / 'state.json']
        check(candidates[0].is_file(), 'resume run does not exist')
    else:
        for path in sorted(runs.glob('*/state.json'), key=lambda p: load(p)['created_at']):
            state = load(path)
            compatible = state['scopes'] == scopes and state['fixed_scopes'] == sorted(spec.get('scope', {}).get('fixed', []))
            if compatible and state['acceptance'] not in TERMINAL:
                candidates.append(path)
            elif state['fixed_scopes'] == sorted(spec.get('scope', {}).get('fixed', [])) and set(state['scopes']) & set(scopes):
                if state['acceptance'] in TERMINAL:
                    prior_finished.append(path)
                else:
                    overlapping.append(state['run_id'])
        if args.new_round:
            check(not overlapping, 'overlapping unfinished runs require explicit completion or resume before changing scopes: ' + ','.join(overlapping))
            quota_path = safe_path(project, 'test/.cache/scheduling/quota.json')
            quota = load(quota_path) if quota_path.exists() else {'schema_version': 1, 'leases': {}}
            check(quota.get('schema_version') == 1 and isinstance(quota.get('leases'), dict), 'invalid quota ledger')
            for prior_path in candidates:
                prior = load(prior_path)
                claim = prior.get('claim')
                check(not claim or claim['owner'] == args.owner,
                      'new round cannot replace another owner; resume/reclaim it explicitly')
                check(not any(t.get('status') == 'running' for t in prior['tasks'].values()),
                      'running tasks must stop and persist recovery before a new round')
                check(not any(lease.get('status') == 'active' and lease.get('run_id') == prior['run_id']
                              for lease in quota['leases'].values()),
                      'active task reservations must finish and release/reap before a new round')
            for prior_path in candidates:
                prior = load(prior_path)
                prior['acceptance'] = 'partially_blocked'
                prior['claim'] = None
                prior.setdefault('events', []).append({
                    'type': 'acceptance', 'status': 'partially_blocked', 'owner': args.owner,
                    'reason': 'Superseded by an explicitly requested new regression round; unresolved work is inherited.',
                    'at': now(), 'seq': len(prior.get('events', [])) + 1})
                superseded[prior_path] = prior
                prior_finished.append(prior_path)
            candidates = []
        check(len(candidates) <= 1, 'multiple unfinished compatible runs; choose --resume')
    if candidates:
        path = candidates[0]
        state = load(path)
        check(state['scopes'] == scopes and state['fixed_scopes'] == sorted(spec.get('scope', {}).get('fixed', [])), 'resume scope mismatch')
        if state['acceptance'] in TERMINAL:
            check(args.resume, 'terminal run requires explicit --resume')
            state['acceptance'] = 'in_progress'
        claim = state.get('claim')
        if claim and claim['owner'] != args.owner:
            check(args.reclaim and args.reason, 'another task owns run; explicit --reclaim --reason required')
            claim_lease = max(platform_support.MIN_RUN_LEASE_SECONDS,
                              claim.get('lease_seconds', platform_support.MIN_RUN_LEASE_SECONDS))
            check((dt(now()) - dt(claim['renewed_at'])).total_seconds() >= claim_lease, 'active run cannot be reclaimed')
        elif claim and (dt(now()) - dt(claim['renewed_at'])).total_seconds() >= max(
                platform_support.MIN_RUN_LEASE_SECONDS,
                claim.get('lease_seconds', platform_support.MIN_RUN_LEASE_SECONDS)):
            check(args.reclaim and args.reason, 'expired run requires explicit --reclaim --reason')
        state['recovery_required'] = True
        state['source_checks'] = {}
        state['freshness_marker'] = secrets.token_hex(12)
        state['resume_history'].append({'at': now(), 'owner': args.owner, 'reason': args.reason, 'reclaimed': bool(args.reclaim)})
    else:
        check(not overlapping, 'overlapping unfinished runs require explicit completion or resume before changing scopes: ' + ','.join(overlapping))
        rid = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(4)
        path = run_path(project, rid) / 'state.json'
        state = {'schema_version': SCHEMA, 'run_id': rid, 'created_at': now(), 'scopes': scopes,
                 'fixed_scopes': sorted(spec.get('scope', {}).get('fixed', [])), 'acceptance': 'in_progress',
                 'native_merge_required': True,
                 'freshness_marker': secrets.token_hex(12), 'source_checks': {}, 'recovery_required': False,
                 'tasks': {}, 'humans': {}, 'case_actions': {}, 'resources': {}, 'issues': {}, 'usage': {},
                 'events': [], 'executions': [], 'phase_times': [], 'resume_history': [], 'invalidated': [],
                 'delegation_blocks': [], 'invalidation_seq': {}, 'asset_fingerprint': fingerprint(cov)}
        latest = {}
        for prior_path in prior_finished:
            prior = superseded.get(prior_path) or load(prior_path)
            for scope in set(prior['scopes']) & set(scopes):
                latest[scope] = prior_path
        for prior_path in sorted(set(latest.values()), key=lambda p: load(p)['created_at']):
            prior = superseded.get(prior_path) or load(prior_path)
            selected = set(scoped_cases(state, cov))
            source_ids = set(needed_sources(state, spec))
            def related(value):
                if value.get('case_ids'):
                    return bool(set(value['case_ids']) & selected)
                return not value.get('source_id') or value['source_id'] in source_ids
            pending = {
                'tasks': {k: v for k, v in prior['tasks'].items() if v['status'] not in DONE and related(v)},
                'humans': {k: v for k, v in prior['humans'].items() if human_pending(v) and related(v)},
                'resources': {k: v for k, v in prior['resources'].items() if v['status'] != 'absence_verified'},
                'issues': {k: v for k, v in prior.get('issues', {}).items()
                           if v.get('status') != 'closed' and
                           (not v.get('affected_case_ids') or set(v['affected_case_ids']) & selected)},
                'case_actions': {k: v for k, v in prior['case_actions'].items() if k in selected and (v.get('action') == 'needs_decision'
                    or v.get('code') in {'pending', 'running', 'blocked'} or v.get('exploration') in {'pending', 'running', 'blocked'})}}
            invalid = set(prior.get('invalidated', [])) & selected
            delegation = [v for v in prior.get('delegation_blocks', []) if related(v)]
            if any(pending.values()) or invalid or delegation:
                claim = prior.get('claim')
                check(not claim or claim['owner'] == args.owner, 'prior ended run retains another owner and unresolved work; explicitly resume/reclaim it first')
                state.setdefault('inherited_runs', []).append(prior['run_id'])
                state['inherited_from'] = prior['run_id']
                state['recovery_required'] = True
                for field, values in pending.items():
                    for key, value in values.items():
                        local_key = key if key not in state[field] or field == 'case_actions' else prior['run_id'] + '::' + key
                        copied = dict(value, origin_run=value.get('origin_run', prior['run_id']))
                        if field == 'resources':
                            copied['origin_resource_id'] = value.get('origin_resource_id', key)
                        if field == 'case_actions':
                            copied['action'] = 'needs_decision' if value.get('action') == 'needs_decision' else 'unchanged'
                        else:
                            copied[{'tasks': 'task_id', 'humans': 'request_id', 'resources': 'resource_id', 'issues': 'issue_id'}[field]] = local_key
                        if copied.get('case_ids'):
                            copied['case_ids'] = sorted(set(copied['case_ids']) & selected)
                        state[field][local_key] = copied
                state['invalidated'] = sorted(set(state['invalidated']) | invalid)
                state['delegation_blocks'].extend(copy.deepcopy(delegation))
        (path.parent / 'evidence').mkdir(parents=True, exist_ok=True)
        (path.parent / 'receipts').mkdir(parents=True, exist_ok=True)
    state['claim'] = {'owner': args.owner, 'renewed_at': now(), 'lease_seconds': LEASE_SECONDS}
    state['config_fingerprint'] = fingerprint(spec)
    for prior_path, prior in superseded.items():
        atomic(prior_path, prior)
    atomic(path, state)
    return status(project, state, spec, cov)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def collect_results(project, state, spec, cov, owner, request, full=False):
    """Register and import one invocation in memory; commit only after all gates pass."""
    check(state['acceptance'] not in TERMINAL, 'collect-results requires an active round')
    check(state.get('config_fingerprint', fingerprint(spec)) == fingerprint(spec),
          'configuration changed; resume and review inputs')
    check(state['asset_fingerprint'] == fingerprint(cov), 'coverage changed; resume and review artifacts')
    check(isinstance(request, dict) and request.get('type') == 'report_bundle',
          'collect-results input must be one report_bundle event')
    updated = copy.deepcopy(state)
    no_secrets(request)
    bundle = request.get('bundle', {})
    check(isinstance(bundle, dict) and isinstance(bundle.get('bundle_id'), str)
          and bundle['bundle_id'], 'collect-results requires bundle.bundle_id')
    bid = bundle.get('bundle_id')
    registered = updated.get('report_bundles', {}).get(bid)
    if registered is None:
        record_event(project, updated, spec, cov, dict(request, writer=owner))
    else:
        check(all(registered.get(key) == value for key, value in bundle.items()),
              'bundle differs from registered invocation; use a new bundle_id for a rerun')
    module_spec = importlib.util.spec_from_file_location('e2e_import_results', Path(__file__).with_name('import_results.py'))
    importer = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(importer)
    batch = importer.import_results(project, state['run_id'], bid,
                                    pending_bundle=updated['report_bundles'][bid])
    context = {}
    for event in batch['events']:
        record_event(project, updated, spec, cov, dict(event, writer=owner), context)
    updated['claim']['renewed_at'] = now()
    atomic(run_path(project, state['run_id']) / 'state.json', updated)
    return dict(status(project, updated, spec, cov, full=full, include_history=full), collection={
        'bundle_id': bid, 'committed': True, 'complete': not batch['unresolved'],
        'events_recorded': len(batch['events']), 'counts': batch['counts'],
        'unresolved': batch['unresolved']})



def doctor(project):
    """Read-only local diagnostics; no initialization, browser, or remote calls."""
    items = []
    spec = cov = None
    for name, reader, repair in [
            ('test/spec.md', parse_spec, 'run init if missing; keep exactly one valid json workflow-config block'),
            ('test/plans/coverage.json', load, 'run init if missing; repair coverage JSON and mappings')]:
        try:
            value = reader(project / name)
            check(isinstance(value, dict), 'root must be an object')
            if name.endswith('.md'):
                spec = value
            else:
                cov = value
            items.append({'check': name, 'status': 'ok'})
        except (ValueError, OSError, UnicodeError) as error:
            items.append({'check': name, 'status': 'error', 'reason': type(error).__name__, 'action': repair})
    if spec is not None:
        try:
            missing = config_missing(spec)
        except (ValueError, TypeError, AttributeError):
            missing = ['invalid_configuration_structure']
        items.append({'check': 'confirmed_configuration', 'status': 'error' if missing else 'ok',
                      'missing': missing, 'action': 'fill confirmed values in test/spec.md' if missing else None})
    if spec is not None and cov is not None:
        try:
            errors = asset_errors(project, spec, cov)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            errors = ['invalid asset shape: ' + type(error).__name__]
        items.append({'check': 'coverage_mappings', 'status': 'error' if errors else 'ok',
                      'errors': errors, 'action': 'repair listed coverage mappings' if errors else None})
    try:
        # --no-index tests the ignore policy even if cache files were accidentally tracked.
        # NUL framing avoids text-mode CRLF becoming part of the probed Git path.
        probe = subprocess.run(['git', 'check-ignore', '--no-index', '--stdin', '-z'],
                               input='test/.cache/__e2e_ignore_probe__\0', text=True,
                               capture_output=True, cwd=project, timeout=5)
        ignored = probe.returncode == 0 and bool(probe.stdout.strip())
        items.append({'check': 'cache_gitignore', 'status': 'ok' if ignored else 'error',
                      'action': None if ignored else 'ignore /test/.cache/ in repository policy before saving credentials/evidence'})
    except (OSError, subprocess.TimeoutExpired):
        items.append({'check': 'cache_gitignore', 'status': 'error',
                      'action': 'verify Git is available and /test/.cache/ is ignored'})
    return {'valid': all(item['status'] == 'ok' for item in items), 'checks': items,
            'note': 'Local diagnostics only; no source freshness, test discovery or business execution is proved.'}


def event_schema(event_type):
    document = load(Path(__file__).resolve().parent.parent / 'assets/events.schema.json')
    check(event_type in document['$defs'], 'unknown schema type; use execution or report_bundle')
    return {'$schema': document['$schema'], **document['$defs'][event_type]}


def main(argv=None):
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['doctor', 'schema', 'init', 'start', 'renew', 'prepare-regression', 'collect-results', 'record', 'status', 'validate', 'report', 'release'])
    parser.add_argument('--project')
    parser.add_argument('--type', dest='event_type')
    parser.add_argument('--run')
    parser.add_argument('--owner')
    parser.add_argument('--writer')
    parser.add_argument('--input')
    parser.add_argument('--resume')
    parser.add_argument('--scopes')
    parser.add_argument('--reclaim', action='store_true')
    parser.add_argument('--reason')
    parser.add_argument('--new-round', action='store_true')
    output = parser.add_mutually_exclusive_group()
    output.add_argument('--brief', action='store_true')
    output.add_argument('--full', action='store_true')
    try:
        args = parser.parse_args(argv)
    except ValueError as error:
        print(json.dumps({'error': str(error)}))
        return 2
    try:
        check(not (args.brief or args.full) or args.command in {'status', 'record', 'collect-results'},
              '--brief/--full only apply to status, record and collect-results')
        if args.command == 'schema':
            print(json.dumps(event_schema(args.event_type), ensure_ascii=False, indent=2))
            return 0
        check(args.project, '--project required')
        project = Path(args.project).resolve()
        check(project.is_dir(), 'project directory does not exist')
        if args.command == 'doctor':
            result = doctor(project)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['valid'] else 2
        if args.command == 'prepare-regression':
            check(args.run and args.owner, 'prepare-regression requires --run and --owner')
            # Retention owns the same project lock; do not recursively acquire it.
            with project_lock(project):
                spec, cov = assets(project)
                state = load(run_path(project, args.run) / 'state.json')
                owner_check(state, args.owner)
                check(state['acceptance'] not in TERMINAL, 'prepare-regression requires an active round')
                check(state.get('config_fingerprint', fingerprint(spec)) == fingerprint(spec),
                      'configuration changed; start --resume and review inputs before regression')
                check(state['asset_fingerprint'] == fingerprint(cov) and not state.get('recovery_required'),
                      'review changed artifacts or recovery before regression')
                scopes = sorted(set(args.scopes.split(','))) if args.scopes else state['scopes']
                check(set(scopes) <= set(state['scopes']), 'cleanup scope outside run')
                prior = state.get('regression_preflight', {})
                reusable = (prior.get('freshness_marker') == state['freshness_marker']
                            and prior.get('config_fingerprint') == fingerprint(spec))
                prepared_scopes = set(prior.get('plan_scopes', prior.get('scopes', []))) if reusable else set()
                pending_scopes = sorted(set(scopes) - prepared_scopes)
                if not pending_scopes:
                    state['claim']['renewed_at'] = now()
                    atomic(run_path(project, args.run) / 'state.json', state)
                    result = dict(status(project, state, spec, cov),
                                  retention=prior['retention'], preflight_reused=True)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 0
                planned_scopes = sorted(prepared_scopes | set(pending_scopes))
                preflight_marker = state['freshness_marker']
                preflight_config = fingerprint(spec)
            if spec.get('retention', {}).get('before_regression', True):
                # Include the union so mixed-module bundles become eligible on expansion.
                plan = retention.make_plan(project, planned_scopes, args.run)
                cleanup = retention.apply_plan(project, plan)
            else:
                cleanup = {'skipped': True, 'reason': 'explicit_project_retention_override'}
            with project_lock(project):
                spec, cov = assets(project)
                state = load(run_path(project, args.run) / 'state.json')
                owner_check(state, args.owner)
                check(state['freshness_marker'] == preflight_marker and fingerprint(spec) == preflight_config
                      and not state.get('recovery_required'), 'inputs changed during regression preflight')
                latest = state.get('regression_preflight', {})
                latest_valid = (latest.get('freshness_marker') == preflight_marker
                                and latest.get('config_fingerprint') == preflight_config)
                batches = list(latest.get('cleanup_batches', [])) if latest_valid else []
                latest_scopes = set(latest.get('scopes', [])) if latest_valid else set()
                batches.append({'scopes': pending_scopes, 'retention': cleanup})
                state['regression_preflight'] = {
                    'at': now(), 'scopes': sorted(latest_scopes | prepared_scopes | set(pending_scopes)),
                    'freshness_marker': preflight_marker, 'config_fingerprint': preflight_config,
                    'plan_scopes': planned_scopes, 'cleanup_batches': batches, 'retention': cleanup}
                state['claim']['renewed_at'] = now()
                atomic(run_path(project, args.run) / 'state.json', state)
                result = dict(status(project, state, spec, cov), retention=cleanup, preflight_reused=False)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        with project_lock(project):
            if args.command == 'init':
                result = initialize(project)
            else:
                spec, cov = assets(project)
                if args.command == 'start':
                    result = start(project, args, spec, cov)
                elif args.command == 'validate' and not args.run:
                    errors = asset_errors(project, spec, cov)
                    result = {'valid': not errors, 'errors': errors, 'missing_configuration': config_missing(spec)}
                else:
                    check(args.run, '--run required')
                    directory = run_path(project, args.run)
                    path = directory / 'state.json'
                    state = load(path)
                    if args.command in {'renew', 'collect-results', 'record', 'report', 'release'}:
                        owner_check(state, args.owner)
                    if args.command == 'renew':
                        result = renew(project, state, spec, cov, args.owner)
                    elif args.command == 'status':
                        result = status(project, state, spec, cov, full=not args.brief, include_history=not args.brief)
                    elif args.command == 'collect-results':
                        check(args.input, '--input required')
                        check(not args.writer or args.writer == args.owner, 'collect-results requires the main owner')
                        result = collect_results(project, state, spec, cov, args.owner, load(Path(args.input)), full=args.full)
                    elif args.command == 'record':
                        check(args.input, '--input required')
                        check(state['asset_fingerprint'] == fingerprint(cov) or state['recovery_required'], 'coverage changed externally; resume and review artifacts')
                        events = load(Path(args.input))
                        if isinstance(events, dict):
                            events = [events]
                        check(isinstance(events, list) and events, 'input must be an event or nonempty event array')
                        updated, newcov = copy.deepcopy(state), copy.deepcopy(cov)
                        context = {}
                        for event in events:
                            writer = args.writer or args.owner
                            if writer != args.owner:
                                check(event.get('type') == 'task', 'delegated writers may submit their assigned task receipts only')
                                assigned = updated['tasks'].get(event.get('task_id'), {})
                                check(assigned.get('owner') == writer and event.get('owner') == writer, 'writer is not assigned to this task')
                                check(event.get('case_ids', assigned.get('case_ids', [])) == assigned.get('case_ids', []) and event.get('source_id', assigned.get('source_id')) == assigned.get('source_id'), 'writer may not expand assigned task scope')
                            event = dict(event, writer=writer)
                            record_event(project, updated, spec, newcov, event, context)
                        if newcov != cov:
                            check(not asset_errors(project, spec, newcov), 'coverage mappings invalid: ' + '; '.join(asset_errors(project, spec, newcov)))
                        updated['claim']['renewed_at'] = now()
                        if newcov != cov or any(e.get('type') == 'recovery_review' for e in events):
                            updated['asset_fingerprint'] = fingerprint(newcov)
                        if newcov != cov:
                            atomic(project / 'test/plans/coverage.json', newcov)
                        atomic(path, updated)
                        for event in updated['events'][len(state['events']):]:
                            if event['type'] == 'task':
                                atomic(directory / 'receipts' / (str(event['seq']) + '.json'), event)
                        result = status(project, updated, spec, newcov, full=args.full, include_history=args.full)
                    elif args.command == 'release':
                        state['claim'] = None
                        atomic(path, state)
                        result = {'run_id': state['run_id'], 'released': True, 'acceptance': state['acceptance']}
                    elif args.command == 'validate':
                        errors = asset_errors(project, spec, cov)
                        if state['asset_fingerprint'] != fingerprint(cov):
                            errors.append('coverage asset changed; recovery review required')
                        for event in state['events']:
                            try:
                                require_evidence(project, event, allow_recycled=state['acceptance'] in TERMINAL)
                            except ValueError as error:
                                errors.append(str(error))
                        consistency = report_consistency(project, state)
                        errors.extend(consistency['errors'])
                        result = {'valid': not errors, 'errors': errors, 'report_consistency': consistency,
                                  'status': status(project, state, spec, cov)}
                    else:
                        result = summary(project, state, spec, cov)
                        atomic(directory / 'summary.json', result)
                        atomic(directory / 'report.html', report_artifacts.review_html(
                            project, state, result['report_consistency']))
                        lines = ['# E2E Run ' + state['run_id'], '', 'Acceptance: ' + state['acceptance'],
                                 '', '报告一致性：' + result['report_consistency']['status'], '']
                        lines += ['[本轮HTML入口：未通过优先](<' + str(directory / 'report.html') + '>)', '']
                        if result['report_paths']['merged_html']:
                            lines += ['[合并的Playwright原生HTML](<' + str(safe_path(
                                project, result['report_paths']['merged_html'])) + '>)', '']
                        for bid, bundle in result['report_bundles'].items():
                            raw = str(safe_path(project, bundle['execution_result']))
                            raw_label = ('原始JSON已按保留策略回收：' + raw if retention.is_recycled(project, bundle['execution_result'])
                                         else '[原始JSON](<' + raw + '>)')
                            lines += ['- ' + bid + '：' + raw_label + '；HTML来源：' + bundle['html_origin']]
                            if bundle.get('html_report'):
                                html_path = bundle['html_report']
                                lines += [('  - HTML报告已按保留策略回收：' + html_path if retention.is_recycled(project, html_path)
                                           else '  - [HTML报告](<' + str(safe_path(project, html_path)) + '>)')]
                            if bundle.get('rebuild_note'):
                                lines += ['  - 重建说明：' + bundle['rebuild_note']]
                        lines += ['- ' + error for error in result['report_consistency']['errors']]
                        lines += failure_markdown(state, cov, project)
                        lines += ['', '## Counts', '', '```json', json.dumps(result['counts'], indent=2), '```', '', '## Timing and Usage', '', '```json', json.dumps({'timing': result['timing'], 'usage': result['usage']}, indent=2), '```', '', '## Cases', '']
                        lines += ['| Case | Result | Code Coverage | Plan | Exploration | Code |', '| --- | --- | --- | --- | --- | --- |']
                        for cid, data in result['cases'].items():
                            action = data['action'] or {}
                            cells = [cid, data['result'], str(data['covered']), action.get('action', 'unrecorded'), action.get('exploration_action', 'unrecorded'), action.get('code_action', 'unrecorded')]
                            lines.append('| ' + ' | '.join(str(c).replace('|', '\\|').replace('\n', ' ') for c in cells) + ' |')
                        lines += ['', '## Per-Case Executions', '']
                        for cid, data in result['cases'].items():
                            action = data['action'] or {}
                            lines += ['- ' + cid + ': result=' + data['result'] + '; covered=' + str(data['covered'])
                                      + '; plan=' + action.get('action', 'unrecorded')
                                      + '; exploration=' + action.get('exploration_action', 'unrecorded')
                                      + '; code=' + action.get('code_action', 'unrecorded')]
                            lines += ['  - execution=' + e['execution_id'] + '; test=' + e['test_id'] + '; attempt=' + str(e['attempt']) + '; result=' + e['result'] for e in data['executions']]
                        lines += ['', '## Setup Executions', '']
                        lines += ['- ' + e['execution_id'] + ': test=' + e['test_id'] + '; attempt=' + str(e['attempt']) + '; result=' + e['result'] for e in state['executions'] if e['setup']]
                        lines += ['', '## Issues', '']
                        lines += ['- ' + key + ': kind=' + value['kind'] + '; affected=' + ','.join(value.get('affected_case_ids', [])) for key, value in state['issues'].items()]
                        lines += ['', '## Resources and Evidence', '', '```json', json.dumps({'resources': result['resources'], 'evidence': result['evidence']}, indent=2), '```', '', 'File existence and state constraints do not prove semantic coverage.']
                        atomic(directory / 'report.md', '\n'.join(lines) + '\n')
                        state['claim']['renewed_at'] = now()
                        atomic(path, state)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if result.get('valid') is False or result.get('report_consistency', {}).get('status') == 'failed' else 0
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, OSError) as error:
        result = {'error': str(error)}
        if isinstance(error, retention.CleanupCandidateMismatch):
            result['diagnostics'] = error.diagnostics
        print(json.dumps(result, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
