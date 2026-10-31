"""Check Playwright evidence and render a derived review index; never run tests."""
import base64
from collections import Counter
from datetime import datetime
import hashlib
import html
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import zipfile
from urllib.parse import quote


_platform_spec = importlib.util.spec_from_file_location(
    'e2e_path_platform', Path(__file__).with_name('platform_support.py'))
platform_support = importlib.util.module_from_spec(_platform_spec)
_platform_spec.loader.exec_module(platform_support)


def check(condition, message):
    if not condition:
        raise ValueError(message)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    check(parsed.tzinfo is not None, 'report timestamp requires timezone')
    return parsed


def path_in(project, value):
    check(isinstance(value, str) and value and not Path(value).is_absolute(), 'report path must be project-relative')
    platform_support.relative_parts(value)
    path = (project / value).resolve()
    check(path.is_relative_to(project.resolve()), 'report path escapes project')
    check(path.is_file(), 'report file missing: ' + value)
    return path


def json_tests(data):
    """Retain repeated tests and all retries; do not reduce them to case pass."""
    def walk(suite):
        for spec in suite.get('specs', []):
            for test in spec.get('tests', []):
                yield (spec, test)
        for child in suite.get('suites', []):
            yield from walk(child)
    return [pair for suite in data.get('suites', []) for pair in walk(suite)]


def result_slots(data):
    def walk(suite, prefix):
        for si, spec in enumerate(suite.get('specs', [])):
            for ti, test in enumerate(spec.get('tests', [])):
                pointer = f'{prefix}/specs/{si}/tests/{ti}'
                for index in range(len(test.get('results', []))):
                    yield pointer, index
                if not test.get('results'):
                    yield pointer, None
        for index, child in enumerate(suite.get('suites', [])):
            yield from walk(child, f'{prefix}/suites/{index}')
    return {slot for index, suite in enumerate(data.get('suites', []))
            for slot in walk(suite, f'/suites/{index}')}


def html_data(path):
    # Internal Playwright format: fail closed when a version changes the layout.
    match = re.search(r'data:application/zip;base64,([A-Za-z0-9+/=]+)', path.read_text(encoding='utf-8'))
    check(match, 'unsupported HTML format; cannot verify consistency')
    try:
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(match[1]))) as archive:
            index = json.loads(archive.read('report.json'))
            tests = []
            for file in index['files']:
                tests.extend(json.loads(archive.read(file['fileId'] + '.json'))['tests'])
    except (zipfile.BadZipFile, KeyError, ValueError, UnicodeError) as error:
        raise ValueError('unsupported or damaged HTML report data; cannot verify consistency') from error
    return index, tests


def signature(title, project, file, outcome, results):
    normalized = file.replace('\\', '/')
    if platform_support.WINDOWS:
        normalized = normalized.casefold()
    return (title, project, normalized, outcome,
            tuple((r.get('startTime'), r.get('retry', 0), r.get('status'), r.get('duration'))
                  for r in results))


ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def error_facts(result):
    messages = set()
    values = list(result.get('errors') or [])
    if not values and result.get('error'):
        values.append(result['error'])
    for value in values:
        text = value.get('message') or value.get('stack') or str(value) if isinstance(value, dict) else str(value)
        normalized = ANSI.sub('', text).strip()
        if normalized:
            messages.add(normalized)
    # Playwright HTML synthesizes this status marker; JSON retains only real errors.
    if result.get("status") == "interrupted":
        messages.add("Test was interrupted.")
    return tuple(sorted(messages))


def attachment_facts(project, result, base_dir=None, raw=False):
    facts = []
    for attachment in result.get('attachments', []):
        name, content_type = attachment.get('name', ''), attachment.get('contentType', '')
        if attachment.get('body') is not None:
            body = attachment['body']
            if raw:
                try:
                    content = base64.b64decode(body, validate=True)
                except (ValueError, TypeError):
                    content = body.encode('utf-8')
            else:
                content = body.encode('utf-8') if isinstance(body, str) else bytes(body)
        elif attachment.get('path'):
            value = Path(attachment['path'])
            if not value.is_absolute():
                value = Path(base_dir, value) if base_dir is not None else value
                value = project / value
            path = value.resolve()
            check(path.is_relative_to(project.resolve()) and path.is_file(),
                  'report attachment missing or outside project')
            content = path.read_bytes()
        else:
            content = b''
        facts.append((name, content_type, hashlib.sha256(content).hexdigest()))
    return tuple(sorted(facts))


def attempt_facts(project, pairs, html_report=None, native=False):
    facts = Counter()
    base_dir = Path(html_report).parent if html_report else None
    for spec, test in pairs:
        title = test['title'] if native else spec['title']
        filename = test['location']['file'] if native else spec['file']
        outcome = test.get('outcome', test.get('status')) if native else test.get('status')
        results = test.get('results', [])
        if not results:
            facts[(signature(title, test.get('projectName', ''), filename, outcome, []), (), ())] += 1
        for result in results:
            key = signature(title, test.get('projectName', ''), filename, outcome, [result])
            facts[(key, error_facts(result), attachment_facts(
                project, result, base_dir=base_dir, raw=not native))] += 1
    return facts


def verify_blob(path):
    try:
        with zipfile.ZipFile(path) as archive:
            check('report.jsonl' in archive.namelist(), 'blob report lacks report.jsonl')
            with archive.open('report.jsonl') as stream:
                first = next((line for line in stream if line.strip()), None)
            check(first is not None, 'blob report is empty')
            metadata = json.loads(first)
            check(metadata.get('method') == 'onBlobReportMetadata'
                  and metadata.get('params', {}).get('userAgent', '').startswith('Playwright/')
                  and isinstance(metadata.get('params', {}).get('version'), int),
                  'blob report lacks Playwright metadata')
            return metadata['params']
    except (zipfile.BadZipFile, KeyError, ValueError, UnicodeError) as error:
        raise ValueError('invalid Playwright blob report') from error


def command_shard(command):
    values = []
    for index, arg in enumerate(command):
        if arg == '--shard':
            check(index + 1 < len(command), '--shard requires current/total')
            values.append(command[index + 1])
        elif arg.startswith('--shard='):
            values.append(arg.split('=', 1)[1])
    check(len(values) <= 1, 'command must specify at most one shard')
    if not values:
        return None
    match = re.fullmatch(r'([1-9]\d*)/([1-9]\d*)', values[0])
    check(match is not None, 'invalid --shard current/total')
    current, total = map(int, match.groups())
    check(current <= total, 'shard current exceeds total')
    return {'current': current, 'total': total}


def check_attempt_command(plan, command, directory):
    planned = plan['command']
    check(command[:len(planned)] == planned, 'actual command does not match planned command prefix')
    suffix, seen = command[len(planned):], set()
    permitted = {'--reporter=list,json,html,blob': [],
                 '--output': [str(directory / 'results/output')]}
    while suffix:
        option = suffix[0]
        check(option in permitted and option not in seen,
              'unplanned command argument; put execution selectors in plan.command')
        expected = [option, *permitted[option]]
        check(suffix[:len(expected)] == expected, 'reporter/output arguments differ from attempt paths')
        seen.add(option)
        suffix = suffix[len(expected):]
    check(command_shard(command) == plan.get('shard'), 'actual shard differs from plan')


def runner_inventory(project, raw, setup_projects):
    """Use complete identities and multiplicity; retries are results of the same target."""
    root = Path(raw.get('config', {}).get('rootDir') or project)
    root = (root if root.is_absolute() else project / root).resolve()
    check(root.is_relative_to(project.resolve()), 'runner root outside project')
    values = {'business': [], 'setup': []}
    for spec, test in json_tests(raw):
        file = Path(spec['file'].replace('\\', '/'))
        file = (file if file.is_absolute() else root / file).resolve()
        check(file.is_relative_to(project.resolve()), 'runner spec outside project')
        kind = 'setup' if test.get('projectName') in setup_projects else 'business'
        values[kind].append({'file': file.relative_to(project).as_posix(),
                             'title': spec['title'], 'project': test.get('projectName', '')})
    return values


def target_counter(rows):
    return Counter((r['file'], r['title'], r['project']) for r in rows)


def target_difference(expected, actual):
    def rows(counter):
        return [dict(zip(('file', 'title', 'project'), key), count=count)
                for key, count in sorted(counter.items())]
    return {'missing': rows(expected-actual), 'unexpected': rows(actual-expected)}


def code_path(project, value):
    platform_support.relative_parts(value)
    root = project.resolve()
    path = root / value
    for part in (path, *path.parents):
        if part == root:
            break
        check(not part.is_symlink() and not (
            part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400),
            'code snapshot cannot follow symlinks or reparse points: ' + value)
    check(path.resolve().is_relative_to(root), 'code snapshot outside project')
    return path


# Dependencies, caches and conventional runner output (storageState, results, reports) are not code.
CODE_EXCLUDED_DIRS = {'node_modules', '.cache', '__pycache__', '.auth',
                      'test-results', 'playwright-report', 'blob-report'}
LEGACY_CODE_EXCLUDED_DIRS = {'node_modules', '.cache', '__pycache__'}


def code_snapshot(project, specs, roots, config_files=(), excluded_dirs=None, excluded_paths=()):
    """Hash code trees and their file sets without dependency/generated caches."""
    def unreadable(error):
        raise error

    excluded_dirs = CODE_EXCLUDED_DIRS if excluded_dirs is None else set(excluded_dirs)
    excluded_paths = set(excluded_paths)

    def skipped(value):
        return any(Path(value).is_relative_to(Path(p)) for p in excluded_paths)

    files = set(specs) | set(config_files)
    for relative in roots:
        directory = code_path(project, relative)
        if not directory.exists() or skipped(relative):
            continue
        if directory.is_file():
            files.add(relative)
            continue
        check(directory.is_dir(), 'code snapshot root must be a directory: ' + relative)
        for parent, dirs, names in os.walk(directory, followlinks=False, onerror=unreadable):
            base = Path(parent).relative_to(project.resolve())
            dirs[:] = [name for name in dirs
                       if name not in excluded_dirs and not skipped((base / name).as_posix())]
            for name in dirs + names:
                path = Path(parent, name)
                value = path.relative_to(project.resolve()).as_posix()
                code_path(project, value)
                if name in names:
                    files.add(value)
    hashes = {}
    for value in sorted(files):
        path = code_path(project, value)
        check(path.is_file() and stat.S_ISREG(path.stat().st_mode), 'code snapshot file missing or nonregular: ' + value)
        hashes[value] = hashlib.sha256(path.read_bytes()).hexdigest()
    snapshot = {'schema_version': 2, 'specs': sorted(set(specs)), 'roots': sorted(set(roots)),
                'config_files': sorted(set(config_files)), 'excluded_dirs': sorted(excluded_dirs),
                'excluded_paths': sorted(excluded_paths), 'files': hashes}
    snapshot['sha256'] = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return snapshot


def project_code_path(project, value):
    """Normalize host aliases such as /var without hiding symlinks inside the project."""
    path = Path(os.path.abspath(value))
    if path.is_relative_to(project):
        return path
    for ancestor in (path, *path.parents):
        if ancestor.resolve() == project:
            return project / path.relative_to(ancestor)
    return path


def discovered_excluded_paths(project, raw, extra=()):
    """Runner output directories plus caller-declared generated paths such as storageState files."""
    project = project.resolve()
    values = set()
    for item in raw.get('config', {}).get('projects') or []:
        output = item.get('outputDir') if isinstance(item, dict) else None
        if isinstance(output, str) and output:
            path = Path(output.replace('\\', '/'))
            path = project_code_path(project, path if path.is_absolute() else project / path)
            if path.is_relative_to(project) and path != project:
                values.add(path.relative_to(project).as_posix())
    for value in extra:
        code_path(project, value)
        check(Path(value).as_posix() not in {'', '.'}, 'code_excluded_paths cannot exclude the project root')
        values.add(Path(value).as_posix())
    return values


def discovered_code_scope(project, raw, extra_roots=()):
    project = project.resolve()
    config = raw.get('config', {})
    root = Path(config.get('rootDir') or project)
    root = project_code_path(project, root if root.is_absolute() else project / root)
    check(root.is_relative_to(project.resolve()), 'code snapshot root outside project')
    if root != project:
        code_path(project, root.relative_to(project).as_posix())
    specs = set()
    for spec, _ in json_tests(raw):
        file = Path(spec['file'].replace('\\', '/'))
        file = project_code_path(project, file if file.is_absolute() else root / file)
        check(file.is_relative_to(project.resolve()), 'code snapshot spec outside project')
        value = file.relative_to(project.resolve()).as_posix()
        code_path(project, value)
        specs.add(value)
    roots = {value for value in extra_roots}
    if root != project:
        roots.add(root.relative_to(project).as_posix())
    roots.add((root / 'fixtures').relative_to(project.resolve()).as_posix())
    # Include conventional fixture trees on each discovered spec's parent chain.
    for value in specs:
        parent = Path(value).parent
        while parent != Path('.'):
            roots.add((parent / 'fixtures').as_posix())
            parent = parent.parent
    configs = set()
    if config.get('configFile'):
        file = Path(config['configFile'].replace('\\', '/'))
        file = project_code_path(project, file if file.is_absolute() else project / file)
        check(file.is_relative_to(project), 'Playwright config outside project')
        configs.add(file.relative_to(project).as_posix())
    # Legacy JSON can omit configFile; cover conventional configuration at the project root.
    for suffix in ('ts', 'js', 'mts', 'cts', 'mjs', 'cjs'):
        if (project / ('playwright.config.' + suffix)).is_file():
            configs.add('playwright.config.' + suffix)
    for value in specs | configs | roots:
        code_path(project, value)
    return specs, roots, configs


def discovered_code_snapshot(project, raw, setup_projects, extra_roots=(), excluded_paths=()):
    specs, roots, configs = discovered_code_scope(project, raw, extra_roots)
    return code_snapshot(project, specs, roots, configs,
                         excluded_paths=discovered_excluded_paths(project, raw, excluded_paths))


def code_import_hints(project, raw, extra_roots=()):
    """Best-effort literal relative-import traversal, not a TS/JS module resolver."""
    project = project.resolve()
    specs, roots, configs = discovered_code_scope(project, raw, extra_roots)
    pending, visited, suggestions, references, warnings = list(specs | configs), set(), set(), [], []
    pattern = re.compile(r'''(?:\b(?:import|export)\s+(?:[^;'"]*?\bfrom\s*)?|\b(?:import|require)\s*\(\s*)['"](\.[^'"\r\n]+)['"]''')
    suffixes = ('.ts', '.tsx', '.js', '.jsx', '.mts', '.cts', '.mjs', '.cjs', '.json')

    def covered(value):
        return value in specs | configs or (not set(Path(value).parts) & CODE_EXCLUDED_DIRS
                                           and any(Path(value).is_relative_to(Path(root)) for root in roots))

    while pending:
        value = pending.pop()
        if value in visited:
            continue
        visited.add(value)
        path = code_path(project, value)
        if path.suffix not in suffixes or not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        text = re.sub(r'/\*.*?\*/|^\s*//[^\n]*', '', text, flags=re.S | re.M)
        for match in pattern.finditer(text):
            target = Path(os.path.abspath(path.parent / match.group(1)))
            if not target.is_relative_to(project):
                warnings.append({'importer': value, 'reason': 'relative_import_outside_project'})
                continue
            candidates = [target]
            if target.suffix in {'.js', '.jsx', '.mjs', '.cjs'}:
                candidates += [target.with_suffix(s) for s in ('.ts', '.tsx', '.mts', '.cts')]
            if not target.suffix:
                candidates += [Path(str(target) + s) for s in suffixes]
            candidates += [target / ('index' + s) for s in suffixes]
            resolved = next((p for p in candidates if p.is_file()), None)
            if resolved is None:
                warnings.append({'importer': value, 'reason': 'relative_import_unresolved', 'import': match.group(1)})
                continue
            relative = resolved.relative_to(project).as_posix()
            code_path(project, relative)
            if set(resolved.relative_to(project).parts) & CODE_EXCLUDED_DIRS:
                warnings.append({'importer': value, 'reason': 'relative_import_in_excluded_directory', 'file': relative})
                continue
            if not covered(relative):
                suggestions.add((resolved.parent if resolved.parent != project else resolved)
                                .relative_to(project).as_posix())
                references.append({'importer': value, 'file': relative})
            pending.append(relative)
    return {'code_roots_suggested': sorted(suggestions),
            'code_references_outside_snapshot': sorted(references, key=lambda row: (row['importer'], row['file'])),
            'code_import_warnings': warnings}


def code_snapshot_difference(project, saved):
    if not saved or saved.get('schema_version') != 2:
        return {'status': 'incomplete', 'reason': 'code_fingerprint_missing'}
    try:
        legacy = 'excluded_dirs' not in saved
        current = code_snapshot(project, saved['specs'], saved['roots'], saved['config_files'],
                                excluded_dirs=LEGACY_CODE_EXCLUDED_DIRS if legacy else saved['excluded_dirs'],
                                excluded_paths=saved.get('excluded_paths', []))
        if legacy:
            # Snapshots recorded before exclusion fields keep their original digest shape.
            for key in ('excluded_dirs', 'excluded_paths', 'sha256'):
                current.pop(key)
            current['sha256'] = hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
        previous = saved['files']
        if current == saved:
            return {'status': 'checked', 'reason': None}
        return {'status': 'incomplete', 'reason': 'code_changed_after_full',
                'added': sorted(set(current['files']) - set(previous)),
                'removed': sorted(set(previous) - set(current['files'])),
                'changed': sorted(p for p in previous.keys() & current['files'].keys()
                                  if previous[p] != current['files'][p])}
    except (ValueError, OSError, KeyError, TypeError) as error:
        return {'status': 'incomplete', 'reason': 'code_changed_after_full', 'error': str(error)}


def inspect_attempt_plan(project, run_id, bundle, raw, track, blob_metadata):
    """The same checks run for helpers, direct collection and later report revalidation."""
    relative = f"test/.cache/runs/{run_id}/attempts/{bundle['bundle_id']}/plan.json"
    check(bundle['attempt_plan'] == relative, 'attempt plan outside invocation directory')
    path = track(relative)
    check(hashlib.sha256(path.read_bytes()).hexdigest() == bundle.get('attempt_plan_sha256'),
          'attempt plan changed after bundle preparation')
    plan = json.loads(path.read_text(encoding='utf-8'))
    check(plan['run_id'] == run_id and plan['bundle_id'] == bundle['bundle_id'], 'attempt plan identity mismatch')
    for key in ('scope', 'setup_projects', 'environment', 'cwd'):
        check(plan[key] == bundle[key], 'bundle differs from attempt plan: ' + key)
    check_attempt_command(plan, bundle['command'], path.parent)
    check(timestamp(plan['planned_at']) <= timestamp(bundle['started_at']), 'invocation precedes attempt preparation')
    check(plan.get('regression_mode') in {'affected', 'full'}, 'attempt must declare regression_mode')
    blobs = bundle.get('blob_reports', [])
    # One CLI produces one blob, including a CLI for one explicitly planned shard.
    for value in blobs:
        metadata = blob_metadata[value]
        check(metadata.get('shard') == plan.get('shard'), 'blob shard differs from attempt plan')
    check(len(blobs) == 1, 'one CLI/attempt requires exactly one native blob; use one attempt per shard')
    if raw.get('config', {}).get('shard') is not None or plan.get('shard') is not None:
        check(raw.get('config', {}).get('shard') == plan.get('shard'), 'runner JSON shard differs from plan')
    result = {'mode': plan['regression_mode'], 'scope': plan['scope'], 'shard': plan.get('shard'),
              'freshness_marker': plan['freshness_marker'],
              'config_fingerprint': plan['config_fingerprint'], 'asset_fingerprint': plan['asset_fingerprint']}
    if plan['regression_mode'] == 'full':
        discovery = plan['discovery']
        discovery_path = path_in(project, discovery['path'])
        check(hashlib.sha256(discovery_path.read_bytes()).hexdigest() == discovery['sha256'],
              'full-regression discovery artifact changed')
        check(set(plan['scope']) == set(discovery['scopes'])
              and plan['setup_projects'] == discovery['setup_projects'], 'full-regression discovery scope/setup mismatch')
        expected = runner_inventory(project, json.loads(discovery_path.read_text(encoding='utf-8')),
                                    plan['setup_projects'])
        check(not discovery.get('reviewed_missing_tests'),
              'full regression discovery omits reviewed independent tests')
        check(expected['business'], 'full regression requires a nonempty business-test inventory')
        actual = runner_inventory(project, raw, plan['setup_projects'])
        result.update(discovery=discovery, expected=expected, actual=actual)
        if 'code_snapshot' in plan:
            result['code_snapshot'] = plan['code_snapshot']
    return result


def regression_completeness(bundles, required_scopes=None, *, required=False, project=None):
    """Full means the selected CLI/shards targeted the discovered multiset, not that they passed."""
    groups, planned = {}, False
    for bid, bundle in bundles.items():
        item = bundle.get('regression_check')
        if item is None:
            continue
        planned = True
        if item['mode'] != 'full':
            continue
        discovery = item['discovery']
        key = (discovery['sha256'], tuple(sorted(item['scope'])),
               discovery['config_fingerprint'], discovery['asset_fingerprint'],
               discovery['freshness_marker'], (item.get('shard') or {}).get('total', 1),
               (item.get('code_snapshot') or {}).get('sha256'))
        groups.setdefault(key, []).append((bid, bundle))
    checks, code_checks = [], {}
    for entries in groups.values():
        latest = {}
        for bid, bundle in sorted(entries, key=lambda v: (timestamp(v[1]['started_at']), v[0])):
            item = bundle['regression_check']
            current = (item.get('shard') or {}).get('current', 1)
            latest[current] = (bid, item)
        item = next(iter(latest.values()))[1]
        total = (item.get('shard') or {}).get('total', 1)
        expected = target_counter(item['expected']['business'])
        actual = Counter()
        setup_errors = []
        for bid, value in latest.values():
            actual.update(target_counter(value['actual']['business']))
            difference = target_difference(target_counter(value['expected']['setup']),
                                           target_counter(value['actual']['setup']))
            if difference['missing'] or difference['unexpected']:
                setup_errors.append(dict(bundle_id=bid, **difference))
        difference = target_difference(expected, actual)
        missing_shards = sorted(set(range(1, total+1))-set(latest))
        snapshot = item.get('code_snapshot')
        key = json.dumps(snapshot, sort_keys=True)
        if key not in code_checks:
            code_checks[key] = (code_snapshot_difference(project, snapshot) if project is not None
                                else {'status': 'incomplete', 'reason': 'project_not_supplied'})
        code_check = code_checks[key]
        checks.append({'complete': not (difference['missing'] or difference['unexpected']
                                        or setup_errors or missing_shards
                                        or code_check['status'] == 'incomplete'),
                       'code_check': code_check,
                       'scope': item['scope'], 'bundle_ids': [v[0] for v in latest.values()],
                       'missing_shards': missing_shards, 'setup_differences': setup_errors, **difference})
    scopes = (set(required_scopes) if required_scopes is not None else
              {s for b in bundles.values() if b.get('regression_check')
               for s in b['regression_check']['scope']})
    completed = {s for c in checks if c['complete'] for s in c['scope']}
    return {'status': ('complete' if checks and scopes <= completed else 'incomplete') if planned or required else 'not_declared',
            'missing_scopes': sorted(scopes-completed), 'groups': checks,
            'note': 'Target multiset and current spec/fixture content are required for complete; '
                    'skips, failures and caller discovery provenance remain separate.'}


def verify_merge_command(command):
    check(isinstance(command, list) and all(isinstance(arg, str) and arg for arg in command),
          'merge command must be the actual argv array')
    normalized = [arg.replace('\\', '/').lower() for arg in command]
    check('merge-reports' in normalized, 'merge command must run Playwright merge-reports')
    playwright_cli = any(Path(arg).name in {'playwright', 'playwright.cmd', 'playwright.ps1'}
                         or arg.endswith('/@playwright/test/cli.js') for arg in normalized)
    check(playwright_cli, 'merge command must identify the Playwright CLI')
    check('--reporter=html' in normalized or any(
        arg == '--reporter' and index + 1 < len(normalized) and normalized[index + 1] == 'html'
        for index, arg in enumerate(normalized)), 'merge command must select the HTML reporter')
    check(any(not arg.startswith('-') and arg not in {'merge-reports', 'html'}
              for arg in normalized[normalized.index('merge-reports') + 1:]),
          'merge command must identify its blob input directory')


def inspect_interrupted_attempt(project, run_id, attempt):
    """Validate receipt-only provenance without inventing runner results."""
    bid = attempt.get('bundle_id')
    check(isinstance(bid, str) and re.fullmatch(r'[A-Za-z0-9_-]+', bid), 'invalid interrupted bundle ID')
    check(attempt.get('run_id') == run_id and attempt.get('status') == 'interrupted',
          'interrupted attempt identity/status mismatch')
    check(isinstance(attempt.get('reason'), str) and attempt['reason'].strip(), 'interruption reason required')
    prefix = f'test/.cache/runs/{run_id}/attempts/{bid}'
    check(attempt.get('attempt_plan') == prefix + '/plan.json', 'interrupted plan outside attempt')
    receipt_value = attempt.get('command_receipt')
    check(isinstance(receipt_value, str) and Path(receipt_value).is_relative_to(Path(prefix)),
          'command receipt outside interrupted attempt')
    files = {}
    for key in ('attempt_plan', 'command_receipt'):
        path = path_in(project, attempt[key])
        check(path.is_relative_to((project / prefix).resolve()), 'interrupted evidence resolves outside attempt')
        check(hashlib.sha256(path.read_bytes()).hexdigest() == attempt.get(key + '_sha256'),
              key + ' changed or has no registered digest')
        files[key] = json.loads(path.read_text(encoding='utf-8'))
    plan, receipt = files['attempt_plan'], files['command_receipt']
    check(plan['run_id'] == run_id and plan['bundle_id'] == bid, 'interrupted plan identity mismatch')
    for key in ('cwd', 'environment', 'scope', 'setup_projects', 'command', 'started_at', 'ended_at', 'exit_code'):
        check(key in attempt and key in receipt and attempt[key] == receipt[key],
              'interrupted receipt mismatch or missing field: ' + key)
        if key in ('cwd', 'environment', 'scope', 'setup_projects'):
            check(attempt[key] == plan[key], 'interrupted receipt differs from plan: ' + key)
    exit_code = receipt['exit_code']
    check(exit_code is None or (type(exit_code) is int and exit_code != 0),
          'interrupted receipt requires a nonzero integer exit code or explicit null')
    if exit_code is None:
        check(isinstance(receipt.get('reason'), str) and receipt['reason'].strip()
              and receipt['reason'] == attempt['reason'],
              'unknown exit code requires a receipt reason explaining why it is unavailable; --reason must match')
    check(timestamp(plan['planned_at']) <= timestamp(receipt['started_at']) <= timestamp(receipt['ended_at']),
          'invalid interrupted invocation times')
    check_attempt_command(plan, receipt['command'], project / prefix)
    directory = project / prefix
    check(not ((directory / 'results/result.json').is_file()
               and (directory / 'report/playwright/index.html').is_file()
               and len(list((directory / 'blob').glob('*.zip'))) == 1),
          'complete artifacts require normal attempt-bundle/collect-results')
    return attempt


def inspect_bundle(project, run_id, bundle, *, require_plan=False):
    """Bind immutable artifacts to an invocation and compare raw runner results."""
    bid = bundle.get('bundle_id')
    check(isinstance(bid, str) and re.fullmatch(r'[A-Za-z0-9_-]+', bid), 'invalid report bundle ID')
    check(bundle.get('run_id') == run_id, 'report run ID mismatch')
    prefix = (project / 'test/.cache/runs' / run_id / 'attempts' / bid).resolve()
    check(prefix.is_relative_to(project.resolve()), 'report run path escapes project')
    check(not require_plan or bundle.get('attempt_plan'), 'native invocation requires its attempt plan binding')
    if (prefix / 'plan.json').is_file():
        local_plan = json.loads((prefix / 'plan.json').read_text(encoding='utf-8'))
        check(not require_plan or local_plan.get('schema_version', 1) >= 2,
              'native invocation requires a current-schema attempt plan')
        if local_plan.get('schema_version', 1) >= 2:
            check(bundle.get('attempt_plan'), 'prepared attempt requires its plan binding')
    for field in ['command', 'cwd', 'environment', 'scope', 'setup_projects']:
        check(field in bundle and bundle[field] is not None, 'report requires ' + field)
    check(bundle['command'] and isinstance(bundle['setup_projects'], list), 'invalid command or setup projects')
    check(isinstance(bundle.get('exit_code'), int), 'report requires actual exit code')
    start, end = timestamp(bundle['started_at']), timestamp(bundle['ended_at'])
    check(start <= end, 'report invocation ends before start')
    tracked = {}

    def track(value):
        path = path_in(project, value)
        check(path.is_relative_to(prefix), 'report artifact outside invocation directory: ' + value)
        tracked[value] = hashlib.sha256(path.read_bytes()).hexdigest()
        return path

    raw_path = track(bundle['execution_result'])
    blobs = bundle.get('blob_reports', [])
    check(isinstance(blobs, list) and len(blobs) == len(set(blobs)), 'blob_reports must be unique paths')
    blob_metadata = {}
    for value in blobs:
        check(isinstance(value, str) and value.endswith('.zip'), 'blob report must be a ZIP path')
        blob = track(value)
        check(zipfile.is_zipfile(blob), 'invalid blob ZIP')
        blob_metadata[value] = verify_blob(blob)
    raw = json.loads(raw_path.read_text(encoding='utf-8'))
    stats = raw['stats']
    raw_start = timestamp(stats['startTime'])
    check(start <= raw_start <= end, 'JSON start time outside invocation window')
    check(raw_start.timestamp() + stats['duration'] / 1000 <= end.timestamp() + 1,
          'JSON end time outside invocation window')
    pairs = json_tests(raw)
    observed = Counter(test['status'] for _, test in pairs)
    for status in ['expected', 'unexpected', 'flaky', 'skipped']:
        check(observed[status] == stats.get(status, 0), 'JSON statistics differ from test records: ' + status)
    if bundle['exit_code'] == 0:
        check(not stats.get('unexpected') and not raw.get('errors'), 'zero exit code conflicts with runner failures')
    # Setup designation is project configuration, not a guess based on a title.
    setups = set(bundle['setup_projects'])
    attempts = [r for _, test in pairs for r in test.get('results', [])]
    for result in attempts:
        for attachment in result.get('attachments', []):
            if attachment.get('path'):
                apath = Path(attachment['path'])
                if apath.is_absolute():
                    check(apath.resolve().is_relative_to(project.resolve()), 'attachment outside project')
                    apath = apath.resolve().relative_to(project.resolve())
                track(apath.as_posix())
    origin = bundle.get('html_origin')
    check(origin in {'same_execution', 'rebuilt', 'none'}, 'invalid HTML origin')
    html = bundle.get('html_report')
    if origin == 'none':
        check(html is None, 'html_origin=none requires html_report=null')
    else:
        check(html, 'HTML path required')
        if origin == 'rebuilt':
            check(bundle.get('rebuild_source') == bundle['execution_result'] and bundle.get('rebuild_note'),
                  'rebuilt HTML requires original JSON source and reconstruction note')
        index, html_tests = html_data(track(html))
        for status in ['expected', 'unexpected', 'flaky', 'skipped']:
            check(index['stats'][status] == stats.get(status, 0), 'HTML/JSON statistics mismatch: ' + status)
        check(index['stats']['total'] == len(pairs), 'HTML/JSON total mismatch')
        check(abs(index['startTime'] / 1000 - raw_start.timestamp()) < .01
              and abs(index['duration'] - stats['duration']) < 1, 'HTML/JSON run time mismatch')
        json_keys = Counter(signature(spec['title'], test.get('projectName', ''), spec['file'],
                                      test['status'], test.get('results', [])) for spec, test in pairs)
        html_keys = Counter(signature(test['title'], test.get('projectName', ''), test['location']['file'],
                                      test['outcome'], test.get('results', [])) for test in html_tests)
        check(json_keys == html_keys, 'HTML/JSON test identities, outcomes or attempts mismatch')
        for test in html_tests:
            for result in test.get('results', []):
                for attachment in result.get('attachments', []):
                    if attachment.get('path'):
                        value = (Path(html).parent / attachment['path']).as_posix()
                        track(value)
    setup_tests = [test for _, test in pairs if test.get('projectName') in setups]
    business = [test for _, test in pairs if test.get('projectName') not in setups]
    regression = inspect_attempt_plan(project, run_id, bundle, raw, track, blob_metadata) if bundle.get('attempt_plan') else None
    return dict(bundle, **({'regression_check': regression} if regression is not None else {}),
                report_consistency='passed' if html else 'json_only',
                artifact_sha256=tracked,
                runner_counts={key: stats.get(key, 0) for key in ['expected', 'unexpected', 'flaky', 'skipped']},
                setup_targets=len(setup_tests), business_targets=len(business),
                business_attempts=sum(r.get('status') not in {'skipped', None}
                                      for test in business for r in test.get('results', [])),
                raw_business_skipped=sum(t['status'] == 'skipped' for t in business))


def recheck_bundle(project, run_id, saved, *, require_plan=False):
    current = inspect_bundle(project, run_id, saved, require_plan=require_plan)
    check(current['artifact_sha256'] == saved['artifact_sha256'], 'report artifacts changed after registration')
    if saved.get('attempt_plan'):
        check(current['regression_check'] == saved.get('regression_check'), 'regression target evidence changed')
    return current


def merged_slots(tests, native=False):
    """Native merging may group retries; compare every raw attempt as a multiset."""
    slots = Counter()
    for spec, test in tests:
        title = test['title'] if native else spec['title']
        file = test['location']['file'] if native else spec['file']
        outcome = test.get('outcome', test.get('status')) if native else test.get('status')
        results = test.get('results', [])
        for result in results:
            slots[signature(title, test.get('projectName', ''), file, outcome, [result])] += 1
        if not results:
            slots[signature(title, test.get('projectName', ''), file, outcome, [])] += 1
    return slots


def inspect_merged_report(project, run_id, bundles, merged):
    check(isinstance(merged, dict), 'merged_report must be an object')
    ids = merged.get('bundle_ids', [])
    check(isinstance(ids, list) and len(ids) == len(set(ids)) and set(ids) == set(bundles) and ids,
          'merged report must include every current invocation exactly once')
    check(merged.get('exit_code') == 0, 'merged report requires successful actual merge command')
    verify_merge_command(merged.get('command'))
    check(timestamp(merged['ended_at']) >= timestamp(merged['started_at']), 'merge ends before start')
    prefix = (project / 'test/.cache/runs' / run_id / 'merged/playwright').resolve()
    tracked = {}

    def track(value):
        path = path_in(project, value)
        check(path.is_relative_to(prefix), 'merged artifact outside run merged directory')
        tracked[value] = hashlib.sha256(path.read_bytes()).hexdigest()
        return path

    expected = Counter()
    expected_details = Counter()
    expected_outcomes = Counter()
    inputs = {}
    for bid in ids:
        bundle = recheck_bundle(project, run_id, bundles[bid])
        check(bundle.get('blob_reports'), 'native merge requires blob reports for invocation: ' + bid)
        inputs[bid] = {p: bundle['artifact_sha256'][p] for p in bundle['blob_reports']}
        raw = json.loads(path_in(project, bundle['execution_result']).read_text(encoding='utf-8'))
        pairs = json_tests(raw)
        expected.update(merged_slots(pairs))
        expected_details.update(attempt_facts(project, pairs))
        expected_outcomes.update(test['status'] for _, test in pairs)
    index, tests = html_data(track(merged['html_report']))
    check(index['stats']['total'] == len(tests), 'merged HTML total differs from records')
    check(merged_slots([(None, test) for test in tests], native=True) == expected,
          'merged HTML omits, duplicates or changes original attempts')
    actual_outcomes = Counter(test['outcome'] for test in tests)
    check(actual_outcomes == expected_outcomes,
          'merged HTML test outcomes differ from original runner outcomes')
    for outcome in ('expected', 'unexpected', 'flaky', 'skipped'):
        check(index['stats'].get(outcome, 0) == actual_outcomes[outcome],
              'merged HTML statistics differ from its test outcomes: ' + outcome)
    actual_details = attempt_facts(project, [(None, test) for test in tests],
                                   html_report=merged['html_report'], native=True)
    check(actual_details == expected_details,
          'merged HTML loses or changes error details or attachments')
    for test in tests:
        for result in test.get('results', []):
            for attachment in result.get('attachments', []):
                if attachment.get('path'):
                    track((Path(merged['html_report']).parent / attachment['path']).as_posix())
    return dict(merged, html_origin='merged', artifact_sha256=tracked, inputs=inputs, report_consistency='checked')


def recheck_merged_report(project, run_id, bundles, saved):
    current = inspect_merged_report(project, run_id, bundles, saved)
    check(current['artifact_sha256'] == saved['artifact_sha256'] and current['inputs'] == saved['inputs'],
          'merged report or inputs changed after registration')
    return current


def review_html(project, state, consistency):
    """Failure-first derived index across all invocations, including unbound raw results."""
    root = Path('test/.cache/runs') / state['run_id']
    escape = lambda value: html.escape(str(value), quote=True)

    def link(value, label):
        path_in(project, value)
        relative = Path(value).relative_to(root).as_posix()
        return '<a href="' + quote(relative, safe='/') + '">' + escape(label) + '</a>'

    def attachment_link(value, label):
        path = Path(value)
        if path.is_absolute():
            try:
                value = path.resolve().relative_to(project.resolve()).as_posix()
            except ValueError:
                return escape(label) + ' (路径不在项目内)'
        try:
            path_in(project, value).relative_to((project / root).resolve())
        except (ValueError, OSError):
            return escape(label) + ' (附件缺失或路径超出本轮)'
        return link(value, label)

    rows, warnings, invocation_links = [], [], []
    for bid, bundle in state.get('report_bundles', {}).items():
        try:
            raw = json.loads(path_in(project, bundle['execution_result']).read_text(encoding='utf-8'))
            try:
                original = link(bundle['html_report'], '原生HTML') if bundle.get('html_report') else '原生HTML缺失'
            except (ValueError, OSError) as error:
                original = '原生HTML不可用'
                warnings.append(escape(bid + ': ' + str(error)))
            invocation_links.append('<li>' + escape(bid) + ' · ' + original + ' · ' +
                                    link(bundle['execution_result'], '原始JSON') + '</li>')
            for spec, test in json_tests(raw):
                for ordinal, result in enumerate(test.get('results') or [{}]):
                    status = result.get('status', 'notrun')
                    failed = status in {'failed', 'timedOut', 'interrupted'} and test.get('expectedStatus', 'passed') != status
                    priority = 0 if failed else (1 if test['status'] in {'unexpected', 'flaky'} else
                                                (2 if status in {'skipped', 'notrun'} else 3))
                    errors = result.get('errors') or ([result['error']] if result.get('error') else [])
                    detail = '\n'.join(error.get('stack') or error.get('message') or str(error)
                                       if isinstance(error, dict) else str(error) for error in errors)
                    cells = [bid, spec['title'], test.get('projectName', ''), spec['file'],
                             str(result.get('retry', ordinal)), status, test['status']]
                    row = '<tr data-priority="' + str(priority) + '">' + ''.join('<td>' + escape(c) + '</td>' for c in cells)
                    row += '<td>' + original
                    if detail:
                        row += '<details><summary>错误详情</summary><pre>' + escape(detail) + '</pre></details>'
                    attachments = result.get('attachments', [])
                    links = [attachment_link(item['path'], item.get('name', Path(item['path']).name))
                             for item in attachments if item.get('path')]
                    if links:
                        row += '<details><summary>截图、Trace等附件</summary>' + '<br>'.join(links) + '</details>'
                    row += '</td></tr>'
                    rows.append((priority, len(rows), row))
            for error in raw.get('errors', []):
                warnings.append(escape(bid + ' · runner error: ' + str(error)))
        except (ValueError, KeyError, TypeError, OSError) as error:
            warnings.append(escape(bid + ': ' + str(error)))
    merged = state.get('merged_report')
    if merged:
        try:
            if any(error.startswith('merged_report:') for error in consistency.get('errors', [])):
                primary = '合并报告未通过完整性核验（仅供诊断）：' + link(merged['html_report'], '打开合并的Playwright HTML报告')
            else:
                primary = '已与本轮原始尝试核验：' + link(merged['html_report'], '打开合并的Playwright HTML报告')
        except (ValueError, OSError) as error:
            primary = '合并HTML不可用：' + escape(error)
    else:
        primary = '尚无原生合并报告；下表是依据原始JSON生成的汇总索引，不代替原生HTML'
    warnings.extend(escape(e) for e in consistency.get('errors', []))
    completeness = regression_completeness(
        state.get('report_bundles', {}), state['scopes'], required=state.get('native_merge_required', False),
        project=project)
    interrupted = []
    for bid, attempt in state.get('interrupted_attempts', {}).items():
        try:
            receipt = link(attempt['command_receipt'], '命令回执')
        except (ValueError, OSError):
            receipt = '命令回执缺失'
        interrupted.append('<li>' + escape(bid) + ' · ' + escape(attempt['reason']) + ' · '
                           + escape(attempt['started_at']) + ' → ' + escape(attempt['ended_at'])
                           + ' · exit=' + escape('unknown' if attempt['exit_code'] is None else attempt['exit_code']) + ' · ' + receipt + '</li>')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>E2E review</title><style>
body{font:15px/1.5 sans-serif;margin:24px;color:#24312c;background:#f6f7f4}
main{max-width:1500px;margin:auto}a{color:#12674e}table{border-collapse:collapse;width:100%;background:white}
td,th{padding:10px;text-align:left;border-bottom:1px solid #d6ddd7;vertical-align:top}
tr[data-priority="0"]{background:#fff0ec}tr[data-priority="1"],tr[data-priority="2"]{background:#fff9e5}
pre{white-space:pre-wrap;overflow-wrap:anywhere;max-width:700px}.scroll{overflow:auto}
</style><main><h1>本轮测试报告 · ''' + escape(state['run_id']) + '''</h1><p>''' + primary + '''</p>
<p>顺序：失败 → 非预期/重跑后通过 → 跳过/未执行 → 通过。Setup与每次原始尝试均保留，不代表唯一用例数。</p>
<p>报告一致性：''' + escape(consistency['status']) + '</p><p>收尾全量目标集合：' + escape(completeness['status']) + '''
（与执行者声明完整的发现清单比较，独立于通过/失败）</p>
<details><summary>全量目标、Setup及分片差集</summary><pre>''' + escape(json.dumps(completeness, ensure_ascii=False, indent=2)) + '''
</pre></details><ul>''' + ''.join('<li>' + w + '</li>' for w in warnings) + '''
</ul><div class="scroll"><table><thead><tr><th>执行批次</th><th>用例</th><th>Project</th><th>文件</th>
<th>Retry</th><th>原始结果</th><th>Runner结论</th><th>证据</th></tr></thead><tbody>''' + ''.join(row for _, _, row in sorted(rows)) + '''
</tbody></table></div><h2>中断调用（仅回执，不计测试结果或全量完整性）</h2><ul>''' + ''.join(interrupted) + '''
</ul><h2>各次执行的原始报告</h2><ul>''' + ''.join(invocation_links) + '</ul></main></html>'


def check_execution(project, bundle, event, expected_title=None):
    """Match a receipt to one raw test result, including unexecuted targets."""
    data = json.loads(path_in(project, bundle['execution_result']).read_text(encoding='utf-8'))
    pointer = event.get('runner_test_pointer', '')
    check(re.fullmatch(r'/suites/\d+(?:/suites/\d+)*/specs/\d+/tests/\d+', pointer),
          'execution requires a JSON runner_test_pointer to tests/N')
    value = data
    for part in pointer.strip('/').split('/'):
        value = value[int(part)] if isinstance(value, list) else value[part]
    if expected_title is not None:
        parent = data
        for part in pointer.strip('/').split('/')[:-2]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        check(parent['title'] == expected_title, 'raw runner title differs from reviewed case')
    check(event['setup'] == (value.get('projectName') in bundle['setup_projects']), 'execution Setup designation differs from bundle')
    index = event.get('runner_result_index')
    if index is None:
        check(not value.get('results') and value['status'] == 'skipped' and event['result'] in {'notrun', 'skip'},
              'missing raw result index requires unexecuted skipped target')
    else:
        check(isinstance(index, int) and 0 <= index < len(value['results']), 'invalid runner_result_index')
        result = value['results'][index]
        mapped = {'passed': 'pass', 'failed': 'fail', 'timedOut': 'fail', 'interrupted': 'fail', 'skipped': 'skip'}
        expected = mapped.get(result['status'])
        actual = 'skip' if event.get('blocked_by') else event['result']
        check(expected == actual, 'execution receipt differs from raw runner result')
    return pointer, index
