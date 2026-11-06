"""UI plan checks operate on declared scope, not business services."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('ui_coverage', ROOT / 'scripts/ui_coverage.py')
ui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ui)


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        (self.project / 'orders.spec.ts').write_text('test declarations', encoding='utf-8')
        self.plan = json.loads((ROOT / 'assets/ui-coverage.template.json').read_text())
        self.page = self.plan['pages'][0]
        self.catalog = {'cases': {'ORDER-CREATE': {'required_kinds': ['ui', 'api'], 'status': 'active'}}}
        self.implementation = {
            'case_id': 'ORDER-CREATE', 'suite_id': 'example-ui', 'kind': 'ui',
            'implementation': 'valid', 'execution': 'automation', 'spec': 'orders.spec.ts',
            'title': 'ORDER-CREATE create and re-enter', 'test_id': 'order-create'}
        self.coverage = {'implementations': {'ui-create': self.implementation}}
        self.discovery = {'config': {'rootDir': str(self.project)}, 'errors': [], 'suites': [
            {'specs': [{'file': 'orders.spec.ts', 'title': self.implementation['title'],
                        'tests': [{'projectName': 'chromium'}, {'projectName': 'firefox'}]}]}]}

    def validate(self, discovery=True):
        return ui.Validator(self.project, self.plan, self.catalog, self.coverage,
                            self.discovery if discovery else None, 'example-ui').run()

    def invalid(self, message):
        with self.assertRaises(ui.CoverageError) as raised:
            self.validate()
        self.assertIn(message, str(raised.exception))

    def complete(self):
        for assessment in self.page['review'].values():
            if assessment['status'] == 'blocked':
                assessment.update(status='not_applicable', reason='Scoped fixture has no such control.')

    def add_scenario(self):
        self.catalog['cases']['ORDER-INVALID'] = {'required_kinds': ['ui']}
        self.coverage['implementations']['ui-invalid'] = dict(
            self.implementation, case_id='ORDER-INVALID', title='Invalid order', test_id='invalid-order')
        self.discovery['suites'][0]['specs'].append(
            {'file': 'orders.spec.ts', 'title': 'Invalid order', 'tests': [{'projectName': ''}]})
        self.page['scenarios'].append({
            'scenario_id': 'invalid-order', 'source_ids': ['REQ'], 'dimensions': ['validation_errors'],
            'expected': 'No write occurs', 'assertions': ['Specific error and no owned resource'],
            'case_ids': ['ORDER-INVALID']})
        self.page['review']['validation_errors'] = {
            'status': 'covered', 'scenario_ids': ['invalid-order']}

    def test_example_keeps_known_gaps_and_native_instance_counts(self):
        result = self.validate()
        self.assertEqual(result['status'], 'known_gaps')
        self.assertEqual(len(result['known_gaps']), 7)
        self.assertEqual(result['counts'], {'pages': 1, 'scenarios': 1, 'mapped_cases': 1,
                                           'discovered_instances': 2})

    def test_complete_declared_scope_and_absent_discovery(self):
        self.complete()
        self.assertEqual(self.validate()['status'], 'checked')
        result = self.validate(discovery=False)
        self.assertEqual(result['discovery'], 'not_supplied')
        self.assertNotIn('discovered_instances', result['counts'])

    def test_unrelated_ui_and_api_implementations_do_not_expand_scope(self):
        self.coverage['implementations']['unrelated'] = dict(self.implementation, case_id='OTHER')
        self.coverage['implementations']['api'] = dict(self.implementation, kind='api', suite_id='backend')
        self.assertEqual(self.validate()['counts']['mapped_cases'], 1)

    def test_api_implementation_cannot_replace_ui(self):
        self.implementation['kind'] = 'api'
        self.invalid('exactly one active UI implementation')

    def test_wrong_suite_retired_or_manual_mapping_fails(self):
        for field, value, message in [
                ('suite_id', 'backend', 'exactly one active'),
                ('implementation', 'retired', 'exactly one active'),
                ('implementation', 'draft', 'valid automation'),
                ('execution', 'manual', 'valid automation')]:
            with self.subTest(field=field, value=value):
                old = self.implementation[field]
                self.implementation[field] = value
                self.invalid(message)
                self.implementation[field] = old

    def test_unknown_retired_and_api_only_business_cases_fail(self):
        case = self.catalog['cases'].pop('ORDER-CREATE')
        self.invalid('unknown business case')
        self.catalog['cases']['ORDER-CREATE'] = dict(case, status='retired')
        self.invalid('case is retired')
        self.catalog['cases']['ORDER-CREATE'] = dict(case, required_kinds=['api'])
        self.invalid('require UI implementation')

    def test_duplicate_mapping_fails(self):
        self.coverage['implementations']['duplicate'] = dict(self.implementation)
        self.invalid('exactly one active UI')

    def test_independent_scenarios_need_independent_cases(self):
        self.add_scenario()
        self.page['scenarios'][1]['case_ids'] = ['ORDER-CREATE']
        self.invalid('distinct business cases')

    def test_independent_cases_need_independent_declarations_and_ids(self):
        self.add_scenario()
        self.coverage['implementations']['ui-invalid']['title'] = self.implementation['title']
        self.invalid('distinct test declarations')
        self.coverage['implementations']['ui-invalid']['title'] = 'Invalid order'
        self.coverage['implementations']['ui-invalid']['test_id'] = self.implementation['test_id']
        self.invalid('distinct test declarations')

    def test_missing_expected_assertions_or_source_fails(self):
        scenario = self.page['scenarios'][0]
        for field, value, message in [('expected', '', 'expected'), ('assertions', [], 'assertions'),
                                      ('source_ids', ['UNKNOWN'], 'unknown source')]:
            with self.subTest(field=field):
                original = scenario[field]
                scenario[field] = value
                self.invalid(message)
                scenario[field] = original

    def test_inventory_and_module_must_match_declared_pages(self):
        self.plan['inventory']['included'].append('missing')
        self.invalid('exactly match')
        self.plan['inventory']['included'].pop()
        self.page['module'] = 'unknown'
        self.invalid('unknown module')

    def test_scope_exclusion_requires_reason_and_cannot_overlap(self):
        item = self.plan['inventory']['excluded'][0]
        item['reason'] = ''
        self.invalid('excluded.reason')
        item.update(reason='outside scope', page_id='orders-list')
        self.invalid('included and excluded overlap')

    def test_every_dimension_is_assessed_and_references_are_consistent(self):
        self.page['review'].pop('keyboard')
        self.invalid('every supported dimension')
        self.page['review']['keyboard'] = {'status': 'not_applicable', 'reason': 'Not in scope'}
        self.page['review']['main_flow']['scenario_ids'] = ['unknown']
        self.invalid('unknown scenario')

    def test_inapplicable_and_blocked_need_reason(self):
        self.page['review']['keyboard'] = {'status': 'not_applicable'}
        self.invalid('keyboard.reason')
        self.page['review']['keyboard'] = {'status': 'blocked'}
        self.invalid('keyboard.reason')

    def test_not_applicable_cannot_claim_scenarios(self):
        self.page['review']['main_flow'].update(status='not_applicable', reason='Absent')
        self.invalid('not_applicable cannot claim')

    def test_discovery_missing_ambiguous_and_error_results_fail(self):
        spec = self.discovery['suites'][0]['specs'][0]
        spec['title'] = 'different'
        self.invalid('exactly one test declaration')
        spec['title'] = self.implementation['title']
        self.discovery['suites'][0]['specs'].append(copy.deepcopy(spec))
        self.invalid('exactly one test declaration')
        self.discovery['errors'] = [{'message': 'Discovery failed'}]
        self.invalid('complete Playwright')

    def test_explicit_unnamed_project_is_not_wildcard(self):
        self.implementation['project'] = ''
        self.invalid('exactly one test declaration')
        self.discovery['suites'][0]['specs'][0]['tests'].append({'projectName': ''})
        self.assertEqual(self.validate()['discovery'], 'checked')
        self.implementation['project'] = 'chromium'
        self.assertEqual(self.validate()['discovery'], 'checked')

    def test_nested_discovery_and_repeats_preserve_declaration(self):
        spec = self.discovery['suites'][0]['specs'].pop()
        spec['tests'] *= 2
        self.discovery['suites'][0]['suites'] = [{'specs': [spec]}]
        self.assertEqual(self.validate()['counts']['discovered_instances'], 4)

    def test_missing_or_external_implementation_file_fails(self):
        self.implementation['spec'] = 'missing.spec.ts'
        self.invalid('existing file inside project')
        self.implementation['spec'] = str(ROOT / 'SKILL.md')
        self.invalid('existing file inside project')

    def test_malformed_values_fail_without_traceback(self):
        for value in [None, True, [], 'main_flow']:
            with self.subTest(value=value):
                self.page['scenarios'][0]['dimensions'] = value
                self.invalid('dimensions')

    def test_cli_exit_codes_and_suite_binding(self):
        paths = {}
        for key, data in [('plan', self.plan), ('catalog', self.catalog),
                          ('coverage', self.coverage), ('discovery', self.discovery)]:
            path = self.project / (key + '.json')
            path.write_text(json.dumps(data), encoding='utf-8')
            paths[key] = path
        argv = ['--project', str(self.project)]
        for key, path in paths.items():
            argv.extend(['--' + key, str(path)])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(ui.main(argv), 2)
        self.assertEqual(json.loads(output.getvalue())['status'], 'known_gaps')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ui.main(argv + ['--allow-known-gaps']), 0)
            self.assertEqual(ui.main(argv + ['--suite-id', 'wrong-suite']), 1)
        self.complete()
        paths['plan'].write_text(json.dumps(self.plan), encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ui.main(argv), 0)


if __name__ == '__main__':
    unittest.main()
