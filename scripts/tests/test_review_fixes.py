"""Regressions for discovery, command selection, atomic publication and host formats."""
import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from unittest import TestCase, mock
import zipfile

import test_tools as base

wf = base.wf


class ReviewFixesTest(TestCase):
    setUp = base.ToolsTest.setUp
    tearDown = base.ToolsTest.tearDown
    write = base.ToolsTest.write
    cli = base.ToolsTest.cli
    start = base.ToolsTest.start
    record = base.ToolsTest.record
    check_sources = base.ToolsTest.check_sources
    state = base.ToolsTest.state
    inventory = base.ToolsTest.inventory
    ignored = base.ToolsTest.ignored
    init_attempt = base.ToolsTest.init_attempt
    html = base.ToolsTest.html
    snapshot = base.ToolsTest.snapshot

    def preflight(self, raw=None):
        self.write('discovery.json', raw or self.inventory())
        self.write('preflight.json', {'discovery': {'path': 'discovery.json', 'complete': True,
                                                    'setup_projects': ['auth']}})
        with self.ignored():
            return self.cli('preflight', '--run', self.run, '--owner', self.owner,
                            '--input', str(self.project / 'preflight.json'))

    def finish(self, bid='E1', fail=False):
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            return self.cli('attempt-bundle', '--run', self.run, '--owner', self.owner,
                            '--bundle', bid, fail=fail)

    def blob(self, bid='E1', name='report.zip', shard=None, valid=True):
        stream = io.BytesIO()
        metadata = {'version': 2, 'userAgent': 'Playwright/1.64.1 synthetic'}
        if shard:
            metadata['shard'] = shard
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('report.jsonl' if valid else 'unrelated.txt',
                             json.dumps({'method': 'onBlobReportMetadata', 'params': metadata})+'\n')
        self.write(f'test/.cache/runs/{self.run}/attempts/{bid}/blob/{name}', '')
        (self.project / f'test/.cache/runs/{self.run}/attempts/{bid}/blob/{name}').write_bytes(stream.getvalue())

    def evidence(self, bid='E1', targets=('C1',), shard=None, appended=None):
        prefix = f'test/.cache/runs/{self.run}/attempts/{bid}'
        plan = wf.load(self.project / prefix / 'plan.json')
        start = datetime.now(timezone.utc)
        self.later = start+timedelta(seconds=5)
        raw = {'config': {'rootDir': str(self.project / 'test/src'), 'shard': shard},
               'stats': {'startTime': start.isoformat(), 'duration': 2000,
                         'expected': len(targets), 'unexpected': 0, 'skipped': 0, 'flaky': 0},
               'errors': [], 'suites': [{'specs': [
                   {'title': 'Synthetic '+cid, 'file': cid+'.spec.ts',
                    'tests': [{'projectName': 'chromium', 'status': 'expected', 'results': [
                        {'startTime': start.isoformat(), 'retry': 0, 'duration': 1000,
                         'status': 'passed', 'attachments': []}]}]} for cid in targets]}]}
        self.write(prefix+'/results/result.json', raw)
        self.html({'html_report': prefix+'/report/playwright/index.html'}, raw)
        self.blob(bid=bid, shard=shard)
        receipt = {key: plan[key] for key in ('cwd', 'environment', 'scope', 'setup_projects')}
        receipt.update(command=plan['command'] + (appended if appended is not None else
                                                  ['--reporter=list,json,html,blob']),
                       started_at=start.isoformat(), ended_at=(start+timedelta(seconds=3)).isoformat(), exit_code=0)
        self.write(prefix+'/command.json', receipt)
        return raw

    def collect(self, result):
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            return self.cli('collect-results', '--run', self.run, '--owner', self.owner,
                            '--input', str(self.project / result['input']))

    def completeness(self):
        return wf.report_artifacts.regression_completeness(self.state().get('report_bundles', {}), self.state()['scopes'], project=self.project)

    def test_legacy_shared_target_has_its_own_bucket_and_no_false_missing(self):
        cov = wf.load(self.project / 'test/plans/coverage.json')
        cov['cases']['C1']['legacy_group'] = True
        cov['cases']['L1'] = copy.deepcopy(cov['cases']['C1'])
        cov['requirements']['R1']['case_ids'].append('L1')
        self.write('test/plans/coverage.json', cov)
        self.start()
        result = self.preflight()['coverage_diff']
        self.assertEqual([], result['unmapped_tests'])
        self.assertEqual([], result['missing_tests'])
        self.assertEqual(['C1','L1'], result['legacy_shared_tests'][0]['case_ids'])
        self.assertIn('legacy_group_requires_independent_test', result['code_review_gaps']['C1'])
        self.assertEqual(['C2'], result['matched_cases'])

    def test_preflight_does_not_reload_assets_in_discovery_diff(self):
        self.start()
        with mock.patch.object(wf, 'assets', wraps=wf.assets) as reader:
            self.preflight()
        self.assertEqual(1, reader.call_count)

    def test_unplanned_filters_spec_paths_and_option_overrides_are_rejected(self):
        self.start()
        self.init_attempt()
        for appended in (['--grep', 'C1'], ['--project=chromium'], ['C1.spec.ts'], ['--no-deps'],
                         ['--reporter=list,json,html,blob', '--grep=C1'],
                         ['--output', '/some-other-output'], ['--shard=1/2']):
            with self.subTest(appended=appended):
                self.evidence(appended=appended)
                self.assertIn('argument', self.finish(fail=True)['error'])

    def test_only_exact_reporter_and_output_arguments_may_be_appended(self):
        self.start()
        result = self.init_attempt()
        self.evidence(appended=result['reporter_arguments'])
        self.assertTrue(self.finish()['valid'])
        self.evidence(appended=[])
        # Project configuration may already supply reporters; real artifacts are still required.
        (self.project / f'test/.cache/runs/{self.run}/attempts/E1/bundle-event.json').unlink()
        self.assertTrue(self.finish()['valid'])

    def test_filters_explicit_in_the_plan_remain_affected_validation(self):
        self.start()
        self.check_sources()
        self.init_attempt(command=['pnpm', 'exec', 'playwright', 'test', '--grep=C1'])
        self.evidence()
        self.collect(self.finish())
        self.assertEqual('incomplete', self.completeness()['status'])
        self.assertEqual('affected', self.state()['report_bundles']['E1']['regression_check']['mode'])

    def test_full_plan_requires_current_registered_discovery(self):
        self.start()
        self.write('full.json', {'command': ['pnpm', 'exec', 'playwright', 'test'],
                                 'cwd': '.', 'environment': 'synthetic', 'scope': ['a','b'],
                                 'setup_projects': ['auth'], 'regression_mode': 'full'})
        result = self.cli('attempt-init', '--run', self.run, '--owner', self.owner, '--bundle', 'E1',
                          '--input', str(self.project / 'full.json'), fail=True)
        self.assertIn('discovery', result['error'])
        self.preflight()
        self.write('discovery.json', {})
        result = self.cli('attempt-init', '--run', self.run, '--owner', self.owner, '--bundle', 'E1',
                          '--input', str(self.project / 'full.json'), fail=True)
        self.assertIn('changed', result['error'])

    def test_full_target_difference_keeps_results_but_blocks_complete_acceptance(self):
        self.start()
        self.check_sources()
        self.preflight()
        self.init_attempt(regression_mode='full', scope=['a','b'])
        self.evidence(targets=('C1',))
        self.assertTrue(self.collect(self.finish())['collection']['complete'])
        completeness = self.completeness()
        self.assertEqual('incomplete', completeness['status'])
        self.assertEqual('Synthetic C2', completeness['groups'][0]['missing'][0]['title'])
        with mock.patch.object(wf, 'report_consistency', return_value={'status': 'checked'}):
            result = self.record({'type':'acceptance','owner':self.owner,'status':'fully_accepted'}, fail=True)
        self.assertIn('final-regression', result['error'])

    def test_declaring_filtered_discovery_complete_does_not_hide_reviewed_test_gap(self):
        self.start()
        raw=self.inventory()
        raw['suites'][0]['specs'].pop()
        result=self.preflight(raw)
        self.assertEqual(['C2'], result['coverage_diff']['reviewed_missing_tests'])
        self.write('full.json', {'command':['pnpm','exec','playwright','test'], 'cwd':'.',
                                 'environment':'synthetic','scope':['a','b'],
                                 'setup_projects':['auth'],'regression_mode':'full'})
        failure=self.cli('attempt-init','--run',self.run,'--owner',self.owner,'--bundle','E1',
                         '--input',str(self.project/'full.json'),fail=True)
        self.assertIn('omits reviewed', failure['error'])

    def test_full_target_match_and_extra_target_detection(self):
        self.start()
        self.check_sources()
        self.preflight()
        self.init_attempt(regression_mode='full', scope=['a','b'])
        self.evidence(targets=('C1','C2'))
        self.collect(self.finish())
        self.assertEqual('complete', self.completeness()['status'])
        bundles = copy.deepcopy(self.state()['report_bundles'])
        bundles['E1']['regression_check']['actual']['business'].append(
            {'file':'test/src/C1.spec.ts','title':'Synthetic extra','project':'chromium'})
        result = wf.report_artifacts.regression_completeness(bundles, ['a','b'], project=self.project)
        self.assertEqual('incomplete', result['status'])
        self.assertTrue(result['groups'][0]['unexpected'])
        bundles=copy.deepcopy(self.state()['report_bundles'])
        evidence=bundles['E1']['regression_check']
        evidence['actual']['business'].append(evidence['actual']['business'][0])
        self.assertEqual('incomplete',wf.report_artifacts.regression_completeness(bundles, project=self.project)['status'])
        evidence['actual']['business'].pop()
        evidence['expected']['setup']=[{'file':'test/src/auth.setup.ts','title':'Auth','project':'auth'}]
        incomplete=wf.report_artifacts.regression_completeness(bundles, project=self.project)
        self.assertTrue(incomplete['groups'][0]['setup_differences'])
        evidence['actual']['setup']=copy.deepcopy(evidence['expected']['setup'])
        self.assertEqual('complete',wf.report_artifacts.regression_completeness(bundles, project=self.project)['status'])

    def test_omitting_plan_binding_cannot_bypass_direct_collect(self):
        self.start()
        self.check_sources()
        self.init_attempt()
        self.evidence()
        result = self.finish()
        event = wf.load(self.project / result['input'])
        event['bundle'].pop('attempt_plan')
        event['bundle'].pop('attempt_plan_sha256')
        self.write('bypass.json', event)
        with mock.patch.object(wf, 'now', return_value=self.later.isoformat()):
            failure = self.cli('collect-results', '--run', self.run, '--owner', self.owner,
                               '--input', str(self.project / 'bypass.json'), fail=True)
        self.assertIn('plan binding', failure['error'])

    def test_atomic_plan_write_failure_can_retry_same_bundle(self):
        self.start()
        actual_atomic = wf.atomic
        def broken(path, *args, **kwargs):
            if path.name == 'plan.json':
                raise OSError('synthetic plan write failure')
            return actual_atomic(path, *args, **kwargs)
        self.write('attempt.json', {'command':['pnpm','exec','playwright','test'], 'cwd':'.',
                                    'environment':'synthetic','scope':['a'],'setup_projects':['auth']})
        with mock.patch.object(wf, 'atomic', side_effect=broken):
            self.cli('attempt-init', '--run', self.run, '--owner', self.owner, '--bundle', 'E1',
                     '--input', str(self.project / 'attempt.json'), fail=True)
        parent = self.project / f'test/.cache/runs/{self.run}/attempts'
        self.assertEqual([], list(parent.iterdir()))
        self.assertTrue(self.init_attempt()['created'])

    def test_atomic_rename_failure_cleans_only_owned_staging_directory(self):
        self.start()
        self.write('attempt.json', {'command':['pnpm','exec','playwright','test'], 'cwd':'.',
                                    'environment':'synthetic','scope':['a'],'setup_projects':['auth']})
        with mock.patch.object(wf.os, 'rename', side_effect=OSError('synthetic publication failure')):
            self.cli('attempt-init', '--run', self.run, '--owner', self.owner, '--bundle', 'E1',
                     '--input', str(self.project / 'attempt.json'), fail=True)
        parent = self.project / f'test/.cache/runs/{self.run}/attempts'
        self.assertEqual([], list(parent.iterdir()))
        self.assertTrue(self.init_attempt()['created'])

    def test_invalid_zip_and_extra_recognized_blob_are_rejected(self):
        self.start()
        self.init_attempt()
        self.evidence()
        self.blob(valid=False)
        self.assertIn('blob', self.finish(fail=True)['error'])
        self.blob(valid=True)
        self.blob(name='copied-report.zip')
        self.assertIn('exactly one', self.finish(fail=True)['error'])

    def test_wrong_blob_shard_is_rejected(self):
        self.start()
        self.init_attempt(command=['pnpm','exec','playwright','test','--shard=1/2'])
        self.evidence(shard={'current':1,'total':2})
        self.blob(shard={'current':2,'total':2})
        self.assertIn('blob shard', self.finish(fail=True)['error'])

    def test_full_shards_require_all_indices_and_matching_combined_targets(self):
        self.start()
        self.check_sources()
        self.preflight()
        for current, cid in [(1,'C1'),(2,'C2')]:
            bid='S'+str(current)
            self.init_attempt(bundle=bid, scope=['a','b'], regression_mode='full',
                              command=['pnpm','exec','playwright','test',f'--shard={current}/2'])
            self.evidence(bid=bid, targets=(cid,), shard={'current':current,'total':2})
            self.collect(self.finish(bid))
            status=self.completeness()
            if current==1:
                self.assertEqual('incomplete', status['status'])
                self.assertEqual([2], status['groups'][0]['missing_shards'])
            else:
                self.assertEqual('complete', status['status'])

    def test_unsupported_host_is_null_but_recognized_empty_codex_is_zero(self):
        self.start()
        a=datetime.now(timezone.utc)-timedelta(seconds=3)
        b=a+timedelta(seconds=1)
        request={'session':{'started_at':a.isoformat(),'ended_at':b.isoformat(),
                            'evidence':['host.jsonl'],'log':'host.jsonl'}}
        self.write('timing.json',request)
        for supported in (False,True):
            payload={'type':'message'} if supported else {'type':'tool_use'}
            rows=[{'timestamp':stamp.isoformat(),'type':'response_item' if supported else 'assistant',
                   'payload':payload} for stamp in (a,b)]
            self.write('host.jsonl','\n'.join(json.dumps(row) for row in rows))
            result=self.cli('timing','--run',self.run,'--input',str(self.project/'timing.json'))['session']
            if supported:
                self.assertEqual(0,result['tool_calls'])
                self.assertEqual('supported',result['host_log_status'])
            else:
                self.assertIsNone(result['tool_calls'])
                self.assertIsNone(result['tool_counts'])
                self.assertIsNone(result['nonzero_tool_results'])
                self.assertEqual('unsupported_host_log',result['host_log_status'])
