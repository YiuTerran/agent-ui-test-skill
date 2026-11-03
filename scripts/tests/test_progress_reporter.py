"""Progress observation without changing test execution or workflow evidence."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node is optional for the Python skill tools')
class ProgressReporterTest(unittest.TestCase):
    def run_reporter(self, body):
        reporter = Path(__file__).resolve().parents[1] / 'progress_playwright_reporter.mjs'
        source = r'''
import assert from 'node:assert/strict';
const {default: Reporter} = await import(process.argv[1]);
const records = [];
const write = process.stdout.write.bind(process.stdout);
process.stdout.write = text => {
  assert.ok(text.startsWith('E2E_PROGRESS '));
  records.push(JSON.parse(text.slice('E2E_PROGRESS '.length)));
  return true;
};
let tick;
let cleared = false;
globalThis.setInterval = (callback, delay) => {
  assert.ok(delay >= 1000 && delay <= 60000);
  tick = callback;
  return {unref() {}};
};
globalThis.clearInterval = () => { cleared = true; };
function test(project = '', retries = 0, expectedStatus = 'passed') {
  return {parent: {project: () => ({name: project})}, retries, expectedStatus, results: [],
    title: 'private-test-title', repeatEachIndex: 0};
}
function begin(reporter, tests) {
  reporter.onBegin({workers: 2, shard: {current: 1, total: 2}}, {allTests: () => tests});
}
function finish(reporter, item, status, retry = 0) {
  const result = {status, retry, error: {message: 'private-error-credential'}};
  item.results.push(result);
  reporter.onTestEnd(item, result);
}
''' + body + r'''
assert.ok(!JSON.stringify(records).includes('private'));
write(JSON.stringify(records));
'''
        environment = dict(os.environ)
        for name in ('E2E_PROGRESS_INTERVAL_MS', 'E2E_PROGRESS_SETUP_PROJECTS'):
            environment.pop(name, None)
        result = subprocess.run(
            ['node', '--input-type=module', '-e', source, reporter.as_uri()],
            capture_output=True, text=True, env=environment, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_begin_heartbeat_and_end_supply_counts_without_exposing_test_details(self):
        records = self.run_reporter('''
const reporter = new Reporter();
assert.equal(reporter.printsToStdio(), true);
const tests = [test(), test()];
begin(reporter, tests);
reporter.onTestBegin(tests[0], {retry: 0});
tick();
finish(reporter, tests[0], 'passed');
reporter.onEnd({status: 'passed'});
assert.ok(cleared);
''')
        self.assertEqual([1, 2, 3], [record['sequence'] for record in records])
        self.assertEqual(2, records[0]['counts']['remaining'])
        self.assertEqual(1, records[1]['counts']['running'])
        self.assertEqual(1, records[-1]['counts']['remaining'])
        self.assertEqual(1, records[-1]['counts']['passed'])
        self.assertEqual('runner_ended', records[-1]['phase'])
        self.assertIsNone(records[-1]['business'])
        self.assertEqual({'current': 1, 'total': 2}, records[-1]['shard'])

    def test_retry_does_not_inflate_total_or_report_failure_before_retry_exhaustion(self):
        records = self.run_reporter('''
const reporter = new Reporter({setupProjects: []});
const item = test('', 2);
begin(reporter, [item]);
finish(reporter, item, 'failed');
tick();
reporter.onTestBegin(item, {retry: 1});
finish(reporter, item, 'passed', 1);
reporter.onEnd({status: 'passed'});
''')
        self.assertEqual(0, records[1]['counts']['ended'])
        self.assertEqual(1, records[1]['counts']['retry_pending'])
        final = records[-1]['business']
        self.assertEqual(1, final['total'])
        self.assertEqual(1, final['ended'])
        self.assertEqual(1, final['retried'])
        self.assertEqual(1, final['flaky'])
        self.assertEqual(0, final['failed'])

    def test_early_runner_end_finalizes_failure_without_synthesizing_unrun_results(self):
        records = self.run_reporter('''
const reporter = new Reporter();
const failed = test('', 3), pending = test();
begin(reporter, [failed, pending]);
finish(reporter, failed, 'failed');
reporter.onError(new Error('private-global-error'));
reporter.onEnd({status: 'failed'});
''')
        final = records[-1]
        self.assertEqual(1, final['counts']['failed'])
        self.assertEqual(1, final['counts']['remaining'])
        self.assertEqual(1, final['counts']['pending'])
        self.assertEqual(1, final['global_errors'])

    def test_setup_repeats_projects_expected_failure_and_skip_are_distinct(self):
        records = self.run_reporter('''
const reporter = new Reporter({setupProjects: ['setup']});
const tests = [test('setup'), test('browser'), test('browser'), test('other'),
  test('browser', 0, 'failed'), test('browser')];
tests[2].repeatEachIndex = 1;
begin(reporter, tests);
for (const item of tests.slice(0, 4)) finish(reporter, item, 'passed');
finish(reporter, tests[4], 'failed');
finish(reporter, tests[5], 'skipped');
reporter.onEnd({status: 'passed'});
''')
        final = records[-1]
        self.assertEqual(6, final['counts']['total'])
        self.assertEqual(1, final['setup']['passed'])
        self.assertEqual(5, final['business']['ended'])
        self.assertEqual(3, final['business']['passed'])
        self.assertEqual(1, final['business']['expected_failed'])
        self.assertEqual(1, final['business']['skipped'])
        self.assertEqual(0, final['business']['remaining'])

    def test_serial_retry_can_reopen_observed_pass_without_double_counting(self):
        records = self.run_reporter('''
const reporter = new Reporter();
const item = test('', 1);
begin(reporter, [item]);
finish(reporter, item, 'passed');
tick();
reporter.onTestBegin(item, {retry: 1});
tick();
finish(reporter, item, 'failed', 1);
reporter.onEnd({status: 'failed'});
''')
        self.assertEqual(1, records[1]['counts']['ended'])
        self.assertEqual(0, records[2]['counts']['ended'])
        self.assertEqual(1, records[2]['counts']['running'])
        self.assertEqual(1, records[-1]['counts']['total'])
        self.assertEqual(1, records[-1]['counts']['failed'])
        self.assertEqual(0, records[-1]['counts']['passed'])

    def test_interruption_retains_remaining_work_and_clears_running_count(self):
        records = self.run_reporter('''
const reporter = new Reporter();
const item = test();
begin(reporter, [item]);
reporter.onTestBegin(item, {retry: 0});
finish(reporter, item, 'interrupted');
reporter.onEnd({status: 'interrupted'});
''')
        final = records[-1]
        self.assertEqual('interrupted', final['runner_status'])
        self.assertEqual(1, final['counts']['interrupted'])
        self.assertEqual(1, final['counts']['remaining'])
        self.assertEqual(0, final['counts']['running'])

    def test_unexpected_pass_is_failure_and_startup_errors_keep_total_unknown(self):
        records = self.run_reporter('''
const reporter = new Reporter();
const item = test('', 0, 'failed');
begin(reporter, [item]);
finish(reporter, item, 'passed');
reporter.onEnd({status: 'failed'});
const startup = new Reporter();
startup.onError(new Error('private-error'));
startup.onEnd({status: 'failed'});
''')
        self.assertEqual(1, records[-2]['counts']['failed'])
        self.assertIsNone(records[-1]['counts'])
        self.assertEqual(1, records[-1]['global_errors'])

    def test_environment_options_and_invalid_configuration(self):
        records = self.run_reporter('''
for (const intervalMs of [0, -1, 999, 60001, 'invalid', 1200.5]) {
  assert.throws(() => new Reporter({intervalMs}));
}
for (const setupProjects of ['setup', [1], {}]) {
  assert.throws(() => new Reporter({setupProjects}));
}
process.env.E2E_PROGRESS_INTERVAL_MS = '45000';
process.env.E2E_PROGRESS_SETUP_PROJECTS = '["setup"]';
const reporter = new Reporter();
begin(reporter, [test('setup')]);
reporter.onEnd({status: 'failed'});
''')
        self.assertEqual(1, records[0]['setup']['total'])
        self.assertEqual(0, records[0]['business']['total'])

    def test_real_timer_emits_heartbeat_while_test_is_running(self):
        reporter = Path(__file__).resolve().parents[1] / 'progress_playwright_reporter.mjs'
        source = '''
const {default: Reporter} = await import(process.argv[1]);
const reporter = new Reporter({intervalMs: 1000, setupProjects: []});
const item = {parent: {project: () => ({name: ''})}, expectedStatus: 'passed', retries: 0, results: []};
reporter.onBegin({workers: 1}, {allTests: () => [item]});
reporter.onTestBegin(item, {retry: 0});
await new Promise(resolve => setTimeout(resolve, 1200));
item.results.push({status: 'passed', retry: 0});
reporter.onTestEnd(item, item.results[0]);
reporter.onEnd({status: 'passed'});
'''
        result = subprocess.run(
            ['node', '--input-type=module', '-e', source, reporter.as_uri()],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        records = [json.loads(line.removeprefix('E2E_PROGRESS ')) for line in result.stdout.splitlines()]
        self.assertGreaterEqual(len(records), 3)
        self.assertEqual(1, records[1]['counts']['running'])
        self.assertEqual(0, records[-1]['counts']['remaining'])


if __name__ == '__main__':
    unittest.main()
