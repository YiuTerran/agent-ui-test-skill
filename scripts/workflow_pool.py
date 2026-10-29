"""Cross-run identity pool leases. The ledger stores account reference names only, never credentials."""
from pathlib import Path
import re
from types import SimpleNamespace

RETURN_RESULTS = {'pooled': 'available', 'quarantined': 'quarantined', 'disabled': 'disabled'}
RESOLVE_RESULTS = {'available', 'disabled'}


def lease_path(wf, project, run_id):
    return wf.run_path(project, run_id) / 'identity-leases.json'


def write_leases(wf, project, run_id, ledger):
    """Keep each runner's slot indices stable, including partially returned requests."""
    requests = {}
    for name, pool in sorted(ledger['pools'].items()):
        accounts = pool.get('accounts', {})
        for request_id, request in pool.get('requests', {}).get(run_id, {}).items():
            assigned = [ref if held_by(accounts.get(ref, {}), run_id, request_id) else None
                        for ref in request['accounts']]
            requests.setdefault(request_id, {'pools': {}})['pools'][name] = {
                'worker_count': request['count'], 'accounts': assigned}
    path = lease_path(wf, project, run_id)
    wf.atomic(path, {'schema_version': 2, 'run_id': run_id, 'requests': requests})
    return path.relative_to(project).as_posix()


def held_by(account, run_id, request_id):
    return (account.get('status') == 'leased' and account.get('leased_by_run') == run_id
            and account.get('leased_by_request') == request_id)


def close_returned_requests(wf, project, state, ledger):
    """Keep replay tombstones in the run before dropping global request details."""
    closed = {}
    for name, pool in ledger['pools'].items():
        requests = pool.get('requests', {})
        if state['run_id'] in requests and not any(
                account.get('status') == 'leased' and account.get('leased_by_run') == state['run_id']
                for account in pool['accounts'].values()):
            closed[name] = requests[state['run_id']]
    if not closed:
        return
    tombstones = state.setdefault('closed_pool_requests', {})
    for name, requests in closed.items():
        tombstones[name] = sorted(set(tombstones.get(name, [])) | set(requests))
    # A crash before global pruning leaves redundant details, never a reusable ID.
    wf.atomic(wf.run_path(project, state['run_id']) / 'state.json', state)
    for name in closed:
        requests = ledger['pools'][name]['requests']
        del requests[state['run_id']]
        if not requests:
            del ledger['pools'][name]['requests']
    wf.atomic(wf.safe_path(project, wf.POOL_LEDGER), ledger)


def request_name(wf, args):
    value = args.request
    wf.check(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value),
             '--request must identify this runner invocation (1-64 letters, digits, _ or -)')
    return value


def worker_account(wf, project, args, state, spec):
    """Verify the authoritative assignment before a fixture uses an identity."""
    wf.pool_config(spec, args.pool)
    request_id = request_name(wf, args)
    pool = wf.pool_ledger(project)['pools'].get(args.pool, {})
    request = pool.get('requests', {}).get(state['run_id'], {}).get(request_id)
    wf.check(request, 'unknown pool lease request')
    wf.check(type(args.workers) is int and args.workers == request['count'],
             '--workers must match the leased concurrent slot count; adjust concurrency or lease a new request')
    wf.check(type(args.slot) is int and 0 <= args.slot < request['count'],
             '--slot outside leased concurrent slots; do not wrap worker indices')
    ref = request['accounts'][args.slot]
    wf.check(held_by(pool['accounts'].get(ref, {}), state['run_id'], request_id),
             'slot account is no longer leased by this runner')
    return {'valid': True, 'pool_ref': ref, 'request': request_id, 'slot': args.slot}


def account_items(wf, project, args):
    wf.check(args.input, '--input required')
    request = wf.load(Path(args.input))
    wf.check(isinstance(request, dict) and set(request) <= {'accounts'}, 'input must be {"accounts": [...]}')
    wf.no_secrets(request)
    items = request.get('accounts')
    wf.check(isinstance(items, list) and items and all(isinstance(i, dict) for i in items),
             'accounts must be a nonempty array')
    refs = [i.get('pool_ref') for i in items]
    wf.check(len(set(refs)) == len(refs), 'duplicate pool_ref in input')
    for item in items:
        wf.check(isinstance(item.get('evidence'), list) and item['evidence'],
                 'each account requires readback evidence: ' + str(item.get('pool_ref')))
        wf.require_evidence(project, item)
    return items


def history(wf, account, state, action, item=None):
    entry = {'at': wf.now(), 'run_id': state['run_id'], 'action': action}
    if item:
        entry.update({k: item[k] for k in ('evidence', 'reason', 'issue_id') if item.get(k)})
    account.setdefault('history', []).append(entry)
    account['updated_at'] = entry['at']


def lease(wf, project, args, state, spec):
    limit = wf.pool_config(spec, args.pool)['max_accounts']
    request_id = request_name(wf, args)
    wf.check(type(args.count) is int and args.count > 0, '--count must be a positive integer')
    wf.check(request_id not in state.get('closed_pool_requests', {}).get(args.pool, []),
             'lease request has returned accounts; use a new request for a new runner invocation')
    ledger = wf.sync_pool_ledger(project, state)
    pool = ledger['pools'].setdefault(args.pool, {'accounts': {}})
    accounts = pool['accounts']
    requests = pool.setdefault('requests', {}).setdefault(state['run_id'], {})
    prior = requests.get(request_id)
    if prior:
        wf.check(prior['count'] == args.count, 'lease request count cannot change')
        wf.check(all(held_by(accounts.get(ref, {}), state['run_id'], request_id) for ref in prior['accounts']),
                 'lease request has returned accounts; use a new request for a new runner invocation')
        return {'valid': True, 'pool': args.pool, 'request': request_id,
                'leased': prior['accounts'], 'replayed': True,
                'lease_file': write_leases(wf, project, state['run_id'], ledger)}
    available = sorted(ref for ref, a in accounts.items() if a['status'] == 'available')
    if len(available) < args.count:
        return {'valid': False, 'error': 'pool_exhausted', 'pool': args.pool, 'requested': args.count,
                'available': len(available), 'occupied': wf.pool_occupancy(project, state, args.pool),
                'max_accounts': limit,
                'next_action': 'return or resolve accounts, or create new ones only while occupied < max_accounts'}
    assigned = available[:args.count]
    requests[request_id] = {'count': args.count, 'accounts': assigned}
    for ref in assigned:
        account = accounts[ref]
        account.pop('last_return', None)
        account.update(status='leased', leased_by_run=state['run_id'],
                       leased_by_request=request_id, leased_at=wf.now())
        history(wf, account, state, 'leased')
    wf.atomic(wf.safe_path(project, wf.POOL_LEDGER), ledger)
    return {'valid': True, 'pool': args.pool, 'request': request_id, 'leased': assigned,
            'lease_file': write_leases(wf, project, state['run_id'], ledger)}


def give_back(wf, project, args, state, spec):
    wf.pool_config(spec, args.pool)
    items = account_items(wf, project, args)
    ledger = wf.sync_pool_ledger(project, state)
    accounts = ledger['pools'].get(args.pool, {}).get('accounts', {})
    # Validate the whole batch before changing any account.
    for item in items:
        account = accounts.get(item.get('pool_ref'))
        if account and account.get('last_return') == {'run_id': state['run_id'], 'item': item}:
            continue
        wf.check(account and account['status'] == 'leased' and account.get('leased_by_run') == state['run_id'],
                 'account is not leased by this run: ' + str(item.get('pool_ref')))
        wf.check(item.get('result') in RETURN_RESULTS, 'result must be pooled, quarantined or disabled')
        if item['result'] == 'quarantined':
            issue = state['issues'].get(item.get('issue_id'), {})
            wf.check(issue.get('status') == 'open' and issue.get('kind') == 'environment',
                     'quarantine requires an open environment issue recorded in this run')
    for item in items:
        account = accounts[item['pool_ref']]
        if account.get('last_return') == {'run_id': state['run_id'], 'item': item}:
            continue
        for key in ('leased_by_run', 'leased_at', 'leased_by_request'):
            account.pop(key, None)
        account['last_return'] = {'run_id': state['run_id'], 'item': item}
        account['status'] = RETURN_RESULTS[item['result']]
        if item['result'] == 'pooled':
            account['baseline_evidence'] = item['evidence']
        if item['result'] == 'quarantined':
            account['quarantine'] = {'run_id': state['run_id'], 'issue_id': item['issue_id'],
                                     'reason': item.get('reason'), 'evidence': item['evidence'], 'at': wf.now()}
        history(wf, account, state, 'returned_' + item['result'], item)
    wf.atomic(wf.safe_path(project, wf.POOL_LEDGER), ledger)
    close_returned_requests(wf, project, state, ledger)
    return {'valid': True, 'pool': args.pool, 'returned': {i['pool_ref']: i['result'] for i in items},
            'lease_file': write_leases(wf, project, state['run_id'], ledger)}


def resolve(wf, project, args, state, spec):
    wf.pool_config(spec, args.pool)
    items = account_items(wf, project, args)
    ledger = wf.sync_pool_ledger(project, state)
    accounts = ledger['pools'].get(args.pool, {}).get('accounts', {})
    for item in items:
        account = accounts.get(item.get('pool_ref'))
        wf.check(account and account['status'] == 'quarantined', 'account is not quarantined: ' + str(item.get('pool_ref')))
        wf.check(item.get('result') in RESOLVE_RESULTS, 'result must be available or disabled')
        wf.check(isinstance(item.get('reason'), str) and item['reason'], 'resolution requires a reason')
    for item in items:
        account = accounts[item['pool_ref']]
        account['resolved_quarantine'] = account.pop('quarantine')
        account['status'] = item['result']
        if item['result'] == 'available':
            account['baseline_evidence'] = item['evidence']
        history(wf, account, state, 'resolved_' + item['result'], item)
    wf.atomic(wf.safe_path(project, wf.POOL_LEDGER), ledger)
    return {'valid': True, 'pool': args.pool, 'resolved': {i['pool_ref']: i['result'] for i in items}}


def dispatch(api, project, args):
    wf = SimpleNamespace(**api)
    wf.check(args.pool, '--pool required')
    with wf.project_lock(project):
        spec, _ = wf.assets(project)
        wf.check(args.run, '--run required')
        state = wf.load(wf.run_path(project, args.run) / 'state.json')
        if args.command == 'pool-account':
            wf.active_claim_check(state)
        else:
            wf.owner_check(state, args.owner)
        wf.check(state['acceptance'] not in wf.TERMINAL, 'pool commands require an active round')
        handler = {'pool-lease': lease, 'pool-account': worker_account,
                   'pool-return': give_back, 'pool-resolve': resolve}[args.command]
        return handler(wf, project, args, state, spec)
