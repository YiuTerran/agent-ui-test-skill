#!/usr/bin/env python3
"""Conservative local E2E history retention. Standard library, no test execution.

The workflow lock is cooperative. Descriptor-relative, no-follow I/O and verified
quarantine renames additionally prevent a path substitution from deleting a
different file. A plan is an immutable capability registered outside runs; an
edited/exported JSON document alone cannot grant a larger deletion set.
Direct Python APIs require a canonical project path (Path(project).resolve()).
The CLI canonicalizes only its explicitly selected project root; all paths
inside that root use descriptor-relative POSIX I/O or pinned Win32 handles.
Windows retention requires a fixed local volume; reparse points/UNC are refused.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from time import perf_counter


_platform_spec = importlib.util.spec_from_file_location(
    'e2e_retention_platform', Path(__file__).with_name('platform_support.py'))
platform_support = importlib.util.module_from_spec(_platform_spec)
_platform_spec.loader.exec_module(platform_support)


RUNS = 'test/.cache/runs'
STORE = 'test/.cache/retention'
TERMINAL = {'fully_accepted', 'completed_with_failures', 'partially_blocked', 'aborted'}
ROOT_SMALL = {'state.json', 'summary.json', 'report.md', 'report.html', 'retention-manifest.json'}
ID = re.compile(r'[A-Za-z0-9_-]+\Z')
HEX = re.compile(r'[0-9a-f]{64}\Z')
CASE_INSENSITIVE = os.name == 'nt'
DIR_FLAGS = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)


def require(value, message):
    if not value:
        raise ValueError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def parts(value):
    return platform_support.relative_parts(value)


def identifier(value):
    require(isinstance(value, str) and ID.fullmatch(value), 'invalid run/bundle ID')
    return value


class PosixTree:
    """Open every component without following symlinks, including the root."""

    def __init__(self, project):
        require('..' not in Path(project).parts, 'project path traversal')
        self.path = os.path.abspath(os.fspath(project))
        self.fd = os.open('/', DIR_FLAGS)
        try:
            for component in Path(self.path).parts[1:]:
                next_fd = os.open(component, DIR_FLAGS, dir_fd=self.fd)
                os.close(self.fd)
                self.fd = next_fd
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)

    @contextmanager
    def parent(self, value, create=False):
        names = parts(value)
        fd = os.dup(self.fd)
        try:
            for component in names[:-1]:
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(component, DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd, names[-1]
        finally:
            os.close(fd)

    def read(self, value):
        with self.parent(value) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = os.fstat(fd)
                require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                        'not a single-link regular file: ' + value)
                chunks = []
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                after = os.fstat(fd)
                require(self.identity(before) == self.identity(after), 'file changed while reading: ' + value)
                data = b''.join(chunks)
                return data, dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                                  identity=self.identity(after))
            finally:
                os.close(fd)

    @staticmethod
    def identity(info):
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

    def exists(self, value):
        try:
            with self.parent(value) as (fd, name):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                require(not stat.S_ISLNK(info.st_mode), 'symlink: ' + value)
                return True
        except FileNotFoundError:
            return False

    def walk(self, value):
        """Inventory names too: additions/empty directory swaps invalidate a plan."""
        result = {}
        with self.parent(value) as (parent, name):
            fd = os.open(name, DIR_FLAGS, dir_fd=parent)
        try:
            result[value + '/'] = list(self.identity(os.fstat(fd))[:2])
            for name in sorted(os.listdir(fd)):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                path = value + '/' + name
                require(not stat.S_ISLNK(info.st_mode), 'symlink: ' + path)
                if stat.S_ISDIR(info.st_mode):
                    result.update(self.walk(path))
                else:
                    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                            'unsupported file type: ' + path)
                    result[path] = None
            return result
        finally:
            os.close(fd)

    def write(self, value, data, exclusive=False):
        payload = canonical(data) + b'\n'
        with self.parent(value, create=True) as (fd, name):
            temp = '.retention-' + secrets.token_hex(12)
            out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(out, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                if exclusive:
                    os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                else:
                    # Never replace a symlink, even though replace would not follow it.
                    if self.exists(value):
                        self.read(value)
                    os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                try:
                    os.unlink(temp, dir_fd=fd)
                except FileNotFoundError:
                    pass


    @contextmanager
    def lock(self, value):
        with self.parent(value, create=True) as (parent, name):
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'unsafe lock file')
            with platform_support.file_lock(fd):
                yield
        finally:
            os.close(fd)

    def move(self, source, target, expected=None):
        with self.parent(source) as (src, name), self.parent(target, create=True) as (dst, stage):
            if expected:
                require(self.read(source)[1] == expected, 'artifact changed before staging: ' + source)
            require(not self.exists(target), 'move target exists')
            os.rename(name, stage, src_dir_fd=src, dst_dir_fd=dst)
            os.fsync(src)
            os.fsync(dst)

    def restore(self, source, target):
        with self.parent(source) as (src, name), self.parent(target) as (dst, stage):
            os.link(name, stage, src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False)
            os.unlink(name, dir_fd=src)
            os.fsync(src)
            os.fsync(dst)

    def delete(self, value, expected_hash):
        require(self.read(value)[1]['sha256'] == expected_hash, 'quarantine artifact changed')
        with self.parent(value) as (fd, name):
            os.unlink(name, dir_fd=fd)
            os.fsync(fd)


class WindowsTree:
    """Local-drive backend: resolve every child relative to verified directory handles."""
    def __init__(self, project):
        require('..' not in Path(project).parts, 'project path traversal')
        self.path = os.path.abspath(os.fspath(project))
        self.files = platform_support.WindowsFiles()
        self.root = self.files.rooted_directory(self.path)
        self.root_handle = self.root.__enter__()

    def close(self):
        self.root.__exit__(None, None, None)

    @contextmanager
    def parent(self, value, create=False):
        from contextlib import ExitStack
        names = parts(value)
        with ExitStack() as stack:
            parent = self.root_handle
            for name in names[:-1]:
                parent = stack.enter_context(self.files.child(parent, name, directory=True, create=create))
            yield parent, names[-1]

    def stamp(self, handle):
        before = self.files.info(handle)
        require(not before['directory'], 'not a regular file')
        data = self.files.read(handle)
        after = self.files.info(handle)
        require(before == after, 'file changed while reading')
        return data, dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                          identity=after['identity'])

    def read(self, value):
        with self.parent(value) as (parent, name):
            with self.files.child(parent, name, access=self.files.READ, directory=False) as handle:
                return self.stamp(handle)

    def exists(self, value):
        try:
            with self.parent(value) as (parent, name):
                with self.files.child(parent, name):
                    return True
        except FileNotFoundError:
            return False

    def walk(self, value):
        result = {}
        def visit(handle, path):
            result[path + '/'] = self.files.info(handle)['identity'][:2]
            for name in sorted(self.files.names(handle)):
                child_value = path + '/' + name
                parts(child_value)
                with self.files.child(handle, name, access=self.files.READ) as child:
                    if self.files.info(child)['directory']:
                        visit(child, child_value)
                    else:
                        result[child_value] = None
        with self.parent(value) as (parent, name):
            with self.files.child(parent, name, access=self.files.READ, directory=True) as handle:
                visit(handle, value)
        return result

    def write(self, value, data, exclusive=False):
        payload = canonical(data) + b'\n'
        with self.parent(value, create=True) as (parent, name):
            temporary = '.retention-' + secrets.token_hex(12)
            with self.files.child(parent, temporary, access=0xC0010000,
                                  directory=False, exclusive=True) as handle:
                renamed = False
                try:
                    with self.files.descriptor(handle, os.O_RDWR) as fd:
                        remaining = memoryview(payload)
                        while remaining:
                            written = os.write(fd, remaining)
                            require(written > 0, 'short metadata write')
                            remaining = remaining[written:]
                        os.fsync(fd)
                    if not exclusive and self.exists(value):
                        self.read(value)
                    self.files.rename_child(handle, parent, name, replace=not exclusive)
                    renamed = True
                finally:
                    if not renamed:
                        self.files.delete(handle)

    @contextmanager
    def lock(self, value):
        with self.parent(value, create=True) as (parent, name):
            with self.files.child(parent, name, access=0xC0000000, directory=False,
                                  create=True, shared_write=True) as handle:
                with self.files.descriptor(handle, os.O_RDWR) as fd:
                    with platform_support.file_lock(fd):
                        yield

    def move(self, source, target, expected=None):
        with self.parent(source) as (src, name), self.parent(target, create=True) as (dst, stage):
            with self.files.child(src, name, access=self.files.READ | self.files.DELETE,
                                  directory=False) as handle:
                if expected:
                    require(self.stamp(handle)[1] == expected, 'artifact changed before staging: ' + source)
                self.files.rename_child(handle, dst, stage)

    def restore(self, source, target):
        self.move(source, target)

    def delete(self, value, expected_hash):
        with self.parent(value) as (parent, name):
            with self.files.child(parent, name, access=self.files.READ | self.files.DELETE,
                                  directory=False) as handle:
                require(self.stamp(handle)[1]['sha256'] == expected_hash, 'quarantine artifact changed')
                self.files.delete(handle)


Tree = WindowsTree if os.name == 'nt' else PosixTree


@contextmanager
def locked(project):
    tree = Tree(project)
    try:
        # One lock order on both platforms; scheduling never takes workflow.lock.
        with tree.lock('test/.cache/workflow.lock'), tree.lock('test/.cache/scheduling/quota.lock'):
            yield tree
    finally:
        tree.close()


def load(tree, path):
    return json.loads(tree.read(path)[0])


def scopes_of(value):
    if isinstance(value, str):
        value = [value]
    require(isinstance(value, list) and value and all(isinstance(v, str) and v for v in value),
            'missing or ambiguous scopes')
    return sorted(set(value))


def active(state):
    claim = state.get('claim')
    if claim is None:
        return False
    require(isinstance(claim, dict) and claim.get('owner'), 'ambiguous run claim')
    at = datetime.fromisoformat(claim['renewed_at'].replace('Z', '+00:00'))
    require(at.tzinfo is not None, 'claim timestamp requires timezone')
    # Honor each claim's lease while refusing implausibly short legacy values.
    lease = max(platform_support.MIN_RUN_LEASE_SECONDS,
                claim.get('lease_seconds', platform_support.MIN_RUN_LEASE_SECONDS))
    return (datetime.now(timezone.utc) - at).total_seconds() < lease


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def strings_at(value, field=''):
    """Retain the exact JSON field behind a reference, including manifest keys."""
    if isinstance(value, str):
        yield value, field
    elif isinstance(value, dict):
        for key, child in value.items():
            pointer = field + '/' + str(key).replace('~', '~0').replace('/', '~1')
            yield str(key), pointer + '/@key'
            yield from strings_at(child, pointer)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from strings_at(child, field + '/' + str(index))


class CleanupCandidateMismatch(ValueError):
    def __init__(self, diagnostics):
        super().__init__('cleanup_candidate_mismatch')
        self.diagnostics = diagnostics


def candidate_diagnostics(candidates, selected, protected, excluded):
    selected = set(selected)
    candidates = set(candidates)
    blocked = {path for item in protected.values() for path in item['paths']}
    diagnostics = dict(candidate_files=len(candidates), selected_files=len(selected),
                       protected_files=len(blocked), zero_reason=None,
                       excluded_files_by_reason=dict(sorted(excluded.items())),
                       protections=[dict(**item['reference'], files=len(item['paths']),
                                         sample_paths=sorted(item['paths'])[:3])
                                    for item in protected.values()])
    if candidates != selected | blocked or selected & blocked:
        diagnostics['unexplained_paths'] = sorted(candidates - selected - blocked)[:10]
        diagnostics['unexpected_paths'] = sorted((selected | blocked) - candidates)[:10]
        raise CleanupCandidateMismatch(diagnostics)
    if not selected:
        diagnostics['zero_reason'] = 'protected_references' if blocked else 'no_eligible_history'
    return diagnostics


def reference_target(value, limitations=None, protect=True, strict=None, reason=None):
    """Turn a legacy cache glob into its stable containing subtree."""
    if strict is None:
        strict = protect
    glob_chars = '*?[]'
    if not any(char in value for char in glob_chars):
        try:
            parts(value)
        except ValueError:
            if limitations is not None:
                limitations.append(dict(path=value, reason=reason or
                                        ('noncanonical_live_reference' if strict else
                                         'noncanonical_historical_reference')))
            if not strict:
                return None
            names = value.replace('\\', '/').split('/')
            if (len(names) > 3 and names[:3] == RUNS.split('/')
                    and ID.fullmatch(names[3])):
                return RUNS + '/' + names[3]
            raise
        return value if protect else None
    if not protect:
        if limitations is not None and not any(item.get('path') == value and
                                              item.get('reason') == (reason or 'historical_wildcard_reference_not_protected')
                                              for item in limitations):
            limitations.append(dict(path=value, reason=reason or
                                    'historical_wildcard_reference_not_protected'))
        return None
    sanitized = ''.join('x' if char in glob_chars else char for char in value)
    try:
        names = parts(sanitized)
    except ValueError:
        if limitations is not None:
            limitations.append(dict(path=value, reason=reason or
                                    ('legacy_wildcard_reference' if strict else
                                     'historical_wildcard_reference_not_protected')))
        if strict:
            raise
        return None
    if names[:3] != RUNS.split('/'):
        if limitations is not None:
            limitations.append(dict(path=value, reason=reason or
                                    ('legacy_wildcard_reference' if strict else
                                     'historical_wildcard_reference_not_protected')))
        return None
    original_names = value.rstrip('/').split('/')
    wildcard_index = next(i for i, name in enumerate(original_names)
                          if any(char in name for char in glob_chars))
    run_id = None
    if not any(char in original_names[3] for char in glob_chars):
        run_id = identifier(original_names[3])
    protected_path = '/'.join(names[:wildcard_index]) or RUNS
    if run_id and len(names) > 4:
        if names[4] not in {'attempts', 'sources', 'handoffs', 'discovery', 'merged'}:
            protected_path = RUNS + '/' + run_id
        elif names[4] == 'attempts' and wildcard_index > 5:
            identifier(original_names[5])
        elif names[4] == 'merged' and wildcard_index > 5 and names[5] != 'playwright':
            protected_path = RUNS + '/' + run_id
    if limitations is not None:
        item = dict(path=value, reason=reason or
                    ('legacy_wildcard_reference' if strict else
                     'historical_wildcard_reference_not_protected'))
        if run_id:
            item['run_id'] = run_id
        if protect and strict:
            item['protected_path'] = protected_path
        for index, existing in enumerate(limitations):
            if existing.get('path') == value and existing.get('reason') in {
                    'legacy_wildcard_reference', 'historical_wildcard_reference_not_protected'}:
                if protect and strict and 'protected_path' not in existing:
                    limitations[index] = item
                return protected_path if protect and strict else None
        limitations.append(item)
    return protected_path if protect and strict else None


def references(value, project, limitations=None, protect=True, strict=None, descriptive=False):
    """Recognize project paths in structured fields and Markdown/free text."""
    generic_roots = {'test/.cache', RUNS}
    for text in strings(value):
        # JSON plan text contains escaped separators; parsed fields contain single ones.
        text = text.replace('\\\\', '/').replace('\\', '/')
        text = text.replace(project.replace('\\', '/') + '/', '')
        if path_key(text).startswith('test/') and '\n' not in text:
            # Structured paths can contain spaces; retain the exact value too.
            reference = re.sub(r':\d+(?::\d+)?$', '', text.rstrip('/'))
            target = reference_target(reference, limitations, protect, strict)
            if target and (not descriptive or path_key(reference) not in generic_roots):
                yield target
        for match in re.finditer(r'test/[^\s\"\'`<>\]\[(){}:,;]+', text,
                                 re.I if CASE_INSENSITIVE else 0):
            candidate = match.group().rstrip('.#')
            target = reference_target(candidate.rstrip('/'), limitations, protect, strict)
            if target and path_key(candidate.rstrip('/')) not in generic_roots:
                yield target


def path_key(value):
    return value.casefold() if CASE_INSENSITIVE else value


def under(path, parent):
    path, parent = path_key(path), path_key(parent)
    return path == parent or path.startswith(parent + '/')


def deletable(path):
    names = parts(path)
    require(len(names) >= 6 and names[:3] == ['test', '.cache', 'runs'], 'outside retention whitelist')
    identifier(names[3])
    require(not small_artifact(names), 'small/state artifact is protected')
    if names[4] == 'attempts':
        require(len(names) >= 7, 'invalid attempt path')
        identifier(names[5])
    elif names[4] == 'merged':
        require(len(names) >= 7 and names[5] == 'playwright', 'outside retention whitelist')
    else:
        require(names[4] in {'sources', 'handoffs', 'discovery'}, 'outside retention whitelist')


def small_artifact(names):
    return ((len(names) == 5 and names[4] in ROOT_SMALL)
            or (len(names) == 7 and names[4] == 'attempts' and names[6] == 'command.json'))


class Snapshot:
    def __init__(self, tree):
        self.tree = tree
        self.cache = {}
        self.guards = {}

    def read(self, path):
        if path not in self.cache:
            data, stamp = self.tree.read(path)
            self.cache[path] = data
            self.guards[path] = stamp
        return self.cache[path]

    def json(self, path):
        return json.loads(self.read(path))


def protected_state_records(state):
    """Approval decisions and unresolved resource-cleanup evidence."""
    records = [r for r in state['humans'].values()
               if r.get('status') in {'approved', 'rejected'}]
    records += [r for r in state['resources'].values() if r.get('status') != 'absence_verified']
    for task in state['tasks'].values():
        if task.get('phase') == 'case_review' and task.get('status') in {'approved', 'complete'}:
            records.append(task)
        if task.get('status') in {'approved', 'complete'} and task.get('dispositions'):
            records.append(task['dispositions'])
    records += [event for event in state['events']
                if event.get('type') == 'human' and event.get('status') in {'approved', 'rejected'}
                or event.get('type') == 'task' and event.get('phase') == 'case_review'
                and event.get('status') in {'approved', 'complete'}]
    return records


def selection(tree, scopes, current_run=None, previously=None):
    """Build the only permitted deletion set; every unreadable run aborts globally."""
    snap = Snapshot(tree)
    quota_path = 'test/.cache/scheduling/quota.json'
    quota = snap.json(quota_path) if tree.exists(quota_path) else {'schema_version': 1, 'leases': {}}
    require(quota.get('schema_version') == 1 and isinstance(quota.get('leases'), dict), 'invalid quota ledger')
    reserved_runs = set()
    for lease in quota['leases'].values():
        require(isinstance(lease, dict) and lease.get('status') in {'active', 'released', 'reaped'}, 'invalid quota lease')
        if lease['status'] == 'active':
            reserved_runs.add(identifier(lease.get('run_id')))
    scheduling_guard = snap.guards.get(quota_path)
    spec_text = snap.read('test/spec.md').decode('utf-8')
    blocks = re.findall(r'```json workflow-config\s*\n(.*?)\n```', spec_text, re.S)
    require(len(blocks) == 1, 'invalid workflow-config')
    config = json.loads(blocks[0])
    fixed = scopes_of(config['scope']['fixed'])
    require(set(scopes) <= set(fixed), 'scopes outside project fixed scopes')
    cov = snap.json('test/plans/coverage.json')
    require(set(scopes_of(cov['scopes'])) == set(fixed), 'coverage/spec scope mismatch')
    inventory = tree.walk(RUNS) if tree.exists(RUNS) else {}
    assets = tree.walk('test/plans')
    aliases = {path_key(p.rstrip('/')): p.rstrip('/') for p in inventory}
    require(not CASE_INSENSITIVE or len(aliases) == len(inventory),
            'case-sensitive Windows cache aliases are not supported for retention')
    limitations = []
    refs = []
    origins = {}

    def add_ref(path, source_file, field, reason):
        refs.append((path, None))
        origins.setdefault(path_key(path), dict(path=path, source_file=source_file,
                                               field=field, reason=reason))

    def add_refs(value, source_file, field='', reason='explicit_reference',
                 descriptive=False, strict=None):
        for text, pointer in strings_at(value, field):
            for path in references(text, tree.path, limitations, strict=strict,
                                   descriptive=descriptive):
                add_ref(path, source_file, pointer, reason)

    add_refs(config, 'test/spec.md', reason='configured_reference')
    add_refs(cov, 'test/plans/coverage.json', reason='effective_coverage_reference')
    # Storage instructions are not evidence; structured paths and explicit links are.
    for path, value in [('test/spec.md', None), *assets.items()]:
        if value is None:
            data = snap.read(path)
            text = data.decode('utf-8')
            for line, body in enumerate(text.splitlines(), 1):
                add_refs(body, path, 'line:' + str(line), 'concrete_plan_reference',
                         descriptive=True)
            # Markdown links relative to the plan's directory are also protected.
            for index, target in enumerate(re.findall(r'\]\(([^)]+)\)', text)):
                if '://' not in target and not target.startswith('#'):
                    target = target.split('#')[0]
                    joined = Path(os.path.normpath(str(Path(path).parent / target))).as_posix()
                    require(not joined.startswith('../') and not os.path.isabs(joined), 'approval path escapes project')
                    target = reference_target(joined, limitations)
                    if target:
                        add_ref(target, path, '/links/' + str(index), 'explicit_plan_link')
    states = {}
    run_dirs = sorted(p for p in inventory if p.endswith('/') and len(p.split('/')) == 5)
    for directory in run_dirs:
        rid = identifier(directory.rstrip('/').split('/')[-1])
        state = snap.json(directory + 'state.json')
        require(isinstance(state, dict) and state.get('run_id') == rid, 'invalid run state: ' + rid)
        scopes_of(state.get('scopes'))
        require(isinstance(state.get('acceptance'), str), 'missing run acceptance: ' + rid)
        for field in ('tasks', 'humans', 'issues', 'resources', 'case_actions', 'source_checks'):
            require(isinstance(state.get(field), dict), 'unreadable run field: ' + field)
        for field in ('events', 'executions'):
            require(isinstance(state.get(field), list), 'unreadable run field: ' + field)
        require(isinstance(state.get('report_bundles', {}), dict), 'invalid report bundles')
        states[rid] = state
    eligible = {rid for rid, state in states.items() if state['acceptance'] in TERMINAL
                and not active(state) and not state.get('recovery_required') and rid != current_run
                and rid not in reserved_runs}
    bundles = {}
    for rid, state in states.items():
        prefix = RUNS + '/' + rid
        live = state['acceptance'] not in TERMINAL or active(state) or rid == current_run or state.get('recovery_required') or rid in reserved_runs
        if live:
            # Inherited issue diagnostics do not renew old attachment retention.
            # Approval, resource recovery and source-cache records remain intact.
            live_refs = dict(state, issues={
                key: issue for key, issue in state['issues'].items()
                if not issue.get('origin_run') or issue['origin_run'] == rid})
            add_refs(live_refs, prefix + '/state.json', reason='live_run_reference')
        else:
            # Preserve the diagnostic but do not let stale issue strings pin a terminal run.
            list(references(state, tree.path, limitations, protect=False))
        protected_records = protected_state_records(state)
        # Effective decisions and resource recovery remain authoritative even
        # after completion; ordinary old failures are not in this protection set.
        # Preserve field identity rather than referring to a synthetic record list.
        for field in ('humans', 'resources', 'tasks'):
            for key, record in state[field].items():
                if any(record is item for item in protected_records):
                    add_refs(record, prefix + '/state.json', '/' + field + '/' + key,
                             'resource_recovery' if field == 'resources' else 'effective_approval',
                             strict=True)
                elif field == 'tasks' and any(record.get('dispositions') is item for item in protected_records):
                    add_refs(record['dispositions'], prefix + '/state.json',
                             '/tasks/' + key + '/dispositions', 'effective_approval', strict=True)
        for index, record in enumerate(state['events']):
            if any(record is item for item in protected_records):
                add_refs(record, prefix + '/state.json', '/events/' + str(index),
                         'effective_approval', strict=True)
        for record in protected_records:
            if not isinstance(record, dict):
                continue
            is_resource = any(record is resource for resource in state['resources'].values()
                              if resource.get('status') != 'absence_verified')
            tokens = set(strings(record)) if is_resource else set()
            case_ids = set(record.get('case_ids', []) + record.get('affected_case_ids', [])) if is_resource else set()
            linked = [e for e in state['executions'] if is_resource and (
                e.get('execution_id') in tokens or e.get('bundle_id') in tokens
                or case_ids.intersection(e.get('case_ids', [])))]
            for execution in linked:
                bid = execution.get('bundle_id')
                bundle = state.get('report_bundles', {}).get(bid, {})
                raw_path = bundle.get('execution_result') if isinstance(bundle, dict) else None
                raw_paths = list(references(raw_path, tree.path, limitations,
                                            protect=True, strict=True))
                if is_resource and not raw_paths:
                    add_ref(prefix, prefix + '/state.json', '/resources',
                            'resource_recovery_evidence_unmapped')
                if is_resource:
                    for path in raw_paths:
                        add_ref(path, prefix + '/state.json',
                                '/executions/' + str(state['executions'].index(execution)),
                                'resource_execution_evidence')
                if not raw_paths or not isinstance(execution.get('runner_test_pointer'), str):
                    continue
                try:
                    raw = snap.json(raw_paths[0])
                    node = raw
                    for part in execution['runner_test_pointer'].strip('/').split('/'):
                        node = node[int(part)] if isinstance(node, list) else node[part]
                    results = node.get('results', [])
                    index = execution.get('runner_result_index')
                    if isinstance(index, int) and 0 <= index < len(results):
                        results = [results[index]]
                    for result in results:
                        for attachment in result.get('attachments', []):
                            value = attachment.get('path')
                            if not isinstance(value, str):
                                continue
                            attachment_path = Path(value)
                            candidates = [attachment_path] if attachment_path.is_absolute() else [
                                Path(tree.path) / attachment_path,
                                Path(tree.path) / Path(raw_paths[0]).parent / attachment_path,
                                Path(tree.path) / prefix / 'attempts' / bid / attachment_path]
                            for candidate in candidates:
                                try:
                                    relative = candidate.resolve().relative_to(Path(tree.path)).as_posix()
                                except (ValueError, OSError):
                                    continue
                                if tree.exists(relative):
                                    add_refs(relative, raw_paths[0],
                                             execution['runner_test_pointer'] + '/results',
                                             'resource_execution_attachment', strict=live)
                                    break
                except (ValueError, KeyError, TypeError, IndexError, OSError) as error:
                    limitations.append(dict(run_id=rid, bundle_id=bid,
                                            reason='historical_resource_execution_evidence_incomplete',
                                            detail=str(error)))
            usable_evidence = list(references(record.get('evidence', []), tree.path,
                                              limitations, protect=True, strict=True))
            if is_resource and not linked and not usable_evidence:
                limitations.append(dict(run_id=rid,
                                        reason='resource_recovery_evidence_unmapped'))
                add_ref(prefix, prefix + '/state.json', '/resources',
                        'resource_recovery_evidence_unmapped')
            is_approval = (any(record is human for human in state['humans'].values())
                           and record.get('status') in {'approved', 'rejected'})
            if is_approval and not usable_evidence:
                limitations.append(dict(run_id=rid, reason='approval_evidence_unmapped'))
                add_ref(prefix, prefix + '/state.json', '/humans',
                        'approval_evidence_unmapped')
        if rid not in eligible:
            add_ref(prefix, prefix + '/state.json', '/acceptance',
                    'current_active_or_unfinished_run')
            continue
        for bid, bundle in state.get('report_bundles', {}).items():
            try:
                identifier(bid)
            except ValueError:
                limitations.append(dict(run_id=rid, bundle_id=str(bid),
                                        reason='historical_bundle_id_invalid'))
                continue
            bprefix = prefix + '/attempts/' + bid
            if not isinstance(bundle, dict):
                limitations.append(dict(run_id=rid, bundle_id=bid,
                                        reason='historical_bundle_identity_incomplete', paths=[bprefix]))
                bundle = {}
            try:
                bscopes = scopes_of(bundle.get('scope'))
            except ValueError:
                limitations.append(dict(run_id=rid, bundle_id=bid,
                                        reason='historical_bundle_scope_unknown', paths=[bprefix]))
                bscopes = scopes_of(state['scopes'])
            if not set(bscopes) <= set(state['scopes']):
                limitations.append(dict(run_id=rid, bundle_id=bid,
                                        reason='historical_bundle_scope_invalid', paths=[bprefix]))
                bscopes = scopes_of(state['scopes'])
            if not set(bscopes) <= set(scopes):
                add_ref(bprefix, prefix + '/state.json', '/report_bundles/' + bid + '/scope',
                        'mixed_module_bundle')
                continue
            if bundle.get('bundle_id') != bid or bundle.get('run_id') != rid:
                limitations.append(dict(run_id=rid, bundle_id=bid,
                                        reason='historical_bundle_identity_incomplete', paths=[bprefix]))
            tracked = bundle.get('artifact_sha256')
            if not isinstance(tracked, dict):
                tracked = {}
                limitations.append(dict(run_id=rid, bundle_id=bid, reason='historical_bundle_registration_incomplete'))
            actual_hashes = {}
            missing = []
            for path, sha in tracked.items():
                if not isinstance(path, str):
                    limitations.append(dict(run_id=rid, bundle_id=bid,
                                            reason='historical_artifact_path_invalid'))
                    continue
                if any(char in path for char in '*?[]'):
                    reference_target(path, limitations, protect=False, strict=False,
                                     reason='historical_artifact_wildcard_path')
                    continue
                try:
                    parts(path)
                except ValueError:
                    limitations.append(dict(run_id=rid, bundle_id=bid,
                                            reason='historical_artifact_path_noncanonical', paths=[path]))
                    continue
                if not under(path, bprefix):
                    limitations.append(dict(run_id=rid, bundle_id=bid,
                                            reason='historical_artifact_outside_attempt', paths=[path]))
                    continue
                try:
                    deletable(path)
                except ValueError:
                    limitations.append(dict(run_id=rid, bundle_id=bid,
                                            reason='historical_artifact_not_recyclable', paths=[path]))
                    continue
                if not isinstance(sha, str) or not HEX.fullmatch(sha):
                    limitations.append(dict(run_id=rid, bundle_id=bid,
                                            reason='historical_artifact_hash_invalid', paths=[path]))
                    continue
                if tree.exists(path):
                    actual = hashlib.sha256(snap.read(path)).hexdigest()
                    actual_hashes[path] = actual
                    if actual != sha:
                        limitations.append(dict(run_id=rid, bundle_id=bid,
                                                reason='historical_artifact_hash_mismatch', paths=[path]))
                else:
                    if previously and previously.get(path, {}).get('sha256') == sha:
                        actual_hashes[path] = sha
                    else:
                        missing.append(path)
            if missing:
                limitations.append(dict(run_id=rid, bundle_id=bid, reason='historical_artifact_unexpectedly_missing', paths=missing))
            if (bundle.get('report_consistency') not in {'passed', 'json_only'}
                    or not isinstance(bundle.get('runner_counts'), dict)
                    or not isinstance(bundle.get('execution_result'), str)
                    or bundle.get('execution_result') not in tracked):
                limitations.append(dict(run_id=rid, bundle_id=bid,
                                        reason='historical_bundle_registration_incomplete'))
            for field in ('execution_result', 'html_report'):
                path = bundle.get(field)
                if isinstance(path, str):
                    if any(char in path for char in '*?[]'):
                        reference_target(path, limitations, protect=False, strict=False,
                                         reason='historical_bundle_report_wildcard_path')
                    else:
                        try:
                            parts(path)
                        except ValueError:
                            limitations.append(dict(run_id=rid, bundle_id=bid,
                                                    reason='historical_bundle_report_path_noncanonical', paths=[path]))
                            continue
                        if not under(path, bprefix):
                            limitations.append(dict(run_id=rid, bundle_id=bid,
                                                    reason='historical_bundle_report_outside_attempt', paths=[path]))
            bundles[bprefix] = dict(run_id=rid, bundle_id=bid, scopes=bscopes,
                                   report_consistency=bundle.get('report_consistency'),
                                   runner_counts=bundle.get('runner_counts', {}),
                                   setup_targets=bundle.get('setup_targets'),
                                   business_targets=bundle.get('business_targets'),
                                   business_attempts=bundle.get('business_attempts'),
                                   raw_business_skipped=bundle.get('raw_business_skipped'),
                                   artifact_sha256=actual_hashes,
                                   missing_unrecovered=bool(missing))
    merge_recyclable = set()

    merged_scopes = {}
    for rid in eligible:
        run_prefix = RUNS + '/' + rid
        merged_dir = run_prefix + '/merged'
        merged_prefix = merged_dir + '/playwright'
        if not any(path.startswith(merged_dir + '/') for path in inventory):
            continue
        merged = states[rid].get('merged_report')
        if not isinstance(merged, dict):
            limitations.append(dict(run_id=rid, reason='historical_merged_report_identity_missing',
                                    paths=[merged_dir]))
            if not set(states[rid]['scopes']) <= set(scopes):
                add_ref(merged_dir, run_prefix + '/state.json', '/scopes', 'mixed_module_merge')
                continue
            merged_scopes[rid] = scopes_of(states[rid]['scopes'])
            merge_recyclable.add(rid)
            continue
        source_ids = merged.get('bundle_ids')
        known_ids = set(states[rid].get('report_bundles', {}))
        # Old incomplete registration cannot pin an entire report. For a partial
        # cleanup, retain the merge unless all scopes of its run are selected.
        complete_mapping = (isinstance(source_ids, list) and bool(source_ids)
                            and all(isinstance(bid, str) and ID.fullmatch(bid) for bid in source_ids)
                            and len(source_ids) == len(set(source_ids))
                            and set(source_ids) == known_ids)
        associated_scopes = set()
        if complete_mapping:
            for bid in source_ids:
                metadata = bundles.get(run_prefix + '/attempts/' + bid)
                associated_scopes.update(metadata['scopes'] if metadata else states[rid]['scopes'])
        else:
            limitations.append(dict(run_id=rid, reason='historical_merged_report_sources_incomplete',
                                    paths=[merged_dir]))
            associated_scopes.update(states[rid]['scopes'])
        if not associated_scopes:
            associated_scopes.update(states[rid]['scopes'])
        if not associated_scopes <= set(scopes):
            add_ref(merged_dir, run_prefix + '/state.json', '/merged_report', 'mixed_module_merge')
            continue
        merged_scopes[rid] = sorted(associated_scopes)
        tracked = merged.get('artifact_sha256', {})
        if not isinstance(tracked, dict):
            tracked = {}
            limitations.append(dict(run_id=rid, reason='historical_merged_report_registration_incomplete',
                                    paths=[merged_dir]))
        for path, sha in tracked.items():
            if not isinstance(path, str):
                continue
            if any(char in path for char in '*?[]'):
                reference_target(path, limitations, protect=False, strict=False,
                                 reason='historical_merged_artifact_wildcard_path')
                continue
            try:
                deletable(path)
            except ValueError:
                limitations.append(dict(run_id=rid, reason='historical_merged_artifact_not_recyclable',
                                        paths=[path]))
                continue
            if not under(path, merged_prefix):
                limitations.append(dict(run_id=rid, reason='historical_merged_artifact_outside_directory',
                                        paths=[path]))
                continue
            if not isinstance(sha, str) or not HEX.fullmatch(sha):
                limitations.append(dict(run_id=rid, reason='historical_merged_artifact_hash_invalid',
                                        paths=[path]))
            elif tree.exists(path) and hashlib.sha256(snap.read(path)).hexdigest() != sha:
                limitations.append(dict(run_id=rid, reason='historical_merged_artifact_hash_mismatch',
                                        paths=[path]))
        html_path = merged.get('html_report')
        if isinstance(html_path, str):
            if any(char in html_path for char in '*?[]'):
                reference_target(html_path, limitations, protect=False, strict=False,
                                 reason='historical_merged_report_path_wildcard')
            else:
                try:
                    parts(html_path)
                except ValueError:
                    limitations.append(dict(run_id=rid, reason='historical_merged_report_path_noncanonical',
                                            paths=[html_path]))
                    html_path = None
                if html_path and not under(html_path, merged_prefix):
                    limitations.append(dict(run_id=rid, reason='historical_merged_report_outside_directory',
                                            paths=[html_path]))
        inputs = merged.get('inputs', {})
        if not isinstance(inputs, dict):
            limitations.append(dict(run_id=rid, reason='historical_merged_inputs_incomplete',
                                    paths=[merged_dir]))
            inputs = {}
        valid_source_ids = set(source_ids) if complete_mapping else set()
        for bid, paths in inputs.items():
            if bid not in valid_source_ids or not isinstance(paths, dict):
                limitations.append(dict(run_id=rid, reason='historical_merged_inputs_incomplete',
                                        paths=[merged_dir]))
                continue
            source_prefix = run_prefix + '/attempts/' + bid
            for path in paths:
                if not isinstance(path, str):
                    continue
                if any(char in path for char in '*?[]'):
                    reference_target(path, limitations, protect=False, strict=False,
                                     reason='historical_merged_input_wildcard_path')
                    continue
                try:
                    parts(path)
                except ValueError:
                    limitations.append(dict(run_id=rid, reason='historical_merged_input_path_noncanonical',
                                            paths=[path]))
                    continue
                if not under(path, source_prefix):
                    limitations.append(dict(run_id=rid, reason='historical_merged_input_outside_attempt',
                                            paths=[path]))
        # Explicit recovery/approval paths are filtered per file below. A trace
        # retained for recovery need not pin every duplicate in the merged HTML.
        merge_recyclable.add(rid)
    # Effective baselines/reuse records may point at a source-cache manifest rather
    # than every snapshot file. Protect that manifest's transitive file inventory.
    followed = set()
    cursor = 0
    while cursor < len(refs):
        path, _ = refs[cursor]
        path = aliases.get(path_key(path), path)
        refs[cursor] = (path, refs[cursor][1])
        cursor += 1
        if path in followed or not path.endswith('.json'):
            continue
        followed.add(path)
        names = path.split('/')
        if path in inventory and len(names) > 4 and names[4] in {'sources', 'handoffs', 'discovery'}:
            value = snap.json(path)
            add_refs(value, path, reason='transitive_snapshot_reference')
    # One deterministic explanation per matched prefix; avoid rechecking duplicate
    # references from state/event mirrors for every historical attachment.
    refs = list(dict.fromkeys(refs))
    files = []
    candidates = set()
    protected = {}
    excluded = {}

    def exclude(reason):
        excluded[reason] = excluded.get(reason, 0) + 1

    for path, value in inventory.items():
        if value is not None or path.endswith('/'):
            continue
        names = path.split('/')
        if len(names) < 6 or small_artifact(names):
            continue
        if names[3] not in eligible:
            exclude('current_active_or_unfinished_run')
            continue
        state = states[names[3]]
        bundle = None
        if names[4] == 'attempts' and names[5].startswith('.'):
            exclude('attempt_staging_directory')
            continue
        if names[4] == 'attempts' and len(names) >= 7:
            bprefix = '/'.join(names[:6])
            bundle = bundles.get(bprefix)
            if not bundle:
                if not set(state['scopes']) <= set(scopes):
                    exclude('outside_selected_modules')
                    continue
                identifier(names[5])
                bundle = dict(run_id=names[3], bundle_id=names[5], scopes=scopes_of(state['scopes']),
                              artifact_sha256={}, report_consistency=None, runner_counts={},
                              missing_unrecovered=False)
                bundles[bprefix] = bundle
                limitations.append(dict(run_id=names[3], bundle_id=names[5],
                                        reason='historical_unregistered_attempt', paths=[bprefix]))
            if path not in bundle['artifact_sha256'] and names[6] not in {
                    'results', 'report', 'blob', 'attachments', 'output', 'test-results'}:
                exclude('outside_report_whitelist')
                continue
            fscopes = bundle['scopes']
        elif (names[4] == 'merged' and names[3] in merge_recyclable
              and len(names) >= 7 and names[5] == 'playwright'):
            fscopes = merged_scopes.get(names[3], [])
            if not fscopes:
                exclude('outside_selected_modules')
                continue
        elif names[4] in {'sources', 'handoffs', 'discovery'} and set(state['scopes']) <= set(scopes):
            fscopes = scopes_of(state['scopes'])
        else:
            exclude('outside_selected_modules_or_whitelist')
            continue
        candidates.add(path)
        match = next((ref for ref, origin in refs
                      if (under(path, ref) or path_key(path).startswith(path_key(ref) + ' '))
                      and not (origin and under(path, origin))), None)
        if match is not None:
            key = path_key(match)
            item = protected.setdefault(key, dict(reference=origins[key], paths=set()))
            item['paths'].add(path)
            continue
        deletable(path)
        snap.read(path)
        files.append(dict(path=path, **snap.guards[path], scopes=fscopes,
                          run_id=names[3], bundle_id=bundle['bundle_id'] if bundle else None,
                          reason='ended_inactive_history' if bundle else 'unreferenced_temporary'))
    selected = {f['path'] for f in files}
    diagnostics = candidate_diagnostics(candidates, selected, protected, excluded)
    markers = []
    for bundle in bundles.values():
        old = previously or {}
        prefix = RUNS + '/' + bundle['run_id'] + '/attempts/' + bundle['bundle_id']
        payloads = sorted({p for p in list(inventory) + list(old)
                           if under(p, prefix) and not p.endswith('/') and not small_artifact(p.split('/'))})
        complete = not bundle.get('missing_unrecovered') and all(
            p in selected or old.get(p, {}).get('sha256') == sha
            for p, sha in bundle['artifact_sha256'].items())
        complete = complete and all(p in selected or p in old for p in payloads)
        markers.append(dict(bundle, complete=complete, payload_paths=payloads))
    return dict(files=sorted(files, key=lambda f: f['path']), bundles=markers, limitations=limitations,
                diagnostics=diagnostics,
                scheduling_guard=scheduling_guard,
                guards=snap.guards, inventory=dict(inventory, **assets))


def plan_body(plan):
    return {k: v for k, v in plan.items() if k != 'plan_id'}


def authority(tree, plan):
    require(isinstance(plan, dict) and plan.get('schema_version') == 1, 'unsupported retention plan')
    pid = plan.get('plan_id')
    require(isinstance(pid, str) and HEX.fullmatch(pid) and digest(plan_body(plan)) == pid, 'plan digest mismatch')
    require(plan.get('project') == tree.path, 'plan belongs to another project')
    require(load(tree, STORE + '/plans/' + pid + '.json') == plan, 'plan differs from registered immutable plan')
    return pid


def valid_manifest(tree, manifest):
    require(isinstance(manifest, dict) and manifest.get('schema_version') == 1, 'invalid retention manifest')
    require(isinstance(manifest.get('plan_id'), str) and HEX.fullmatch(manifest['plan_id']), 'invalid manifest plan ID')
    plan = load(tree, STORE + '/plans/' + manifest['plan_id'] + '.json')
    authority(tree, plan)
    require(manifest.get('project') == tree.path, 'manifest project mismatch')
    expected = {f['path']: f for f in plan['files']}
    require(set(manifest['files']) == set(expected), 'manifest file set mismatch')
    for path, record in manifest['files'].items():
        deletable(path)
        require(record.get('original') == expected[path] and record.get('status') in {'pending', 'staged', 'deleted'},
                'manifest file metadata mismatch')
    require(manifest.get('bundles') == plan['bundles'], 'manifest bundle metadata mismatch')
    return plan


def manifests(tree):
    directory = STORE + '/manifests'
    if not tree.exists(directory):
        return []
    result = []
    for path in tree.walk(directory):
        if not path.endswith('/'):
            require(path.endswith('.json'), 'unexpected retention manifest file')
            value = load(tree, path)
            valid_manifest(tree, value)
            result.append(value)
    return result


def recycled_records(tree):
    result = {}
    for manifest in manifests(tree):
        for path, record in manifest['files'].items():
            if record['status'] == 'deleted' and not tree.exists(path):
                result[path] = record['original']
    return result


def make_plan(project, scopes, current_run=None):
    """Register and return a plan. Export it unchanged for apply_plan()."""
    started = perf_counter()
    scopes = scopes_of(scopes)
    if current_run is not None:
        identifier(current_run)
    with locked(project) as tree:
        data = selection(tree, scopes, current_run, recycled_records(tree))
        plan = dict(schema_version=1, project=tree.path, created_at=now(), scopes=scopes,
                    current_run=current_run, **data)
        plan['cleanup_scan_seconds'] = perf_counter() - started
        plan['plan_id'] = digest(plan)
        tree.write(STORE + '/plans/' + plan['plan_id'] + '.json', plan, exclusive=True)
        return plan


def verify_snapshot(tree, plan):
    quota_path = 'test/.cache/scheduling/quota.json'
    guard = tree.read(quota_path)[1] if tree.exists(quota_path) else None
    require(guard == plan.get('scheduling_guard'), 'scheduling reservations changed; create a new plan')
    inventory = tree.walk(RUNS) if tree.exists(RUNS) else {}
    inventory.update(tree.walk('test/plans'))
    # Directory names/inodes and all files must still agree; deletion by this
    # transaction is the sole permissible difference on crash recovery.
    require(inventory == plan['inventory'], 'project inventory changed; create a new plan')
    for path, stamp in plan['guards'].items():
        require(tree.read(path)[1] == stamp, 'protected state/reference or artifact changed: ' + path)


def quarantine(tree, original, target):
    """Move first, verify again, then unlink only the private staged inode."""
    path = original['path']
    expected = {k: original[k] for k in ('sha256', 'bytes', 'identity')}
    tree.move(path, target, expected)
    try:
        data, stamp = tree.read(target)
        require(stamp['sha256'] == original['sha256'] and stamp['bytes'] == original['bytes']
                and stamp['identity'][:4] == original['identity'][:4], 'artifact replaced during staging: ' + path)
    except BaseException:
        tree.restore(target, path)
        raise


def restore(tree, original, stage):
    """Exclusive restoration on a failed final reference/identity check."""
    tree.restore(stage, original['path'])


def apply_plan(project, plan):
    """Apply only the registered plan; safe to repeat after completion/interruption."""
    started = perf_counter()
    with locked(project) as tree:
        pid = authority(tree, plan)
        mpath = STORE + '/manifests/' + pid + '.json'
        if tree.exists(mpath):
            manifest = load(tree, mpath)
            valid_manifest(tree, manifest)
        else:
            verify_snapshot(tree, plan)
            fresh = selection(tree, plan['scopes'], plan['current_run'], recycled_records(tree))
            require(all(value == plan.get(key) for key, value in fresh.items()
                        if key != 'diagnostics' or key in plan),
                    'eligibility or protected references changed')
            manifest = dict(schema_version=1, plan_id=pid, project=tree.path, started_at=now(),
                            completed_at=None, bundles=plan['bundles'],
                            files={f['path']: dict(original=f, status='pending', recycled_at=None)
                                   for f in plan['files']})
            tree.write(mpath, manifest, exclusive=True)
        replayed = manifest['completed_at'] is not None
        # Resumption revalidates all control hashes and directory identities,
        # allowing only files that this journal has already staged/deleted.
        removed = set()
        for index, item in enumerate(plan['files']):
            path = item['path']
            stage = STORE + '/quarantine/' + pid + '/' + str(index)
            record = manifest['files'][path]
            if record['status'] != 'pending' or tree.exists(stage):
                require(not tree.exists(path), 'recycled path recreated; refusing plan replay: ' + path)
                removed.add(path)
        expected = dict(plan)
        expected['inventory'] = {p: v for p, v in plan['inventory'].items() if p not in removed}
        expected['guards'] = {p: v for p, v in plan['guards'].items() if p not in removed}
        verify_snapshot(tree, expected)
        # Acceptance/lease can change eligibility with time as well as bytes.
        for rid in {f['run_id'] for f in plan['files']}:
            state = load(tree, RUNS + '/' + rid + '/state.json')
            require(state['acceptance'] in TERMINAL and not active(state) and rid != plan['current_run'],
                    'run is no longer eligible: ' + rid)
        staged = []
        try:
            for index, original in enumerate(plan['files']):
                stage = STORE + '/quarantine/' + pid + '/' + str(index)
                record = manifest['files'][original['path']]
                if record['status'] == 'deleted':
                    continue
                already_staged = tree.exists(stage)
                if not already_staged:
                    require(record['status'] == 'pending', 'staged artifact unexpectedly missing')
                    quarantine(tree, original, stage)
                staged.append((original, stage))
                if already_staged:
                    data, stamp = tree.read(stage)
                    require(stamp['sha256'] == original['sha256'] and stamp['bytes'] == original['bytes']
                            and stamp['identity'][:4] == original['identity'][:4], 'quarantine artifact changed')
            removed = {f['path'] for f in plan['files']}
            expected['inventory'] = {p: v for p, v in plan['inventory'].items() if p not in removed}
            expected['guards'] = {p: v for p, v in plan['guards'].items() if p not in removed}
            verify_snapshot(tree, expected)
        except BaseException:
            for original, stage in reversed(staged):
                restore(tree, original, stage)
            raise
        for original, stage in staged:
            manifest['files'][original['path']].update(status='deleted', recycled_at=now())
        # Journal before dropping quarantine bytes: interruption leaves extra
        # bytes, never an unexplained missing original. All guards rechecked after
        # staging; cooperative writers are excluded by workflow.lock throughout.
        tree.write(mpath, manifest)
        for index, original in enumerate(plan['files']):
            stage = STORE + '/quarantine/' + pid + '/' + str(index)
            if tree.exists(stage):
                # delete() verifies the bytes with the same backend before unlink.
                tree.delete(stage, original['sha256'])
        manifest['completed_at'] = manifest['completed_at'] or now()
        if not replayed:
            manifest['timing'] = dict(cleanup_scan_seconds=plan.get('cleanup_scan_seconds'),
                                      cleanup_apply_seconds=perf_counter() - started)
        tree.write(mpath, manifest)
        return dict(plan_id=pid, manifest=mpath, files=len(plan['files']),
                    bytes=sum(f['bytes'] for f in plan['files']), completed=True,
                    replayed=replayed, diagnostics=plan.get('diagnostics'),
                    timing=manifest.get('timing'),
                    limitations=plan.get('limitations', []))


def is_recycled(project, path):
    """True only for a journal-confirmed original path that is currently missing.

    Pure read API: no lock acquisition/writes, so workflow may call while holding
    workflow.lock. Unsafe paths, symlinks, damaged journals and recreated files
    return False. Absolute paths must be lexically inside the canonical project.
    """
    tree = None
    try:
        tree = Tree(project)
        value = os.fspath(path)
        if os.path.isabs(value):
            value = Path(value).relative_to(Path(tree.path)).as_posix()
        deletable(value)
        return value in recycled_records(tree)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    finally:
        if tree:
            tree.close()


def recycled_bundle(project, run_id, bundle_id):
    """Return original counts/consistency only when ALL tracked artifacts are recycled.

    This is a historical marker, never a new report-consistency pass or a test
    result. Partial failure-evidence retention intentionally returns None.
    """
    tree = None
    try:
        identifier(run_id)
        identifier(bundle_id)
        tree = Tree(project)
        records = recycled_records(tree)
        for manifest in manifests(tree):
            if not manifest['completed_at']:
                continue
            for bundle in manifest['bundles']:
                if (bundle['run_id'] == run_id and bundle['bundle_id'] == bundle_id and bundle['complete']
                        and all(p in records for p in bundle.get('payload_paths', []))
                        and all(records.get(p, {}).get('sha256') == sha for p, sha in bundle['artifact_sha256'].items())):
                    return dict(bundle, recycled=True, plan_id=manifest['plan_id'],
                                recycled_at=manifest['completed_at'])
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    finally:
        if tree:
            tree.close()


def external_read(path):
    require('..' not in Path(path).parts, 'plan input path traversal')
    absolute = os.path.abspath(os.fspath(path))
    tree = Tree(Path(absolute).parent.resolve(strict=True))
    try:
        return json.loads(tree.read(Path(absolute).name)[0])
    finally:
        tree.close()


def export_plan(path, plan):
    require('..' not in Path(path).parts, 'plan output path traversal')
    absolute = str(Path(path).absolute().parent.resolve(strict=True) / Path(path).name)
    require(not Path(absolute).is_relative_to(Path(plan['project']) / 'test'),
            'export plan outside project/test')
    tree = Tree(Path(absolute).parent)
    try:
        tree.write(Path(absolute).name, plan, exclusive=True)
    finally:
        tree.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    cmd = commands.add_parser('plan')
    cmd.add_argument('--project', required=True)
    cmd.add_argument('--scopes', required=True)
    cmd.add_argument('--current-run')
    cmd.add_argument('--output', required=True)
    cmd = commands.add_parser('apply')
    cmd.add_argument('--project', required=True)
    cmd.add_argument('--input', required=True)
    args = parser.parse_args(argv)
    try:
        require('..' not in Path(args.project).parts, 'project path traversal')
        if args.command == 'plan':
            plan = make_plan(Path(args.project).resolve(strict=True), args.scopes.split(','), args.current_run)
            export_plan(args.output, plan)
            result = dict(plan_id=plan['plan_id'], output=os.path.abspath(args.output),
                          files=len(plan['files']), bytes=sum(f['bytes'] for f in plan['files']),
                          diagnostics=plan['diagnostics'],
                          cleanup_scan_seconds=plan['cleanup_scan_seconds'])
        else:
            result = apply_plan(Path(args.project).resolve(strict=True), external_read(args.input))
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        result = {'error': str(error)}
        if isinstance(error, CleanupCandidateMismatch):
            result['diagnostics'] = error.diagnostics
        print(json.dumps(result, ensure_ascii=True))
        return 2


if __name__ == '__main__':
    sys.exit(main())
