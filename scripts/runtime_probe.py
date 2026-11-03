#!/usr/bin/env python3
"""Probe local artifact operations in the inherited runtime; never run product tests."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

_platform_spec = importlib.util.spec_from_file_location(
    'e2e_probe_platform', Path(__file__).with_name('platform_support.py'))
platform_support = importlib.util.module_from_spec(_platform_spec)
_platform_spec.loader.exec_module(platform_support)

NODE_PROBE = r"""
const fs = require('node:fs');
const path = require('node:path');
const root = process.argv[1];
let operation = 'mkdir';
try {
  const directory = path.join(root, 'node-directory');
  fs.mkdirSync(directory, { recursive: true });
  fs.mkdirSync(directory, { recursive: true });
  operation = 'playwright_artifact_directory';
  fs.mkdtempSync(path.join(root, '.playwright-artifacts-'));
  operation = 'exclusive_write';
  const original = path.join(directory, 'original');
  const replacement = path.join(directory, 'replacement');
  fs.writeFileSync(original, 'original', { flag: 'wx' });
  const descriptor = fs.openSync(replacement, 'wx', 0o600);
  try {
    fs.writeSync(descriptor, 'replacement');
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
  operation = 'atomic_replace';
  fs.renameSync(replacement, original);
  if (fs.readFileSync(original, 'utf8') !== 'replacement')
    throw Object.assign(new Error(), { code: 'CONTENT_MISMATCH' });
  process.stdout.write(JSON.stringify({ valid: true }));
} catch (error) {
  process.stdout.write(JSON.stringify({ valid: false, operation, error_type: error.code || 'Error' }));
  process.exitCode = 2;
}
"""


def probe_parent(project, directory):
    parts = platform_support.relative_parts(directory)
    if parts[:2] != ['test', '.cache']:
        raise ValueError('runtime probe directory must be inside test/.cache/')
    current = project
    for component in parts:
        current = current / component
        if current.is_symlink() or (current.exists()
                                   and getattr(current.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('runtime probe directory contains a symlink or reparse point')
        platform_support.ensure_directory(current)
        if not current.resolve(strict=True).is_relative_to(project):
            raise ValueError('runtime probe directory escapes project')
    return current


def python_probe(root):
    directory = root / 'python-directory'
    directory.mkdir()
    try:
        directory.mkdir(exist_ok=True)
    except FileExistsError:
        if not directory.is_dir():
            raise
        return {'valid': False, 'error_type': 'fs_mkdir_not_idempotent',
                'operation': 'mkdir_exist_ok',
                'message': '当前运行时的 mkdir(exist_ok=True) 无法复用已有目录；'
                           '请检查文件系统拦截与运行环境兼容性。'}
    platform_support.ensure_directory(directory)
    with (directory / 'lock').open('a+b') as stream, \
            platform_support.file_lock(stream.fileno(), timeout=5):
        original = directory / 'original'
        original.write_bytes(b'original')
        replacement = directory / 'replacement'
        with replacement.open('xb') as output:
            output.write(b'replacement')
            output.flush()
            os.fsync(output.fileno())
        os.replace(replacement, original)
        platform_support.sync_directory(directory)
        if original.read_bytes() != b'replacement':
            raise ValueError('atomic replacement content mismatch')
    return {'valid': True}


def node_probe(project, root, executable):
    result = subprocess.run([executable, '-e', NODE_PROBE, str(root)], cwd=project,
                            text=True, encoding='utf-8', capture_output=True, timeout=15)
    try:
        value = json.loads(result.stdout)
        if (not isinstance(value, dict) or type(value.get('valid')) is not bool
                or (value['valid'] and result.returncode != 0)):
            raise ValueError()
    except (ValueError, TypeError):
        return {'valid': False, 'error_type': 'invalid_probe_output',
                'exit_code': result.returncode}
    if any(key in value and not isinstance(value[key], str) for key in ('operation', 'error_type')):
        return {'valid': False, 'error_type': 'invalid_probe_output',
                'exit_code': result.returncode}
    return {key: value[key] for key in ('valid', 'operation', 'error_type') if key in value}


def inspect(project, directory='test/.cache/runtime-probes', node=None, require_node=False):
    project = Path(project).resolve()
    result = {'valid': False, 'checks': {},
              'python_runtime': {'version': '.'.join(str(part) for part in sys.version_info[:3]),
                                 'executable': sys.executable,
                                 'minimum_version': '.'.join(
                                     map(str, platform_support.MIN_PYTHON_VERSION))},
              'injection_present': {name: bool(os.environ.get(name))
                                    for name in ('PYTHONPATH', 'NODE_OPTIONS')},
              'sitecustomize_loaded': 'sitecustomize' in sys.modules,
              'note': 'Local filesystem operations only; browser launch, network, product tests '
                      'and business cleanup are not proved. The inherited environment is unchanged.'}
    if require_node and not node:
        result['checks']['node'] = {'valid': False, 'error_type': 'node_required'}
        result['action'] = 'Supply the actual project Node executable and rerun before dispatch.'
        return result
    root = None
    stage = 'directory'
    try:
        if not project.is_dir():
            raise ValueError('project must be an existing directory')
        parent = probe_parent(project, directory)
        root = Path(tempfile.mkdtemp(prefix='.runtime-probe-', dir=parent))
        stage = 'python'
        result['checks']['python'] = python_probe(root)
        if node is not None:
            stage = 'node'
            result['checks']['node'] = node_probe(project, root, node)
        else:
            result['checks']['node'] = {'valid': None, 'status': 'not_checked'}
        result['valid'] = all(item['valid'] is not False for item in result['checks'].values())
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        result['checks'][stage] = {'valid': False, 'error_type': type(error).__name__,
                                  'errno': getattr(error, 'errno', None)}
    finally:
        if root is not None:
            try:
                shutil.rmtree(root)
            except OSError as error:
                result['valid'] = False
                result['checks']['cleanup'] = {'valid': False, 'error_type': type(error).__name__,
                                               'probe_directory': root.relative_to(project).as_posix()}
    if not result['valid']:
        result['action'] = ('Pause affected dispatch; retain original errors and use the host approval '
                            'or runtime repair path. Do not strip injected hooks or edit ledgers.')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--directory', default='test/.cache/runtime-probes')
    parser.add_argument('--node', help='actual Node executable used by the project runner')
    parser.add_argument('--require-node', action='store_true',
                        help='fail unless the actual project Node executable is supplied and passes')
    args = parser.parse_args(argv)
    result = inspect(args.project, args.directory, args.node, args.require_node)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if result['valid'] else 2


if __name__ == '__main__':
    sys.exit(main())
