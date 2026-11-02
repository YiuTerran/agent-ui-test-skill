"""Local runtime probes only; never access a product or remove host hooks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_probe as rp


class RuntimeProbeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()

    def test_python_probe_reports_runtime_and_cleans_only_own_files(self):
        parent = self.project / 'test/.cache/runtime-probes'
        parent.mkdir(parents=True)
        retained = parent / 'existing-result'
        retained.write_text('retain', encoding='utf-8')
        result = rp.inspect(self.project)
        self.assertTrue(result['valid'], result)
        self.assertEqual('not_checked', result['checks']['node']['status'])
        self.assertEqual(sys.executable, result['python_runtime']['executable'])
        self.assertEqual('3.10', result['python_runtime']['minimum_version'])
        self.assertEqual([retained], list(parent.iterdir()))
        self.assertEqual('retain', retained.read_text(encoding='utf-8'))

    def test_non_idempotent_runtime_fails_with_neutral_error_code(self):
        mkdir = Path.mkdir

        def broker_mkdir(path, *args, **options):
            if options.get('exist_ok') and path.is_dir():
                raise FileExistsError()
            return mkdir(path, *args, **options)

        with mock.patch.object(Path, 'mkdir', broker_mkdir):
            result = rp.inspect(self.project)
        self.assertFalse(result['valid'], result)
        self.assertEqual('fs_mkdir_not_idempotent', result['checks']['python']['error_type'])

    def test_permission_denial_fails_and_cleans_probe_directory(self):
        with mock.patch.object(rp.os, 'replace', side_effect=PermissionError):
            result = rp.inspect(self.project)
        self.assertFalse(result['valid'])
        self.assertEqual('PermissionError', result['checks']['python']['error_type'])
        self.assertEqual([], list((self.project / 'test/.cache/runtime-probes').iterdir()))

    def test_directory_scope_and_existing_file_are_rejected(self):
        for directory in ['outside', '../test/.cache', 'test/.cache/../outside',
                          '/test/.cache', 'test/.cache\\outside']:
            with self.subTest(directory=directory):
                self.assertFalse(rp.inspect(self.project, directory)['valid'])
        (self.project / 'test').touch()
        result = rp.inspect(self.project)
        self.assertFalse(result['valid'])
        self.assertEqual('FileExistsError', result['checks']['directory']['error_type'])

    def test_symlink_ancestor_is_rejected_without_external_writes(self):
        outside = self.project / 'outside'
        outside.mkdir()
        try:
            (self.project / 'test').symlink_to(outside, target_is_directory=True)
        except OSError as error:
            if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
                self.skipTest('Windows symlink creation requires Developer Mode or privilege')
            raise
        self.assertFalse(rp.inspect(self.project)['valid'])
        self.assertEqual([], list(outside.iterdir()))

    def test_node_inherits_environment_and_no_values_are_in_report(self):
        completed = subprocess.CompletedProcess([], 0, '{"valid":true}', 'private-stderr')
        with mock.patch.dict(os.environ, PYTHONPATH='private-python', NODE_OPTIONS='private-node'), \
                mock.patch.object(rp.subprocess, 'run', return_value=completed) as run:
            result = rp.inspect(self.project, node='project-node')
            self.assertEqual('private-python', os.environ['PYTHONPATH'])
            self.assertEqual('private-node', os.environ['NODE_OPTIONS'])
        self.assertTrue(result['valid'], result)
        self.assertEqual({'PYTHONPATH': True, 'NODE_OPTIONS': True}, result['injection_present'])
        self.assertNotIn('env', run.call_args.kwargs)
        self.assertEqual(self.project, run.call_args.kwargs['cwd'])
        self.assertEqual('project-node', run.call_args.args[0][0])
        self.assertNotIn('private-', json.dumps(result))

    def test_node_invalid_output_exit_failure_and_operation_errors(self):
        for code, output in [(0, 'unexpected output'), (0, '[]'), (0, '{"valid":1}'),
                             (2, '{"valid":true}'), (0, '{"valid":true,"operation":[]}')]:
            with self.subTest(code=code, output=output), \
                    mock.patch.object(rp.subprocess, 'run', return_value=
                                      subprocess.CompletedProcess([], code, output, '')):
                result = rp.inspect(self.project, node='node')
                self.assertFalse(result['valid'])
                self.assertEqual('invalid_probe_output', result['checks']['node']['error_type'])
        with mock.patch.object(rp.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 2, '{"valid":false,"operation":"mkdir","error_type":"EACCES"}', '')):
            result = rp.inspect(self.project, node='node')
        self.assertFalse(result['valid'])
        self.assertEqual('mkdir', result['checks']['node']['operation'])
        self.assertEqual('EACCES', result['checks']['node']['error_type'])

    def test_node_timeout_and_missing_executable_fail(self):
        for error in [subprocess.TimeoutExpired('node', 15), FileNotFoundError()]:
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(rp.subprocess, 'run', side_effect=error):
                result = rp.inspect(self.project, node='node')
                self.assertFalse(result['valid'])
                self.assertEqual(type(error).__name__, result['checks']['node']['error_type'])

    def test_cleanup_failure_is_reported_with_preserved_probe_location(self):
        with mock.patch.object(rp.shutil, 'rmtree', side_effect=PermissionError):
            result = rp.inspect(self.project)
        self.assertFalse(result['valid'])
        cleanup = result['checks']['cleanup']
        self.assertEqual('PermissionError', cleanup['error_type'])
        self.assertTrue((self.project / cleanup['probe_directory']).is_dir())

    @unittest.skipUnless(shutil.which('node'), 'Node is optional for Python tooling')
    def test_real_node_probe_and_cli_exit_code(self):
        result = subprocess.run([sys.executable, str(Path(rp.__file__)), '--project',
                                 str(self.project), '--node', shutil.which('node')],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        value = json.loads(result.stdout)
        self.assertTrue(value['valid'])
        self.assertTrue(value['checks']['node']['valid'])
        self.assertEqual([], list((self.project / 'test/.cache/runtime-probes').iterdir()))


if __name__ == '__main__':
    unittest.main()
