"""Check Playwright evidence and render a derived review index; never run tests."""
import base64
from collections import Counter
from datetime import datetime
import hashlib
import html
import importlib.util
import io
import json
from pathlib import Path
import re
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
            lines = [line for line in archive.read('report.jsonl').splitlines() if line.strip()]
            check(lines, 'blob report is empty')
            metadata = json.loads(lines[0])
            check(metadata.get('method') == 'onBlobReportMetadata'
                  and metadata.get('params', {}).get('userAgent', '').startswith('Playwright/')
                  and isinstance(metadata.get('params', {}).get('version'), int),
                  'blob report lacks Playwright metadata')
    except (zipfile.BadZipFile, KeyError, ValueError, UnicodeError) as error:
        raise ValueError('invalid Playwright blob report') from error


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


def inspect_bundle(project, run_id, bundle):
    """Bind immutable artifacts to an invocation and compare raw runner results."""
    bid = bundle.get('bundle_id')
    check(isinstance(bid, str) and re.fullmatch(r'[A-Za-z0-9_-]+', bid), 'invalid report bundle ID')
    check(bundle.get('run_id') == run_id, 'report run ID mismatch')
    prefix = (project / 'test/.cache/runs' / run_id / 'attempts' / bid).resolve()
    check(prefix.is_relative_to(project.resolve()), 'report run path escapes project')
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
    for value in blobs:
        check(isinstance(value, str) and value.endswith('.zip'), 'blob report must be a ZIP path')
        blob = track(value)
        check(zipfile.is_zipfile(blob), 'invalid blob ZIP')
        verify_blob(blob)
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
    return dict(bundle, report_consistency='passed' if html else 'json_only',
                artifact_sha256=tracked,
                runner_counts={key: stats.get(key, 0) for key in ['expected', 'unexpected', 'flaky', 'skipped']},
                setup_targets=len(setup_tests), business_targets=len(business),
                business_attempts=sum(r.get('status') not in {'skipped', None}
                                      for test in business for r in test.get('results', [])),
                raw_business_skipped=sum(t['status'] == 'skipped' for t in business))


def recheck_bundle(project, run_id, saved):
    current = inspect_bundle(project, run_id, saved)
    check(current['artifact_sha256'] == saved['artifact_sha256'], 'report artifacts changed after registration')
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
<p>报告一致性：''' + escape(consistency['status']) + '</p><ul>' + ''.join('<li>' + w + '</li>' for w in warnings) + '''
</ul><div class="scroll"><table><thead><tr><th>执行批次</th><th>用例</th><th>Project</th><th>文件</th>
<th>Retry</th><th>原始结果</th><th>Runner结论</th><th>证据</th></tr></thead><tbody>''' + ''.join(row for _, _, row in sorted(rows)) + '''
</tbody></table></div><h2>各次执行的原始报告</h2><ul>''' + ''.join(invocation_links) + '</ul></main></html>'


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
