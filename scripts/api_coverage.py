#!/usr/bin/env python3
"""Validate API coverage plans against suite mappings and Playwright discovery."""
import argparse
from collections import Counter
import hashlib
import itertools
import json
from pathlib import Path
import sys


NEGATIVE_CATEGORIES = {
    'validation', 'authentication', 'authorization', 'not_found', 'conflict',
    'state', 'dependency', 'malformed', 'rate_limit', 'idempotency',
    'business_rule', 'other'
}
HTTP_METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'TRACE'}


def endpoint_key(service, method, route):
    return f'{service}:{method}:{route}'


def openapi_inventory(path, service):
    raw = Path(path).read_bytes()
    document = json.loads(raw)
    if not isinstance(document, dict) or not (
            str(document.get('openapi', '')).startswith('3.') or document.get('swagger') == '2.0'):
        raise ValueError('expected an OpenAPI 3.x or Swagger 2.0 JSON snapshot')
    paths = document.get('paths')
    if not isinstance(paths, dict) or not paths:
        raise ValueError('OpenAPI paths must be a nonempty object')
    endpoints = set()
    for route, item in paths.items():
        if not isinstance(route, str) or not route.startswith('/') or not isinstance(item, dict):
            raise ValueError('invalid OpenAPI path item')
        if '$ref' in item:
            raise ValueError('resolve path-item references before exporting the inventory')
        for method, operation in item.items():
            if method.upper() in HTTP_METHODS:
                if not isinstance(operation, dict):
                    raise ValueError('invalid OpenAPI operation')
                endpoints.add(endpoint_key(service, method.upper(), route))
    if not endpoints:
        raise ValueError('OpenAPI snapshot has no HTTP operations')
    return endpoints, hashlib.sha256(raw).hexdigest()


class CoverageError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__('; '.join(errors))


class Validator:
    def __init__(self, project, plan, catalog, coverage, discovery=None, suite_id=None, contracts=None):
        self.project = Path(project).resolve()
        self.plan = plan
        self.catalog = catalog
        self.coverage = coverage
        self.discovery = discovery
        self.requested_suite_id = suite_id
        self.errors = []
        self.gaps = []
        self.case_ids = set()
        self.counts = Counter()
        self.endpoints = {}
        self.source_kinds = {}
        self.backend_code_access = False
        self.inventory_included = set()
        self.contracts = contracts or {}
        self.inventory_excluded = set()
        self.scenario_case_owners = {}

    def require(self, condition, path, message):
        if not condition:
            self.errors.append(f'{path}: {message}')
        return condition

    def text(self, value, path):
        return self.require(isinstance(value, str) and bool(value.strip()), path, 'must be a nonempty string')

    def choice(self, value, allowed):
        return isinstance(value, str) and value in allowed

    def gap(self, kind, identifier, reason, **details):
        self.gaps.append({'kind': kind, 'id': identifier, 'reason': reason, **details})

    def checkpoint(self):
        if self.errors:
            raise CoverageError(self.errors)

    def string_list(self, value, path, nonempty=True):
        valid = isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)
        self.require(valid and (not nonempty or bool(value)), path,
                     'must be a nonempty list of strings' if nonempty else 'must be a list of strings')
        if not valid:
            return []
        self.require(len(value) == len(set(value)), path, 'entries must be unique')
        return value

    def object_list(self, value, path, nonempty=True):
        valid = isinstance(value, list) and all(isinstance(item, dict) for item in value)
        self.require(valid and (not nonempty or bool(value)), path,
                     'must be a nonempty list of objects' if nonempty else 'must be a list of objects')
        return value if valid else []

    def identifiers(self, value, path):
        values = self.string_list(value, path)
        self.case_ids.update(values)
        return values

    def source_refs(self, value, path, require_contract=False):
        refs = self.string_list(value, path)
        kinds = set()
        for source_id in refs:
            self.require(source_id in self.source_kinds, path, f'unknown source_id {source_id}')
            if source_id in self.source_kinds:
                kinds.add(self.source_kinds[source_id])
        if require_contract:
            self.require(bool(kinds & {'requirements', 'openapi', 'contract'}), path,
                         'must cite a requirement or API contract source')
        return kinds

    def validate_sources(self):
        inventory = self.plan.get('inventory')
        if not self.require(isinstance(inventory, dict), 'inventory', 'must be an object'):
            inventory = {}
        self.text(inventory.get('method'), 'inventory.method')
        services = self.string_list(inventory.get('services'), 'inventory.services')
        included = self.string_list(inventory.get('included'), 'inventory.included')
        excluded = self.object_list(inventory.get('excluded'), 'inventory.excluded', nonempty=False)
        excluded_ids = set()
        for index, row in enumerate(excluded):
            path = f'inventory.excluded[{index}]'
            endpoint_id = row.get('endpoint_id')
            self.text(endpoint_id, path + '.endpoint_id')
            if isinstance(endpoint_id, str):
                self.require(endpoint_id not in excluded_ids, path + '.endpoint_id', 'duplicate endpoint_id')
                excluded_ids.add(endpoint_id)
            self.text(row.get('reason'), path + '.reason')
        self.require(not set(included) & excluded_ids, 'inventory',
                     'an endpoint cannot be both included and excluded')
        code_access = inventory.get('backend_code_access')
        self.require(type(code_access) is bool, 'inventory.backend_code_access', 'must be boolean')
        if code_access is False:
            self.text(inventory.get('backend_unavailable_reason'), 'inventory.backend_unavailable_reason')

        sources = self.object_list(self.plan.get('sources'), 'sources')
        source_ids = set()
        for index, source in enumerate(sources):
            path = f'sources[{index}]'
            source_id = source.get('source_id')
            self.text(source_id, path + '.source_id')
            if isinstance(source_id, str):
                self.require(source_id not in source_ids, path + '.source_id', 'duplicate source_id')
                source_ids.add(source_id)
            kind = source.get('kind')
            self.require(self.choice(kind, {'requirements', 'openapi', 'backend_code', 'contract'}), path + '.kind',
                         'unsupported source kind')
            if isinstance(source_id, str) and self.choice(kind, {'requirements', 'openapi', 'backend_code', 'contract'}):
                self.source_kinds[source_id] = kind
            self.text(source.get('revision'), path + '.revision')
            self.text(source.get('fingerprint'), path + '.fingerprint')
            self.string_list(source.get('locations'), path + '.locations')

        self.require(any(kind == 'requirements' for kind in self.source_kinds.values()),
                     'sources', 'include a requirements source')
        if code_access is True:
            self.require(any(kind == 'backend_code' for kind in self.source_kinds.values()),
                         'sources', 'include a backend_code source when backend code is available')
        elif code_access is False:
            self.require(not any(kind == 'backend_code' for kind in self.source_kinds.values()),
                         'sources', 'do not claim backend_code evidence when access is unavailable')
        self.backend_code_access = code_access is True
        self.inventory_included = set(included)
        self.inventory_excluded = excluded_ids
        for index, row in enumerate(excluded):
            self.source_refs(row.get('source_ids'), f'inventory.excluded[{index}].source_ids')
        assessment = inventory.get('cross_service_assessment', {})
        status = assessment.get('status') if isinstance(assessment, dict) else None
        self.require(self.choice(status, {'covered', 'not_applicable', 'blocked'}),
                     'inventory.cross_service_assessment.status', 'assess cross-service journeys')
        if status != 'covered':
            self.text(assessment.get('reason') if isinstance(assessment, dict) else None,
                      'inventory.cross_service_assessment.reason')
        if status == 'blocked':
            self.gap('journey_inventory', 'cross_service', assessment.get('reason'))
        self.cross_service_status = status
        return set(services)

    def validate_assertions(self, row, path):
        self.text(row.get('expected'), path + '.expected')
        self.string_list(row.get('assertions'), path + '.assertions')
        return self.identifiers(row.get('case_ids'), path + '.case_ids')

    def scenarios(self, endpoint, field, path, negative=False, seen=None):
        rows = self.object_list(endpoint.get(field), path, nonempty=not negative)
        seen = seen if seen is not None else set()
        for index, row in enumerate(rows):
            item_path = f'{path}[{index}]'
            scenario_id = row.get('scenario_id')
            self.text(scenario_id, item_path + '.scenario_id')
            if isinstance(scenario_id, str):
                self.require(scenario_id not in seen, item_path + '.scenario_id', 'duplicate scenario_id')
                seen.add(scenario_id)
            case_ids = self.validate_assertions(row, item_path)
            for case_id in case_ids:
                self.require(case_id not in self.scenario_case_owners, item_path + '.case_ids',
                             'independent endpoint scenarios must use distinct cases')
                self.scenario_case_owners[case_id] = scenario_id
            if negative:
                self.require(self.choice(row.get('category'), NEGATIVE_CATEGORIES), item_path + '.category',
                             'unsupported negative category')
            self.counts['negative_scenarios' if negative else 'positive_scenarios'] += 1
        return rows

    def validate_negative_review(self, endpoint, path):
        review = endpoint.get('negative_review')
        if not self.require(isinstance(review, dict), path + '.negative_review', 'must be an object'):
            review = {}
        self.text(review.get('method'), path + '.negative_review.method')
        review_status = review.get('status')
        self.require(self.choice(review_status, {'complete', 'partial', 'not_applicable'}),
                     path + '.negative_review.status', 'must be complete, partial, or not_applicable')
        rows = self.object_list(review.get('categories'), path + '.negative_review.categories')
        observed = set()
        mapped = set()
        for index, row in enumerate(rows):
            row_path = f'{path}.negative_review.categories[{index}]'
            category, disposition = row.get('category'), row.get('disposition')
            self.require(self.choice(category, NEGATIVE_CATEGORIES), row_path + '.category',
                         'unsupported negative category')
            if self.choice(category, NEGATIVE_CATEGORIES):
                self.require(category not in observed, row_path + '.category', 'duplicate category')
                observed.add(category)
            self.require(self.choice(disposition, {'covered', 'not_applicable', 'blocked'}), row_path + '.disposition',
                         'must be covered, not_applicable, or blocked')
            if disposition == 'covered':
                scenario_ids = self.string_list(row.get('scenario_ids'), row_path + '.scenario_ids')
                self.require(all(scenario_id in self.negative_by_id for scenario_id in scenario_ids),
                             row_path + '.scenario_ids', 'must reference negative scenarios on this endpoint')
                self.require(all(self.negative_by_id.get(scenario_id, {}).get('category') == category
                                 for scenario_id in scenario_ids), row_path + '.scenario_ids',
                             'all referenced scenarios must belong to this category')
                mapped.update(scenario_ids)
            else:
                self.text(row.get('reason'), row_path + '.reason')
                if disposition == 'blocked':
                    self.gaps.append({'kind': 'negative_category', 'id': category, 'reason': row.get('reason')})
            self.counts['negative_categories'] += 1
        if self.choice(review_status, {'complete', 'partial'}):
            self.require(bool(rows), path + '.negative_review.categories',
                         'review at least one applicable negative category')
            for scenario in self.negative_by_id.values():
                if isinstance(scenario, dict):
                    self.require(scenario.get('category') in observed, path + '.negative_review.categories',
                                 f"negative scenario category {scenario.get('category')} is not assessed")
        else:
            self.require(not self.negative_by_id, path + '.negative_scenarios',
                         'negative scenarios require a reviewed negative assessment')
            self.text(review.get('reason'), path + '.negative_review.reason')
            self.require(all(row.get('disposition') == 'not_applicable' for row in rows),
                         path + '.negative_review.categories', 'not_applicable cannot contain applicable categories')
        self.require(mapped == set(self.negative_by_id), path + '.negative_review.categories',
                     'every negative scenario must map to a covered category')
        if review_status == 'complete':
            self.require(not any(row.get('disposition') == 'blocked' for row in rows),
                         path + '.negative_review.status', 'complete cannot contain blocked categories')
        if review_status == 'partial' or any(row.get('disposition') == 'blocked' for row in rows):
            self.text(review.get('reason'), path + '.negative_review.reason')
            self.gaps.append({'kind': 'negative_scenarios', 'id': path,
                              'reason': review.get('reason') or 'one or more applicable negative categories are blocked'})

    def validate_branches(self, endpoint, path):
        assessment = endpoint.get('branch_assessment')
        if not self.require(isinstance(assessment, dict), path + '.branch_assessment', 'must be an object'):
            assessment = {}
        completeness = assessment.get('completeness')
        self.require(self.choice(completeness, {'complete', 'partial', 'not_applicable'}),
                     path + '.branch_assessment.completeness', 'unsupported branch assessment')
        self.text(assessment.get('method'), path + '.branch_assessment.method')
        refs = self.source_refs(assessment.get('source_ids'), path + '.branch_assessment.source_ids')
        if self.backend_code_access:
            self.require('backend_code' in refs or completeness == 'not_applicable',
                         path + '.branch_assessment.source_ids', 'branch review must cite backend source code')
        if completeness != 'complete':
            self.text(assessment.get('reason'), path + '.branch_assessment.reason')
            if completeness == 'partial':
                self.gaps.append({'kind': 'branch_inventory', 'id': path, 'reason': assessment.get('reason')})
        if not self.backend_code_access and completeness == 'complete':
            self.gap('source_branch_review', path, 'contract scenarios checked; implementation branches unavailable')

        rows = self.object_list(endpoint.get('branches'), path + '.branches', nonempty=False)
        branch_ids = set()
        endpoint_scenario_ids = {row.get('scenario_id') for row in self.positive_rows + self.negative_rows}
        endpoint_scenario_kinds = {row.get('scenario_id'): kind
                                   for kind, scenarios in (('positive', self.positive_rows),
                                                           ('negative', self.negative_rows))
                                   for row in scenarios}
        mapped_scenario_ids = set()
        for index, branch in enumerate(rows):
            item_path = f'{path}.branches[{index}]'
            branch_id = branch.get('branch_id')
            self.text(branch_id, item_path + '.branch_id')
            if isinstance(branch_id, str):
                self.require(branch_id not in branch_ids, item_path + '.branch_id', 'duplicate branch_id')
                branch_ids.add(branch_id)
            self.text(branch.get('description'), item_path + '.description')
            self.source_refs(branch.get('source_ids'), item_path + '.source_ids')
            self.text(branch.get('reachability'), item_path + '.reachability')
            disposition = branch.get('disposition')
            self.require(self.choice(disposition, {'covered', 'blocked', 'not_applicable'}),
                         item_path + '.disposition',
                         'must be covered, blocked, or not_applicable')
            outcome = branch.get('outcome')
            self.require(self.choice(outcome, {'positive', 'negative', 'alternate', 'side_effect', 'error'}),
                         item_path + '.outcome',
                         'must classify the branch as positive, negative, alternate, side_effect, or error')
            if disposition == 'covered':
                scenario_ids = self.string_list(branch.get('scenario_ids'), item_path + '.scenario_ids')
                self.require(set(scenario_ids) <= endpoint_scenario_ids, item_path + '.scenario_ids',
                             'must reference endpoint scenarios')
                expected_kind = 'negative' if self.choice(outcome, {'negative', 'error'}) else 'positive'
                self.require(all(endpoint_scenario_kinds.get(scenario_id) == expected_kind
                                 for scenario_id in scenario_ids), item_path + '.scenario_ids',
                             f'{outcome} branch must map to {expected_kind} scenarios')
                mapped_scenario_ids.update(scenario_ids)
            else:
                self.text(branch.get('reason'), item_path + '.reason')
                self.require(not branch.get('scenario_ids'), item_path + '.scenario_ids',
                             'uncovered branches cannot claim scenarios')
                if disposition == 'blocked':
                    self.gaps.append({'kind': 'branch', 'id': branch_id, 'reason': branch.get('reason')})
            if completeness == 'complete':
                self.require(disposition != 'blocked', item_path + '.disposition',
                             'a complete assessment cannot contain blocked branches')
            self.counts['branches'] += 1
            if isinstance(disposition, str):
                self.counts['branches_' + disposition] += 1
        if completeness == 'not_applicable':
            self.require(not rows, path + '.branches', 'must be empty when branch logic is not applicable')
        if completeness == 'complete':
            self.require(bool(rows), path + '.branches',
                         'a complete branch assessment must inventory at least one branch')
            self.require(mapped_scenario_ids == endpoint_scenario_ids, path + '.branches',
                         'every positive and negative scenario must map to an inventoried branch')

    def validate_dynamic(self, endpoint, path):
        supports = endpoint.get('supports_dynamic_parameters')
        self.require(type(supports) is bool, path + '.supports_dynamic_parameters', 'must be boolean')
        dynamic = endpoint.get('dynamic_parameters')
        if supports is not True:
            self.require(dynamic in (None, {}), path + '.dynamic_parameters',
                         'must be omitted for a fixed-parameter endpoint')
            return
        if not self.require(isinstance(dynamic, dict), path + '.dynamic_parameters',
                            'required for dynamic parameters'):
            return
        strategy = dynamic.get('strategy')
        self.require(self.choice(strategy, {'risk_based', 'pairwise', 'decision_table', 'full'}),
                     path + '.dynamic_parameters.strategy', 'unsupported combination strategy')
        self.text(dynamic.get('strategy_reason'), path + '.dynamic_parameters.strategy_reason')
        dimensions = self.object_list(dynamic.get('dimensions'), path + '.dynamic_parameters.dimensions')
        dimension_values = {}
        for index, dimension in enumerate(dimensions):
            dim_path = f'{path}.dynamic_parameters.dimensions[{index}]'
            name = dimension.get('name')
            self.text(name, dim_path + '.name')
            self.text(dimension.get('rationale'), dim_path + '.rationale')
            if not isinstance(name, str):
                continue
            self.require(name not in dimension_values, dim_path + '.name', 'duplicate dimension')
            representatives = self.object_list(dimension.get('representatives'), dim_path + '.representatives')
            labels = set()
            for value_index, representative in enumerate(representatives):
                value_path = f'{dim_path}.representatives[{value_index}]'
                label = representative.get('value')
                self.text(label, value_path + '.value')
                self.text(representative.get('meaning'), value_path + '.meaning')
                if isinstance(label, str):
                    self.require(label not in labels, value_path + '.value', 'duplicate representative value')
                    labels.add(label)
            dimension_values[name] = labels
            self.counts['dynamic_dimensions'] += 1
        self.checkpoint()
        selected = self.object_list(dynamic.get('selected_scenarios'),
                                    path + '.dynamic_parameters.selected_scenarios')
        exercised = {name: set() for name in dimension_values}
        selected_kinds = set()
        scenario_ids = set()
        selected_values = []
        endpoint_scenarios = {row.get('scenario_id'): row for row in self.positive_rows + self.negative_rows}
        for index, scenario in enumerate(selected):
            item_path = f'{path}.dynamic_parameters.selected_scenarios[{index}]'
            scenario_id = scenario.get('scenario_id')
            self.text(scenario_id, item_path + '.scenario_id')
            if isinstance(scenario_id, str):
                self.require(scenario_id not in scenario_ids, item_path + '.scenario_id', 'duplicate scenario_id')
                scenario_ids.add(scenario_id)
            target = endpoint_scenarios.get(scenario_id) if isinstance(scenario_id, str) else None
            self.require(target is not None, item_path + '.scenario_id',
                         'must reference a positive or negative scenario on this endpoint')
            kind = scenario.get('kind')
            self.require(self.choice(kind, {'typical', 'boundary', 'interaction', 'invalid_combination'}),
                         item_path + '.kind', 'unsupported representative scenario kind')
            if self.choice(kind, {'typical', 'boundary', 'interaction', 'invalid_combination'}):
                selected_kinds.add(kind)
            selections = scenario.get('values')
            if not self.require(isinstance(selections, dict), item_path + '.values',
                                'must map every parameter dimension to a representative'):
                selections = {}
            self.require(set(selections) == set(dimension_values), item_path + '.values',
                         'must specify exactly the declared dimensions')
            if set(selections) == set(dimension_values) and all(
                    isinstance(label, str) and label in dimension_values[name]
                    for name, label in selections.items()):
                selected_values.append(selections)
            for name, label in selections.items():
                self.require(name in dimension_values and isinstance(label, str)
                             and label in dimension_values.get(name, set()),
                             item_path + '.values', f'unknown dimension value {name}={label}')
                if name in exercised and isinstance(label, str):
                    exercised[name].add(label)
            case_ids = self.identifiers(scenario.get('case_ids'), item_path + '.case_ids')
            if target:
                self.require(set(case_ids) <= set(target.get('case_ids', [])), item_path + '.case_ids',
                             'dynamic scenario must use its endpoint scenario cases')
                if kind == 'invalid_combination':
                    self.require(scenario_id in self.negative_by_id, item_path + '.kind',
                                 'invalid combination must reference a negative scenario')
            self.text(scenario.get('rationale'), item_path + '.rationale')
            self.string_list(scenario.get('assertions'), item_path + '.assertions')
            self.counts['dynamic_scenarios'] += 1
        self.checkpoint()
        self.require('typical' in selected_kinds, path + '.dynamic_parameters.selected_scenarios',
                     'include a typical representative scenario')
        for assessment_name, scenario_kind in (
                ('boundary_assessment', 'boundary'), ('interaction_assessment', 'interaction')):
            assessment = dynamic.get(assessment_name, {})
            status = assessment.get('status') if isinstance(assessment, dict) else None
            self.require(self.choice(status, {'covered', 'not_applicable', 'blocked'}),
                         path + '.dynamic_parameters.' + assessment_name, 'assess representative coverage')
            if status == 'covered':
                self.require(scenario_kind in selected_kinds, path + '.dynamic_parameters.selected_scenarios',
                             f'{assessment_name} requires a matching selected scenario')
            else:
                self.text(assessment.get('reason') if isinstance(assessment, dict) else None,
                          path + '.dynamic_parameters.' + assessment_name + '.reason')
                self.require(scenario_kind not in selected_kinds, path + '.dynamic_parameters.selected_scenarios',
                             f'{scenario_kind} scenarios contradict their assessment')
                if status == 'blocked':
                    self.gap(assessment_name, path, assessment.get('reason'))
        uncovered = self.object_list(dynamic.get('uncovered_values'),
                                     path + '.dynamic_parameters.uncovered_values', nonempty=False)
        declared_uncovered = set()
        for index, row in enumerate(uncovered):
            item_path = f'{path}.dynamic_parameters.uncovered_values[{index}]'
            name, value = row.get('dimension'), row.get('value')
            self.require(name in dimension_values and value in dimension_values.get(name, set()), item_path,
                         'references an unknown representative')
            self.text(row.get('reason'), item_path + '.reason')
            declared_uncovered.add((name, value))
            self.require(value not in exercised.get(name, set()), item_path,
                         'an exercised representative cannot also be uncovered')
            self.gaps.append({'kind': 'dynamic_value', 'id': f'{name}={value}', 'reason': row.get('reason')})
        for name, labels in dimension_values.items():
            for label in labels - exercised.get(name, set()):
                self.require((name, label) in declared_uncovered,
                             path + '.dynamic_parameters.uncovered_values',
                             f'{name}={label} must be selected or explicitly documented as uncovered')
        interactions = self.object_list(dynamic.get('uncovered_interactions'),
                                        path + '.dynamic_parameters.uncovered_interactions', nonempty=False)
        for index, row in enumerate(interactions):
            item_path = f'{path}.dynamic_parameters.uncovered_interactions[{index}]'
            self.text(row.get('description'), item_path + '.description')
            self.text(row.get('reason'), item_path + '.reason')
            self.gaps.append({'kind': 'dynamic_interaction', 'id': row.get('description'),
                              'reason': row.get('reason')})
        required = self.object_list(dynamic.get('required_interactions'),
                                    path + '.dynamic_parameters.required_interactions', nonempty=False)
        for index, row in enumerate(required):
            item_path = f'{path}.dynamic_parameters.required_interactions[{index}]'
            values = row.get('values', {})
            valid = isinstance(values, dict) and len(values) >= 2 and all(
                name in dimension_values and isinstance(value, str) and value in dimension_values[name]
                for name, value in values.items())
            self.require(valid, item_path + '.values', 'must specify at least two known dimension values')
            self.text(row.get('rationale'), item_path + '.rationale')
            self.source_refs(row.get('source_ids'), item_path + '.source_ids')
            status = row.get('status')
            self.require(self.choice(status, {'covered', 'blocked'}), item_path + '.status',
                         'must be covered or blocked')
            if status == 'covered':
                refs = self.string_list(row.get('scenario_ids'), item_path + '.scenario_ids')
                matching = {row.get('scenario_id') for row in selected
                            if isinstance(row.get('values'), dict) and valid
                            and all(row['values'].get(name) == value for name, value in values.items())}
                self.require(set(refs) <= matching, item_path + '.scenario_ids',
                             'referenced selections must exercise the required interaction')
            else:
                self.text(row.get('reason'), item_path + '.reason')
                self.gap('required_interaction', item_path, row.get('reason'))
        self.validate_combination_strategy(dynamic, dimension_values, selected_values, path)
        invalid_review = dynamic.get('invalid_combination_assessment')
        if not self.require(isinstance(invalid_review, dict),
                             path + '.dynamic_parameters.invalid_combination_assessment', 'must be an object'):
            invalid_review = {}
        invalid_status = invalid_review.get('status')
        self.require(self.choice(invalid_status, {'covered', 'not_applicable', 'blocked'}),
                     path + '.dynamic_parameters.invalid_combination_assessment.status',
                     'must be covered, not_applicable, or blocked')
        if invalid_status == 'covered':
            self.require('invalid_combination' in selected_kinds,
                         path + '.dynamic_parameters.selected_scenarios',
                         'invalid combination assessment requires a matching selected scenario')
        else:
            self.require('invalid_combination' not in selected_kinds, path + '.dynamic_parameters',
                         'invalid combinations contradict their assessment')
            self.text(invalid_review.get('reason'),
                      path + '.dynamic_parameters.invalid_combination_assessment.reason')
            if invalid_status == 'blocked':
                self.gaps.append({'kind': 'invalid_combination', 'id': path,
                                  'reason': invalid_review.get('reason')})
        self.counts['uncovered_interactions'] += len(interactions)

    def validate_combination_strategy(self, dynamic, dimensions, selected, path):
        exclusions = self.object_list(dynamic.get('excluded_combinations'),
                                       path + '.dynamic_parameters.excluded_combinations', nonempty=False)
        constraints = []
        for index, row in enumerate(exclusions):
            item_path = f'{path}.dynamic_parameters.excluded_combinations[{index}]'
            values = row.get('values')
            valid = isinstance(values, dict) and bool(values) and all(
                name in dimensions and isinstance(value, str) and value in dimensions[name]
                for name, value in values.items())
            self.require(valid, item_path + '.values', 'must reference known representatives')
            self.text(row.get('reason'), item_path + '.reason')
            self.source_refs(row.get('source_ids'), item_path + '.source_ids')
            if valid:
                constraints.append(values)
        matches = lambda candidate, values: all(candidate.get(name) == value for name, value in values.items())
        self.require(not any(matches(candidate, values) for candidate in selected for values in constraints),
                     path + '.dynamic_parameters', 'selected scenarios contain an excluded combination')
        strategy = dynamic.get('strategy')
        if not self.choice(strategy, {'full', 'pairwise'}):
            return
        size = 1
        for values in dimensions.values():
            size *= len(values)
        if not self.require(size <= 100000, path + '.dynamic_parameters.strategy',
                            'combination space exceeds 100000; narrow partitions or use risk_based'):
            return
        names = list(dimensions)
        candidates = [dict(zip(names, values)) for values in itertools.product(
            *(sorted(dimensions[name]) for name in names))]
        feasible = [candidate for candidate in candidates if not any(matches(candidate, row) for row in constraints)]
        self.require(bool(feasible), path + '.dynamic_parameters', 'constraints exclude every combination')
        widths = [len(names)] if strategy == 'full' else [min(2, len(names))]
        for width in widths:
            for group in itertools.combinations(names, width):
                expected = {tuple(candidate[name] for name in group) for candidate in feasible}
                observed = {tuple(candidate[name] for name in group) for candidate in selected}
                missing = expected - observed
                if missing:
                    self.gap('combination', f'{path}:{",".join(group)}',
                             f'{strategy}: {len(missing)} feasible combinations lack selected scenarios',
                             missing_count=len(missing),
                             missing_examples=[dict(zip(group, values)) for values in sorted(missing)[:20]])

    def validate_cleanup(self, endpoint, path):
        writes = self.string_list(endpoint.get('writes'), path + '.writes', nonempty=False)
        cleanup = self.object_list(endpoint.get('cleanup'), path + '.cleanup', nonempty=False)
        mapped = set()
        for index, row in enumerate(cleanup):
            item_path = f'{path}.cleanup[{index}]'
            kind = row.get('resource_kind')
            self.text(kind, item_path + '.resource_kind')
            self.text(row.get('owner_service'), item_path + '.owner_service')
            self.require(row.get('owner_service') in self.plan.get('inventory', {}).get('services', []),
                         item_path + '.owner_service', 'must reference an inventoried service')
            self.text(row.get('owner_suite'), item_path + '.owner_suite')
            self.text(row.get('lookup_by'), item_path + '.lookup_by')
            self.text(row.get('absence_evidence'), item_path + '.absence_evidence')
            if isinstance(kind, str):
                self.require(kind not in mapped, item_path + '.resource_kind', 'duplicate cleanup mapping')
                mapped.add(kind)
        self.require(set(writes) == mapped, path + '.cleanup',
                     'must map each written resource kind exactly once')
        self.counts['cleanup_mappings'] += len(cleanup)

    def validate_endpoints(self, services):
        rows = self.object_list(self.plan.get('endpoints'), 'endpoints')
        endpoint_ids = set()
        for index, endpoint in enumerate(rows):
            path = f'endpoints[{index}]'
            endpoint_id = endpoint.get('endpoint_id')
            self.text(endpoint_id, path + '.endpoint_id')
            if isinstance(endpoint_id, str):
                self.require(endpoint_id not in endpoint_ids, path + '.endpoint_id', 'duplicate endpoint_id')
                endpoint_ids.add(endpoint_id)
            service = endpoint.get('service')
            self.text(service, path + '.service')
            self.require(self.choice(service, services), path + '.service', 'service is outside inventory.services')
            method = endpoint.get('method')
            self.require(self.choice(method, HTTP_METHODS),
                         path + '.method', 'must be an uppercase HTTP method')
            route = endpoint.get('path')
            self.require(isinstance(route, str) and route.startswith('/'), path + '.path',
                         'must be a route template beginning with /')
            self.require(endpoint_id == endpoint_key(service, method, route), path + '.endpoint_id',
                         'must equal service:METHOD:/route-template')
            self.checkpoint()
            source_kinds = self.source_refs(endpoint.get('source_ids'), path + '.source_ids', require_contract=True)
            if self.plan.get('inventory', {}).get('backend_code_access') is True:
                self.require('backend_code' in source_kinds, path + '.source_ids',
                             'must cite backend_code when available')
            scenario_ids = set()
            self.scenario_case_owners = {}
            self.positive_rows = self.scenarios(endpoint, 'positive_scenarios', path + '.positive_scenarios',
                                                seen=scenario_ids)
            self.negative_rows = self.scenarios(endpoint, 'negative_scenarios', path + '.negative_scenarios',
                                                negative=True, seen=scenario_ids)
            self.checkpoint()
            self.negative_by_id = {row.get('scenario_id'): row for row in self.negative_rows}
            self.validate_negative_review(endpoint, path)
            self.checkpoint()
            self.validate_branches(endpoint, path)
            self.checkpoint()
            self.validate_dynamic(endpoint, path)
            self.checkpoint()
            self.validate_cleanup(endpoint, path)
            self.checkpoint()
            self.endpoints[endpoint_id] = endpoint
            self.counts['endpoints'] += 1
        self.require(endpoint_ids == self.inventory_included, 'inventory.included',
                     'must exactly match the endpoint rows')
        accounted = self.inventory_included | self.inventory_excluded
        for service, (contract_ids, fingerprint) in self.contracts.items():
            self.require(service in services, 'contracts', f'{service} is outside inventory.services')
            actual = {key for key in accounted if key.startswith(service + ':')}
            self.require(actual == contract_ids, 'inventory',
                         f'{service} contract mismatch: missing={sorted(contract_ids - actual)}, '
                         f'unknown={sorted(actual - contract_ids)}')
            self.require(any(source.get('kind') == 'openapi' and source.get('fingerprint') == fingerprint
                             for source in self.plan.get('sources', [])), 'sources',
                         f'{service} must cite the SHA256 of the supplied OpenAPI snapshot')

    def validate_journeys(self, services):
        rows = self.object_list(self.plan.get('journeys'), 'journeys', nonempty=False)
        journey_ids = set()
        self.require(bool(rows) == (self.cross_service_status == 'covered'), 'journeys',
                     'cross_service_assessment must agree with the journey inventory')
        for index, journey in enumerate(rows):
            path = f'journeys[{index}]'
            journey_id = journey.get('journey_id')
            self.text(journey_id, path + '.journey_id')
            if isinstance(journey_id, str):
                self.require(journey_id not in journey_ids, path + '.journey_id', 'duplicate journey_id')
                journey_ids.add(journey_id)
            self.text(journey.get('title'), path + '.title')
            self.identifiers(journey.get('case_ids'), path + '.case_ids')
            steps = self.object_list(journey.get('steps'), path + '.steps')
            step_ids, journey_services, written = set(), set(), {}
            prior_steps = {}
            cross_links = 0
            for step_index, step in enumerate(steps):
                step_path = f'{path}.steps[{step_index}]'
                step_id, endpoint_id = step.get('step_id'), step.get('endpoint_id')
                self.text(step_id, step_path + '.step_id')
                self.checkpoint()
                self.require(step_id not in step_ids, step_path + '.step_id', 'duplicate step_id')
                self.text(endpoint_id, step_path + '.endpoint_id')
                self.checkpoint()
                endpoint = self.endpoints.get(endpoint_id)
                self.require(endpoint is not None, step_path + '.endpoint_id', 'references an unknown endpoint')
                if endpoint:
                    journey_services.add(endpoint.get('service'))
                    for owner in endpoint.get('cleanup', []):
                        key = (owner.get('owner_service'), owner.get('resource_kind'))
                        previous = written.get(key)
                        self.require(previous is None or previous == owner.get('owner_suite'), step_path,
                                     'a resource cannot have different cleanup suites')
                        written[key] = owner.get('owner_suite')
                depends = self.string_list(step.get('depends_on'), step_path + '.depends_on', nonempty=False)
                self.require(set(depends) <= step_ids, step_path + '.depends_on',
                             'dependencies must reference earlier steps')
                links = self.object_list(step.get('data_links'), step_path + '.data_links', nonempty=False)
                for link_index, link in enumerate(links):
                    link_path = f'{step_path}.data_links[{link_index}]'
                    origin = link.get('from_step')
                    self.text(origin, link_path + '.from_step')
                    self.checkpoint()
                    self.require(origin in depends, link_path + '.from_step',
                                 'data producer must be an earlier dependency')
                    self.text(link.get('output'), link_path + '.output')
                    self.text(link.get('input'), link_path + '.input')
                    if origin in prior_steps and endpoint and prior_steps[origin] != endpoint.get('service'):
                        cross_links += 1
                step_ids.add(step_id)
                prior_steps[step_id] = endpoint.get('service') if endpoint else None
                self.text(step.get('action'), step_path + '.action')
                self.string_list(step.get('observations'), step_path + '.observations')
                self.string_list(step.get('assertions'), step_path + '.assertions')
            self.require(journey_services <= services, path + '.steps', 'references a service outside inventory')
            self.require(len(journey_services) >= 2, path + '.steps',
                         'a cross-service journey must exercise at least two services')
            self.require(cross_links > 0, path + '.steps',
                         'link a business output or state between services, not unrelated requests')
            failures = self.object_list(journey.get('failure_scenarios'), path + '.failure_scenarios',
                                        nonempty=False)
            failure_review = journey.get('failure_assessment')
            if not self.require(isinstance(failure_review, dict), path + '.failure_assessment', 'must be an object'):
                failure_review = {}
            failure_status = failure_review.get('status')
            self.require(self.choice(failure_status, {'covered', 'not_applicable', 'blocked'}),
                         path + '.failure_assessment.status', 'unsupported failure assessment')
            if failure_status == 'covered':
                self.require(bool(failures), path + '.failure_scenarios',
                             'covered failure assessment requires at least one failure scenario')
            else:
                self.text(failure_review.get('reason'), path + '.failure_assessment.reason')
                if failure_status == 'not_applicable':
                    self.require(not failures, path + '.failure_scenarios',
                                 'not_applicable cannot contain failure scenarios')
                if failure_status == 'blocked':
                    self.gaps.append({'kind': 'journey_failure', 'id': journey_id,
                                      'reason': failure_review.get('reason')})
            failure_ids = set()
            for failure_index, failure in enumerate(failures):
                failure_path = f'{path}.failure_scenarios[{failure_index}]'
                self.text(failure.get('failure_id'), failure_path + '.failure_id')
                self.checkpoint()
                failure_id = failure.get('failure_id')
                self.require(failure_id not in failure_ids, failure_path + '.failure_id', 'duplicate failure_id')
                failure_ids.add(failure_id)
                self.require(self.choice(failure.get('at_step_id'), step_ids), failure_path + '.at_step_id',
                             'must reference a journey step')
                self.validate_assertions(failure, failure_path)
                self.string_list(failure.get('observations'), failure_path + '.observations')
                self.counts['journey_failure_scenarios'] += 1
            cleanup = self.object_list(journey.get('cleanup'), path + '.cleanup', nonempty=False)
            cleanup_owners = {}
            for cleanup_index, item in enumerate(cleanup):
                item_path = f'{path}.cleanup[{cleanup_index}]'
                kind = item.get('resource_kind')
                self.text(kind, item_path + '.resource_kind')
                owner_service = item.get('owner_service')
                self.text(owner_service, item_path + '.owner_service')
                self.require(self.choice(owner_service, services), item_path + '.owner_service',
                             'cleanup owner must be an inventoried service')
                self.text(item.get('owner_suite'), item_path + '.owner_suite')
                self.text(item.get('lookup_by'), item_path + '.lookup_by')
                self.text(item.get('absence_evidence'), item_path + '.absence_evidence')
                self.checkpoint()
                key = (owner_service, kind)
                self.require(key not in cleanup_owners, item_path, 'duplicate resource ownership')
                cleanup_owners[key] = item.get('owner_suite')
            self.require(written == cleanup_owners, path + '.cleanup',
                         'must match the service, resource kind and cleanup suite of journey steps')
            self.counts['journeys'] += 1

    def discovery_rows(self, data):
        if not self.require(isinstance(data, dict) and isinstance(data.get('suites'), list)
                            and not data.get('errors'), 'discovery',
                            'must be a valid complete Playwright --list JSON'):
            return []
        rows = []
        config = data.get('config', {})
        if not self.require(isinstance(config, dict), 'discovery.config', 'must be an object'):
            return []
        root = config.get('rootDir') or str(self.project)
        if not self.require(isinstance(root, str), 'discovery.config.rootDir', 'must be a path string'):
            return []
        root = Path(root)
        root = (root if root.is_absolute() else self.project / root).resolve()
        self.require(root.is_relative_to(self.project), 'discovery.config.rootDir', 'must be inside project')
        spec_serial = 0

        def walk(suite):
            nonlocal spec_serial
            if not isinstance(suite, dict):
                self.errors.append('discovery.suites: entries must be objects')
                return
            specs = suite.get('specs', [])
            children = suite.get('suites', [])
            self.require(isinstance(specs, list), 'discovery.specs', 'must be a list')
            self.require(isinstance(children, list), 'discovery.suites', 'must be a list')
            for spec in specs if isinstance(specs, list) else []:
                if not isinstance(spec, dict):
                    self.errors.append('discovery.specs: entries must be objects')
                    continue
                file_name = spec.get('file')
                normalized = self.normalize_file(file_name, root)
                spec_serial += 1
                tests = spec.get('tests', [])
                self.require(isinstance(tests, list), 'discovery.tests', 'must be a list')
                for test in tests if isinstance(tests, list) else []:
                    if not isinstance(test, dict):
                        self.errors.append('discovery.tests: entries must be objects')
                        continue
                    rows.append({'file': normalized, 'title': spec.get('title'), 'spec_serial': spec_serial,
                                 'project': test.get('projectName', '')})
            for child in children if isinstance(children, list) else []:
                walk(child)

        for suite in data['suites']:
            walk(suite)
        self.counts['discovered_tests'] = len(rows)
        return rows

    def normalize_file(self, value, root=None):
        if not isinstance(value, str) or not value.strip():
            return ''
        path = Path(value.replace('\\', '/'))
        if not path.is_absolute():
            path = (root or self.project) / path
        return path.resolve().as_posix()

    def validate_cases(self, suite_id):
        if not self.require(isinstance(self.catalog, dict) and isinstance(self.catalog.get('cases'), dict),
                            'catalog', 'must contain a cases object'):
            return
        if not self.require(isinstance(self.coverage, dict)
                            and isinstance(self.coverage.get('implementations'), dict),
                            'coverage', 'must contain implementations'):
            return
        implementations = {}
        for implementation_id, implementation in self.coverage['implementations'].items():
            if not isinstance(implementation, dict) or implementation.get('implementation') == 'retired':
                continue
            case_id = implementation.get('case_id')
            if implementation.get('suite_id') == suite_id and implementation.get('kind') == 'api':
                implementations.setdefault(case_id, []).append((implementation_id, implementation))
        discovered = self.discovery_rows(self.discovery) if self.discovery is not None else None
        binding_owners = {}
        for case_id in sorted(self.case_ids):
            path = f'case_ids[{case_id}]'
            case = self.catalog['cases'].get(case_id)
            self.require(isinstance(case, dict), path, 'unknown business case in catalog')
            if not isinstance(case, dict):
                continue
            self.require(case.get('status', 'active') == 'active', path, 'case is retired')
            self.require('api' in case.get('required_kinds', []), path,
                         'catalog case does not require API implementation')
            mappings = implementations.get(case_id, [])
            self.require(len(mappings) == 1, path,
                         'must map to exactly one active API implementation in this suite')
            if len(mappings) != 1:
                continue
            implementation_id, implementation = mappings[0]
            self.require(implementation.get('execution') == 'automation'
                         and implementation.get('implementation') == 'valid', path,
                         f'implementation {implementation_id} is not valid automation')
            spec, title = implementation.get('spec'), implementation.get('title')
            self.text(spec, path + '.spec')
            self.text(implementation.get('test_id'), path + '.test_id')
            self.text(title, path + '.title')
            spec_path = self.normalize_file(spec)
            self.require(bool(spec_path) and Path(spec_path).is_relative_to(self.project)
                         and Path(spec_path).is_file(), path + '.spec',
                         'must reference an existing file inside project')
            if isinstance(title, str):
                binding = (spec_path, title)
                self.require(binding not in binding_owners, path,
                             'independent cases must map to distinct test declarations')
                binding_owners[binding] = case_id
            project_name = implementation.get('project', implementation.get('projectName', ''))
            if discovered is not None and isinstance(spec, str) and isinstance(title, str):
                matches = [row for row in discovered if row['file'] == spec_path and row['title'] == title
                           and (not project_name or row['project'] == project_name)]
                self.require(len({row['spec_serial'] for row in matches}) == 1, path,
                             'implementation must match exactly one test declaration in complete Playwright discovery')
        for case_id in set(implementations) - self.case_ids:
            self.errors.append(f'case_ids[{case_id}]: active suite API implementation is absent from the plan')
        self.counts['mapped_cases'] = len(self.case_ids)

    def run(self):
        if not self.require(isinstance(self.plan, dict) and self.plan.get('schema_version') == 1,
                            'plan.schema_version', 'must equal 1'):
            raise CoverageError(self.errors)
        suite_id = self.plan.get('suite_id')
        self.text(suite_id, 'plan.suite_id')
        if self.requested_suite_id is not None:
            self.require(suite_id == self.requested_suite_id, 'plan.suite_id', 'does not match --suite-id')
        services = self.validate_sources()
        self.checkpoint()
        self.validate_endpoints(services)
        self.checkpoint()
        self.validate_journeys(services)
        self.checkpoint()
        self.validate_cases(suite_id)
        if self.errors:
            raise CoverageError(self.errors)
        return {
            'status': 'known_gaps' if self.gaps else 'checked',
            'suite_id': suite_id,
            'inventory_services': sorted(services),
            'counts': dict(sorted(self.counts.items())),
            'known_gaps': self.gaps,
            'discovery': 'checked' if self.discovery is not None else 'not_supplied',
            'contract_inventory': sorted(self.contracts),
            'contract_inventory_unchecked': sorted(services - set(self.contracts)),
            'scope_note': 'Checks declared coverage and supplied contract differences; does not prove runtime execution, source branch coverage or exhaustive dynamic inputs.'
        }


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--coverage', required=True)
    parser.add_argument('--project', default='.')
    parser.add_argument('--discovery')
    parser.add_argument('--suite-id')
    parser.add_argument('--allow-known-gaps', action='store_true')
    parser.add_argument('--openapi', action='append', default=[], metavar='SERVICE=JSON',
                        help='compare operations with a local OpenAPI snapshot; repeat for each service')
    args = parser.parse_args(argv)
    try:
        contracts = {}
        for item in args.openapi:
            service, separator, path = item.partition('=')
            if not separator or not service or not path or service in contracts:
                raise ValueError('--openapi requires a unique SERVICE=JSON')
            contracts[service] = openapi_inventory(path, service)
        result = Validator(args.project, load_json(args.plan), load_json(args.catalog),
                           load_json(args.coverage), load_json(args.discovery) if args.discovery else None,
                           args.suite_id, contracts).run()
    except (OSError, json.JSONDecodeError, CoverageError, ValueError) as error:
        print(json.dumps({'status': 'invalid', 'error': str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result['status'] == 'known_gaps' and not args.allow_known_gaps else 0


if __name__ == '__main__':
    sys.exit(main())
