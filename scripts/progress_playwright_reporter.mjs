export default class ProgressReporter {
  constructor(options = {}) {
    this.intervalMs = Number(options.intervalMs ?? process.env.E2E_PROGRESS_INTERVAL_MS ?? 30000);
    if (!Number.isInteger(this.intervalMs) || this.intervalMs < 1000 || this.intervalMs > 60000) {
      throw new Error('Progress intervalMs must be an integer between 1000 and 60000');
    }
    const setup = options.setupProjects ?? (process.env.E2E_PROGRESS_SETUP_PROJECTS === undefined
      ? null : JSON.parse(process.env.E2E_PROGRESS_SETUP_PROJECTS));
    if (setup !== null && (!Array.isArray(setup) || setup.some(value => typeof value !== 'string'))) {
      throw new Error('Progress setupProjects must be an array of project names');
    }
    this.setupProjects = setup === null ? null : new Set(setup);
    this.tests = null;
    this.started = Date.now();
    this.lastEvent = this.started;
    this.globalErrors = 0;
    this.sequence = 0;
  }

  printsToStdio() { return true; }

  onBegin(config, suite) {
    this.tests = new Map(suite.allTests().map(test => [test, {
      setup: this.setupProjects?.has(test.parent.project()?.name || '') ?? false,
      status: 'pending', retry: 0, flaky: false,
    }]));
    this.workers = config.workers;
    this.shard = config.shard || null;
    this.started = this.lastEvent = Date.now();
    this.emit('testing');
    this.timer = setInterval(() => this.emit('testing'), this.intervalMs);
    this.timer.unref();
  }

  onTestBegin(test, result) {
    const item = this.tests?.get(test);
    if (!item) return;
    Object.assign(item, { status: 'running', retry: result.retry, flaky: false });
    this.lastEvent = Date.now();
  }

  outcome(test, result, final) {
    if (result.status === 'interrupted') return 'interrupted';
    if (result.status === 'skipped') return 'skipped';
    if (result.status === test.expectedStatus) {
      return result.status === 'failed' ? 'expected_failed' : 'passed';
    }
    return final || result.retry >= test.retries ? 'failed' : 'retry_pending';
  }

  onTestEnd(test, result) {
    const item = this.tests?.get(test);
    if (!item) return;
    const status = this.outcome(test, result, false);
    Object.assign(item, { status, retry: result.retry,
      flaky: result.retry > 0 && ['passed', 'expected_failed'].includes(status) });
    this.lastEvent = Date.now();
  }

  onError() {
    this.globalErrors += 1;
    this.lastEvent = Date.now();
  }

  counts(setup = null) {
    if (!this.tests) return null;
    const counts = { total: 0, ended: 0, passed: 0, failed: 0, skipped: 0,
      expected_failed: 0, running: 0, pending: 0, retry_pending: 0, interrupted: 0,
      remaining: 0, retried: 0, flaky: 0 };
    for (const item of this.tests.values()) {
      if (setup !== null && item.setup !== setup) continue;
      counts.total += 1;
      counts[item.status] += 1;
      if (item.retry > 0) counts.retried += 1;
      if (item.flaky) counts.flaky += 1;
    }
    counts.ended = counts.passed + counts.failed + counts.skipped + counts.expected_failed;
    counts.remaining = counts.total - counts.ended;
    return counts;
  }

  emit(phase, runnerStatus = null) {
    const now = Date.now();
    process.stdout.write('E2E_PROGRESS ' + JSON.stringify({
      schema_version: 1, sequence: ++this.sequence, phase, runner_status: runnerStatus,
      elapsed_seconds: Math.floor((now - this.started) / 1000),
      seconds_since_test_event: Math.floor((now - this.lastEvent) / 1000),
      workers: this.workers ?? null, shard: this.shard ?? null,
      counts: this.counts(),
      business: this.setupProjects === null ? null : this.counts(false),
      setup: this.setupProjects === null ? null : this.counts(true),
      global_errors: this.globalErrors,
    }) + '\n');
  }

  onEnd(result) {
    clearInterval(this.timer);
    for (const [test, item] of this.tests || []) {
      const last = test.results.at(-1);
      if (last) {
        const status = this.outcome(test, last, true);
        Object.assign(item, { status, retry: last.retry,
          flaky: last.retry > 0 && ['passed', 'expected_failed'].includes(status) });
      } else {
        item.status = 'pending';
      }
    }
    this.emit('runner_ended', result.status);
  }
}
