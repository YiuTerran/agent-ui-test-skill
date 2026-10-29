"""Deterministic collection and finalization; only offline merge may spawn a CLI."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace


def sibling(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def attempt_collect(wf, project, args):
    tools = sibling('workflow_tools')
    prepared = tools.attempt_finish(wf, project, SimpleNamespace(**dict(vars(args), command='attempt-bundle')))
    with wf.project_lock(project):
        spec, cov = wf.assets(project)
        state = tools.run_state(wf, project, args, owned=True, active=True)
        result = wf.collect_results(project, state, spec, cov, args.owner,
                                    wf.load(project / prepared['input']))
    collection = result['collection']
    return {'valid': collection['complete'], 'run_id': args.run, 'bundle_id': args.bundle,
            'collection': collection, 'input': prepared['input'],
            'runner_counts': prepared['runner_counts'],
            'next_action': 'continue' if collection['complete'] else 'resolve_mapping_without_rerun'}


PACKAGE_RUNNERS = {'npx', 'pnpm', 'yarn', 'bunx', 'corepack'}
SCRIPT_LAUNCHERS = {'node', 'node.exe'}
PLAYWRIGHT_BINS = {'playwright', 'playwright.cmd', 'playwright.ps1'}
FORBIDDEN_MERGE_ARGS = {'test', 'install', 'add', 'dlx', 'create', '-c', '--eval', '-e', '--require', '-r',
                        '--import', '--loader', '--experimental-loader', '-p', '--print'}


def check_merge_launcher(wf, command):
    """Accept a package runner, node + Playwright CLI script, or the Playwright binary itself."""
    def name(value):
        return Path(value.replace('\\', '/')).name.lower()

    last, first = command[-1].replace('\\', '/'), name(command[0])
    wf.check(not any(x in command for x in FORBIDDEN_MERGE_ARGS),
             'merge.command must be a local offline launcher without test/install/eval/preload arguments')
    if first in PLAYWRIGHT_BINS:
        wf.check(len(command) == 1, 'a direct Playwright binary takes no launcher arguments')
    elif first in SCRIPT_LAUNCHERS:
        parts = [p.lower() for p in Path(last).parts[:-1]]
        wf.check(len(command) == 2 and name(last) in {'cli.js', 'playwright.js'}
                 and 'node_modules' in parts and bool({'@playwright', 'playwright', 'playwright-core'} & set(parts)),
                 'node must run only the installed Playwright CLI script')
    elif first.split('@')[0] in PACKAGE_RUNNERS:
        wf.check(name(last) in PLAYWRIGHT_BINS, 'package runner must end with the playwright binary name')
    else:
        wf.check(False, 'merge.command must start with npx/pnpm/yarn/bunx/corepack, node, or playwright')


def merge_reports(wf, project, state, request):
    wf.check(isinstance(request, dict), 'merge must be an object')
    command = request.get('command')
    wf.check(isinstance(command, list) and command
             and all(isinstance(x, str) and x for x in command), 'merge.command requires argv')
    # Only a local, already installed Playwright invocation belongs in this helper.
    check_merge_launcher(wf, command)
    cwd = project if request.get('cwd', '.') == '.' else wf.safe_path(project, request['cwd'])
    wf.check(cwd.is_dir(), 'merge cwd must be a project directory')
    timeout = request.get('timeout_seconds', 120)
    wf.check(type(timeout) is int and timeout > 0, 'merge timeout must be a positive integer')
    bundles = state.get('report_bundles', {})
    wf.check(bundles, 'merge requires registered bundles')
    tools = sibling('workflow_tools')
    root = tools.safe_cache(wf, project, f"test/.cache/runs/{state['run_id']}/merged/playwright")
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='batch-', dir=root))
    inputs = directory / 'input'
    inputs.mkdir()
    for bid, bundle in bundles.items():
        wf.check(bundle.get('blob_reports'), 'merge requires native blobs for ' + bid)
        for index, name in enumerate(bundle['blob_reports']):
            source = wf.safe_path(project, name)
            expected = bundle['artifact_sha256'][name]
            wf.check(hashlib.sha256(source.read_bytes()).hexdigest() == expected,
                     'merge input changed after collection: ' + name)
            target = inputs / f'{bid}-{index}.zip'
            shutil.copyfile(source, target)
            wf.check(hashlib.sha256(target.read_bytes()).hexdigest() == expected, 'merge copy digest mismatch')
    argv = command + ['merge-reports', '--reporter=html', str(inputs)]
    output = directory / 'playwright'
    env = dict(os.environ, PLAYWRIGHT_HTML_OPEN='never', PLAYWRIGHT_HTML_OUTPUT_DIR=str(output),
               npm_config_offline='true', COREPACK_ENABLE_NETWORK='0')
    started = wf.now()
    log = directory / 'merge.log'
    # Persist output once, without sending large reporter logs through the model.
    with log.open('w', encoding='utf-8') as stream:
        process = subprocess.run(argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                 timeout=timeout, shell=False)
    report = {'html_report': (output / 'index.html').relative_to(project).as_posix(),
              'bundle_ids': list(bundles), 'command': argv, 'started_at': started,
              'ended_at': wf.now(), 'exit_code': process.returncode}
    wf.atomic(directory / 'command.json', report)
    wf.check(process.returncode == 0, 'offline merge failed; see ' + log.relative_to(project).as_posix())
    return {'type': 'merged_report', 'report': report}


def compact_report(report, released):
    status = report['status']
    return {'valid': True, 'run_id': report['run_id'], 'acceptance': status['acceptance'],
            'released': released, 'report_paths': report['report_paths'],
            'report_consistency': report['report_consistency']['status'],
            'regression_completeness': report['regression_completeness']['status'],
            'case_results': report['counts']['case_results'],
            'execution_results': report['counts']['execution_results'],
            'setup_results': report['counts']['setup_results'],
            'gap_case_ids': status.get('work_needed', []),
            'residual_resources': status.get('residual_resources', []),
            'global_residual_run_count': len(status.get('global_residual_runs', [])),
            'report_generation_seconds': report['report_generation']['wall_seconds']}


def require_stopped(wf, state, ledger):
    active = [key for key, lease in ledger['leases'].items()
              if lease['run_id'] == state['run_id'] and lease['status'] == 'active']
    running = [key for key, task in state['tasks'].items() if task.get('status') == 'running']
    wf.check(not active and not running, 'finalize requires stopped work and released quota; '
             + 'active_leases=' + str(active) + '; running_tasks=' + str(running))


def require_returned(wf, project, state):
    leased = wf.pool_run_accounts(project, state['run_id'])['leased']
    wf.check(not leased, 'finalize requires returned pool accounts; leased=' + str(leased))


def finalize(wf, project, args):
    tools = sibling('workflow_tools')
    scheduling = sibling('scheduling')
    request = tools.input_object(wf, args, required=True)
    wf.no_secrets(request)
    wf.check(set(request) <= {'events', 'acceptance', 'require_full', 'merge'}, 'unknown finalize input field')
    acceptance = request.get('acceptance')
    wf.check(isinstance(acceptance, dict) and acceptance.get('status') in wf.TERMINAL,
             'finalize requires an explicit terminal acceptance decision')
    wf.check(not acceptance.get('owner') or acceptance['owner'] == args.owner, 'acceptance owner mismatch')
    wf.check(not acceptance.get('type') or acceptance['type'] == 'acceptance', 'invalid acceptance type')
    require_full = request.get('require_full', True)
    wf.check(type(require_full) is bool, 'require_full must be a boolean')
    events = request.get('events', [])
    wf.check(isinstance(events, list) and all(isinstance(e, dict) for e in events), 'events must be an array')
    wf.check(all(e.get('type') in {'resource', 'issue', 'merged_report'} for e in events),
             'finalize.events may contain only resource, issue and merged_report receipts')
    wf.check(not request.get('merge') or not any(e.get('type') == 'merged_report' for e in events),
             'provide either merge launcher or existing merged_report event')
    digest = wf.fingerprint(request)
    with wf.project_lock(project), scheduling.locked_ledger(project) as (ledger, _):
        spec, cov = wf.assets(project)
        state = tools.run_state(wf, project, args)
        checkpoint = state.get('finalization', {})
        if not state.get('claim') and checkpoint.get('result'):
            wf.check(checkpoint['input_sha256'] == digest and checkpoint['owner'] == args.owner,
                     'completed finalization belongs to a different input or owner')
            return dict(checkpoint['result'], replayed=True)
        wf.owner_check(state, args.owner)
        require_stopped(wf, state, ledger)
        require_returned(wf, project, state)
        wf.check(state.get('config_fingerprint') == wf.fingerprint(spec)
                 and state['asset_fingerprint'] == wf.fingerprint(cov)
                 and not state.get('recovery_required'), 'review changed inputs before finalization')
        config_digest, coverage_digest = wf.fingerprint(spec), wf.fingerprint(cov)
        # An unfinished checkpoint may take a revised decision; applied receipts are never replayed.
        wf.check(not checkpoint or checkpoint['owner'] == args.owner,
                 'unfinished finalization belongs to a different owner')
        before = wf.fingerprint(state)
    # A registered merge that still covers every bundle is rechecked by write_report, not rerun.
    merged = None
    registered = state.get('merged_report')
    stale = not registered or set(registered.get('bundle_ids', [])) != set(state.get('report_bundles', {}))
    if request.get('merge') and stale:
        merged = merge_reports(wf, project, state, request['merge'])
    with wf.project_lock(project), scheduling.locked_ledger(project) as (ledger, _):
        spec, cov = wf.assets(project)
        current = tools.run_state(wf, project, args, owned=True)
        require_stopped(wf, current, ledger)
        require_returned(wf, project, current)
        wf.check(wf.fingerprint(spec) == config_digest and wf.fingerprint(cov) == coverage_digest,
                 'configuration or coverage changed during finalization; preserve artifacts and inspect')
        wf.check(wf.fingerprint(current) == before, 'run changed during finalization; preserve artifacts and inspect')
        final = dict(acceptance, type='acceptance', owner=args.owner)
        if current['acceptance'] in wf.TERMINAL:
            wf.check(current['acceptance'] == final['status'], 'terminal acceptance differs from request')
        prior = current.get('finalization') or {}
        applied = list(prior.get('applied_event_sha256', []))
        if prior and 'applied_event_sha256' not in prior and prior['input_sha256'] == digest:
            # Checkpoints from before receipt tracking already applied this exact input.
            applied = [wf.fingerprint(e) for e in events]
        batch = [e for e in events if wf.fingerprint(e) not in applied] + ([merged] if merged else [])
        if batch:
            current, newcov = wf.apply_events(project, current, spec, cov, args.owner, batch)
            wf.check(newcov == cov, 'finalize cannot change coverage')
        if not prior or prior['input_sha256'] != digest or batch:
            history = list(prior.get('superseded_inputs', []))
            if prior and prior['input_sha256'] != digest:
                history.append({'input_sha256': prior['input_sha256'], 'recorded_at': prior['recorded_at']})
            current['finalization'] = {
                'input_sha256': digest, 'owner': args.owner, 'recorded_at': wf.now(),
                'applied_event_sha256': applied + [wf.fingerprint(e) for e in batch if e is not merged],
                'superseded_inputs': history}
            wf.sync_recorded_pools(project, current, batch)
            wf.atomic(wf.run_path(project, args.run) / 'state.json', current)
        # Keep terminal acceptance tentative until report and target checks succeed.
        # A failed finalization can still collect missing evidence through normal gates.
        current, newcov = wf.apply_events(project, current, spec, cov, args.owner, [final])
        report = wf.write_report(project, current, spec, cov, persist_state=False)
        checked = report['report_consistency']['status'] == 'checked'
        no_execution = (not current.get('report_bundles') and not current['executions']
                        and current['acceptance'] in {'partially_blocked', 'aborted'} and not require_full)
        full_ok = not require_full or report['regression_completeness']['status'] == 'complete'
        if not (checked or no_execution) or not full_ok:
            return {'valid': False, 'stage': 'report_checks', 'released': False,
                    'report_paths': report['report_paths'], 'report_consistency': report['report_consistency'],
                    'regression_completeness': report['regression_completeness'],
                    'next_action': 'resolve_report_evidence_without_business_rerun'}
        current['claim'] = None
        report['status']['claim'] = None
        result = compact_report(report, released=True)
        current['finalization'].update(completed_at=wf.now(), result=result)
        wf.atomic(wf.run_path(project, args.run) / 'summary.json', report)
        wf.atomic(wf.run_path(project, args.run) / 'state.json', current)
        return result


def dispatch(api, project, args):
    wf = SimpleNamespace(**api)
    try:
        if args.command == 'attempt-collect':
            return attempt_collect(wf, project, args)
        return finalize(wf, project, args)
    except subprocess.TimeoutExpired as error:
        return {'valid': False, 'stage': 'offline_merge', 'released': False,
                'error': 'offline merge timed out; preserve merge.log and inspect remaining processes',
                'timeout_seconds': error.timeout,
                'evidence_directory': str(Path(error.cmd[-1]).parent.relative_to(project)),
                'next_action': 'inspect_merge_without_business_rerun'}
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, OSError) as error:
        return {'valid': False, 'stage': args.command, 'released': False, 'error': str(error),
                'next_action': 'resolve_the_reported_input_or_evidence_without_business_rerun'}
