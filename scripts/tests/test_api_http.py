"""Optional real Playwright requests against an isolated loopback HTTP fixture."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest

import test_suite_catalog as suites

wf = suites.wf
CLI = os.environ.get('AUTO_TEST_PLAYWRIGHT_CLI')


@unittest.skipUnless(CLI and shutil.which('node') and shutil.which('git'),
                     'set AUTO_TEST_PLAYWRIGHT_CLI to an installed Playwright CLI')
class ApiHttpTest(unittest.TestCase):
    for _name in ('tearDown', 'write', 'cli', 'start', 'record', 'check_sources', 'state', 'migrate'):
        locals()[_name] = getattr(suites.CatalogTest, _name)

    def setUp(self):
        suites.CatalogTest.setUp(self)
        catalog_root = self.project
        backend = suites.CatalogTest.backend(self, kinds=('api',))
        suites.CatalogTest.add_api_implementations(self, backend)
        self.project = backend
        self.launcher = [shutil.which('node'), str(Path(CLI).resolve())]
        self.assertTrue(Path(CLI).is_file())
        spec = wf.parse_spec(self.project / 'test/spec.md')
        spec['concurrency']['browser_parallel_cap'] = 0
        self.write('test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```\n')
        self.write('test/evidence.txt', 'Isolated HTTP fixture evidence')
        for root in (catalog_root, self.project):
            wf.atomic(root / '.gitignore', '/test/.cache/\n')
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True, capture_output=True)

    def test_real_http_attempt_collect_merge_and_finalize(self):
        records, observations = {}, []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, body):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def handle_request(self):
                observations.append((self.command, self.path))
                if self.headers.get('Authorization') != 'Bearer fixture-only':
                    self.respond(401, {'error': 'unauthorized'})
                    return
                if self.path == '/restricted':
                    self.respond(403, {'error': 'forbidden'})
                    return
                if self.command == 'POST':
                    body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                    if not body.get('name'):
                        self.respond(400, {'error': 'name_required'})
                        return
                    records['record-1'] = body
                    self.respond(201, {'id': 'record-1'})
                elif self.path == '/records/record-1' and 'record-1' in records:
                    if self.command == 'DELETE':
                        del records['record-1']
                        self.respond(200, {'deleted': True})
                    else:
                        self.respond(200, records['record-1'])
                else:
                    self.respond(404, {'error': 'not_found'})

            do_GET = handle_request
            do_POST = handle_request
            do_DELETE = handle_request

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base_url = 'http://127.0.0.1:' + str(server.server_port)
        package = json.dumps(str(Path(CLI).resolve().parent))
        self.write('playwright.config.cjs', 'module.exports = { testDir: "./test/src", retries: 0 };')
        self.write('test/src/C1.spec.ts', f'''
const {{ test, expect }} = require({package});
test('Synthetic C1', async ({{ request }}) => {{
  const url = {json.dumps(base_url)};
  expect((await request.get(url + '/records/record-1')).status()).toBe(401);
  const headers = {{ Authorization: 'Bearer fixture-only' }};
  expect((await request.get(url + '/restricted', {{ headers }})).status()).toBe(403);
  expect((await request.post(url + '/records', {{ headers, data: {{}} }})).status()).toBe(400);
  let created = false;
  try {{
    const result = await request.post(url + '/records', {{ headers, data: {{ name: 'fixture' }} }});
    expect(result.status()).toBe(201);
    created = true;
    expect(await result.json()).toEqual({{ id: 'record-1' }});
    const readback = await request.get(url + '/records/record-1', {{ headers }});
    expect(readback.status()).toBe(200);
    expect(await readback.json()).toEqual({{ name: 'fixture' }});
  }} finally {{
    if (created) expect((await request.delete(url + '/records/record-1', {{ headers }})).status()).toBe(200);
    expect((await request.get(url + '/records/record-1', {{ headers }})).status()).toBe(404);
  }}
}});
''')
        self.start('--scopes', 'a')
        self.check_sources('S1')
        command = self.launcher + ['test', 'C1.spec.ts', '--workers=1']
        discovery = subprocess.run(command + ['--list', '--reporter=json'], cwd=self.project,
                                   capture_output=True, text=True, timeout=30, check=True)
        self.write('test/.cache/discovery.json', json.loads(discovery.stdout))
        self.write('preflight.json', {'discovery': {'path': 'test/.cache/discovery.json',
                                                   'complete': True, 'setup_projects': []}})
        self.cli('preflight', '--run', self.run, '--owner', self.owner,
                 '--input', str(self.project / 'preflight.json'))
        self.cli('prepare-regression', '--run', self.run, '--owner', self.owner)
        self.write('attempt.json', {'command': command, 'cwd': '.', 'environment': 'loopback-fixture',
                                    'scope': ['a'], 'setup_projects': [], 'regression_mode': 'full',
                                    'extra_reporters': [str(Path(wf.__file__).with_name('progress_playwright_reporter.mjs'))]})
        prepared = self.cli('attempt-init', '--run', self.run, '--owner', self.owner,
                            '--bundle', 'HTTP', '--input', str(self.project / 'attempt.json'))
        lease = suites.sc.execute('acquire', self.project, {
            'run_id': self.run, 'task_id': 'http', 'owner': self.owner, 'owner_pid': os.getpid(),
            'tasks': {'http': {'phase': 'execution', 'writable_paths': [], 'isolation': 'verified',
                               'quota': {'agents': 1, 'playwright_workers': 1}}}})
        self.assertTrue(lease['acquired'], lease)
        self.assertEqual(lease['lease']['quota']['browsers'], 0)
        gate = suites.cp.predispatch_gate(self.project, {
            'resource_environment': 'loopback-fixture',
            'resource_access': {'mode': 'write', 'kinds': ['fixture-record'], 'dependencies': []}})
        self.assertTrue(gate['allowed'])
        argv = command + prepared['reporter_arguments']
        started = wf.now()
        try:
            process = subprocess.run(argv, cwd=self.project, env=dict(os.environ, **prepared['environment']),
                                     capture_output=True, text=True, timeout=60)
        finally:
            suites.sc.execute('release', self.project, {
                'lease_id': lease['lease']['lease_id'], 'release_token': lease['lease']['release_token']})
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn('E2E_PROGRESS', process.stdout)
        self.assertFalse(records)
        self.assertEqual(len(observations), 7)
        self.write(prepared['receipt_path'], {
            'command': argv, 'cwd': '.', 'environment': 'loopback-fixture',
            'scope': ['a'], 'setup_projects': [], 'started_at': started, 'ended_at': wf.now(),
            'exit_code': process.returncode})
        collected = self.cli('attempt-collect', '--run', self.run, '--owner', self.owner, '--bundle', 'HTTP')
        self.assertTrue(collected['collection']['complete'])
        self.assertEqual(self.state()['executions'][0]['suite'], {'id': 'backend', 'kind': 'api'})
        self.assertEqual(self.state()['executions'][0]['implementation_ids'], ['backend-C1'])
        self.write('finalize.json', {
            'acceptance': {'status': 'fully_accepted', 'reason': 'Isolated HTTP fixture and cleanup verified',
                           'evidence': ['test/evidence.txt']}, 'require_full': True,
            'merge': {'command': self.launcher}})
        result = self.cli('finalize', '--run', self.run, '--owner', self.owner,
                          '--input', str(self.project / 'finalize.json'))
        self.assertTrue(result['released'])
        self.assertEqual(result['regression_completeness'], 'complete')
        summary = wf.load(self.project / result['report_paths']['machine_report'])
        self.assertEqual(summary['coverage_layers']['results_by_kind'], {'api': {'C1': 'pass'}})
        self.assertEqual(summary['report_consistency']['status'], 'checked')


if __name__ == '__main__':
    unittest.main()
