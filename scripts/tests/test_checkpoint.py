"""Scheduling and interruption safety; no browsers or remote writes."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checkpoint as cp
import work_packages as wp


class CheckpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for name in ['test/a.ts', 'test/b.ts', 'test/shared.ts', 'proof.json']:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text('original')
        self.journal = 'test/.cache/checkpoint.jsonl'
        self.spec = {'environment_id': 'dev-v1', 'cases': [self.case('A', 'a'), self.case('B', 'b')]}
        self.call('init', self.spec)

    def case(self, cid, file, conflicts=None):
        return {'case_id': cid, 'target': {'file': f'test/{file}.ts', 'project': 'chromium', 'title_path': ['', 'chromium', f'test/{file}.ts', cid]},
                'inputs': [f'test/{file}.ts', 'test/shared.ts'], 'dependencies': ['login'],
                'conflicts': conflicts or [], 'reviewed': True}

    def call(self, command, request=None, workers=4):
        return cp.dispatch(self.root, self.journal, command, request or {}, workers)

    def claim(self, attempt='run1', cases=None):
        return self.call('claim', {'attempt_id': attempt, 'case_ids': cases or ['A']})

    def event(self, kind, eid=None, **values):
        return self.call('record', {'type': kind, 'event_id': eid or kind, 'evidence': ['proof.json'], **values})

    def end(self, status='passed', attempt='run1', cid='A', eid='end'):
        return self.event('case_end', eid, attempt_id=attempt, case_id=cid, status=status)

    def close(self, attempt='run1', eid='close'):
        return self.event('close', eid, attempt_id=attempt, cleanup_verified=True)

    def interrupt(self, cases=None, attempt='run1', eid='interruption', **flags):
        return self.event('interruption', eid, attempt_id=attempt, unfinished_case_ids=cases or ['A'],
                          **{**dict(process_exited=True, results_reconciled=True), **flags})

    def test_pass_reused_but_never_claimed_as_formal_full(self):
        self.claim(); self.end(); result = self.close()
        self.assertEqual(result['reused_observations'], ['A'])
        self.assertEqual(result['ready'], ['B'])
        self.assertEqual(result['formal_regression_completeness'], 'not_evaluated')

    def test_restart_preserves_inflight_and_conflicts_without_automatic_replay(self):
        self.claim()
        result = cp.dispatch(self.root, self.journal, 'plan', {})
        self.assertEqual(result['blocked']['A'], 'inflight_or_cleanup_unverified')
        with self.assertRaisesRegex(ValueError, 'ready'):
            self.claim('run2')

    def test_invalid_stream_commits_no_pass_prefix(self):
        self.claim()
        stream = 'test/.cache/duplicate.jsonl'
        events = [dict(type='case_end', event_id=f'end-{i}', attempt_id='run1',
                       case_id='A', status=status) for i, status in enumerate(['passed', 'failed'])]
        (self.root / stream).write_text(''.join(json.dumps(e) + '\n' for e in events))
        before = (self.root / self.journal).read_bytes()
        with self.assertRaisesRegex(ValueError, 'reserved'):
            self.call('ingest', {'attempt_id': 'run1', 'stream': stream})
        self.assertEqual((self.root / self.journal).read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'ingest case_end'):
            self.close()
        self.interrupt()
        result = self.close()
        self.assertEqual(result['reused_observations'], [])
        self.assertIn('A', result['ready'])

    def test_cross_process_claim_is_exclusive_and_restart_keeps_reservation(self):
        program = '''import sys,json
sys.path.insert(0,sys.argv[1]); import checkpoint as c
try:
 c.dispatch(sys.argv[2],sys.argv[3],'claim',{'attempt_id':sys.argv[4],'case_ids':['A']})
except ValueError: sys.exit(2)
'''
        args = [sys.executable, '-B', '-c', program, str(Path(cp.__file__).parent), str(self.root), self.journal]
        children = [subprocess.Popen(args + [f'process-{i}'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for i in range(2)]
        codes = []
        for process in children:
            _, err = process.communicate(timeout=15)
            self.assertFalse(err, err)
            codes.append(process.returncode)
        self.assertEqual(sorted(codes), [0, 2])
        self.assertEqual(self.call('plan')['blocked']['A'], 'inflight_or_cleanup_unverified')

    def test_close_requires_cleanup_proof_even_when_case_passed(self):
        self.claim(); self.end()
        with self.assertRaisesRegex(ValueError, 'cleanup proof'):
            self.event('close', attempt_id='run1', cleanup_verified=False)

    def test_unstarted_reserved_case_becomes_pending_only_after_verified_close(self):
        self.claim()
        with self.assertRaisesRegex(ValueError, 'ingest case_end'):
            self.close()
        self.interrupt()
        self.assertEqual(self.call('plan')['blocked']['A'], 'inflight_or_cleanup_unverified')
        self.assertIn('A', self.close()['ready'])

    def test_close_before_ingest_rejected_and_results_remain_ingestable(self):
        self.claim()
        before = (self.root / self.journal).read_bytes()
        with self.assertRaisesRegex(ValueError, 'ingest case_end'):
            self.close()
        self.assertEqual((self.root / self.journal).read_bytes(), before)
        stream = 'test/.cache/late-stream.jsonl'
        (self.root / stream).write_text(json.dumps(dict(type='case_end', event_id='pw:late',
            attempt_id='run1', case_id='A', status='passed')) + '\n')
        self.call('ingest', {'attempt_id': 'run1', 'stream': stream})
        self.assertEqual(self.close()['reused_observations'], ['A'])

    def test_partial_results_require_explicit_interruption_preserving_pass(self):
        self.claim(cases=['A', 'B']); self.end()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.close()
        with self.assertRaisesRegex(ValueError, 'exactly match'):
            self.interrupt(['A', 'B'])
        self.interrupt(['B'])
        result = self.close()
        self.assertEqual(result['reused_observations'], ['A'])
        self.assertEqual(result['ready'], ['B'])

    def test_interruption_requires_process_exit_and_result_reconciliation(self):
        self.claim()
        for flags in [dict(process_exited=False), dict(results_reconciled=False)]:
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, 'exited process'):
                self.event('interruption', attempt_id='run1', unfinished_case_ids=['A'],
                           **{**dict(process_exited=True, results_reconciled=True), **flags})
        with self.assertRaisesRegex(ValueError, 'ingest case_end'):
            self.close()

    def test_only_changed_dependency_consumers_are_rescheduled(self):
        self.claim(cases=['A', 'B']); self.end(); self.end(cid='B', eid='endB'); self.close()
        (self.root / 'test/a.ts').write_text('updated')
        result = self.call('plan')
        self.assertEqual(result['ready'], ['A'])
        self.assertEqual(result['reused_observations'], ['B'])

    def test_shared_input_invalidates_all_consumers(self):
        self.claim(cases=['A', 'B']); self.end(); self.end(cid='B', eid='endB'); self.close()
        (self.root / 'test/shared.ts').write_text('updated')
        self.assertEqual(self.call('plan')['ready'], ['A', 'B'])

    def test_missing_input_stops_scheduling(self):
        (self.root / 'test/shared.ts').unlink()
        with self.assertRaisesRegex(ValueError, 'input missing'):
            self.call('plan')

    def test_unrelated_file_does_not_invalidate_observation(self):
        self.claim(); self.end(); self.close()
        (self.root / 'proof.json').write_text('unrelated')
        self.assertEqual(self.call('plan')['reused_observations'], ['A'])

    def test_environment_or_manifest_change_requires_new_checkpoint(self):
        changed = copy.deepcopy(self.spec); changed['environment_id'] = 'dev-v2'
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.call('init', changed)

    def test_unreviewed_case_fails_before_claim(self):
        changed = copy.deepcopy(self.spec); changed['cases'][0]['reviewed'] = False
        with self.assertRaisesRegex(ValueError, 'reviewed'):
            cp.dispatch(self.root, 'test/.cache/other.jsonl', 'init', changed)

    def test_duplicate_target_rejected(self):
        changed = copy.deepcopy(self.spec); changed['cases'][1]['target'] = changed['cases'][0]['target']
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            cp.dispatch(self.root, 'test/.cache/other.jsonl', 'init', changed)

    def test_dependency_circuit_blocks_only_consumers(self):
        result = self.event('block', dependency='login')
        self.assertEqual(result['ready'], [])
        self.assertEqual(set(result['blocked'].values()), {'dependency_circuit_open'})
        result = self.event('unblock', dependency='login')
        self.assertEqual(result['ready'], ['A', 'B'])

    def test_circuit_survives_process_restart(self):
        self.event('block', dependency='login')
        self.assertEqual(cp.dispatch(self.root, self.journal, 'plan', {})['ready'], [])

    def test_conflict_group_serializes_only_conflicting_cases(self):
        spec = copy.deepcopy(self.spec)
        for case in spec['cases']: case['conflicts'] = ['tenant-config']
        journal = 'test/.cache/conflicts.jsonl'
        result = cp.dispatch(self.root, journal, 'init', spec)
        self.assertEqual(result['ready'], ['A'])
        self.assertEqual(result['blocked']['B'], 'conflict_group_busy')

    def test_capacity_is_enforced_atomically(self):
        result = self.call('plan', workers=1)
        self.assertEqual(result['ready'], ['A'])
        with self.assertRaisesRegex(ValueError, 'ready'):
            self.call('claim', {'attempt_id': 'too-wide', 'case_ids': ['A', 'B']}, workers=1)

    def test_event_replay_is_idempotent_and_conflicting_id_rejected(self):
        self.claim(); self.end(); before = (self.root / self.journal).read_bytes(); self.end()
        self.assertEqual(before, (self.root / self.journal).read_bytes())
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.end('failed')

    def test_partial_tail_never_truncated(self):
        p = self.root / self.journal; p.write_bytes(p.read_bytes() + b'{"partial"')
        before = p.read_bytes()
        with self.assertRaisesRegex(ValueError, 'incomplete journal tail'):
            self.call('plan')
        self.assertEqual(before, p.read_bytes())

    def test_tampering_is_detected(self):
        p = self.root / self.journal; p.write_text(p.read_text().replace('dev-v1', 'dev-v2'))
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.call('plan')

    def test_failure_needs_explicit_confirmation_and_only_one_is_allowed(self):
        self.claim(); self.end('failed'); self.close()
        self.assertNotIn('A', self.call('plan')['ready'])
        self.event('confirm', case_id='A', root_cause_id='network')
        self.claim('run2'); self.end('failed', 'run2', eid='end2'); self.close('run2', 'close2')
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            self.event('confirm', 'confirm2', case_id='A', root_cause_id='network')

    def test_repair_budget_persists_across_attempts_and_roots(self):
        for index in range(3):
            attempt = f'run{index}'
            self.claim(attempt); self.end('failed', attempt, eid=f'end{index}'); self.close(attempt, f'close{index}')
            if index < 2:
                self.event('repair', f'repair{index}', case_id='A', root_cause_id=f'cause{index}')
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            self.event('repair', 'third', case_id='A', root_cause_id='new-root')

    def test_unknown_fields_cannot_persist_secrets(self):
        with self.assertRaisesRegex(ValueError, 'fields'):
            self.event('block', dependency='login', token='should-not-be-saved')
        self.assertNotIn('should-not-be-saved', (self.root / self.journal).read_text())

    def test_paths_cannot_escape_cache_or_follow_alias(self):
        with self.assertRaises(ValueError): cp.dispatch(self.root, '../escape', 'plan', {})
        with self.assertRaises(ValueError): cp.dispatch(self.root, 'test/state.jsonl', 'plan', {})

    def test_reporter_stream_is_imported_once_without_native_acceptance(self):
        self.claim()
        stream = 'test/.cache/stream.jsonl'
        (self.root / stream).write_text(json.dumps({'type': 'case_end', 'event_id': 'pw:1',
            'attempt_id': 'run1', 'case_id': 'A', 'status': 'passed'}) + '\n')
        req = {'attempt_id': 'run1', 'stream': stream}
        self.call('ingest', req); before = (self.root / self.journal).read_bytes(); self.call('ingest', req)
        self.assertEqual(before, (self.root / self.journal).read_bytes())

    def test_unmapped_reporter_stream_blocks_import(self):
        self.claim(); stream = 'test/.cache/stream.jsonl'
        (self.root / stream).write_text('{"type":"mapping_error","attempt_id":"run1"}\n')
        with self.assertRaisesRegex(ValueError, 'unmapped'):
            self.call('ingest', {'attempt_id': 'run1', 'stream': stream})


class WorkPackagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); (self.root / 'proof.json').write_text('{}')

    def item(self, cid='A', category='script', **extra):
        return dict(case_id=cid, category=category, evidence=['proof.json'], **extra)

    def build(self, *items): return wp.build(self.root, {'items': list(items)})

    def test_explicit_shared_root_combines_cases(self):
        result = self.build(self.item(root_cause_id='same'), self.item('B', root_cause_id='same'))
        self.assertEqual(result['package_count'], 1)
        self.assertEqual(result['packages'][0]['case_ids'], ['A', 'B'])

    def test_component_group_does_not_claim_shared_root(self):
        result = self.build(self.item(component='picker'), self.item('B', component='picker'))
        self.assertEqual(result['packages'][0]['grouping_basis'], 'component_triage_only')

    def test_exhausted_budget_is_not_mixed_with_actionable_repairs(self):
        result = self.build(self.item(root_cause_id='same'), self.item('B', root_cause_id='same', repair_attempts=2))
        self.assertEqual(result['package_count'], 2)
        self.assertIn('repair_budget_exhausted', [p['action'] for p in result['packages']])

    def test_permission_and_unknown_resources_prevent_retry(self):
        self.assertEqual(self.build(self.item(permission_required=True))['packages'][0]['action'], 'awaiting_authorization')
        self.assertEqual(self.build(self.item(cleanup_unknown=True))['packages'][0]['action'], 'cleanup_review_no_blind_retry')

    def test_missing_budget_is_not_treated_as_zero(self):
        self.assertEqual(self.build(self.item())['packages'][0]['action'], 'review_repair_budget')
        self.assertEqual(self.build(self.item(category='environment'))['packages'][0]['action'], 'review_confirmation_budget')

    def test_repeated_environment_failure_waits_for_change(self):
        result = self.build(self.item(category='environment', confirmation_attempts=1))
        self.assertEqual(result['packages'][0]['action'], 'awaiting_environment_change')

    def test_api_deferred_and_unbound_are_separate_obligations(self):
        result = self.build(*(self.item(str(i), category=c) for i, c in enumerate(['api_exclusive', 'deferred', 'unbound_observation'])))
        self.assertEqual(result['package_count'], 3)
        self.assertFalse(result['execution_or_acceptance_changed'])

    def test_duplicate_items_and_untyped_flags_fail(self):
        with self.assertRaises(ValueError): self.build(self.item(), self.item())
        with self.assertRaises(ValueError): self.build(self.item(permission_required='false'))

    def test_group_retains_compound_gates_and_exhausted_budget(self):
        result = self.build(self.item(permission_required=True, cleanup_unknown=True, repair_attempts=2))
        package = result['packages'][0]
        self.assertEqual(package['action'], 'awaiting_authorization')
        constraints = package['constraints_by_case'][package['case_ids'][0]]
        self.assertTrue(constraints['cleanup_unknown'])
        self.assertEqual(constraints['repair_attempts'], 2)

    def test_missing_evidence_or_credential_fields_fail(self):
        with self.assertRaises(ValueError): self.build(self.item(token='not-allowed'))
        (self.root / 'proof.json').unlink()
        with self.assertRaises(ValueError): self.build(self.item())

    def test_shared_evidence_checked_once_per_build(self):
        with mock.patch.object(wp, 'safe', wraps=wp.safe) as paths:
            result = self.build(*(self.item(str(i), component='shared') for i in range(100)))
        self.assertEqual(result['item_count'], 100)
        paths.assert_called_once_with(self.root, 'proof.json')

    def test_late_missing_evidence_still_rejects_whole_worklist(self):
        missing = self.item('B')
        missing['evidence'] = ['missing.json']
        with self.assertRaisesRegex(ValueError, 'evidence missing'):
            self.build(self.item(), missing)


@unittest.skipUnless(shutil.which('node'), 'Node is optional for the Python skill tools')
class ReporterTest(unittest.TestCase):
    def test_full_describe_path_and_repeat_each_are_rejected_before_reuse(self):
        reporter = Path(cp.__file__).with_name('checkpoint_playwright_reporter.mjs')
        code = r'''
import fs from 'node:fs'; import os from 'node:os'; import path from 'node:path';
const {default: Reporter} = await import(process.argv[1]);
const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'checkpoint-identity-')));
try {
 const manifest = path.join(root, 'manifest.json');
 const wanted=['','browser','test/a.ts','intended','A'];
 fs.writeFileSync(manifest,JSON.stringify({cases:[{case_id:'A',target:{file:'test/a.ts',project:'browser',title_path:wanted}}]}));
 Object.assign(process.env,{E2E_CHECKPOINT_PROJECT:root,E2E_CHECKPOINT_ATTEMPT:'attempt',E2E_CHECKPOINT_MANIFEST:manifest});
 const t={titlePath:()=>wanted,location:{file:path.join(root,'test/a.ts')},parent:{project:()=>({name:'browser'})}};
 for (const [name,tests] of [['wrong',[{...t,titlePath:()=>['','browser','test/a.ts','other','A']}]],
                            ['repeat',[t,{...t,repeatEachIndex:1}]]]) {
  process.env.E2E_CHECKPOINT_STREAM=path.join(root,`test/.cache/${name}.jsonl`);
  const r=new Reporter(); r.onBegin({}, {allTests:()=>tests});
  for (const test of tests) r.onTestEnd(test,{status:'passed',retry:0});
  if (r.onEnd()?.status!=='failed') throw new Error('ambiguous identity accepted');
  if (fs.readFileSync(process.env.E2E_CHECKPOINT_STREAM,'utf8').includes('case_end')) throw new Error('false pass written');
 }
} finally {fs.rmSync(root,{recursive:true,force:true});}
'''
        result = subprocess.run(['node', '--input-type=module', '-e', code, reporter.as_uri()], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fsync_sidecar_survives_without_on_end_and_contains_no_error_credentials(self):
        reporter = Path(cp.__file__).with_name('checkpoint_playwright_reporter.mjs')
        code = r'''
import fs from 'node:fs'; import os from 'node:os'; import path from 'node:path';
const {default: Reporter} = await import(process.argv[1]);
const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'checkpoint-reporter-')));
try {
 const manifest = path.join(root, 'manifest.json');
 fs.writeFileSync(manifest, JSON.stringify({cases:[{case_id:'A',target:{file:'test/a.ts',project:'browser',title_path:['','browser','test/a.ts','intended','A']}}]}));
 Object.assign(process.env,{E2E_CHECKPOINT_PROJECT:root,E2E_CHECKPOINT_ATTEMPT:'attempt',
 E2E_CHECKPOINT_MANIFEST:manifest,E2E_CHECKPOINT_STREAM:path.join(root,'test/.cache/stream.jsonl')});
 const r = new Reporter();
 const t = {title:'A',titlePath:()=>['','browser','test/a.ts','intended','A'],location:{file:path.join(root,'test/a.ts')},parent:{project:()=>({name:'browser'})}};
 r.onBegin({}, {allTests:()=>[t]});
 r.onTestEnd(t,{status:'failed',retry:0,error:{message:'password=must-not-persist'}});
 const text = fs.readFileSync(process.env.E2E_CHECKPOINT_STREAM,'utf8');
 if (!text.endsWith('\n') || text.includes('must-not-persist') || JSON.parse(text).status !== 'failed') throw new Error('unsafe sidecar');
 r.onTestEnd({...t,title:'unmapped-secret'},{status:'passed',retry:0});
 if (r.onEnd().status !== 'failed') throw new Error('unmapped target accepted');
 let rejected = false; try { new Reporter(); } catch { rejected = true; }
 if (!rejected) throw new Error('existing stream overwritten');
} finally {fs.rmSync(root,{recursive:true,force:true});}
'''
        result = subprocess.run(['node', '--input-type=module', '-e', code, reporter.as_uri()], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
