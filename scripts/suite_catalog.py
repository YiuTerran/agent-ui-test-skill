"""Shared business cases with project-local Playwright implementations."""
import copy
from contextlib import contextmanager, ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile

_spec = importlib.util.spec_from_file_location(
    'auto_test_catalog_platform', Path(__file__).with_name('platform_support.py'))
platform = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(platform)

CATALOG = 'test/plans/catalog.json'
COVERAGE = 'test/plans/coverage.json'
KINDS = {'ui', 'api'}
IMPLEMENTATION_FIELDS = {
    'spec', 'file', 'test_id', 'title', 'project', 'projectName', 'acceptance_version',
    'execution', 'implementation', 'legacy_group',
}
VIEW_FIELDS = {'implementation_id', 'suite_id', 'kind', 'business_title'}


def check(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def config(project):
    path = Path(project) / 'test/spec.md'
    if not path.is_file():
        return {}
    blocks = re.findall(r'```json workflow-config\s*\n(.*?)\n```', path.read_text(encoding='utf-8'), re.S)
    if not blocks:
        return {}
    check(len(blocks) == 1, 'spec.md must contain exactly one json workflow-config block')
    value = json.loads(blocks[0])
    check(isinstance(value, dict), 'workflow-config must be an object')
    return value


def suite(spec):
    value = spec.get('suite', {'id': 'default-ui', 'kind': 'ui'})
    check(isinstance(value, dict) and set(value) == {'id', 'kind'}, 'suite requires id and kind')
    check(isinstance(value['id'], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value['id']),
          'suite.id must be a stable identifier')
    check(isinstance(value['kind'], str) and value['kind'] in KINDS, 'suite.kind must be ui or api')
    return value


def configured_root(project, spec, key):
    value = spec.get(key)
    if value is None:
        return Path(project).resolve()
    check(isinstance(value, dict) and set(value) == {'root'}, key + ' requires root only')
    root = value['root']
    check(isinstance(root, str) and bool(root.strip()), key + '.root must be a nonempty path')
    path = Path(root)
    if not path.is_absolute():
        path = Path(project) / path
    check(path.is_dir(), key + '.root must be an existing directory')
    return path.resolve()


def coordination_root(project):
    project = Path(project).resolve()
    root = configured_root(project, config(project), 'coordination')
    binding = project / 'test/.cache/coordination-root.json'
    if binding.exists() or binding.is_symlink():
        check(binding.resolve().is_relative_to(project), 'coordination binding escapes project')
    if binding.exists():
        check(read(binding) == {'schema_version': 1, 'root': str(root)},
              'coordination root changed; restore the bound root and reconcile state before migration')
    elif root != project:
        for relative in ('test/.cache/scheduling/quota.json', 'test/.cache/identity-pools.json',
                         'test/.cache/resource-residuals.json'):
            path = project / relative
            check(not path.exists(), 'existing local coordination ledger; reconcile before enabling shared root')
    return root


def ensure_child_directory(root, relative):
    root = Path(root).resolve()
    directory = root
    for name in platform.relative_parts(relative):
        directory = directory / name
        if directory.exists() or directory.is_symlink():
            check(directory.resolve().is_relative_to(root), 'shared directory escapes configured root')
        platform.ensure_directory(directory)
        check(directory.resolve(strict=True).is_relative_to(root), 'shared directory escapes configured root')
    return directory


def bind_coordination(project):
    project = Path(project).resolve()
    with directory_lock(project, 'coordination-binding.lock'):
        root = coordination_root(project)
        path = project / 'test/.cache/coordination-root.json'
        if not path.exists():
            descriptor, temporary = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    json.dump({'schema_version': 1, 'root': str(root)}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                platform.sync_directory(path.parent)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return root


def coordination_path(project, relative):
    root = coordination_root(project)
    platform.relative_parts(relative)
    path = (root / relative).resolve()
    check(path.is_relative_to(root), 'coordination path escapes configured root')
    return path


def catalog_path(root):
    path = (root / CATALOG).resolve()
    check(path.is_relative_to(root), 'catalog path escapes configured root')
    return path


@contextmanager
def directory_lock(root, name):
    directory = ensure_child_directory(root, 'test/.cache')
    path = directory / name
    if path.exists() or path.is_symlink():
        check(path.resolve().is_relative_to(Path(root).resolve()), 'shared lock escapes configured root')
    with path.open('a+b') as stream:
        with platform.file_lock(stream.fileno()):
            yield


@contextmanager
def shared_locks(project):
    spec = config(project)
    roots = {(bind_coordination(project), 'coordination.lock')}
    if 'catalog' in spec:
        roots.add((configured_root(project, spec, 'catalog'), 'catalog.lock'))
    with ExitStack() as stack:
        for root, name in sorted(roots, key=lambda item: (str(item[0]), item[1])):
            stack.enter_context(directory_lock(root, name))
        yield


class Coverage(dict):
    pass


def validate_catalog(catalog):
    check(isinstance(catalog, dict) and catalog.get('schema_version') == 2,
          'catalog schema_version must be 2')
    for field in ('sources', 'requirements', 'cases'):
        check(isinstance(catalog.get(field), dict), 'catalog.' + field + ' must be an object')
    scopes = catalog.get('scopes')
    check(isinstance(scopes, list) and all(isinstance(value, str) and value for value in scopes)
          and len(scopes) == len(set(scopes)), 'catalog.scopes must be unique strings')
    for source_id, source in catalog['sources'].items():
        check(isinstance(source, dict), 'invalid catalog source: ' + source_id)
        check(source.get('kind', 'requirements') in {'requirements', 'backend_code'}, 'invalid source kind')
        if source.get('kind') == 'backend_code':
            check(source.get('location') and (source.get('revision') or source.get('fingerprint')),
                  'backend code source needs location and version')
    for case_id, case in catalog['cases'].items():
        check(isinstance(case_id, str) and bool(case_id) and isinstance(case, dict), 'invalid catalog case')
        kinds = case.get('required_kinds')
        check(isinstance(kinds, list) and bool(kinds) and all(isinstance(kind, str) for kind in kinds)
              and len(kinds) == len(set(kinds))
              and set(kinds) <= KINDS, 'case.required_kinds must select ui and/or api: ' + case_id)
        check(not (IMPLEMENTATION_FIELDS - {'title'}) & set(case), 'implementation fields belong in local coverage')
        check(case.get('scope') in scopes and case.get('source_id') in catalog['sources'],
              'unknown catalog case scope/source: ' + case_id)
        check(case.get('status', 'active') in {'active', 'retired'}, 'invalid business case status')
        check(isinstance(case.get('requirement_ids'), list) and bool(case['requirement_ids']),
              'catalog case requires requirement_ids: ' + case_id)
        for requirement_id in case['requirement_ids']:
            check(isinstance(requirement_id, str), 'invalid requirement ID')
            requirement = catalog['requirements'].get(requirement_id, {})
            check(isinstance(requirement, dict), 'invalid catalog requirement')
            check(case_id in requirement.get('case_ids', [])
                  and requirement.get('scope') == case['scope']
                  and requirement.get('source_id') == case['source_id'],
                  'broken catalog case/requirement mapping: ' + case_id)
    for requirement_id, requirement in catalog['requirements'].items():
        check(isinstance(requirement, dict) and requirement.get('scope') in scopes
              and requirement.get('source_id') in catalog['sources']
              and requirement.get('expected_version') is not None, 'invalid catalog requirement: ' + requirement_id)
        check(isinstance(requirement.get('case_ids'), list)
              and (bool(requirement['case_ids']) or requirement.get('status') == 'retired'),
              'catalog requirement needs case_ids: ' + requirement_id)
        for case_id in requirement['case_ids']:
            check(isinstance(case_id, str), 'invalid case ID')
            case = catalog['cases'].get(case_id, {})
            check(isinstance(case, dict), 'invalid catalog case')
            check(requirement_id in case.get('requirement_ids', []),
                  'broken catalog requirement/case mapping: ' + requirement_id)


def project_view(project, spec, local, catalog=None):
    if local.get('schema_version') == 1:
        check(suite(spec)['kind'] == 'ui' and 'catalog' not in spec,
              'API or shared catalogs require coverage v2; use migrate')
        return local
    check(local.get('schema_version') == 2 and isinstance(local.get('implementations'), dict),
          'local coverage requires schema_version 2 and implementations')
    current = suite(spec)
    check('suite' in spec and 'catalog' in spec, 'coverage v2 requires suite and catalog configuration')
    root = configured_root(project, spec, 'catalog')
    catalog = read(catalog_path(root)) if catalog is None else catalog
    validate_catalog(catalog)
    scopes = local.get('scopes')
    check(isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes)
          and len(scopes) == len(set(scopes)) and set(scopes) <= set(catalog['scopes']), 'local scopes outside catalog')
    mappings = {}
    for implementation_id, implementation in local['implementations'].items():
        check(isinstance(implementation_id, str) and bool(implementation_id)
              and isinstance(implementation, dict), 'invalid implementation')
        check(implementation.get('suite_id') == current['id'] and implementation.get('kind') == current['kind'],
              'implementation belongs to another suite')
        case_id = implementation.get('case_id')
        check(isinstance(case_id, str) and case_id in catalog['cases'], 'implementation references unknown business case')
        check(set(implementation) <= IMPLEMENTATION_FIELDS | {'case_id', 'suite_id', 'kind'},
              'unknown local implementation fields')
        if implementation.get('implementation') != 'retired':
            check(case_id not in mappings, 'one active implementation per case per suite; split distinct business cases')
            mappings[case_id] = (implementation_id, implementation)
    cases = {}
    for case_id, business in catalog['cases'].items():
        if business['scope'] not in scopes:
            continue
        if current['kind'] not in business['required_kinds'] and case_id not in mappings:
            continue
        implementation_id, implementation = mappings.get(case_id, (
            current['id'] + '-' + case_id, {'execution': 'automation', 'implementation': 'missing'}))
        case = copy.deepcopy(business)
        case.update({key: copy.deepcopy(value) for key, value in implementation.items()
                     if key in IMPLEMENTATION_FIELDS})
        case.update(implementation_id=implementation_id, suite_id=current['id'], kind=current['kind'],
                    business_title=business.get('title', case_id))
        if business.get('status') == 'retired':
            case['implementation'] = 'retired'
        cases[case_id] = case
    requirements = {}
    for requirement_id, requirement in catalog['requirements'].items():
        if requirement['scope'] not in scopes:
            continue
        projected = copy.deepcopy(requirement)
        projected['case_ids'] = [case_id for case_id in requirement['case_ids'] if case_id in cases]
        if not projected['case_ids'] and projected.get('status') != 'retired':
            projected['kind_not_required'] = True
        requirements[requirement_id] = projected
    sources = {source_id: copy.deepcopy(source) for source_id, source in catalog['sources'].items()
               if set(source.get('scopes', [source.get('scope')])) & set(scopes)}
    view = Coverage(schema_version=1, scopes=copy.deepcopy(scopes), sources=sources,
                    requirements=requirements, cases=cases)
    view.root, view.project, view.spec = root, Path(project), copy.deepcopy(spec)
    view.local, view.catalog = copy.deepcopy(local), copy.deepcopy(catalog)
    view.original = copy.deepcopy(dict(view))
    return view


def asset_path(project, coverage, relative):
    root = coverage.root if isinstance(coverage, Coverage) else Path(project).resolve()
    check(isinstance(relative, str) and relative and not Path(relative).is_absolute(),
          'business asset path must be catalog-relative')
    platform.relative_parts(relative)
    path = (root / relative).resolve()
    check(path.is_relative_to(root), 'business asset path escapes catalog')
    return path


def documents(view):
    catalog, local = copy.deepcopy(view.catalog), copy.deepcopy(view.local)
    for field in ('sources', 'requirements'):
        for key, value in view[field].items():
            original = view.original[field].get(key)
            if value == original:
                continue
            updated = copy.deepcopy(value)
            updated.pop('kind_not_required', None)
            if field == 'requirements':
                outside = [case_id for case_id in catalog[field].get(key, {}).get('case_ids', [])
                           if case_id not in view.original['cases']]
                updated['case_ids'] = list(dict.fromkeys(outside + updated.get('case_ids', [])))
            catalog[field][key] = updated
    for case_id, value in view['cases'].items():
        if value == view.original['cases'].get(case_id):
            continue
        business = {key: copy.deepcopy(item) for key, item in value.items()
                    if key not in IMPLEMENTATION_FIELDS | VIEW_FIELDS}
        business['title'] = value.get('business_title', catalog['cases'].get(case_id, {}).get('title',
                                                                            value.get('title', case_id)))
        business.setdefault('required_kinds', [suite(view.spec)['kind']])
        catalog['cases'][case_id] = business
        implementation_id = value.get('implementation_id', suite(view.spec)['id'] + '-' + case_id)
        prior = local['implementations'].get(implementation_id)
        check(prior is None or prior['case_id'] == case_id, 'implementation_id cannot change business case')
        implementation = {key: copy.deepcopy(item) for key, item in value.items() if key in IMPLEMENTATION_FIELDS}
        implementation.update(case_id=case_id, suite_id=suite(view.spec)['id'], kind=suite(view.spec)['kind'])
        local['implementations'][implementation_id] = implementation
    return catalog, local


def rebuild(view):
    if not isinstance(view, Coverage):
        return view
    catalog, local = documents(view)
    return project_view(view.project, view.spec, local, catalog)


def persist(project, original, updated, atomic):
    if not isinstance(original, Coverage):
        atomic(Path(project) / COVERAGE, updated)
        return
    path = catalog_path(original.root)
    local_path = Path(project) / COVERAGE
    check(digest(read(path)) == digest(original.catalog)
          and digest(read(local_path)) == digest(original.local),
          'catalog or implementation changed concurrently; reload and review')
    journal = {'schema_version': 1, 'root': str(original.root),
               'before_catalog': digest(original.catalog), 'before_local': digest(original.local),
               'catalog': updated.catalog, 'coverage': updated.local, 'complete': False}
    journal_path = Path(project) / 'test/.cache/catalog-update.json'
    if journal_path.exists():
        check(read(journal_path).get('complete') is True, 'pending catalog update; use catalog-recover')
    atomic(journal_path, journal)
    if updated.catalog != original.catalog:
        atomic(path, updated.catalog)
    if updated.local != original.local:
        atomic(local_path, updated.local)
    atomic(journal_path, dict(journal, complete=True))


def recover(project, atomic):
    journal_path = Path(project) / 'test/.cache/catalog-update.json'
    journal = read(journal_path)
    root = configured_root(project, config(project), 'catalog')
    check(journal.get('schema_version') == 1 and journal.get('root') == str(root), 'catalog recovery root mismatch')
    targets = [(catalog_path(root), 'catalog', 'before_catalog'),
               (Path(project) / COVERAGE, 'coverage', 'before_local')]
    with directory_lock(root, 'catalog.lock'):
        for path, after, before in targets:
            current = digest(read(path)) if path.exists() else None
            check(current in {journal[before], digest(journal[after])},
                  'catalog recovery conflicts with newer edits; manual reconciliation required')
        spec = journal.get('spec', config(project))
        if 'spec_text' in journal:
            check((Path(project) / 'test/spec.md').read_text(encoding='utf-8') in
                  {journal['before_spec_text'], journal['spec_text']}, 'migration configuration changed')
        project_view(project, spec, journal['coverage'], journal['catalog'])
        for path, after, before in targets:
            if not path.exists() or digest(read(path)) != digest(journal[after]):
                atomic(path, journal[after])
        if 'spec_text' in journal:
            atomic(Path(project) / 'test/spec.md', journal['spec_text'])
        atomic(journal_path, dict(journal, complete=True))
    return {'valid': True, 'recovered': True, 'next': 'resume run and review assets'}


def empty_catalog(scopes=None):
    return {'schema_version': 2, 'scopes': list(scopes or []), 'sources': {}, 'requirements': {}, 'cases': {}}


def initialize(project, spec, kind, suite_id, root, atomic):
    spec = copy.deepcopy(spec)
    check(kind in KINDS and suite_id, '--kind and --suite-id required for a new suite')
    spec.update(suite={'id': suite_id, 'kind': kind}, catalog={'root': root})
    suite(spec)
    catalog_root = configured_root(project, spec, 'catalog')
    path = catalog_path(catalog_root)
    local = {'schema_version': 2, 'scopes': list(spec.get('scope', {}).get('fixed', [])), 'implementations': {}}
    with directory_lock(catalog_root, 'catalog.lock'):
        created = not path.exists()
        if not created:
            existing = read(path)
            validate_catalog(existing)
            spec['scope']['fixed'] = copy.deepcopy(existing['scopes'])
            local['scopes'] = copy.deepcopy(existing['scopes'])
            spec['source'] = [{'id': source_id, 'scopes': source.get('scopes', [source.get('scope')]),
                               'location': source.get('location', source_id)}
                              for source_id, source in existing['sources'].items()]
        else:
            atomic(path, empty_catalog(local['scopes']), exclusive=True)
    return spec, local, {'root': str(catalog_root), 'path': CATALOG, 'created': created}


def migration(project, spec, legacy, kind, suite_id, root):
    check(legacy.get('schema_version') == 1, 'migration requires legacy coverage version 1')
    current_spec = copy.deepcopy(spec)
    current_spec.update(suite={'id': suite_id, 'kind': kind}, catalog={'root': root})
    current = suite(current_spec)
    check(kind == 'ui', 'legacy coverage represents UI; create a separate API suite')
    check(configured_root(project, current_spec, 'catalog') == Path(project).resolve(),
          'migrate in place first; moving business files requires explicit path review')
    catalog = empty_catalog(legacy['scopes'])
    catalog.update({field: copy.deepcopy(legacy[field]) for field in ('sources', 'requirements')})
    local = {'schema_version': 2, 'scopes': copy.deepcopy(legacy['scopes']), 'implementations': {}}
    for case_id, case in legacy['cases'].items():
        business = {key: copy.deepcopy(value) for key, value in case.items() if key not in IMPLEMENTATION_FIELDS}
        business.update(title=case.get('title', case_id), required_kinds=['ui'])
        if case.get('implementation') == 'retired':
            business['status'] = 'retired'
        catalog['cases'][case_id] = business
        implementation = {key: copy.deepcopy(value) for key, value in case.items() if key in IMPLEMENTATION_FIELDS}
        implementation.update(case_id=case_id, suite_id=current['id'], kind=kind)
        local['implementations'][current['id'] + '-' + case_id] = implementation
    validate_catalog(catalog)
    project_view(project, current_spec, local, catalog)
    return {'schema_version': 1, 'before_spec': digest(spec), 'before_coverage': digest(legacy),
            'spec': current_spec, 'catalog': catalog, 'coverage': local,
            'business_cases': len(catalog['cases']), 'implementations': len(local['implementations'])}


def report_view(coverage, case_results):
    if not isinstance(coverage, Coverage):
        return {'suite': {'id': 'default-ui', 'kind': 'ui'}, 'business_case_count': len(case_results),
                'implementation_count': len(case_results), 'results_by_kind': {'ui': dict(case_results)}}
    current = suite(coverage.spec)
    implementations = {case['implementation_id']: {
        'case_id': case_id, 'suite_id': current['id'], 'kind': current['kind'],
        'implementation': case.get('implementation', 'missing'),
        'result': case_results.get(case_id, 'notrun')}
        for case_id, case in coverage['cases'].items()
        if case_id in case_results and case['implementation_id'] in coverage.local['implementations']
        and coverage.local['implementations'][case['implementation_id']]['case_id'] == case_id
        and coverage.local['implementations'][case['implementation_id']].get('implementation') != 'retired'}
    business = {case_id: {'required_kinds': case['required_kinds'],
                           'observed_kind': current['kind'], 'result': case_results.get(case_id, 'notrun'),
                           'other_kinds': {kind: 'not_evaluated' for kind in case['required_kinds']
                                           if kind != current['kind']}}
                for case_id, case in coverage['cases'].items() if case_id in case_results}
    return {'suite': current, 'business_case_count': len(business), 'implementation_count': len(implementations),
            'implementations': implementations, 'business_cases': business,
            'results_by_kind': {current['kind']: dict(case_results)},
            'acceptance_basis': 'current_suite_only; other kinds are not evaluated'}
