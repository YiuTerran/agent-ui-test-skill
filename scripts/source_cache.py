#!/usr/bin/env python3
"""Verify immutable source snapshots after a fresh external observation. No network."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


_platform_spec = importlib.util.spec_from_file_location(
    'e2e_path_platform', Path(__file__).with_name('platform_support.py'))
platform_support = importlib.util.module_from_spec(_platform_spec)
_platform_spec.loader.exec_module(platform_support)


def safe_file(project, value):
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError('snapshot file must be project-relative')
    platform_support.relative_parts(value)
    target = project / value
    if '..' in Path(value).parts or any(
            p.is_symlink() or (p.exists() and getattr(p.lstat(), 'st_file_attributes', 0) & 0x400)
            for p in [target, *target.parents]):
        raise ValueError('snapshot path traversal or symlink')
    if not target.resolve().is_relative_to(project) or not target.is_file():
        raise ValueError('snapshot file missing or outside project: ' + value)
    return target


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(project, request):
    """The reader supplies version/hash identity; byte digests protect local integrity."""
    project = Path(project).resolve()
    if not isinstance(request, dict):
        raise ValueError('capture request must be an object')
    if not request.get('source_id') or not request.get('fingerprint') or not request.get('files'):
        raise ValueError('capture requires source_id, fingerprint and complete snapshot files')
    if (not isinstance(request['files'], list)
            or not all(isinstance(p, str) for p in request['files'])
            or len(set(request['files'])) != len(request['files'])):
        raise ValueError('snapshot files must be a unique relative-path array')
    if request.get('fingerprint_basis') not in {'canonical_content', 'document_bytes'}:
        raise ValueError('capture requires reliable content fingerprint_basis')
    files = {p: sha(safe_file(project, p)) for p in request['files']}
    return {k: request.get(k) for k in ['source_id', 'revision', 'fingerprint', 'fingerprint_basis']} | {
        'schema_version': 1, 'captured_at': datetime.now(timezone.utc).isoformat(), 'files': files}


def verify(project, request):
    project = Path(project).resolve()
    if not isinstance(request, dict) or not isinstance(request.get('observation', {}), dict):
        raise ValueError('verify request and observation must be objects')
    observation = request.get('observation', {})
    result = {'action': 'full_read', 'cache_hit': False, 'reason': None, 'verified_files': 0,
              'source_id': observation.get('source_id')}
    if observation.get('readable') is not True:
        return dict(result, action='unavailable', reason='latest_source_unreadable')
    if not request.get('round_started_at') or not observation.get('observed_at'):
        raise ValueError('verify requires round_started_at and actual observed_at')
    def date(value):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('source timestamps require timezone')
        return parsed
    if not date(request['round_started_at']) <= date(observation['observed_at']) <= datetime.now(timezone.utc):
        raise ValueError('source observation is stale or in the future')
    # An observation artifact proves the read was recorded, not its external authenticity.
    safe_file(project, observation.get('evidence'))
    try:
        manifest = json.loads(safe_file(project, request.get('manifest')).read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return dict(result, reason='snapshot_manifest_missing_or_invalid')
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1:
        return dict(result, reason='snapshot_manifest_missing_or_invalid')
    keys = ['source_id', 'revision', 'fingerprint', 'fingerprint_basis']
    if (not observation.get('fingerprint') or
            observation.get('fingerprint_basis') not in {'canonical_content', 'document_bytes'} or
            any(observation.get(k) != manifest.get(k) for k in keys)):
        return dict(result, reason='source_identity_changed_or_unreliable')
    if not isinstance(manifest.get('files'), dict) or not manifest['files']:
        return dict(result, reason='snapshot_inventory_empty')
    for path, expected in manifest['files'].items():
        try:
            if sha(safe_file(project, path)) != expected:
                return dict(result, reason='snapshot_integrity_failed', failed_path=path)
        except (ValueError, OSError):
            return dict(result, reason='snapshot_integrity_failed', failed_path=path)
        result['verified_files'] += 1
    return dict(result, action='reuse', cache_hit=True, reason='fresh_identity_and_snapshot_match',
                files=list(manifest['files']))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['capture', 'verify'])
    parser.add_argument('--project', required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    try:
        request = json.loads(Path(args.input).read_text(encoding='utf-8'))
        result = globals()[args.command](Path(args.project).resolve(), request)
        text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            output = Path(args.output)
            if output.exists():
                raise ValueError('refusing to overwrite immutable source output')
            with output.open('x', encoding='utf-8') as stream:
                stream.write(text)
        print(text, end='')
        return 0
    except (ValueError, OSError, TypeError, KeyError, AttributeError) as error:
        print(json.dumps({'error': str(error)}))
        return 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
