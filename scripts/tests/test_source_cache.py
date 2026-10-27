import importlib.util
import test_platform_support as platform_tests
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('source_cache', Path(__file__).resolve().parents[1] / 'source_cache.py')
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


class SourceCacheTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'doc.md').write_text('body')
        (self.root / 'image.png').write_bytes(b'image')
        (self.root / 'observation.json').write_text('{}')
        self.identity = {'source_id': 'S', 'revision': '1', 'fingerprint': 'digest',
                         'fingerprint_basis': 'canonical_content'}
        manifest = cache.capture(self.root, dict(self.identity, files=['doc.md', 'image.png']))
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        self.request = {'manifest': 'manifest.json', 'round_started_at': '2026-01-01T00:00:00+00:00',
                        'observation': dict(self.identity, readable=True, evidence='observation.json',
                                            observed_at='2026-01-02T00:00:00+00:00')}

    def test_matching_source_reuses_complete_bytes_without_fetch(self):
        result = cache.verify(self.root, self.request)
        self.assertEqual('reuse', result['action'])
        self.assertEqual(2, result['verified_files'])

    def test_missing_and_modified_attachment_invalidate_reuse(self):
        (self.root / 'image.png').write_bytes(b'changed')
        self.assertFalse(cache.verify(self.root, self.request)['cache_hit'])
        (self.root / 'image.png').unlink()
        self.assertEqual('full_read', cache.verify(self.root, self.request)['action'])

    def test_same_version_changed_hash_requires_full_read(self):
        self.request['observation']['fingerprint'] = 'new'
        self.assertEqual('full_read', cache.verify(self.root, self.request)['action'])

    def test_unavailable_is_not_unchanged(self):
        self.request['observation']['readable'] = False
        self.assertEqual('unavailable', cache.verify(self.root, self.request)['action'])

    def test_old_observation_is_rejected(self):
        self.request['round_started_at'] = '2026-01-03T00:00:00+00:00'
        with self.assertRaises(ValueError):
            cache.verify(self.root, self.request)

    def test_unreliable_revision_only_is_not_cache_hit(self):
        del self.request['observation']['fingerprint']
        self.assertFalse(cache.verify(self.root, self.request)['cache_hit'])

    def test_symlink_manifest_or_snapshot_is_not_reused(self):
        (self.root / 'image.png').unlink()
        platform_tests.symlink(self, self.root / 'image.png', self.root / 'doc.md')
        self.assertFalse(cache.verify(self.root, self.request)['cache_hit'])

    def test_capture_requires_full_inventory_and_safe_paths(self):
        with self.assertRaises(ValueError):
            cache.capture(self.root, dict(self.identity, files=[]))
        with self.assertRaises(ValueError):
            cache.capture(self.root, dict(self.identity, files=['../outside']))

    def test_cli_json_and_exclusive_output(self):
        request = self.root / 'request.json'
        output = self.root / 'verified.json'
        request.write_text(json.dumps(self.request))
        arguments = ['verify', '--project', str(self.root), '--input', str(request), '--output', str(output)]
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(0, cache.main(arguments))
        self.assertTrue(json.loads(stream.getvalue())['cache_hit'])
        original = output.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(2, cache.main(arguments))
        self.assertIn('overwrite', json.loads(stream.getvalue())['error'])
        self.assertEqual(original, output.read_bytes())

    def test_invalid_inventory_shape_and_manifest_require_full_read(self):
        for files in ['doc.md', ['doc.md', 'doc.md'], [None]]:
            with self.assertRaises(ValueError):
                cache.capture(self.root, dict(self.identity, files=files))
        (self.root / 'manifest.json').write_text('[]')
        self.assertEqual('full_read', cache.verify(self.root, self.request)['action'])
        for request in [[], {'observation': None}]:
            with self.assertRaises(ValueError):
                cache.verify(self.root, request)
