// Observation sidecar only. Native JSON/blob/HTML and workflow binding stay authoritative.
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`Missing ${name}`);
  return value;
}

function cacheFile(root, value) {
  const relative = path.relative(root, value);
  if (path.isAbsolute(relative) || !relative.startsWith(`test${path.sep}.cache${path.sep}`)
      || relative.split(path.sep).includes('..')) throw new Error('Stream must be in project test/.cache');
  for (let item = value; item !== root; item = path.dirname(item)) {
    if (fs.existsSync(item) && fs.lstatSync(item).isSymbolicLink()) throw new Error('Symlink prohibited');
  }
  return value;
}

export default class CheckpointReporter {
  constructor() {
    this.root = fs.realpathSync(required('E2E_CHECKPOINT_PROJECT'));
    this.attempt = required('E2E_CHECKPOINT_ATTEMPT');
    if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/.test(this.attempt)) throw new Error('Invalid attempt ID');
    const manifest = JSON.parse(fs.readFileSync(required('E2E_CHECKPOINT_MANIFEST'), 'utf8'));
    this.targets = new Map();
    for (const item of manifest.cases) {
      if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/.test(item.case_id)) throw new Error('Invalid case ID');
      // Target keys are constructed explicitly to avoid dependence on JSON object key order.
      const exact = this.key(item.target);
      if (this.targets.has(exact)) throw new Error('Ambiguous target');
      this.targets.set(exact, item.case_id);
    }
    this.setupProjects = new Set((process.env.E2E_CHECKPOINT_SETUP_PROJECTS || '').split(',').filter(Boolean));
    const filename = cacheFile(this.root, path.resolve(required('E2E_CHECKPOINT_STREAM')));
    fs.mkdirSync(path.dirname(filename), { recursive: true });
    this.fd = fs.openSync(filename, 'wx', 0o600);
    this.sequence = 0;
    this.unmapped = false;
  }

  key(target) { return JSON.stringify([target.file, target.project, target.title_path]); }

  target(test) {
    return { file: path.relative(this.root, test.location.file).split(path.sep).join('/'),
      project: test.parent.project()?.name || '', title_path: test.titlePath() };
  }

  onBegin(_config, suite) {
    this.discovered = new Set();
    const seen = new Set();
    for (const test of suite.allTests()) {
      if (this.setupProjects.has(test.parent.project()?.name)) continue;
      const key = this.key(this.target(test));
      if (seen.has(key) || !this.targets.has(key) || (test.repeatEachIndex || 0) !== 0) {
        this.mappingError();
      }
      seen.add(key);
      this.discovered.add(test);
    }
  }

  mappingError() {
    this.unmapped = true;
    this.write({ type: 'mapping_error', attempt_id: this.attempt });
  }

  printsToStdio() { return false; }

  onTestEnd(test, result) {
    const project = test.parent.project()?.name || '';
    if (this.setupProjects.has(project)) return;
    const target = this.target(test);
    const cid = this.targets.get(this.key(target));
    if (this.unmapped || !this.discovered?.has(test) || !cid || result.retry !== 0) {
      // Retries must have separate checkpoint claims; never let a later pass erase the failure.
      this.mappingError();
      return;
    }
    const eventId = createHash('sha256').update(`${this.attempt}:${++this.sequence}`).digest('hex');
    this.write({ type: 'case_end', event_id: `pw:${eventId}`,
      attempt_id: this.attempt, case_id: cid, status: result.status });
  }

  write(event) {
    fs.writeSync(this.fd, `${JSON.stringify(event)}\n`, null, 'utf8');
    fs.fsyncSync(this.fd);
  }

  onEnd() {
    fs.closeSync(this.fd);
    if (this.unmapped) return { status: 'failed' };
  }
}
