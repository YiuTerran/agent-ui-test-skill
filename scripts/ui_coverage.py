#!/usr/bin/env python3
"""Check declared UI coverage against business cases and Playwright discovery."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys


DIMENSIONS = (
    'main_flow', 'validation_errors', 'loading_empty', 'persistence',
    'roles_permissions', 'filter_sort_pagination', 'keyboard', 'viewport', 'visual'
)


class CoverageError(ValueError):
    pass


class Validator:
    def __init__(self, project, plan, catalog, coverage, discovery=None, suite_id=None):
        self.project = Path(project).resolve()
        self.plan = plan
        self.catalog = catalog
        self.coverage = coverage
        self.discovery = discovery
        self.suite_id = suite_id
        self.errors = []
        self.gaps = []
        self.counts = Counter()
        self.case_owners = {}

    def require(self, condition, path, message):
        if not condition:
            self.errors.append(f'{path}: {message}')
        return bool(condition)

    def text(self, value, path):
        return self.require(isinstance(value, str) and bool(value.strip()), path,
                            'must be a nonempty string')

    def strings(self, value, path, nonempty=True):
        if not self.require(isinstance(value, list) and
                            all(isinstance(item, str) and item.strip() for item in value),
                            path, 'must be a list of nonempty strings'):
            return []
        self.require(not nonempty or bool(value), path, 'must not be empty')
        self.require(len(set(value)) == len(value), path, 'must be unique')
        return value

    def objects(self, value, path, nonempty=True):
        if not self.require(isinstance(value, list) and all(isinstance(item, dict) for item in value),
                            path, 'must be a list of objects'):
            return []
        self.require(not nonempty or bool(value), path, 'must not be empty')
        return value

    def checkpoint(self):
        if self.errors:
            raise CoverageError('; '.join(self.errors))

    def refs(self, value, path, sources):
        refs = self.strings(value, path)
        self.require(set(refs) <= sources, path, 'unknown source reference')

    def inventory(self):
        sources = {}
        for source in self.objects(self.plan.get('sources'), 'sources'):
            source_id = source.get('source_id')
            if not self.text(source_id, 'sources.source_id'):
                continue
            self.require(source_id not in sources, 'sources.source_id', 'duplicate source')
            for field in ('kind', 'revision', 'fingerprint'):
                self.text(source.get(field), f'sources[{source_id}].{field}')
            self.strings(source.get('locations'), f'sources[{source_id}].locations')
            sources[source_id] = source
        inventory = self.plan.get('inventory')
        if not self.require(isinstance(inventory, dict), 'inventory', 'must be an object'):
            return {}, set()
        self.text(inventory.get('method'), 'inventory.method')
        modules = self.strings(inventory.get('modules'), 'inventory.modules')
        included = self.strings(inventory.get('included'), 'inventory.included')
        excluded = []
        for item in self.objects(inventory.get('excluded'), 'inventory.excluded', False):
            if self.text(item.get('page_id'), 'inventory.excluded.page_id'):
                excluded.append(item['page_id'])
            self.text(item.get('reason'), 'inventory.excluded.reason')
        self.require(len(excluded) == len(set(excluded)), 'inventory.excluded', 'duplicate page')
        self.require(not set(included) & set(excluded), 'inventory', 'included and excluded overlap')
        pages = {}
        for page in self.objects(self.plan.get('pages'), 'pages'):
            page_id = page.get('page_id')
            if not self.text(page_id, 'pages.page_id'):
                continue
            self.require(page_id not in pages, 'pages.page_id', 'duplicate page')
            pages[page_id] = page
            self.require(isinstance(page.get('module'), str) and page['module'] in modules,
                         f'pages[{page_id}].module', 'unknown module')
            self.text(page.get('route_key'), f'pages[{page_id}].route_key')
            self.refs(page.get('source_ids'), f'pages[{page_id}].source_ids', set(sources))
        self.require(set(included) == set(pages), 'inventory.included',
                     'must exactly match declared page rows')
        self.counts['pages'] = len(pages)
        return pages, set(sources)

    def scenarios(self, pages, sources):
        scenario_ids = set()
        for page_id, page in pages.items():
            path = f'pages[{page_id}]'
            scenarios = {}
            scenario_dimensions = {}
            for scenario in self.objects(page.get('scenarios'), path + '.scenarios'):
                scenario_id = scenario.get('scenario_id')
                if not self.text(scenario_id, path + '.scenario_id'):
                    continue
                self.require(scenario_id not in scenario_ids, path + '.scenario_id', 'duplicate scenario')
                scenario_ids.add(scenario_id)
                scenarios[scenario_id] = scenario
                location = f'{path}.scenarios[{scenario_id}]'
                self.text(scenario.get('expected'), location + '.expected')
                self.strings(scenario.get('assertions'), location + '.assertions')
                self.refs(scenario.get('source_ids'), location + '.source_ids', sources)
                dimensions = self.strings(scenario.get('dimensions'), location + '.dimensions')
                scenario_dimensions[scenario_id] = set(dimensions)
                self.require(set(dimensions) <= set(DIMENSIONS), location + '.dimensions',
                             'unknown dimension')
                for case_id in self.strings(scenario.get('case_ids'), location + '.case_ids'):
                    self.require(case_id not in self.case_owners, location + '.case_ids',
                                 'independent scenarios must use distinct business cases')
                    self.case_owners[case_id] = scenario_id
            review = page.get('review')
            if not self.require(isinstance(review, dict), path + '.review', 'must be an object'):
                continue
            self.require(set(review) == set(DIMENSIONS), path + '.review',
                         'must assess every supported dimension without unknown keys')
            for dimension in DIMENSIONS:
                assessment = review.get(dimension)
                location = path + '.review.' + dimension
                if not self.require(isinstance(assessment, dict), location, 'must be an object'):
                    continue
                status = assessment.get('status')
                self.require(isinstance(status, str) and status in
                             {'covered', 'blocked', 'not_applicable'}, location + '.status',
                             'must be covered, blocked or not_applicable')
                refs = self.strings(assessment.get('scenario_ids', []),
                                    location + '.scenario_ids', status == 'covered')
                self.require(set(refs) <= set(scenarios), location, 'unknown scenario reference')
                declared = {identifier for identifier, dimensions in scenario_dimensions.items()
                            if dimension in dimensions}
                self.require(set(refs) == declared, location,
                             'scenario references must match their declared dimension')
                if status != 'covered':
                    self.text(assessment.get('reason'), location + '.reason')
                if status == 'not_applicable':
                    self.require(not refs, location, 'not_applicable cannot claim scenarios')
                if status == 'blocked':
                    self.gaps.append({'page_id': page_id, 'dimension': dimension,
                                      'reason': assessment.get('reason')})
            self.counts['scenarios'] += len(scenarios)

    def normalize(self, value, root=None):
        if not isinstance(value, str) or not value.strip():
            return ''
        path = Path(value.replace('\\', '/'))
        if not path.is_absolute():
            path = (root or self.project) / path
        return os.path.normcase(str(path.resolve()))

    def discovery_rows(self):
        data = self.discovery
        if not self.require(isinstance(data, dict) and isinstance(data.get('suites'), list)
                            and not data.get('errors'), 'discovery',
                            'must be complete Playwright --list JSON without errors'):
            return []
        config = data.get('config', {})
        if not self.require(isinstance(config, dict), 'discovery.config', 'must be an object'):
            return []
        raw_root = config.get('rootDir', str(self.project))
        if not self.text(raw_root, 'discovery.config.rootDir'):
            return []
        root = Path(self.normalize(raw_root))
        self.require(root.is_relative_to(self.project), 'discovery.config.rootDir',
                     'must be inside project')
        rows = []
        serial = 0

        def walk(suite):
            nonlocal serial
            if not self.require(isinstance(suite, dict), 'discovery.suites', 'must contain objects'):
                return
            for spec in self.objects(suite.get('specs', []), 'discovery.specs', False):
                serial += 1
                self.text(spec.get('file'), 'discovery.spec.file')
                self.text(spec.get('title'), 'discovery.spec.title')
                for test in self.objects(spec.get('tests'), 'discovery.tests'):
                    name = test.get('projectName', '')
                    self.require(isinstance(name, str), 'discovery.projectName', 'must be a string')
                    rows.append({'file': self.normalize(spec.get('file'), root),
                                 'title': spec.get('title'), 'project': name, 'declaration': serial})
            for child in self.objects(suite.get('suites', []), 'discovery.suites', False):
                walk(child)

        for suite in data['suites']:
            walk(suite)
        self.counts['discovered_instances'] = len(rows)
        return rows

    def mappings(self):
        if not self.require(isinstance(self.catalog, dict) and
                            isinstance(self.catalog.get('cases'), dict), 'catalog', 'must contain cases'):
            return
        if not self.require(isinstance(self.coverage, dict) and
                            isinstance(self.coverage.get('implementations'), dict),
                            'coverage', 'must contain implementations'):
            return
        discovered = self.discovery_rows() if self.discovery is not None else None
        bindings, test_ids = set(), set()
        for case_id in sorted(self.case_owners):
            path = f'case_ids[{case_id}]'
            case = self.catalog['cases'].get(case_id)
            if not self.require(isinstance(case, dict), path, 'unknown business case'):
                continue
            self.require(case.get('status', 'active') == 'active', path, 'business case is retired')
            kinds = case.get('required_kinds')
            self.require(isinstance(kinds, list) and 'ui' in kinds, path,
                         'business case must require UI implementation')
            matches = [item for item in self.coverage['implementations'].values()
                       if isinstance(item, dict) and item.get('case_id') == case_id
                       and item.get('suite_id') == self.plan['suite_id'] and item.get('kind') == 'ui'
                       and item.get('implementation') != 'retired']
            if not self.require(len(matches) == 1, path,
                                'must map to exactly one active UI implementation in this suite'):
                continue
            implementation = matches[0]
            self.require(implementation.get('implementation') == 'valid' and
                         implementation.get('execution') == 'automation', path,
                         'implementation must be valid automation')
            spec, title, test_id = (implementation.get(key) for key in ('spec', 'title', 'test_id'))
            for field, value in (('spec', spec), ('title', title), ('test_id', test_id)):
                self.text(value, path + '.' + field)
            normalized = self.normalize(spec)
            self.require(bool(normalized) and Path(normalized).is_relative_to(self.project)
                         and Path(normalized).is_file(), path + '.spec',
                         'must reference an existing file inside project')
            if not isinstance(title, str) or not isinstance(test_id, str):
                continue
            binding = (normalized, title)
            self.require(binding not in bindings and test_id not in test_ids, path,
                         'independent cases must map to distinct test declarations and test_ids')
            bindings.add(binding)
            test_ids.add(test_id)
            if discovered is not None:
                project = implementation.get('project', implementation.get('projectName'))
                self.require(project is None or isinstance(project, str), path + '.project',
                             'must be a string when supplied')
                rows = [row for row in discovered if row['file'] == normalized and row['title'] == title
                        and (project is None or row['project'] == project)]
                self.require(len({row['declaration'] for row in rows}) == 1, path,
                             'must match exactly one test declaration in complete discovery')
        self.counts['mapped_cases'] = len(self.case_owners)

    def run(self):
        self.require(self.project.is_dir(), 'project', 'must be an existing directory')
        if not self.require(isinstance(self.plan, dict) and
                            type(self.plan.get('schema_version')) is int and
                            self.plan['schema_version'] == 1, 'plan.schema_version', 'must equal 1'):
            self.checkpoint()
        self.text(self.plan.get('suite_id'), 'plan.suite_id')
        if self.suite_id is not None:
            self.require(self.suite_id == self.plan.get('suite_id'), 'plan.suite_id',
                         'does not match --suite-id')
        pages, sources = self.inventory()
        self.checkpoint()
        self.scenarios(pages, sources)
        self.checkpoint()
        self.mappings()
        self.checkpoint()
        return {'status': 'known_gaps' if self.gaps else 'checked',
                'suite_id': self.plan['suite_id'], 'counts': dict(sorted(self.counts.items())),
                'known_gaps': self.gaps,
                'discovery': 'checked' if self.discovery is not None else 'not_supplied',
                'scope_note': 'Checks declared scope and mappings only; does not prove semantic completeness, authorization or runtime success.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ('plan', 'catalog', 'coverage'):
        parser.add_argument('--' + field, required=True)
    parser.add_argument('--project', default='.')
    parser.add_argument('--discovery')
    parser.add_argument('--suite-id')
    parser.add_argument('--allow-known-gaps', action='store_true')
    args = parser.parse_args(argv)
    try:
        def load(path):
            return json.loads(Path(path).read_text(encoding='utf-8'))
        result = Validator(args.project, load(args.plan), load(args.catalog), load(args.coverage),
                           load(args.discovery) if args.discovery else None, args.suite_id).run()
    except (OSError, ValueError) as error:
        print(json.dumps({'status': 'invalid', 'error': str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result['status'] == 'known_gaps' and not args.allow_known_gaps else 0


if __name__ == '__main__':
    sys.exit(main())
