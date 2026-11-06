"""Offline API design checks; no product services or test execution."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('api_coverage', ROOT / 'scripts/api_coverage.py')
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        spec = self.project / 'test/src/api.spec.ts'
        spec.parent.mkdir(parents=True)
        spec.write_text('fixture test declarations')
        self.plan = json.loads((ROOT / 'assets/api-coverage.template.json').read_text())
        self.endpoint = self.plan['endpoints'][0]
        self.dynamic = self.endpoint['dynamic_parameters']
        self.catalog = {'cases': {}}
        self.coverage = {'implementations': {}}
        self.discovery = {'suites': [{'specs': []}], 'errors': []}
        identifiers = set()

        def collect(value):
            if isinstance(value, dict):
                identifiers.update(value.get('case_ids', []))
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(self.plan)
        for case_id in sorted(identifiers):
            self.catalog['cases'][case_id] = {'required_kinds': ['api'], 'status': 'active'}
            self.coverage['implementations'][case_id] = {
                'case_id': case_id, 'suite_id': 'joint-api', 'kind': 'api', 'spec': 'test/src/api.spec.ts',
                'test_id': case_id, 'title': case_id + ' scenario', 'project': 'api',
                'implementation': 'valid', 'execution': 'automation'}
            self.discovery['suites'][0]['specs'].append({
                'file': 'test/src/api.spec.ts', 'title': case_id + ' scenario',
                'tests': [{'projectName': 'api'}]})

    def validate(self, contracts=None):
        return api.Validator(self.project, self.plan, self.catalog, self.coverage,
                             self.discovery, 'joint-api', contracts).run()

    def invalid(self, text):
        with self.assertRaises(api.CoverageError) as raised:
            self.validate()
        self.assertIn(text, str(raised.exception))

    def make_complete(self):
        for endpoint in self.plan['endpoints']:
            endpoint['negative_review']['status'] = 'complete'
            endpoint['negative_review']['categories'] = [
                row for row in endpoint['negative_review']['categories'] if row['disposition'] == 'covered']
            endpoint['branch_assessment']['completeness'] = 'complete'
        self.dynamic['uncovered_interactions'] = []

    def test_example_reports_real_gaps_without_claiming_execution(self):
        result = self.validate()
        self.assertEqual(result['status'], 'known_gaps')
        self.assertEqual(result['discovery'], 'checked')
        self.assertEqual(result['counts']['mapped_cases'], 8)
        self.assertIn('branch_inventory', {row['kind'] for row in result['known_gaps']})

    def test_complete_declared_plan_is_checked(self):
        self.make_complete()
        result = self.validate()
        self.assertEqual(result['status'], 'checked')
        self.assertEqual(result['counts']['journeys'], 1)
        self.assertEqual(result['counts']['dynamic_scenarios'], 4)

    def test_inventory_cannot_omit_endpoint(self):
        self.plan['inventory']['included'].pop()
        self.invalid('exactly match the endpoint rows')

    def test_positive_and_negative_are_both_required_when_applicable(self):
        self.endpoint['positive_scenarios'] = []
        self.invalid('positive_scenarios')
        self.setUp()
        self.endpoint['negative_scenarios'] = []
        self.invalid('negative scenarios')

    def test_inapplicable_negatives_need_explicit_reason(self):
        self.endpoint['negative_scenarios'] = []
        self.endpoint['negative_review'] = {
            'method': 'Public static response has no input or protected context.',
            'status': 'not_applicable', 'categories': [{'category': 'validation',
                'disposition': 'not_applicable', 'reason': 'No input'}]}
        self.invalid('negative_review.reason')

    def test_negative_category_cannot_claim_another_category(self):
        self.endpoint['negative_review']['categories'][0]['category'] = 'conflict'
        self.invalid('all referenced scenarios')

    def test_independent_negative_scenarios_cannot_reuse_one_case(self):
        additional = copy.deepcopy(self.endpoint['negative_scenarios'][0])
        additional['scenario_id'] = 'different-invalid-input'
        self.endpoint['negative_scenarios'].append(additional)
        self.invalid('independent endpoint scenarios must use distinct cases')

    def test_distinct_cases_cannot_inflate_one_test_declaration(self):
        self.coverage['implementations']['SEND-INVALID']['title'] = 'SEND-SYNC scenario'
        self.invalid('distinct test declarations')

    def test_blocked_category_cannot_claim_scenario_coverage(self):
        row = self.endpoint['negative_review']['categories'][0]
        row.update(disposition='blocked', reason='Unavailable target')
        self.invalid('every negative scenario')

    def test_complete_negative_review_cannot_have_blocked_category(self):
        self.endpoint['negative_review']['status'] = 'complete'
        self.invalid('complete cannot contain blocked')

    def test_complete_branch_review_rejects_unmapped_scenario(self):
        self.make_complete()
        self.endpoint['branches'][0]['scenario_ids'] = ['send-sync']
        self.invalid('every positive and negative scenario')

    def test_branch_outcome_must_match_scenario_polarity(self):
        self.endpoint['branches'][0]['scenario_ids'] = ['send-invalid']
        self.invalid('positive scenarios')

    def test_complete_branch_review_cannot_hide_blocked_branch(self):
        self.make_complete()
        self.endpoint['branches'][0].update(disposition='blocked', reason='Missing simulator')
        self.invalid('complete assessment cannot')

    def test_branch_needs_reachability_and_source(self):
        del self.endpoint['branches'][0]['reachability']
        self.invalid('reachability')

    def test_blackbox_cannot_claim_implementation_branch_completeness(self):
        self.make_complete()
        self.plan['inventory'].update(backend_code_access=False, backend_unavailable_reason='Source unavailable')
        self.plan['sources'] = [self.plan['sources'][0]]

        def replace_refs(value):
            if isinstance(value, dict):
                if 'source_ids' in value:
                    value['source_ids'] = ['REQ']
                for child in value.values():
                    replace_refs(child)
            elif isinstance(value, list):
                for child in value:
                    replace_refs(child)

        replace_refs(self.plan)
        result = self.validate()
        self.assertIn('source_branch_review', {row['kind'] for row in result['known_gaps']})

    def test_dynamic_case_cannot_be_borrowed_from_another_scenario(self):
        self.dynamic['selected_scenarios'][0]['case_ids'] = ['READ-RESULT']
        self.invalid('must use its endpoint scenario cases')

    def test_invalid_dynamic_combination_must_be_a_negative(self):
        self.dynamic['selected_scenarios'][0]['kind'] = 'invalid_combination'
        self.invalid('must reference a negative')

    def test_dynamic_representatives_must_be_exercised_or_declared_missing(self):
        self.dynamic['dimensions'][0]['representatives'].append({'value': 'scheduled', 'meaning': 'Deferred mode'})
        self.invalid('scheduled must be selected')
        self.dynamic['uncovered_values'].append({
            'dimension': 'mode', 'value': 'scheduled', 'reason': 'Requires a scheduler'})
        result = self.validate()
        self.assertIn('dynamic_value', {row['kind'] for row in result['known_gaps']})

    def test_required_interaction_checks_values_not_only_a_scenario_name(self):
        self.dynamic['required_interactions'][0]['scenario_ids'] = ['send-limit']
        self.invalid('must exercise the required interaction')

    def test_pairwise_claim_computes_missing_pairs(self):
        self.make_complete()
        self.dynamic['strategy'] = 'pairwise'
        result = self.validate()
        gaps = [row for row in result['known_gaps'] if row['kind'] == 'combination']
        self.assertEqual(len(gaps), 1)
        self.assertIn('2 feasible combinations', gaps[0]['reason'])
        self.assertEqual(gaps[0]['missing_examples'], [
            {'mode': 'async', 'payload': 'invalid'}, {'mode': 'async', 'payload': 'typed'}])

    def test_full_strategy_accounts_for_explicit_infeasible_combinations(self):
        self.make_complete()
        self.dynamic['strategy'] = 'full'
        self.dynamic['excluded_combinations'] = [
            {'values': {'mode': 'async', 'payload': label}, 'reason': 'Example constraint', 'source_ids': ['REQ']}
            for label in ('typed', 'invalid')]
        self.assertEqual(self.validate()['status'], 'checked')

    def test_exclusion_cannot_remove_a_selected_combination(self):
        self.dynamic['excluded_combinations'] = [
            {'values': {'mode': 'sync'}, 'reason': 'Incorrect exclusion', 'source_ids': ['REQ']}]
        self.invalid('selected scenarios contain an excluded')

    def test_finite_enums_need_not_invent_numeric_boundaries(self):
        self.dynamic['selected_scenarios'][1]['kind'] = 'typical'
        self.dynamic['boundary_assessment'] = {'status': 'not_applicable', 'reason': 'Only unordered enum choices'}
        self.validate()

    def test_invalid_core_field_types_return_validation_errors(self):
        for field in ('inventory', 'sources', 'endpoints', 'journeys'):
            with self.subTest(field=field):
                original = self.plan[field]
                self.plan[field] = None
                with self.assertRaises(api.CoverageError):
                    self.validate()
                self.plan[field] = original

    def test_malformed_plan_strings_return_field_errors_without_crashing(self):
        paths = []

        def collect(value, path=()):
            if isinstance(value, dict):
                for key, child in value.items():
                    collect(child, path + (key,))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    collect(child, path + (index,))
            elif isinstance(value, str):
                paths.append(path)

        collect(self.plan)
        for path in paths:
            parent = self.plan
            for key in path[:-1]:
                parent = parent[key]
            original = parent[path[-1]]
            parent[path[-1]] = []
            with self.subTest(path=path):
                with self.assertRaises(api.CoverageError):
                    self.validate()
            parent[path[-1]] = original

    def test_two_unrelated_services_do_not_form_a_journey(self):
        self.plan['journeys'][0]['steps'][1]['data_links'] = []
        self.invalid('link a business output or state')

    def test_journey_step_cannot_depend_on_future_step(self):
        self.plan['journeys'][0]['steps'][0]['depends_on'] = ['read']
        self.invalid('dependencies must reference earlier')

    def test_journey_cleanup_owner_must_match_endpoint_owner(self):
        self.plan['journeys'][0]['cleanup'][0]['owner_suite'] = 'concurrent-ui'
        self.invalid('cleanup suite of journey steps')

    def test_missing_journey_requires_assessment_and_reason(self):
        self.plan['journeys'] = []
        self.invalid('cross_service_assessment')

    def test_journey_failure_requires_downstream_observation(self):
        self.plan['journeys'][0]['failure_scenarios'][0]['observations'] = []
        self.invalid('observations')

    def test_journey_failure_cannot_reuse_success_case(self):
        self.make_complete()
        journey = self.plan['journeys'][0]
        journey['failure_scenarios'][0]['case_ids'] = journey['case_ids'][:]
        del self.coverage['implementations']['JOURNEY-REJECTED']
        self.invalid('independent journey scenarios must use distinct cases')

    def test_independent_journey_failures_cannot_reuse_one_case(self):
        self.make_complete()
        journey = self.plan['journeys'][0]
        additional = copy.deepcopy(journey['failure_scenarios'][0])
        additional.update(failure_id='downstream-rejects', at_step_id='read',
                          expected='Downstream rejects the query.')
        journey['failure_scenarios'].append(additional)
        self.invalid('independent journey scenarios must use distinct cases')

    def test_journey_failures_with_distinct_discovered_tests_are_checked(self):
        self.make_complete()
        journey = self.plan['journeys'][0]
        additional = copy.deepcopy(journey['failure_scenarios'][0])
        additional.update(failure_id='downstream-rejects', at_step_id='read',
                          case_ids=['JOURNEY-DOWNSTREAM'], expected='Downstream rejects the query.')
        journey['failure_scenarios'].append(additional)
        self.catalog['cases']['JOURNEY-DOWNSTREAM'] = {'required_kinds': ['api'], 'status': 'active'}
        self.coverage['implementations']['JOURNEY-DOWNSTREAM'] = dict(
            self.coverage['implementations']['JOURNEY-REJECTED'], case_id='JOURNEY-DOWNSTREAM',
            test_id='JOURNEY-DOWNSTREAM', title='JOURNEY-DOWNSTREAM scenario')
        self.discovery['suites'][0]['specs'].append({
            'file': 'test/src/api.spec.ts', 'title': 'JOURNEY-DOWNSTREAM scenario',
            'tests': [{'projectName': 'api'}]})
        result = self.validate()
        self.assertEqual(result['status'], 'checked')
        self.assertEqual(result['counts']['journey_failure_scenarios'], 2)

    def test_case_must_be_active_valid_api_automation(self):
        self.coverage['implementations']['SEND-SYNC']['implementation'] = 'draft'
        self.invalid('not valid automation')

    def test_discovery_uses_spec_title_and_exact_project(self):
        self.discovery['suites'][0]['specs'][0]['tests'][0]['title'] = 'misleading per-test field'
        self.validate()
        self.discovery['suites'][0]['specs'][0]['tests'][0]['projectName'] = 'ui'
        self.invalid('exactly one test')

    def test_unknown_case_and_duplicate_discovery_are_rejected(self):
        del self.catalog['cases']['SEND-SYNC']
        self.invalid('unknown business case')
        self.setUp()
        specs = self.discovery['suites'][0]['specs']
        specs.append(copy.deepcopy(specs[0]))
        self.invalid('exactly one test')

    def test_discovery_root_and_native_project_instances_are_preserved(self):
        self.discovery['config'] = {'rootDir': str(self.project / 'test/src')}
        for spec in self.discovery['suites'][0]['specs']:
            spec['file'] = 'api.spec.ts'
            spec['tests'].extend([{'projectName': 'secondary-api'}, {'projectName': 'api', 'repeatEachIndex': 1}])
        for implementation in self.coverage['implementations'].values():
            del implementation['project']
        self.assertEqual(self.validate()['counts']['discovered_tests'], 24)

    def test_missing_or_external_spec_is_rejected(self):
        self.coverage['implementations']['SEND-SYNC']['spec'] = '../outside.spec.ts'
        self.invalid('existing file inside project')

    def test_unplanned_suite_implementation_is_detected(self):
        self.coverage['implementations']['EXTRA'] = dict(
            self.coverage['implementations']['SEND-SYNC'], case_id='EXTRA')
        self.invalid('absent from the plan')

    def snapshot(self, extra=False):
        document = {'openapi': '3.1.0', 'paths': {'/commands': {'post': {'responses': {}}}}}
        if extra:
            document['paths']['/commands/{id}'] = {'delete': {'responses': {}}}
        path = self.project / 'openapi.json'
        path.write_text(json.dumps(document))
        ids, fingerprint = api.openapi_inventory(path, 'commands')
        self.plan['sources'].append({'source_id': 'API', 'kind': 'openapi', 'revision': 'snapshot',
                                    'fingerprint': fingerprint, 'locations': [str(path)]})
        return path, (ids, fingerprint)

    def test_contract_diff_finds_uninventoried_endpoint(self):
        path, snapshot = self.snapshot(extra=True)
        with self.assertRaises(api.CoverageError) as raised:
            self.validate({'commands': snapshot})
        self.assertIn('commands:DELETE:/commands/{id}', str(raised.exception))
        self.plan['inventory']['excluded'].append({
            'endpoint_id': 'commands:DELETE:/commands/{id}', 'reason': 'Outside this authorized module',
            'source_ids': ['API']})
        self.validate({'commands': snapshot})
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), snapshot[1])

    def test_contract_fingerprint_must_match(self):
        _, snapshot = self.snapshot()
        self.plan['sources'][-1]['fingerprint'] = 'stale'
        with self.assertRaises(api.CoverageError) as raised:
            self.validate({'commands': snapshot})
        self.assertIn('SHA256', str(raised.exception))

    def test_unresolved_path_refs_do_not_silently_omit_operations(self):
        path = self.project / 'refs.json'
        path.write_text(json.dumps({'openapi': '3.0.0', 'paths': {'/commands': {'$ref': 'other.json'}}}))
        with self.assertRaisesRegex(ValueError, 'resolve path-item'):
            api.openapi_inventory(path, 'commands')

    def test_cli_exit_codes_and_no_plan_mutation(self):
        files = {}
        for name, value in [('plan', self.plan), ('catalog', self.catalog),
                            ('coverage', self.coverage), ('discovery', self.discovery)]:
            path = self.project / (name + '.json')
            path.write_text(json.dumps(value))
            files[name] = path
        args = [argument for name, path in files.items() for argument in ('--' + name, str(path))]
        args.extend(['--project', str(self.project)])
        before = files['plan'].read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(api.main(args), 2)
            self.assertEqual(api.main(args + ['--allow-known-gaps']), 0)
            self.assertEqual(api.main(args + ['--suite-id', 'wrong']), 1)
        self.assertEqual(files['plan'].read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
