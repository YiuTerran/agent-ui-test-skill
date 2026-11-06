"""Optional real Chromium diagnostics, native attachments and offline merge."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
CLI = os.environ.get('AUTO_TEST_PLAYWRIGHT_CLI')
SPEC = importlib.util.spec_from_file_location('ui_browser_artifacts', ROOT / 'scripts/report_artifacts.py')
artifacts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifacts)
UI_SPEC = importlib.util.spec_from_file_location('ui_browser_coverage', ROOT / 'scripts/ui_coverage.py')
ui = importlib.util.module_from_spec(UI_SPEC)
UI_SPEC.loader.exec_module(ui)


@unittest.skipUnless(CLI and shutil.which('node'),
                     'set AUTO_TEST_PLAYWRIGHT_CLI and install matching Chromium for UI smoke')
class UiBrowserTest(unittest.TestCase):
    def test_browser_diagnostics_preserve_failure_and_survive_native_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            url = f'http://127.0.0.1:{server.server_port}'
            launcher = [shutil.which('node'), str(Path(CLI).resolve())]
            package = (Path(CLI).resolve().parent / 'index.mjs').as_uri()
            fixture = (ROOT / 'assets/ui-diagnostics.mjs').as_uri()
            specs = project / 'specs'
            specs.mkdir()
            (project / 'playwright.config.cjs').write_text('module.exports = ' + json.dumps({
                'testDir': './specs', 'timeout': 15000, 'retries': 0, 'workers': 1,
                'use': {'trace': 'off', 'screenshot': 'off', 'video': 'off'},
                'reporter': [['list'], ['json', {'outputFile': 'original.json'}],
                             ['html', {'outputFolder': 'html', 'open': 'never'}],
                             ['blob', {'outputDir': 'blobs'}]]
            }), encoding='utf-8')
            (specs / 'diagnostics.spec.mjs').write_text(f'''
import {{ test as base, expect }} from {json.dumps(package)};
import {{ withUiDiagnostics }} from {json.dumps(fixture)};
const url = {json.dumps(url)};
const diagnosticTest = withUiDiagnostics(base);
const test = diagnosticTest.extend({{
  ownedFixture: [async ({{ uiDiagnostics }}, use, testInfo) => {{
    await use('project fixture preserved');
    if (testInfo.title === 'UI native failure') {{
      await uiDiagnostics.step('orders.cleanup', async () => {{
        throw new Error('CLEANUP_ORIGINAL_FAILURE');
      }}, {{ phase: 'cleanup' }});
    }}
  }}, {{ auto: true }}]
}});
test('UI success', async ({{ page, uiDiagnostics, ownedFixture }}) => {{
  expect(ownedFixture).toBe('project fixture preserved');
  await uiDiagnostics.step('orders.open', async () => {{
    await page.goto(url);
    await expect(page.getByRole('heading')).toHaveText('Local fixture');
  }});
}});
test('UI native failure', async ({{ page, context, uiDiagnostics }}) => {{
  await page.goto(url + '/private/SECRET_CANARY_PRIVATE?token=SECRET_CANARY_PRIVATE');
  const popup = await context.newPage();
  await Promise.all([
    popup.waitForEvent('pageerror'),
    popup.evaluate(() => setTimeout(() => {{ throw new Error('SECRET_CANARY_PRIVATE'); }}, 0))
  ]);
  await page.evaluate(async () => {{
    await fetch('/error/SECRET_CANARY_PRIVATE?token=SECRET_CANARY_PRIVATE');
    await fetch('http://127.0.0.1:1/SECRET_CANARY_PRIVATE').catch(() => {{}});
  }});
  await uiDiagnostics.step('orders.submit', async () => {{
    expect('BUSINESS_ORIGINAL_ACTUAL').toBe('BUSINESS_ORIGINAL_EXPECTED');
  }});
}});
base('Existing UI without diagnostics', async ({{ page }}) => {{
  await page.goto(url);
  await expect(page.getByRole('heading')).toHaveText('Local fixture');
}});
''', encoding='utf-8')
            (specs / 'api.spec.mjs').write_text(f'''
import {{ test, expect }} from {json.dumps(package)};
test.use({{ launchOptions: {{ executablePath: {json.dumps(str(project / 'browser-must-not-start'))} }} }});
test('API without UI fixture', async ({{ request }}, testInfo) => {{
  expect((await request.get({json.dumps(url)})).status()).toBe(200);
  expect(testInfo.attachments).toHaveLength(0);
}});
''', encoding='utf-8')
            discovery = subprocess.run(launcher + ['test', '--list', '--reporter=json'],
                                       cwd=project, capture_output=True, text=True, timeout=30)
            self.assertEqual(discovery.returncode, 0, discovery.stdout + discovery.stderr)
            plan = json.loads((ROOT / 'assets/ui-coverage.template.json').read_text(encoding='utf-8'))
            coverage = {'implementations': {'order-create-ui': {
                'case_id': 'ORDER-CREATE', 'suite_id': 'example-ui', 'kind': 'ui',
                'implementation': 'valid', 'execution': 'automation', 'test_id': 'ORDER-CREATE',
                'spec': 'specs/diagnostics.spec.mjs', 'title': 'UI success'}}}
            checked = ui.Validator(project, plan, {'cases': {
                'ORDER-CREATE': {'required_kinds': ['ui', 'api']}}}, coverage,
                json.loads(discovery.stdout)).run()
            self.assertEqual(checked['discovery'], 'checked')
            self.assertEqual(checked['counts']['mapped_cases'], 1)
            process = subprocess.run(launcher + ['test'], cwd=project, capture_output=True,
                                     text=True, timeout=90)
            self.assertEqual(process.returncode, 1, process.stdout + process.stderr)
            raw = json.loads((project / 'original.json').read_text(encoding='utf-8'))
            self.assertFalse(raw.get('errors'), raw.get('errors'))
            pairs = artifacts.json_tests(raw)
            self.assertEqual(len(pairs), 4)
            failures = [(spec, test) for spec, test in pairs if test['status'] == 'unexpected']
            self.assertEqual(len(failures), 1, process.stdout)
            result = failures[0][1]['results'][0]
            self.assertEqual(result['status'], 'failed')
            messages = str(result['errors'])
            self.assertIn('BUSINESS_ORIGINAL_ACTUAL', messages)
            self.assertIn('CLEANUP_ORIGINAL_FAILURE', messages)
            attachments = [item for item in result['attachments'] if item['name'] == 'ui-diagnostics']
            self.assertEqual(len(attachments), 1)
            attachment = attachments[0]
            content = (Path(attachment['path']).read_bytes() if attachment.get('path')
                       else base64.b64decode(attachment['body']))
            self.assertNotIn(b'SECRET_CANARY_PRIVATE', content)
            diagnostic = json.loads(content)
            self.assertLessEqual(len(content), 65536)
            self.assertEqual(diagnostic['native_error_count'], 2)
            self.assertTrue({'page_error', 'http_error', 'request_failed'} <= {
                event['type'] for event in diagnostic['events']})
            self.assertEqual({event['phase'] for event in diagnostic['events']
                              if event.get('outcome') == 'failed'}, {'test', 'cleanup'})
            for spec, test in pairs:
                if test['status'] != 'unexpected':
                    self.assertEqual(test['status'], 'expected')
                    self.assertFalse(test['results'][0].get('attachments'))
            blobs = list((project / 'blobs').glob('*.zip'))
            self.assertEqual(len(blobs), 1)
            artifacts.verify_blob(blobs[0])
            merged = subprocess.run(launcher + ['merge-reports', '--reporter=html', str(project / 'blobs')],
                                    cwd=project, capture_output=True, text=True, timeout=60,
                                    env=dict(os.environ, PLAYWRIGHT_HTML_OUTPUT_DIR=str(project / 'merged'),
                                             PLAYWRIGHT_HTML_OPEN='never'))
            self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
            expected = artifacts.attempt_facts(project, pairs)
            for folder in ('html', 'merged'):
                html = project / folder / 'index.html'
                _, tests = artifacts.html_data(html)
                actual = artifacts.attempt_facts(project, [(None, test) for test in tests],
                                                html_report=html, native=True)
                self.assertEqual(expected, actual)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(503 if self.path.startswith('/error/') else 200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<h1>Local fixture</h1>')


if __name__ == '__main__':
    unittest.main()
