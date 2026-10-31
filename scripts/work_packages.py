#!/usr/bin/env python3
"""Group normalized remaining work without changing execution or acceptance history."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

from checkpoint import check, digest, identifier, read, safe

CATEGORIES = {'product_candidate', 'needs_decision', 'environment', 'script', 'condition_missing',
              'api_exclusive', 'deferred', 'unbound_observation', 'cleanup_unknown'}


def action(item):
    if item.get('permission_required'):
        return 'awaiting_authorization'
    if item.get('cleanup_unknown') or item['category'] == 'cleanup_unknown':
        return 'cleanup_review_no_blind_retry'
    if item['category'] == 'script':
        return 'repair_then_affected'
    if item['category'] == 'environment':
        return ('awaiting_environment_change' if item.get('environment_changed') is False
                else 'diagnose_dependency_before_retry')
    return {'product_candidate': 'triage_product_no_automatic_fix', 'needs_decision': 'diagnose_readonly',
            'condition_missing': 'supply_condition', 'api_exclusive': 'handoff_api',
            'deferred': 'preserve_deferred', 'unbound_observation': 'preserve_evidence_limitation'}[item['category']]


def build(project, request):
    check(set(request) == {'items'}, 'worklist requires items only')
    check(isinstance(request['items'], list), 'items must be a list')
    groups, seen, checked_evidence = {}, set(), set()
    allowed = {'case_id', 'category', 'root_cause_id', 'component', 'evidence', 'repair_attempts',
               'confirmation_attempts', 'permission_required', 'cleanup_unknown', 'environment_changed'}
    for item in request['items']:
        check(isinstance(item, dict) and set(item) <= allowed, 'unknown item fields; normalize raw reports first')
        identifier(item['case_id'])
        check(item['case_id'] not in seen, 'duplicate item identity; use distinct facet IDs')
        seen.add(item['case_id'])
        check(item['category'] in CATEGORIES, 'unknown category')
        for key in ['root_cause_id', 'component']:
            if key in item:
                identifier(item[key])
        for key in ['repair_attempts', 'confirmation_attempts']:
            value = item.get(key, 0)
            check(isinstance(value, int) and not isinstance(value, bool) and value >= 0, 'invalid attempt count')
        for key in ['permission_required', 'cleanup_unknown', 'environment_changed']:
            check(key not in item or isinstance(item[key], bool), 'safety flags must be boolean')
        check(isinstance(item.get('evidence'), list) and item['evidence'], 'evidence required')
        for name in item['evidence']:
            check(isinstance(name, str), 'evidence path must be a string')
            if name not in checked_evidence:
                check(safe(project, name).is_file(), 'evidence missing')
                checked_evidence.add(name)
        todo = action(item)
        basis = 'explicit_root_cause' if item.get('root_cause_id') else ('component_triage_only' if item.get('component') else 'individual')
        group_id = item.get('root_cause_id') or item.get('component') or item['case_id']
        key = (item['category'], todo, basis, group_id)
        group = groups.setdefault(key, {'package_id': 'work-' + digest(key)[:16], 'category': item['category'],
                                       'action': todo, 'grouping_basis': basis, 'group_id': group_id,
                                       'case_ids': [], 'evidence': [], 'constraints_by_case': {}})
        group['case_ids'].append(item['case_id'])
        group['constraints_by_case'][item['case_id']] = {
            key: item[key] for key in ['repair_attempts', 'confirmation_attempts', 'permission_required',
                                      'cleanup_unknown', 'environment_changed'] if key in item}
        group['evidence'] = sorted(set(group['evidence']) | set(item['evidence']))
    packages = sorted(groups.values(), key=lambda g: (g['action'], g['category'], g['group_id']))
    for group in packages:
        group['case_ids'].sort()
    return {'schema_version': 1, 'input_sha256': digest(request), 'counts': dict(Counter(i['category'] for i in request['items'])),
            'item_count': len(seen), 'package_count': len(packages), 'packages': packages,
            'execution_or_acceptance_changed': False,
            'boundary': 'Component grouping is ownership triage, not proof of a shared root cause. No retries or approval are inferred.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        project = Path(args.project).resolve()
        result = build(project, read(Path(args.input)))
        output = safe(project, args.output, cache=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if output.exists():
            check(output.read_text(encoding='utf-8') == text, 'output exists with different input; use a new output name')
        else:
            with output.open('x', encoding='utf-8') as stream:
                stream.write(text)
        print(json.dumps({k: result[k] for k in ['item_count', 'package_count', 'counts', 'execution_or_acceptance_changed']}, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({'valid': False, 'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
