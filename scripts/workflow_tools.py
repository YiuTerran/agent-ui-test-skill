"""Local preparation and read-only diagnostics. Never execute tests or contact sources."""
from collections import Counter
from datetime import timedelta
import importlib.util
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from types import SimpleNamespace


def sibling(name):
    spec = importlib.util.spec_from_file_location('e2e_tools_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def input_object(wf, args, required=False):
    wf.check(args.input or not required, '--input required')
    value = wf.load(Path(args.input)) if args.input else {}
    wf.check(isinstance(value, dict), 'input must be an object')
    return value


def run_state(wf, project, args, owned=False, active=False):
    wf.check(args.run, '--run required')
    state = wf.load(wf.run_path(project, args.run) / 'state.json')
    if owned:
        wf.owner_check(state, args.owner)
    if active:
        wf.check(state['acceptance'] not in wf.TERMINAL, 'command requires an active round')
    return state


def safe_cache(wf, project, relative):
    """Preparation must not follow symlinks, including an in-project alias."""
    wf.check(relative.startswith('test/.cache/'), 'output must be inside test/.cache/')
    path = wf.safe_path(project, relative)
    lexical = project / relative
    for item in [lexical, *lexical.parents]:
        wf.check(not item.is_symlink()
                 and not (item.exists() and getattr(item.lstat(), 'st_file_attributes', 0) & 0x400),
                 'cache path must not contain symlinks or reparse points')
    return path


def source_checks(wf, project, state, spec, cov, request):
    requests = request.get('sources', [])
    wf.check(isinstance(requests, list), 'sources must be a verify-request array')
    verifier = sibling('source_cache')
    results = {}
    for item in requests:
        wf.check(isinstance(item, dict), 'source verify request must be an object')
        sid = item.get('observation', {}).get('source_id')
        wf.check(sid in wf.needed_sources(state, spec) and sid not in results,
                 'source outside run or duplicate source request')
        # A resumed run starts a new observation round; creation time is insufficient.
        round_start = max([state['created_at']] +
                          [v['at'] for v in state.get('resume_history', [])], key=wf.dt)
        result = verifier.verify(project, dict(item, round_started_at=round_start))
        baseline = cov.get('sources', {}).get(sid, {})
        observation = item.get('observation', {})
        result['matches_effective_baseline'] = (
            result['cache_hit']
            and all(observation.get(k) == baseline.get(k) for k in ('revision', 'fingerprint')))
        results[sid] = result
    return {sid: results.get(sid, {'action': 'observe', 'cache_hit': False,
                                  'reason': 'fresh_observation_not_supplied'})
            for sid in wf.needed_sources(state, spec)}


def discovery_diff(wf, project, state, spec, cov, request):
    """Compare a supplied, unfiltered Playwright --list JSON with reviewed mappings."""
    discovery = request.get('discovery')
    selected = wf.scoped_cases(state, cov)
    context = {}
    reasons = {cid: wf.coverage_reasons(project, cov, cid, context)
               for cid, case in selected.items() if case.get('execution') == 'automation'}
    result = {'status': 'unknown', 'reason': 'runner_discovery_not_supplied',
              'code_review_gaps': {cid: r for cid, r in reasons.items() if r},
              'missing_plan_files': [], 'missing_tests': [], 'unmapped_tests': [],
              'reviewed_missing_tests': [],
              'ambiguous_tests': [], 'matched_cases': [], 'setup_targets': 0,
              'business_target_count': None,
              'legacy_shared_tests': [],
              'code_roots_suggested': [], 'code_references_outside_snapshot': [], 'code_import_warnings': [],
              'requirement_mapping_errors': wf.asset_errors(project, spec, cov),
              'independent_test_execution_proved': False}
    if discovery is None:
        return result
    wf.check(isinstance(discovery, dict) and discovery.get('complete') is True,
             'discovery requires complete=true for an unfiltered inventory')
    wf.check(isinstance(discovery.get('setup_projects'), list), 'discovery requires explicit setup_projects')
    path = wf.safe_path(project, discovery['path'])
    raw = wf.load(path)
    wf.check(isinstance(raw, dict) and isinstance(raw.get('suites'), list)
             and not raw.get('errors'), 'discovery JSON is invalid or contains runner errors')
    importer = sibling('import_results')
    root = Path(raw.get('config', {}).get('rootDir') or project)
    root = (root if root.is_absolute() else project / root).resolve()
    wf.check(root.is_relative_to(project), 'runner root outside project')
    setups = set(discovery['setup_projects'])
    rows = []
    for spec, test in wf.report_artifacts.json_tests(raw):
        file = importer.relative_file(project, spec.get('file'), root)
        wf.check(file is not None and wf.safe_path(project, file).is_file(),
                 'discovery spec file is missing or outside project')
        if test.get('projectName') in setups:
            result['setup_targets'] += 1
        else:
            rows.append({'file': file, 'title': spec['title'], 'project': test.get('projectName', '')})
    counts = Counter((v['file'], v['title'], v['project']) for v in rows)
    mapping, legacy_mapping = {}, {}
    for cid, case in selected.items():
        if case.get('execution') == 'automation':
            target = legacy_mapping if case.get('legacy_group') else mapping
            target.setdefault((case.get('spec'), case.get('title')), []).append(cid)
        if case.get('plan') and not wf.safe_path(project, case['plan']).is_file():
            result['missing_plan_files'].append(cid)
    matched = set()
    for row in rows:
        ids = mapping.get((row['file'], row['title']), [])
        legacy = legacy_mapping.get((row['file'], row['title']), [])
        if legacy:
            result['legacy_shared_tests'].append(dict(row, case_ids=legacy))
        if not ids and legacy:
            continue
        if not ids:
            result['unmapped_tests'].append(row)
        elif len(ids) != 1 or counts[(row['file'], row['title'], row['project'])] != 1:
            result['ambiguous_tests'].append(dict(row, case_ids=ids))
        else:
            matched.update(ids)
    result.update(status='checked', reason=None, matched_cases=sorted(matched),
                  business_target_count=len(rows),
                  missing_tests=sorted(cid for cid, case in selected.items()
                                       if case.get('execution') == 'automation'
                                       and not case.get('legacy_group') and cid not in matched),
                  evidence=discovery['path'], evidence_sha256=wf.hashlib.sha256(path.read_bytes()).hexdigest())
    result['reviewed_missing_tests'] = [cid for cid in result['missing_tests'] if not reasons[cid]]
    roots = request.get('code_roots', [])
    string_array(wf, roots, 'code_roots')
    result.update(wf.report_artifacts.code_import_hints(project, raw, roots))
    return result


def preflight(wf, project, args):
    request = input_object(wf, args)
    diagnostics = wf.doctor(project)
    if not diagnostics['valid']:
        return {'valid': False, 'diagnostics': diagnostics, 'run_started': False}
    with wf.project_lock(project):
        spec, cov = wf.assets(project)
        if args.run:
            wf.check(not (args.resume or args.new_round or args.reclaim or args.scopes),
                     'with --run use renew semantics; use --resume/--scopes on start instead')
            state = run_state(wf, project, args, owned=True, active=True)
            # Validate external inputs before changing the claim.
            sources = source_checks(wf, project, state, spec, cov, request)
            diff = discovery_diff(wf, project, state, spec, cov, request)
            view = wf.renew(project, state, spec, cov, args.owner, full=False)
        else:
            view = wf.start(project, args, spec, cov, full=False)
            args.run = view['run_id']
            state = run_state(wf, project, args)
            try:
                sources = source_checks(wf, project, state, spec, cov, request)
                diff = discovery_diff(wf, project, state, spec, cov, request)
            except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
                # start may have inherited obligations; never silently roll it back.
                return {'valid': False, 'run': view, 'diagnostics': diagnostics,
                        'error': str(error), 'run_started': True,
                        'next_steps': ['repair_supplied_evidence_then_reuse_this_run']}
        if diff['status'] == 'checked':
            context = {'coverage': {cid: diff['code_review_gaps'].get(cid, [])
                                    for cid in wf.scoped_cases(state, cov)}}
            gaps = {cid for cid in wf.scoped_cases(state, cov)
                    if wf.automation_gap(project, state, cov, cid, context, include_discovery=False)}
            gaps.update(diff['missing_tests'])
            gaps.update(cid for row in diff['legacy_shared_tests'] for cid in row['case_ids'])
            state['automation_gap_case_ids'] = sorted(gaps)
            inventory = {
                'path': diff['evidence'], 'sha256': diff['evidence_sha256'],
                'setup_projects': request['discovery']['setup_projects'],
                'scopes': state['scopes'], 'freshness_marker': state['freshness_marker'],
                'missing_tests': diff['missing_tests'],
                'legacy_shared_tests': diff['legacy_shared_tests'],
                'automation_gap_case_ids': sorted(gaps),
                'reviewed_missing_tests': diff['reviewed_missing_tests'],
                'business_target_count': diff['business_target_count'],
                'config_fingerprint': wf.fingerprint(spec), 'asset_fingerprint': wf.fingerprint(cov)}
            previous = state.get('discovery_inventory')
            if previous and previous != inventory:
                state.setdefault('discovery_inventory_history', []).append(previous)
            state['discovery_inventory'] = inventory
            wf.atomic(wf.run_path(project, args.run) / 'state.json', state)
            view = wf.status(project, state, spec, cov, full=False, include_history=False)
        return {'valid': True, 'run': view, 'diagnostics': diagnostics,
                'sources': sources, 'coverage_diff': diff,
                'next_steps': view['next_steps'],
                'note': 'Local preflight; source observations/discovery are caller evidence. '
                        'No source event, approval, regression cleanup or test execution is recorded.'}


def validate_input(wf, project, args):
    wf.check(args.input, '--input required')
    wf.check(args.event_type in {'source', 'record', 'execution', 'report_bundle'},
             '--type must be source, record, execution or report_bundle')
    # No project lock is created: dry replay never commits and is only a point-in-time preview.
    spec, cov = wf.assets(project)
    state = run_state(wf, project, args, owned=True)
    request = wf.load(Path(args.input))
    if args.event_type == 'report_bundle':
        wf.check(not args.writer or args.writer == args.owner, 'collect-results requires the main owner')
        result = wf.collect_results(project, state, spec, cov, args.owner, request, dry_run=True)
        return {'valid': True, 'committed': False, 'collection': result['collection'],
                'scope': 'collect-results replay; unresolved mappings remain unresolved'}
    events = [request] if isinstance(request, dict) else request
    wf.check(isinstance(events, list) and events, 'input must be an event or nonempty event array')
    if args.event_type != 'record':
        wf.check(all(isinstance(e, dict) and e.get('type') == args.event_type for e in events),
                 'event type differs from --type')
    wf.apply_events(project, state, spec, cov, args.owner, events, args.writer)
    return {'valid': True, 'committed': False, 'events_validated': len(events),
            'scope': 'record replay; actual submission rechecks current state and evidence'}


def string_array(wf, value, name, nonempty=False):
    wf.check(isinstance(value, list) and (value or not nonempty)
             and all(isinstance(v, str) and v for v in value), name + ' must be a string array')
    wf.check(len(value) == len(set(value)) or name == 'command', name + ' must be unique')


def attempt_plan(wf, project, args, state):
    wf.check(re.fullmatch(r'[A-Za-z0-9_-]+', args.bundle or ''), '--bundle must be a portable bundle ID')
    relative = 'test/.cache/runs/' + state['run_id'] + '/attempts/' + args.bundle
    directory = safe_cache(wf, project, relative)
    return relative, directory


def attempt_init(wf, project, args):
    request = input_object(wf, args, required=True)
    wf.no_secrets(request)
    with wf.project_lock(project):
        spec, cov = wf.assets(project)
        state = run_state(wf, project, args, owned=True, active=True)
        wf.check(state.get('config_fingerprint') == wf.fingerprint(spec)
                 and state['asset_fingerprint'] == wf.fingerprint(cov)
                 and not state.get('recovery_required'), 'review changed configuration/artifacts before preparing an attempt')
        for key in ('command', 'scope', 'setup_projects'):
            string_array(wf, request.get(key), key, nonempty=key != 'setup_projects')
        wf.check(set(request['scope']) <= set(state['scopes']), 'attempt scope outside run')
        for key in ('cwd', 'environment'):
            wf.check(isinstance(request.get(key), str) and request[key], 'attempt requires ' + key)
        cwd = project if request['cwd'] == '.' else wf.safe_path(project, request['cwd'])
        wf.check(cwd.is_dir(), 'attempt cwd must be a project directory')
        relative, directory = attempt_plan(wf, project, args, state)
        wf.check(args.bundle not in state.get('report_bundles', {})
                 and args.bundle not in state.get('interrupted_attempts', {}), 'bundle ID is already registered')
        plan = {key: request[key] for key in ('command', 'scope', 'setup_projects', 'cwd', 'environment')}
        mode = request.get('regression_mode', 'affected')
        wf.check(mode in {'affected', 'full'}, 'regression_mode must be affected or full')
        plan['regression_mode'] = mode
        if mode == 'full':
            inventory = state.get('discovery_inventory', {})
            wf.check(inventory.get('freshness_marker') == state['freshness_marker']
                     and inventory.get('config_fingerprint') == wf.fingerprint(spec)
                     and inventory.get('asset_fingerprint') == wf.fingerprint(cov),
                     'full regression requires a current preflight discovery inventory')
            wf.check(set(inventory['scopes']) == set(plan['scope'])
                     and inventory['setup_projects'] == plan['setup_projects'],
                     'full regression scope/setup_projects differ from discovery')
            wf.check(not inventory['reviewed_missing_tests'],
                     'full regression discovery omits reviewed independent tests')
            wf.check(inventory['business_target_count'] > 0,
                     'full regression requires a nonempty business-test inventory')
            path = wf.safe_path(project, inventory['path'])
            wf.check(wf.hashlib.sha256(path.read_bytes()).hexdigest() == inventory['sha256'],
                     'discovery changed after preflight')
            plan['discovery'] = inventory
            roots = request.get('code_roots', [])
            string_array(wf, roots, 'code_roots')
            excluded = request.get('code_excluded_paths', [])
            string_array(wf, excluded, 'code_excluded_paths')
            plan['code_snapshot'] = wf.report_artifacts.discovered_code_snapshot(
                project, wf.load(path), plan['setup_projects'], roots, excluded)
            plan['code_scope_review'] = wf.report_artifacts.code_import_hints(project, wf.load(path), roots)
        plan['shard'] = wf.report_artifacts.command_shard(plan['command'])
        plan.update(bundle_id=args.bundle, run_id=state['run_id'], schema_version=2,
                    planned_at=wf.now(),
                    freshness_marker=state['freshness_marker'],
                    config_fingerprint=wf.fingerprint(spec), asset_fingerprint=wf.fingerprint(cov))
        wf.check(not directory.exists(), 'attempt directory already exists; use a new bundle ID or inspect existing evidence')
        # Publish a complete intent atomically; failures never reserve the bundle ID.
        directory.parent.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix='.' + args.bundle + '-', dir=directory.parent))
        try:
            for name in ('results/output', 'report/playwright', 'blob'):
                (staged / name).mkdir(parents=True, exist_ok=False)
            wf.atomic(staged / 'plan.json', plan, exclusive=True)
            wf.check(not directory.exists(), 'attempt directory appeared during preparation')
            os.rename(staged, directory)
        finally:
            if staged.exists():
                shutil.rmtree(staged)
        return {'created': True, 'plan': relative + '/plan.json',
                **plan.get('code_scope_review', {}),
                'environment': {'PLAYWRIGHT_JSON_OUTPUT_FILE': str(directory / 'results/result.json'),
                                'PLAYWRIGHT_HTML_OUTPUT_DIR': str(directory / 'report/playwright'),
                                'PLAYWRIGHT_BLOB_OUTPUT_DIR': str(directory / 'blob'),
                                'PLAYWRIGHT_HTML_OPEN': 'never'},
                'reporter_arguments': ['--reporter=list,json,html,blob', '--output', str(directory / 'results/output')],
                'receipt_path': relative + '/command.json',
                'receipt_required_fields': ['command', 'cwd', 'environment', 'scope', 'setup_projects',
                                            'started_at', 'ended_at', 'exit_code'],
                'note': 'Acquire quota and check prepare-regression before the real CLI. '
                        'Record actual argv including reporter/output arguments; no test was started.'}


def checked_attempt(wf, project, args, state, spec, cov):
    relative, directory = attempt_plan(wf, project, args, state)
    wf.check(args.bundle not in state.get('interrupted_attempts', {}), 'bundle ID registered as interrupted')
    plan = wf.load(directory / 'plan.json')
    wf.check(plan['run_id'] == state['run_id'] and plan['bundle_id'] == args.bundle, 'attempt plan identity mismatch')
    wf.check(plan['freshness_marker'] == state['freshness_marker']
             and plan['config_fingerprint'] == wf.fingerprint(spec)
             and plan['asset_fingerprint'] == wf.fingerprint(cov)
             and not state.get('recovery_required'), 'attempt inputs changed; retain evidence and review recovery')
    receipt_path = wf.safe_path(project, args.input) if args.input else directory / 'command.json'
    wf.check(receipt_path.is_relative_to(directory), 'command receipt must be inside the attempt directory')
    receipt = wf.load(receipt_path)
    wf.no_secrets(receipt)
    for key in ('scope', 'setup_projects', 'cwd', 'environment'):
        wf.check(receipt.get(key) == plan[key], 'actual receipt differs from plan: ' + key)
    string_array(wf, receipt.get('command'), 'command', nonempty=True)
    wf.report_artifacts.check_attempt_command(plan, receipt['command'], directory)
    wf.check(type(receipt.get('exit_code')) is int, 'receipt requires an actual integer exit code')
    wf.check(wf.dt(plan['planned_at']) <= wf.dt(receipt['started_at'])
             <= wf.dt(receipt['ended_at']) <= wf.dt(wf.now()),
             'receipt has invalid or future invocation times')
    bundle = {key: receipt[key] for key in ('command', 'cwd', 'environment', 'scope', 'setup_projects',
                                           'started_at', 'ended_at', 'exit_code')}
    bundle.update(bundle_id=args.bundle, run_id=state['run_id'],
                  attempt_plan=relative + '/plan.json',
                  attempt_plan_sha256=wf.hashlib.sha256((directory / 'plan.json').read_bytes()).hexdigest(),
                  execution_result=relative + '/results/result.json',
                  html_origin='same_execution', html_report=relative + '/report/playwright/index.html',
                  blob_reports=sorted(p.relative_to(project).as_posix() for p in (directory / 'blob').glob('*.zip')))
    wf.check(bundle['blob_reports'], 'attempt requires native blob reports, including failed CLI invocations')
    wf.check(args.bundle not in state.get('report_bundles', {})
             or all(state['report_bundles'][args.bundle].get(k) == v for k, v in bundle.items()),
             'bundle differs from registered invocation')
    wf.check(set(bundle['scope']) <= set(state['scopes']), 'report scope outside run')
    checked = wf.report_artifacts.inspect_bundle(
        project, state['run_id'], bundle, require_plan=state.get('native_merge_required', False))
    return directory, {'type': 'report_bundle', 'bundle': bundle}, checked


def attempt_finish(wf, project, args):
    with wf.project_lock(project):
        spec, cov = wf.assets(project)
        state = run_state(wf, project, args, owned=True, active=True)
        directory, event, checked = checked_attempt(wf, project, args, state, spec, cov)
        result = {'valid': True, 'bundle_id': args.bundle, 'committed': False,
                  'artifact_consistency': checked['report_consistency'],
                  'runner_counts': checked['runner_counts'],
                  'note': 'Artifact check only; collect-results still checks review gates and maps every result.'}
        if args.command == 'attempt-bundle':
            path = directory / 'bundle-event.json'
            if path.exists():
                wf.check(wf.load(path) == event, 'refusing to overwrite a different bundle event')
            else:
                wf.atomic(path, event, exclusive=True)
            result['input'] = path.relative_to(project).as_posix()
            result['next_command'] = ['collect-results', '--project', str(project), '--run', args.run,
                                      '--owner', args.owner, '--input', str(path)]
        return result


def attempt_interrupted(wf, project, args):
    with wf.project_lock(project):
        state = run_state(wf, project, args, owned=True, active=True)
        relative, directory = attempt_plan(wf, project, args, state)
        wf.check(args.bundle not in state.get('report_bundles', {}), 'bundle ID already has runner results')
        receipt_path = args.input or relative + '/command.json'
        receipt = wf.load(wf.safe_path(project, receipt_path))
        attempt = {key: receipt.get(key) for key in ('command', 'cwd', 'environment', 'scope', 'setup_projects',
                                                    'started_at', 'ended_at', 'exit_code')}
        attempt.update(bundle_id=args.bundle, run_id=args.run, status='interrupted', reason=args.reason,
                       attempt_plan=relative + '/plan.json', command_receipt=receipt_path)
        for key in ('attempt_plan', 'command_receipt'):
            attempt[key + '_sha256'] = wf.hashlib.sha256(wf.safe_path(project, attempt[key]).read_bytes()).hexdigest()
        wf.no_secrets(attempt)
        wf.report_artifacts.inspect_interrupted_attempt(project, args.run, attempt)
        wf.check(wf.dt(attempt['ended_at']) <= wf.dt(wf.now()), 'interrupted receipt cannot be in the future')
        event = {'type': 'interrupted_attempt', 'attempt': attempt,
                 'evidence': [attempt['attempt_plan'], attempt['command_receipt']]}
        path = directory / 'interrupted-event.json'
        if path.exists():
            wf.check(wf.load(path) == event, 'refusing to overwrite a different interruption event')
        else:
            wf.atomic(path, event, exclusive=True)
        return {'valid': True, 'committed': False, 'bundle_id': args.bundle,
                'input': path.relative_to(project).as_posix(), 'next_command': [
                    'record', '--project', str(project), '--run', args.run, '--owner', args.owner,
                    '--input', str(path)],
                'note': 'Receipt only; no runner results, full coverage or resource cleanup are inferred.'}


def clipped(wf, intervals, start, end):
    result = []
    for a, b in intervals:
        a, b = max(start, wf.dt(a)), min(end, wf.dt(b))
        if a <= b:
            result.append((a.isoformat(), b.isoformat()))
    return result


CLAUDE_TOKEN_FIELDS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')


def codex_row(row, tally):
    """Codex rollout: response_item payloads; exit codes appear in function outputs."""
    item = row.get('payload') if row.get('type') == 'response_item' else None
    if not isinstance(item, dict):
        return False
    if item.get('type') in {'function_call', 'custom_tool_call'}:
        tally['counts'][item.get('name', 'unknown')] += 1
        if item.get('call_id'):
            tally['pending'].add(item['call_id'])
    elif item.get('type') in {'function_call_output', 'custom_tool_call_output'} and item.get('call_id') in tally['pending']:
        output = item.get('output', '')
        if isinstance(output, str):
            codes = re.findall(r'Process exited with code (-?\d+)', output)
            tally['failures'] += bool(codes and int(codes[-1]) != 0)
    return item.get('type') in {'message', 'reasoning', 'function_call', 'custom_tool_call',
                                'function_call_output', 'custom_tool_call_output'}


def claude_code_row(row, tally):
    """Claude Code transcript: one row per content block, so usage repeats per message ID."""
    message = row.get('message')
    if row.get('type') not in {'assistant', 'user'} or not isinstance(message, dict):
        return False
    content = message.get('content')
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    usage = message.get('usage')
    if row['type'] == 'assistant' and isinstance(usage, dict) and message.get('id') not in tally['messages']:
        tally['messages'].add(message.get('id'))
        for key in CLAUDE_TOKEN_FIELDS:
            value = usage.get(key)
            if type(value) is int and value >= 0:
                tally['tokens'][key] += value
            else:
                tally['token_gaps'].add(key)
    for block in blocks:
        if block.get('type') == 'tool_use' and block.get('id') not in tally['pending']:
            tally['counts'][block.get('name', 'unknown')] += 1
            tally['pending'].add(block.get('id'))
        elif block.get('type') == 'tool_result' and block.get('tool_use_id') in tally['pending'] - tally['resolved']:
            tally['resolved'].add(block['tool_use_id'])
            output = block.get('content', '')
            if isinstance(output, list):
                output = next((b.get('text', '') for b in output if isinstance(b, dict) and b.get('type') == 'text'), '')
            tally['errors'] += block.get('is_error') is True
            code = re.match(r'Exit code (-?\d+)', output) if isinstance(output, str) else None
            tally['failures'] += bool(code and int(code.group(1)) != 0)
    return True


HOST_LOG_PARSERS = {'codex': codex_row, 'claude_code': claude_code_row}


def host_tally():
    return {'counts': Counter(), 'pending': set(), 'resolved': set(), 'failures': 0, 'errors': 0,
            'messages': set(), 'tokens': Counter(), 'token_gaps': set(), 'recognized': False}


def host_timing(wf, project, request):
    """Count bounded host receipts without copying command arguments or tool output."""
    session = request.get('session')
    if session is None:
        return None
    wf.require_evidence(project, session)
    wf.check(session.get('evidence'), 'session timing requires boundary evidence')
    start, end = wf.dt(session['started_at']), wf.dt(session['ended_at'])
    wf.check(start <= end <= wf.dt(wf.now()), 'session has invalid or future boundaries')
    result = {'started_at': start.isoformat(), 'ended_at': end.isoformat(),
              'wall_seconds': (end-start).total_seconds(), 'evidence': session['evidence'],
              'provenance': 'caller_supplied_boundaries', 'tool_calls': None,
              'tool_counts': None, 'nonzero_tool_results': None, 'tokens': None}
    if not session.get('log'):
        return result
    path = wf.safe_path(project, session['log'])
    wf.check(session['log'] in session['evidence'], 'session log must be listed as boundary evidence')
    host_format = session.get('host_format', 'auto')
    wf.check(host_format == 'auto' or host_format in HOST_LOG_PARSERS,
             'host_format must be auto, ' + ' or '.join(sorted(HOST_LOG_PARSERS)))
    tallies = {name: host_tally() for name in HOST_LOG_PARSERS}
    boundaries = set()
    # Streaming avoids loading large host transcripts or emitting credential-bearing content.
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            row = wf.json.loads(line)
            stamp = row.get('timestamp') if isinstance(row, dict) else None
            if not stamp or not start <= wf.dt(stamp) <= end:
                continue
            boundaries.add(wf.dt(stamp))
            for name, parser in HOST_LOG_PARSERS.items():
                if host_format in {'auto', name}:
                    tallies[name]['recognized'] |= parser(row, tallies[name])
    recognized = [name for name, tally in tallies.items() if tally['recognized']]
    if len(recognized) != 1:
        status = 'unsupported_host_log' if not recognized else 'ambiguous_host_log'
        return dict(result, host_log_status=status)
    name = recognized[0]
    tally = tallies[name]
    wf.check(start in boundaries and end in boundaries, 'session boundaries do not match host log timestamps')
    result.update(provenance='host_log_timestamp_boundaries', tool_calls=sum(tally['counts'].values()),
                  host_log_status='supported', host_log_format=name,
                  tool_counts=dict(tally['counts']), nonzero_tool_results=tally['failures'])
    if name == 'codex':
        result['tool_failure_scope'] = 'matched function outputs with explicit process exit codes only'
    else:
        tokens = {key: None if key in tally['token_gaps'] else tally['tokens'][key] for key in CLAUDE_TOKEN_FIELDS}
        tokens['total_tokens'] = None if tally['token_gaps'] else sum(tally['tokens'].values())
        result.update(tokens=dict(tokens, assistant_messages=len(tally['messages']),
                                  scope='this transcript only; subagent transcripts are separate files'),
                      error_tool_results=tally['errors'],
                      tool_failure_scope='tool_result blocks starting with an explicit Exit code; '
                                         'error_tool_results also counts denials and tool errors')
    return result


def timing(wf, project, args):
    state = run_state(wf, project, args)
    summary_path = wf.run_path(project, args.run) / 'summary.json'
    saved = wf.load(summary_path) if summary_path.exists() else {}
    report_generation = saved.get('report_generation')
    if state['acceptance'] in wf.TERMINAL:
        end_text = (report_generation or {}).get('completed_at') or saved.get('generated_at') or max(
            [state['created_at']] + [e['at'] for e in state['events']], key=wf.dt)
    else:
        end_text = wf.now()
    start, end = wf.dt(state['created_at']), wf.dt(end_text)
    wf.check(start <= end, 'invalid workflow interval')
    runners, invocations, phase_intervals = [], [], {}
    for bid, bundle in state.get('report_bundles', {}).items():
        a, b = wf.dt(bundle['started_at']), wf.dt(bundle['ended_at'])
        wf.check(a <= b, 'runner ends before start')
        invocations.append((a.isoformat(), b.isoformat()))
        row = {'bundle_id': bid, 'invocation_seconds': (b-a).total_seconds(),
               'result_duration_sum_seconds': None, 'result_interval_union_seconds': None,
               'unattributed_wall_seconds': None, 'raw_available': False}
        path = wf.safe_path(project, bundle['execution_result'])
        if path.is_file():
            raw = wf.load(path)
            expected_hash = bundle.get('artifact_sha256', {}).get(bundle['execution_result'])
            wf.check(expected_hash and wf.hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash,
                     'runner JSON has no registered digest or changed after registration')
            results = [r for _, test in wf.report_artifacts.json_tests(raw) for r in test.get('results', [])]
            durations = [r.get('duration') for r in results]
            wf.check(all(type(d) in {int, float} and math.isfinite(d) and d >= 0 for d in durations),
                     'invalid result duration')
            intervals = [(r['startTime'], (wf.dt(r['startTime']) + timedelta(milliseconds=r['duration'])).isoformat())
                         for r in results if r.get('startTime')]
            measured = wf.union_seconds(clipped(wf, intervals, a, b))
            complete = all(r.get('startTime') for r in results)
            row.update(raw_available=True, result_duration_sum_seconds=sum(durations)/1000,
                       result_interval_union_seconds=measured if complete else None,
                       unattributed_wall_seconds=max(0, (b-a).total_seconds()-measured) if complete else None,
                       results=len(results))
        runners.append(row)
    for bid, attempt in state.get('interrupted_attempts', {}).items():
        wf.report_artifacts.inspect_interrupted_attempt(project, state['run_id'], attempt)
        a, b = wf.dt(attempt['started_at']), wf.dt(attempt['ended_at'])
        invocations.append((a.isoformat(), b.isoformat()))
        runners.append({'bundle_id': bid, 'status': 'interrupted', 'receipt_only': True,
                        'started_at': attempt['started_at'], 'ended_at': attempt['ended_at'],
                        'exit_code': attempt['exit_code'], 'invocation_seconds': (b-a).total_seconds(),
                        'result_duration_sum_seconds': None, 'result_interval_union_seconds': None,
                        'unattributed_wall_seconds': None, 'raw_available': False})
    for event in state.get('phase_times', []):
        wf.check(wf.dt(event['started_at']) <= wf.dt(event['ended_at']), 'phase ends before start')
        phase_intervals.setdefault(event['phase'], []).append((event['started_at'], event['ended_at']))
    phases = {name: wf.union_seconds(clipped(wf, intervals, start, end))
              for name, intervals in phase_intervals.items()}
    wall = (end-start).total_seconds()
    source_rows, source_intervals = [], []
    for path in sorted((wf.run_path(project, args.run) / 'sources').glob('*/observation.json')):
        observation = wf.load(path)
        row = {'evidence': path.relative_to(project).as_posix(),
               'source_id': observation.get('source_id'), 'seconds': None}
        if observation.get('started_at') and observation.get('observed_at'):
            a, b = wf.dt(observation['started_at']), wf.dt(observation['observed_at'])
            wf.check(a <= b <= wf.dt(wf.now()), 'invalid source observation interval')
            row['seconds'] = (b-a).total_seconds()
            source_intervals.append((a.isoformat(), b.isoformat()))
        source_rows.append(row)
    request = input_object(wf, args)
    report_intervals = []
    if report_generation:
        a, b = wf.dt(report_generation['started_at']), wf.dt(report_generation['completed_at'])
        wf.check(start <= a <= b <= end, 'invalid report generation interval')
        report_intervals.append((a.isoformat(), b.isoformat()))
    known = clipped(wf, invocations + source_intervals + report_intervals
                    + [v for values in phase_intervals.values() for v in values], start, end)
    return {'run_id': args.run, 'workflow': {'started_at': start.isoformat(), 'observed_until': end.isoformat(),
             'wall_seconds': wall, 'known_interval_union_seconds': wf.union_seconds(known),
             'unknown_wall_seconds': max(0, wall-wf.union_seconds(known))},
            'runner_invocation_sum_seconds': sum(r['invocation_seconds'] for r in runners),
            'runner_interval_union_seconds': wf.union_seconds(invocations),
            'phases_union_seconds': phases, 'runners': runners, 'session': host_timing(wf, project, request),
            'report_generation': report_generation,
            'source_observations': source_rows,
            'source_read_interval_union_seconds': wf.union_seconds(source_intervals) if source_intervals else None,
            'model_cpu_seconds': None, 'network_wait_seconds': None,
            'note': 'Read-only timing, not evidence revalidation or acceptance. Overlapping phases '
                    'must not be added; result-duration sums can exceed wall time with parallel workers. '
                    'Unattributed wall time includes unmeasured work/waits; recycled raw results remain null.'}


def dispatch(api, project, args):
    wf = SimpleNamespace(**api)
    if args.command == 'preflight':
        return preflight(wf, project, args)
    if args.command == 'validate-input':
        return validate_input(wf, project, args)
    if args.command == 'attempt-init':
        return attempt_init(wf, project, args)
    if args.command in {'attempt-check', 'attempt-bundle'}:
        return attempt_finish(wf, project, args)
    if args.command == 'attempt-interrupted':
        return attempt_interrupted(wf, project, args)
    return timing(wf, project, args)
