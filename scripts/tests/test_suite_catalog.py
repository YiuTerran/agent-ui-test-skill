"""Business catalogs and suite isolation, with no product or browser access."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_workflow as base
import test_scheduling as scheduler_tests
import test_checkpoint as checkpoint_tests
import test_residual_gate as residual_tests
import test_identity_pool as pool_tests
import test_retention as retention_tests
import test_platform_support as platform_tests
import test_source_cache as source_tests

wf = base.wf
catalog = wf.suite_catalog
sc = scheduler_tests.sc
cp = checkpoint_tests.cp


class CatalogTest(unittest.TestCase):
    write = base.WorkflowTest.write
    cli = base.WorkflowTest.cli
    start = base.WorkflowTest.start
    state = base.WorkflowTest.state
    record = base.WorkflowTest.record
    check_sources = base.WorkflowTest.check_sources
    tearDown = base.WorkflowTest.tearDown

    def setUp(self):
        base.WorkflowTest.setUp(self)
        self.project = self.project.resolve()
        self.legacy = wf.load(self.project / 'test/plans/coverage.json')

    def migrate(self):
        preview = self.cli('migrate', '--suite-id', 'frontend')
        self.write('preview.json', preview)
        self.cli('migrate', '--suite-id', 'frontend', '--apply', '--input', str(self.project / 'preview.json'))
        return preview

    def backend(self, kinds=('ui', 'api')):
        self.migrate()
        business = wf.load(self.project / catalog.CATALOG)
        for case in business['cases'].values():
            case['required_kinds'] = list(kinds)
        self.write(catalog.CATALOG, business)
        backend = self.project / 'backend'
        backend.mkdir()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = wf.main(['init', '--project', str(backend), '--kind', 'api', '--suite-id', 'backend',
                            '--catalog-root', '..'])
        self.assertEqual(code, 0, buffer.getvalue())
        spec = wf.parse_spec(backend / 'test/spec.md')
        spec.update({key: wf.parse_spec(self.project / 'test/spec.md')[key]
                     for key in ('env', 'authref', 'data-policy', 'commands')})
        wf.atomic(backend / 'test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```\n')
        return backend

    def add_api_implementations(self, backend):
        local = wf.load(backend / catalog.COVERAGE)
        for case_id, case in self.legacy['cases'].items():
            path = backend / case['spec']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('API fixture', encoding='utf-8')
            local['implementations']['backend-' + case_id] = {
                key: value for key, value in case.items() if key in catalog.IMPLEMENTATION_FIELDS}
            local['implementations']['backend-' + case_id].update(
                case_id=case_id, suite_id='backend', kind='api')
        wf.atomic(backend / catalog.COVERAGE, local)

    def test_migration_preview_is_readonly_and_preserves_case_ids(self):
        before = {path: path.read_bytes() for path in self.project.rglob('*') if path.is_file()}
        preview = self.cli('migrate', '--suite-id', 'frontend')
        self.assertEqual(set(preview['catalog']['cases']), {'C1', 'C2'})
        self.assertEqual(preview['catalog']['cases']['C1']['required_kinds'], ['ui'])
        self.assertEqual(preview['coverage']['implementations']['frontend-C1']['case_id'], 'C1')
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(before, {path: path.read_bytes() for path in self.project.rglob('*') if path.is_file()})
        self.assertFalse((self.project / catalog.CATALOG).exists())

    def test_typed_init_reports_created_and_preserved_catalog_assets(self):
        project = self.project / 'new-api'
        project.mkdir()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = wf.main(['init', '--project', str(project), '--kind', 'api', '--suite-id', 'api'])
        self.assertEqual(code, 0, stream.getvalue())
        result = json.loads(stream.getvalue())
        self.assertIn(catalog.CATALOG, result['created'])
        self.assertEqual(result['catalog_asset'], {'root': str(project), 'path': catalog.CATALOG, 'created': True})
        before = (project / catalog.CATALOG).read_bytes()
        other = self.project / 'other-api'
        other.mkdir()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = wf.main(['init', '--project', str(other), '--kind', 'api', '--suite-id', 'other',
                            '--catalog-root', '../new-api'])
        self.assertEqual(code, 0, stream.getvalue())
        result = json.loads(stream.getvalue())
        self.assertNotIn(catalog.CATALOG, result['created'])
        self.assertEqual(result['catalog_asset'], {'root': str(project), 'path': catalog.CATALOG, 'created': False})
        self.assertEqual((project / catalog.CATALOG).read_bytes(), before)

    def test_migration_keeps_existing_ui_coverage_and_test_identity(self):
        self.migrate()
        spec, view = wf.assets(self.project)
        self.assertEqual(wf.asset_errors(self.project, spec, view), [])
        self.assertTrue(wf.covered(self.project, view, 'C1'))
        self.assertEqual(view['cases']['C1']['test_id'], 'C1')
        self.assertEqual(view['cases']['C1']['implementation_id'], 'frontend-C1')
        self.assertEqual(wf.load(self.project / catalog.COVERAGE)['schema_version'], 2)

    def test_migration_requires_exact_reviewed_preview(self):
        preview = self.cli('migrate', '--suite-id', 'frontend')
        preview['catalog']['cases']['C1']['review'] = 'approved-by-other-agent'
        self.write('preview.json', preview)
        self.cli('migrate', '--suite-id', 'frontend', '--apply',
                 '--input', str(self.project / 'preview.json'), fail=True)
        self.assertEqual(wf.load(self.project / catalog.COVERAGE), self.legacy)

    def test_migration_refuses_unfinished_run(self):
        self.start()
        self.cli('migrate', fail=True)
        self.assertFalse((self.project / catalog.CATALOG).exists())

    def test_migration_interrupted_before_spec_commit_recovers_all_assets(self):
        preview = self.cli('migrate', '--suite-id', 'frontend')
        self.write('preview.json', preview)
        original_atomic = wf.atomic

        def interrupted_write(path, value, **options):
            if path == self.project / 'test/spec.md':
                raise OSError('simulated configuration write interruption')
            original_atomic(path, value, **options)

        with patch.object(wf, 'atomic', side_effect=interrupted_write):
            self.cli('migrate', '--suite-id', 'frontend', '--apply',
                     '--input', str(self.project / 'preview.json'), fail=True)
        self.assertNotIn('suite', wf.parse_spec(self.project / 'test/spec.md'))
        self.assertEqual(wf.load(self.project / catalog.COVERAGE)['schema_version'], 2)
        self.cli('catalog-recover')
        spec, view = wf.assets(self.project)
        self.assertEqual(spec, preview['spec'])
        self.assertEqual(view.local, preview['coverage'])
        self.assertEqual(view.catalog, preview['catalog'])
        self.assertEqual(wf.asset_errors(self.project, spec, view), [])

    def test_migration_cannot_reinterpret_ui_as_api(self):
        self.cli('migrate', '--kind', 'api', fail=True)
        self.assertEqual(wf.load(self.project / catalog.COVERAGE), self.legacy)

    def test_api_reuses_business_cases_but_requires_own_implementation(self):
        backend = self.backend()
        spec, view = wf.assets(backend)
        self.assertEqual(set(view['cases']), {'C1', 'C2'})
        self.assertIn('implementation_not_valid', wf.coverage_reasons(backend, view, 'C1'))
        self.assertEqual(wf.asset_errors(backend, spec, view), [])
        self.add_api_implementations(backend)
        spec, view = wf.assets(backend)
        self.assertTrue(wf.covered(backend, view, 'C1'))
        self.assertEqual(view['cases']['C1']['kind'], 'api')
        self.assertTrue(catalog.asset_path(backend, view, view['cases']['C1']['plan']).is_file())

    def test_ui_only_business_case_is_not_implicitly_required_for_api(self):
        backend = self.backend(kinds=('ui',))
        spec, view = wf.assets(backend)
        self.assertEqual(view['cases'], {})
        self.assertTrue(all(value.get('kind_not_required') for value in view['requirements'].values()))
        self.assertEqual(wf.asset_errors(backend, spec, view), [])

    def test_api_result_does_not_claim_ui_passed(self):
        backend = self.backend()
        self.add_api_implementations(backend)
        _, view = wf.assets(backend)
        result = catalog.report_view(view, {'C1': 'pass', 'C2': 'fail'})
        self.assertEqual(result['results_by_kind'], {'api': {'C1': 'pass', 'C2': 'fail'}})
        self.assertEqual(result['business_cases']['C1']['other_kinds'], {'ui': 'not_evaluated'})
        self.assertEqual(result['implementations']['backend-C1']['case_id'], 'C1')

    def test_missing_api_implementation_does_not_count_ui_code_as_local(self):
        backend = self.backend()
        _, view = wf.assets(backend)
        result = catalog.report_view(view, {'C1': 'notrun', 'C2': 'notrun'})
        self.assertEqual(result['business_case_count'], 2)
        self.assertEqual(result['implementation_count'], 0)
        self.assertEqual(result['implementations'], {})
        self.assertEqual(result['business_cases']['C1']['other_kinds'], {'ui': 'not_evaluated'})

    def test_implementation_report_uses_mapping_instead_of_identifier_shape(self):
        backend = self.backend()
        self.add_api_implementations(backend)
        local = wf.load(backend / catalog.COVERAGE)
        local['implementations']['backend-C1'] = local['implementations'].pop('backend-C2')
        wf.atomic(backend / catalog.COVERAGE, local)
        _, view = wf.assets(backend)
        result = catalog.report_view(view, {'C1': 'notrun', 'C2': 'pass'})
        self.assertEqual(result['implementation_count'], 1)
        self.assertEqual(result['implementations']['backend-C1']['case_id'], 'C2')
        self.assertEqual(result['implementations']['backend-C1']['result'], 'pass')

    def test_historical_run_cannot_be_reported_under_another_suite(self):
        self.migrate()
        self.start()
        spec, view = wf.assets(self.project)
        spec['suite']['id'] = 'other-suite'
        with self.assertRaisesRegex(ValueError, 'another suite'):
            wf.summary(self.project, self.state(), spec, view)

    def test_local_implementation_change_does_not_invalidate_other_suite(self):
        backend = self.backend()
        self.add_api_implementations(backend)
        _, before = wf.assets(backend)
        local = wf.load(self.project / catalog.COVERAGE)
        local['implementations']['frontend-C1']['title'] = 'new UI title'
        self.write(catalog.COVERAGE, local)
        _, after = wf.assets(backend)
        self.assertEqual(wf.fingerprint(before), wf.fingerprint(after))

    def test_shared_business_change_invalidates_both_suites(self):
        backend = self.backend()
        _, ui_before = wf.assets(self.project)
        _, api_before = wf.assets(backend)
        business = wf.load(self.project / catalog.CATALOG)
        business['cases']['C1']['expected'] = 'changed approved business contract'
        self.write(catalog.CATALOG, business)
        self.assertNotEqual(wf.fingerprint(ui_before), wf.fingerprint(wf.assets(self.project)[1]))
        self.assertNotEqual(wf.fingerprint(api_before), wf.fingerprint(wf.assets(backend)[1]))

    def test_api_preflight_reuses_shared_snapshot_with_local_observation(self):
        shared_root = self.project
        backend = self.backend()
        manifest = source_tests.cache.capture(shared_root, {
            'source_id': 'S1', 'revision': '1', 'fingerprint': 'f1',
            'fingerprint_basis': 'canonical_content', 'files': ['test/source-a.txt']})
        wf.atomic(shared_root / 'test/.cache/sources/manifest.json', manifest)
        self.project = backend
        self.start()
        self.write('test/evidence.txt', 'Local fresh source observation')
        request = {'sources': [{'manifest': 'test/.cache/sources/manifest.json',
                               'observation': {
                                   'source_id': 'S1', 'revision': '1', 'fingerprint': 'f1',
                                   'readable': True, 'fingerprint_basis': 'canonical_content',
                                   'observed_at': wf.now(), 'evidence': 'test/evidence.txt'}}]}
        self.write('preflight.json', request)
        with patch.object(wf, 'doctor', return_value={'valid': True, 'checks': []}):
            result = self.cli('preflight', '--run', self.run, '--owner', self.owner,
                              '--input', str(self.project / 'preflight.json'))
        self.assertEqual(result['sources']['S1']['action'], 'reuse')
        self.assertTrue(result['sources']['S1']['matches_effective_baseline'])
        self.assertEqual(self.state()['source_checks'], {})
        (self.project / 'test/evidence.txt').unlink()
        with patch.object(wf, 'doctor', return_value={'valid': True, 'checks': []}):
            self.cli('preflight', '--run', self.run, '--owner', self.owner,
                     '--input', str(self.project / 'preflight.json'), fail=True)
        self.assertTrue((shared_root / 'test/evidence.txt').is_file())

    def test_wrong_suite_and_duplicate_active_mapping_are_rejected(self):
        backend = self.backend()
        self.add_api_implementations(backend)
        local = wf.load(backend / catalog.COVERAGE)
        local['implementations']['duplicate'] = copy.deepcopy(local['implementations']['backend-C1'])
        wf.atomic(backend / catalog.COVERAGE, local)
        with self.assertRaisesRegex(ValueError, 'one active implementation'):
            wf.assets(backend)
        local['implementations'].pop('duplicate')
        local['implementations']['backend-C1']['kind'] = 'ui'
        wf.atomic(backend / catalog.COVERAGE, local)
        with self.assertRaisesRegex(ValueError, 'another suite'):
            wf.assets(backend)

    def test_business_paths_and_test_paths_keep_separate_boundaries(self):
        backend = self.backend()
        self.add_api_implementations(backend)
        spec, view = wf.assets(backend)
        view['cases']['C1']['spec'] = '../test/src/C1.spec.ts'
        self.assertTrue(wf.asset_errors(backend, spec, view))
        with self.assertRaises(ValueError):
            catalog.asset_path(backend, view, '../outside.md')

    def test_record_update_preserves_other_kind_requirement_links(self):
        backend = self.backend(kinds=('ui',))
        business = wf.load(self.project / catalog.CATALOG)
        business['cases']['C2']['required_kinds'] = ['api']
        self.write(catalog.CATALOG, business)
        _, original = wf.assets(self.project)
        updated = copy.deepcopy(original)
        updated['cases']['C1']['expected'] = 'updated UI business expectation'
        updated = catalog.rebuild(updated)
        with wf.project_lock(self.project):
            catalog.persist(self.project, original, updated, wf.atomic)
        saved = wf.load(self.project / catalog.CATALOG)
        self.assertEqual(saved['requirements']['R2']['case_ids'], ['C2'])
        self.assertEqual(saved['cases']['C2']['required_kinds'], ['api'])

    def test_catalog_optimistic_update_refuses_lost_update(self):
        self.migrate()
        _, original = wf.assets(self.project)
        updated = copy.deepcopy(original)
        updated['cases']['C1']['expected'] = 'agent one'
        updated = catalog.rebuild(updated)
        business = wf.load(self.project / catalog.CATALOG)
        business['cases']['C2']['expected'] = 'agent two'
        self.write(catalog.CATALOG, business)
        with self.assertRaisesRegex(ValueError, 'concurrently'):
            catalog.persist(self.project, original, updated, wf.atomic)
        self.assertEqual(wf.load(self.project / catalog.CATALOG)['cases']['C2']['expected'], 'agent two')

    def test_catalog_partial_commit_is_recoverable_without_reexecution(self):
        self.migrate()
        _, original = wf.assets(self.project)
        updated = copy.deepcopy(original)
        updated['cases']['C1'].update(expected='new expectation', title='new UI test title')
        updated = catalog.rebuild(updated)

        def interrupted_write(path, value, **options):
            if path == self.project / catalog.COVERAGE:
                raise OSError('simulated disk interruption')
            wf.atomic(path, value, **options)

        with self.assertRaises(OSError):
            catalog.persist(self.project, original, updated, interrupted_write)
        with self.assertRaisesRegex(ValueError, 'catalog-recover'):
            wf.assets(self.project)
        self.cli('catalog-recover')
        _, recovered = wf.assets(self.project)
        self.assertEqual(recovered, updated)

    def test_api_first_catalog_can_be_reused_by_ui(self):
        api_root = self.project / 'api-first'
        ui_root = self.project / 'ui-second'
        api_root.mkdir()
        ui_root.mkdir()
        for project, kind, root in [(api_root, 'api', '.'), (ui_root, 'ui', '../api-first')]:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = wf.main(['init', '--project', str(project), '--kind', kind,
                                '--suite-id', kind, '--catalog-root', root])
            self.assertEqual(code, 0, stream.getvalue())
        business = catalog.empty_catalog(self.legacy['scopes'])
        business.update({field: copy.deepcopy(self.legacy[field]) for field in ('sources', 'requirements')})
        for case_id, case in self.legacy['cases'].items():
            business['cases'][case_id] = {key: value for key, value in case.items()
                                          if key not in catalog.IMPLEMENTATION_FIELDS}
            business['cases'][case_id].update(title=case_id, required_kinds=['ui', 'api'])
        wf.atomic(api_root / catalog.CATALOG, business)
        for project in (api_root, ui_root):
            spec = wf.parse_spec(project / 'test/spec.md')
            spec['scope']['fixed'] = ['a', 'b']
            wf.atomic(project / 'test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```')
            wf.atomic(project / catalog.COVERAGE, {'schema_version': 2, 'scopes': ['a', 'b'], 'implementations': {}})
        self.assertEqual(set(wf.assets(api_root)[1]['cases']), set(wf.assets(ui_root)[1]['cases']))

    def test_backend_code_source_requires_provenance(self):
        self.migrate()
        business = wf.load(self.project / catalog.CATALOG)
        business['sources']['S1'].update(kind='backend_code', location='src/Controller.java')
        catalog.validate_catalog(business)
        business['sources']['S1'].update(revision=None, fingerprint=None)
        with self.assertRaisesRegex(ValueError, 'location and version'):
            catalog.validate_catalog(business)

    def test_record_case_persists_business_and_local_mapping_then_renews(self):
        self.migrate()
        self.start()
        self.check_sources()
        _, view = wf.assets(self.project)
        case = copy.deepcopy(view['cases']['C1'])
        case.update(expected='reviewed behavior', business_title='Shared business title')
        self.record({'type': 'case', 'case_id': 'C1', 'action': 'updated',
                     'code_action': 'reused', 'coverage': case})
        self.assertEqual(wf.load(self.project / catalog.CATALOG)['cases']['C1']['title'],
                         'Shared business title')
        self.assertEqual(wf.load(self.project / catalog.COVERAGE)['implementations']['frontend-C1']['title'],
                         'Synthetic C1')
        self.assertEqual(self.state()['asset_fingerprint'], wf.fingerprint(wf.assets(self.project)[1]))
        self.cli('renew', '--run', self.run, '--owner', self.owner)

    def test_shared_lock_rejects_symlink_before_creating_external_cache(self):
        outside = self.project / 'outside'
        outside.mkdir()
        root = self.project / 'shared'
        root.mkdir()
        platform_tests.symlink(self, root / 'test', outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'escapes configured root'):
            with catalog.directory_lock(root, 'catalog.lock'):
                pass
        self.assertEqual(list(outside.iterdir()), [])

    def test_pending_catalog_journal_is_visible_to_readonly_doctor(self):
        self.migrate()
        self.write('test/.cache/catalog-update.json', {'complete': False})
        result = wf.doctor(self.project)
        self.assertEqual(next(item for item in result['checks'] if item['check'] == 'catalog_update')['status'],
                         'error')

    def test_malformed_required_kinds_and_suite_kind_fail_as_validation_errors(self):
        self.migrate()
        business = wf.load(self.project / catalog.CATALOG)
        business['cases']['C1']['required_kinds'] = [{}]
        with self.assertRaises(ValueError):
            catalog.validate_catalog(business)
        with self.assertRaises(ValueError):
            catalog.suite({'suite': {'id': 'backend', 'kind': []}})

    def test_external_coordination_retention_preserves_terminal_run_payloads(self):
        fixture = retention_tests.RetentionTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        shared = fixture.project / 'shared'
        shared.mkdir()
        fixture.spec['coordination'] = {'root': 'shared'}
        fixture.write('test/spec.md', '```json workflow-config\n' + json.dumps(fixture.spec) + '\n```\n')
        plan = fixture.plan()
        self.assertEqual(plan['files'], [])
        self.assertIn('external_coordination_preserves_runs', {item['reason'] for item in plan['limitations']})

    def test_local_catalog_baseline_protects_referenced_run_artifacts(self):
        fixture = retention_tests.RetentionTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.spec.update(suite={'id': 'ui', 'kind': 'ui'}, catalog={'root': '.'})
        fixture.cov = {'schema_version': 2, 'scopes': ['a', 'b'], 'implementations': {}}
        business = catalog.empty_catalog(fixture.cov['scopes'])
        business['sources']['S1'] = {'scope': 'a', 'baseline_path': fixture.html}
        fixture.write(catalog.CATALOG, business)
        fixture.write(catalog.COVERAGE, fixture.cov)
        fixture.write('test/spec.md', '```json workflow-config\n' + json.dumps(fixture.spec) + '\n```\n')
        plan = fixture.plan()
        self.assertNotIn(fixture.html, fixture.paths(plan))
        self.assertIn(fixture.noise, fixture.paths(plan))


class SharedPoolTest(unittest.TestCase):
    def setUp(self):
        self.fixtures = []
        for index in range(2):
            fixture = pool_tests.IdentityPoolTest()
            fixture.setUp()
            self.addCleanup(fixture.tearDown)
            fixture.project = fixture.project.resolve()
            self.fixtures.append(fixture)
        root = self.fixtures[0].project
        for fixture in self.fixtures:
            fixture.configure(limit=1)
            spec = wf.parse_spec(fixture.project / 'test/spec.md')
            spec['coordination'] = {'root': str(root)}
            fixture.write('test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```\n')
            fixture.start()
            fixture.check_sources()

    def test_pending_intent_in_root_project_blocks_other_project_until_absence(self):
        first, second = self.fixtures
        first.record(first.intent('account'))
        result = second.record(second.intent('account'), fail=True)
        self.assertIn('is full: 1/1', result['error'])
        first.record({'type': 'resource', 'resource_id': 'account', 'status': 'absence_verified',
                      'created_by_run': first.run,
                      'absence_check': 'Synthetic fixture verified no account was created',
                      'evidence': ['test/evidence.txt']})
        second.record(second.intent('account'))
        self.assertEqual(wf.pool_occupancy(first.project, first.state(), 'pool-a'), 1)

    def test_interrupted_state_commit_can_retry_own_persisted_intent(self):
        first, second = self.fixtures
        before = copy.deepcopy(first.state())
        first.record(first.intent('account'))
        wf.atomic(wf.run_path(first.project, first.run) / 'state.json', before)
        first.record(first.intent('account'))
        self.assertIn('is full: 1/1', second.record(second.intent('other'), fail=True)['error'])

    def test_retained_shared_account_keeps_provenance_and_cannot_be_double_leased(self):
        first, second = self.fixtures
        first.create('account', 'shared-user')
        ledger = wf.pool_ledger(second.project)
        self.assertEqual(ledger['pools']['pool-a']['accounts']['shared-user']['origin_project'], str(first.project))
        self.assertTrue(first.pool('pool-lease', '--count', '1')['valid'])
        self.assertEqual(second.pool('pool-lease', '--count', '1', fail=True)['error'], 'pool_exhausted')

    def test_shared_pool_limit_mismatch_fails_before_new_resource(self):
        first, second = self.fixtures
        first.record(first.intent('account'))
        second.configure(limit=2)
        spec = wf.parse_spec(second.project / 'test/spec.md')
        spec['coordination'] = {'root': str(first.project)}
        second.write('test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```\n')
        with self.assertRaisesRegex(ValueError, 'max_accounts mismatch'):
            wf.check_pool_limit(second.project, spec, 'pool-a')


class ApiSchedulingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='api-coordination-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.projects = [self.root / name for name in ('frontend', 'backend')]
        for project, kind in zip(self.projects, ('ui', 'api')):
            (project / 'test').mkdir(parents=True)
            spec = {'schema_version': 1, 'suite': {'id': kind, 'kind': kind},
                    'coordination': {'root': '..'}, 'concurrency': {'browser_parallel_cap': 4}}
            wf.atomic(project / 'test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```')
        self.probe = patch.object(sc, 'probe_resources', return_value={
            'available_cpus': 16, 'available_memory_bytes': 16 * sc.GIB})
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def request(self, kind, conflict='account:shared', workers=1):
        return {'run_id': kind + '-run', 'task_id': 'T1', 'owner': kind, 'owner_pid': os.getpid(),
                'resources': {'available_cpus': 16, 'available_memory_bytes': 16 * sc.GIB,
                              'available_agent_slots': 4, 'independent_tasks': 4},
                'tasks': {'T1': {'phase': 'execution', 'writable_paths': [], 'isolation': 'verified',
                                 'isolation_group': kind, 'conflict_keys': [conflict],
                                 'quota': {'agents': 1, 'playwright_workers': workers}}}}

    def test_api_workers_do_not_consume_browser_capacity(self):
        request = self.request('api', workers=2)
        request['concurrency'] = {'browser_parallel_cap': 0}
        result = sc.execute('acquire', self.projects[1], request)
        self.assertTrue(result['acquired'])
        self.assertEqual(result['lease']['quota']['browsers'], 0)
        self.assertEqual(result['lease']['quota']['playwright_workers'], 2)

    def test_worker_and_agent_caps_are_independent_in_both_directions(self):
        for first_index, second_index in ((0, 1), (1, 0)):
            first_kind, second_kind = ('ui', 'api')[first_index], ('ui', 'api')[second_index]
            first = self.request(first_kind, conflict=first_kind)
            second = self.request(second_kind, conflict=second_kind)
            for request in (first, second):
                request['concurrency'] = {'max_parallel_agents': 1, 'playwright_workers': 1}
            leases = []
            try:
                first_result = sc.execute('acquire', self.projects[first_index], first)
                self.assertTrue(first_result['acquired'])
                leases.append((first_index, first_result['lease']))
                second_result = sc.execute('acquire', self.projects[second_index], second)
                self.assertTrue(second_result['acquired'], second_result)
                leases.append((second_index, second_result['lease']))
                self.assertEqual(second_result['budget']['available']['playwright_workers'], 0)
                self.assertEqual(second_result['budget']['quota']['active']['playwright_workers'], 2)
                for kind in ('ui', 'api'):
                    self.assertEqual(second_result['budget']['quota']['active_by_kind'][kind]['playwright_workers'], 1)
                extra = self.request(second_kind, conflict='other-resource')
                extra.update(run_id=second_kind + '-other', concurrency=second['concurrency'])
                blocked = sc.execute('acquire', self.projects[second_index], extra)
                self.assertFalse(blocked['acquired'])
                self.assertTrue(any(reason['kind'] == 'quota' for reason in blocked['reasons']))
            finally:
                for index, lease in leases:
                    sc.execute('release', self.projects[index], {
                        'lease_id': lease['lease_id'], 'release_token': lease['release_token']})

    def test_legacy_ui_lease_keeps_signature_and_api_gets_separate_capacity(self):
        ui = sc.execute('acquire', self.projects[0], self.request('ui', conflict='ui-only'))
        self.assertNotIn('test_kind', ui['lease']['task'])
        api = self.request('api', conflict='api-only')
        api['concurrency'] = {'max_parallel_agents': 1, 'playwright_workers': 1, 'browser_parallel_cap': 0}
        self.assertTrue(sc.execute('acquire', self.projects[1], api)['acquired'])
        self.assertTrue(sc.execute('acquire', self.projects[0], self.request('ui', conflict='ui-only'))['idempotent'])

    def test_mixed_ready_uses_each_task_kind_without_splitting_conflicts(self):
        request = self.request('ui', conflict='ui-only')
        request['concurrency'] = {'max_parallel_agents': 1, 'playwright_workers': 1}
        request['tasks']['API'] = dict(self.request('api', conflict='api-only')['tasks']['T1'], test_kind='api')
        ready = sc.execute('ready', self.projects[0], request)
        self.assertEqual(ready['ready'], ['T1', 'API'])
        first = sc.execute('acquire', self.projects[0], request)
        self.assertTrue(first['acquired'])
        request['task_id'] = 'API'
        second = sc.execute('acquire', self.projects[0], request)
        self.assertTrue(second['acquired'], second)
        request['tasks']['other-api'] = dict(request['tasks']['API'], conflict_keys=['ui-only'])
        request['task_id'] = 'other-api'
        denied = sc.execute('acquire', self.projects[0], request)
        self.assertFalse(denied['acquired'])
        self.assertTrue(any(reason['kind'] == 'conflict_keys' for reason in denied['reasons']))

    def test_api_workers_respect_existing_browser_memory_reservation(self):
        ui = self.request('ui', conflict='ui-only')
        self.assertTrue(sc.execute('acquire', self.projects[0], ui)['acquired'])
        api = self.request('api', conflict='api-only', workers=2)
        api['resources']['available_memory_bytes'] = 4 * sc.GIB + sc.API_WORKER_BYTES
        self.assertFalse(sc.execute('acquire', self.projects[1], api)['acquired'])
        api['tasks']['T1']['quota']['playwright_workers'] = 1
        self.assertTrue(sc.execute('acquire', self.projects[1], api)['acquired'])

    def test_ui_browser_respects_existing_api_worker_memory_reservation(self):
        self.assertTrue(sc.execute('acquire', self.projects[1], self.request('api', conflict='api-only'))['acquired'])
        ui = self.request('ui', conflict='ui-only')
        ui['resources']['available_memory_bytes'] = 4 * sc.GIB
        self.assertFalse(sc.execute('acquire', self.projects[0], ui)['acquired'])

    def test_unknown_memory_does_not_mint_capacity_across_test_kinds(self):
        for first_kind, first_project, second_kind, second_project in (
                ('ui', self.projects[0], 'api', self.projects[1]),
                ('api', self.projects[1], 'ui', self.projects[0])):
            first = self.request(first_kind, conflict=first_kind)
            first['resources']['available_memory_bytes'] = None
            lease = sc.execute('acquire', first_project, first)
            self.assertTrue(lease['acquired'])
            second = self.request(second_kind, conflict=second_kind)
            second['resources']['available_memory_bytes'] = None
            self.assertFalse(sc.execute('acquire', second_project, second)['acquired'])
            sc.execute('release', first_project, {
                'lease_id': lease['lease']['lease_id'], 'release_token': lease['lease']['release_token']})

    def test_shared_resource_conflict_blocks_other_project_and_releases(self):
        ui = sc.execute('acquire', self.projects[0], self.request('ui'))
        blocked = sc.execute('acquire', self.projects[1], self.request('api'))
        self.assertFalse(blocked['acquired'])
        self.assertTrue(any(reason['kind'] == 'conflict_keys' for reason in blocked['reasons']))
        sc.execute('release', self.projects[0], {
            'lease_id': ui['lease']['lease_id'], 'release_token': ui['lease']['release_token']})
        self.assertTrue(sc.execute('acquire', self.projects[1], self.request('api'))['acquired'])

    def test_shared_lock_serializes_competing_acquisitions(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(sc.execute, 'acquire', project, self.request(kind))
                       for project, kind in zip(self.projects, ('ui', 'api'))]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(result['acquired'] for result in results), 1)

    def test_api_failure_routing_stays_in_api_workflow(self):
        self.assertEqual(residual_tests.wp.action({'category': 'api_exclusive'}, 'api'), 'diagnose_api')
        self.assertEqual(residual_tests.wp.action({'category': 'api_exclusive'}, 'ui'), 'review_api_scope')

    def test_shared_residual_index_blocks_both_projects(self):
        path = catalog.coordination_path(self.projects[0], cp.RESIDUAL_INDEX)
        resource = {'resource_environment': 'same-env', 'kind': 'device', 'id': 'record-1',
                    'status': 'ambiguous', 'first_seen_run': 'ui-run', 'environment_is_stable': True}
        wf.atomic(path, {'schema_version': 1, 'resources': {
            cp.digest(['same-env', 'device', 'record-1']): resource}, 'receipts': {}})
        request = {'resource_environment': 'same-env',
                   'resource_access': {'mode': 'write', 'kinds': ['device'], 'dependencies': []}}
        for project in self.projects:
            self.assertFalse(cp.predispatch_gate(project, request)['allowed'])

    def test_bound_coordination_root_cannot_be_switched_to_empty_ledger(self):
        project = self.projects[0]
        sc.execute('acquire', project, self.request('ui'))
        spec = catalog.config(project)
        spec['coordination'] = {'root': '.'}
        wf.atomic(project / 'test/spec.md', '```json workflow-config\n' + json.dumps(spec) + '\n```')
        with self.assertRaisesRegex(ValueError, 'coordination root changed'):
            sc.execute('acquire', project, self.request('new'))

    def test_legacy_local_ledger_prevents_enabling_external_coordination(self):
        project = self.projects[0]
        wf.atomic(project / 'test/.cache/resource-residuals.json',
                  {'schema_version': 1, 'resources': {}, 'receipts': {}})
        with self.assertRaisesRegex(ValueError, 'existing local coordination ledger'):
            sc.execute('acquire', project, self.request('ui'))

    def test_actual_shared_cleanup_writer_blocks_then_resolves_other_project(self):
        first, second = self.projects
        wf.atomic(first / 'test/.cache/evidence.json', {'observed': 'pending'})
        spec = {'environment_id': 'same-env'}
        resource = {'kind': 'device', 'id': 'record-1', 'status': 'ambiguous',
                    'evidence': ['test/.cache/evidence.json']}
        with cp.residual_lock(first):
            cp.persist_cleanup(first, 'test/.cache/journal.jsonl', spec,
                               {'type': 'cleanup_state', 'event_id': 'pending', 'resources': [resource]})
        request = {'resource_environment': 'same-env',
                   'resource_access': {'mode': 'write', 'kinds': ['device'], 'dependencies': []}}
        self.assertFalse(cp.predispatch_gate(second, request)['allowed'])
        resource['status'] = 'absence_verified'
        wf.atomic(first / 'test/.cache/evidence.json', {
            'resource_environment': 'same-env', 'kind': 'device', 'id': 'record-1',
            'status': 'absence_verified', 'verified': True})
        with cp.residual_lock(first):
            cp.persist_cleanup(first, 'test/.cache/journal.jsonl', spec,
                               {'type': 'cleanup_state', 'event_id': 'resolved', 'resources': [resource]})
        self.assertTrue(cp.predispatch_gate(second, request)['allowed'])

    def test_api_workpackage_schema_carries_routing_and_permission_precedence(self):
        project = self.projects[1]
        wf.atomic(project / 'test/.cache/failure.json', {'fixture': 'diagnostic'})
        item = {'case_id': 'C1', 'category': 'api_exclusive', 'evidence': ['test/.cache/failure.json']}
        result = residual_tests.wp.build(project, {'items': [item], 'test_kind': 'api'})
        self.assertEqual(result['schema_version'], 3)
        self.assertEqual(result['packages'][0]['action'], 'diagnose_api')
        item['permission_required'] = True
        result = residual_tests.wp.build(project, {'items': [item], 'test_kind': 'api'})
        self.assertEqual(result['packages'][0]['action'], 'awaiting_authorization')


if __name__ == '__main__':
    unittest.main()
