"""Host-neutral lock checks plus Win32 boundary mocks and native-only safety checks."""
import ctypes
import errno
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / 'platform_support.py'
SPEC = importlib.util.spec_from_file_location('platform_under_test', MODULE)
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


def symlink(test, path, target, target_is_directory=False):
    try:
        path.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as error:
        if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
            test.skipTest('Windows symlink creation requires Developer Mode or privilege')
        raise


class PlatformTest(unittest.TestCase):
    def test_portable_relative_names_reject_windows_aliases_on_every_host(self):
        for value in ['../a', '/a', 'a//b', 'a\\b', 'C:relative', 'C:/absolute',
                      '//server/share', 'a:stream', 'a.', 'a ', 'CON', 'nul.txt',
                      'LPT1.log', 'COM1', 'a/..', 'a/./b', 'a\0b']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                p.relative_parts(value)
        self.assertEqual(['test', '.cache', 'a file.json'], p.relative_parts('test/.cache/a file.json'))

    def test_windows_byte_lock_uses_same_byte_and_releases(self):
        calls = []
        fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0,
                                     locking=lambda *args: calls.append(args))
        with tempfile.TemporaryFile('w+b') as stream, \
                mock.patch.object(p, 'WINDOWS', True), mock.patch.dict(sys.modules, msvcrt=fake):
            with p.file_lock(stream.fileno()):
                self.assertEqual(1, os.fstat(stream.fileno()).st_size)
            self.assertEqual([(stream.fileno(), 2, 1), (stream.fileno(), 0, 1)], calls)

    def test_windows_lock_failure_is_bounded_and_nonlocking_errors_propagate(self):
        for error, expected in [(OSError(errno.EACCES, 'busy'), TimeoutError),
                                (OSError(errno.EBADF, 'bad descriptor'), OSError)]:
            fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0,
                                         locking=mock.Mock(side_effect=error))
            with tempfile.TemporaryFile('w+b') as stream, \
                    mock.patch.object(p, 'WINDOWS', True), mock.patch.dict(sys.modules, msvcrt=fake):
                with self.assertRaises(expected):
                    with p.file_lock(stream.fileno(), timeout=0):
                        self.fail('must not acquire')

    def test_windows_directory_sync_never_uses_unsupported_directory_open(self):
        with mock.patch.object(p, 'WINDOWS', True), mock.patch.object(p.os, 'open') as opened:
            p.sync_directory('unused')
            opened.assert_not_called()

    def test_multiple_cli_processes_serialize_read_modify_write(self):
        code = '''import importlib.util,json,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('locks',sys.argv[1]); p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
root=Path(sys.argv[2])
for i in range(25):
 with (root/'lock').open('a+b') as f, p.file_lock(f.fileno()):
  counter=root/'counter'; value=int(counter.read_text(encoding='utf-8'))
  time.sleep(.001)
  counter.write_text(str(value+1),encoding='utf-8')
'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'counter').write_text('0', encoding='utf-8')
            children = [subprocess.Popen([sys.executable, '-c', code, str(MODULE), temp],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
            try:
                for child in children:
                    _, error = child.communicate(timeout=30)
                    self.assertEqual(0, child.returncode, error.decode('utf-8', errors='replace'))
                self.assertEqual('100', (root / 'counter').read_text(encoding='utf-8'))
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.communicate()

    def test_kernel_lock_releases_after_process_crash(self):
        code = '''import importlib.util,os,sys
spec=importlib.util.spec_from_file_location('locks',sys.argv[1]); p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
f=open(sys.argv[2],'a+b')
with p.file_lock(f.fileno()): os._exit(0)
'''
        with tempfile.TemporaryDirectory() as temp:
            lock = str(Path(temp) / 'lock')
            child = subprocess.run([sys.executable, '-c', code, str(MODULE), lock],
                                   capture_output=True, timeout=10)
            self.assertEqual(0, child.returncode, child.stderr)
            with open(lock, 'a+b') as stream, p.file_lock(stream.fileno(), timeout=1):
                pass

    def test_win32_handle_metadata_rejects_reparse_and_multilink_files(self):
        files = object.__new__(p.WindowsFiles)
        def information(handle, pointer):
            data = ctypes.cast(pointer, ctypes.POINTER(p.HandleInfo)).contents
            data.attributes = attributes
            data.links = links
            data.volume, data.index_low, data.size_low = 7, 11, 20
            return 1
        files.api = types.SimpleNamespace(GetFileInformationByHandle=information, GetFileType=lambda h: 1)
        for attributes, links in [(0x400, 1), (0, 2)]:
            with self.assertRaises(ValueError):
                files.info(123)
        attributes, links = 0, 1
        self.assertEqual([7, 11, 20, 0, 0], files.info(123)['identity'])

    def test_win32_rename_is_exclusive_and_uses_utf16_byte_length(self):
        files = object.__new__(p.WindowsFiles)
        calls = []
        def rename(handle, kind, pointer, size):
            info = p.RenameInfo.from_buffer(pointer)
            end = p.RenameInfo.name.offset + info.length
            raw = bytes(pointer)[p.RenameInfo.name.offset:end]
            self.assertGreaterEqual(size, end + 2)
            self.assertEqual(b'\0\0', bytes(pointer)[end:end + 2])
            calls.append((kind, info.flags, info.root, info.length, raw.decode('utf-16-le')))
            return 1
        files.api = types.SimpleNamespace(SetFileInformationByHandle=rename)
        with mock.patch.object(files, 'native', return_value='C:\\cache\\结果.json'):
            files.rename(123, 'unused')
            files.rename(123, 'unused', replace=True)
        kind, flags, root, length, path = calls[0]
        self.assertEqual((3, 0, None), (kind, flags, root))
        self.assertEqual(len(path.encode('utf-16-le')), length)
        self.assertEqual(1, calls[1][1])

    def test_win32_pins_deny_write_and_delete_sharing_except_lock_files(self):
        files = object.__new__(p.WindowsFiles)
        files.api = types.SimpleNamespace(CreateFileW=mock.Mock(return_value=123),
                                         CloseHandle=mock.Mock())
        for directory, shared_write in [(True, False), (False, False), (False, True)]:
            with self.subTest(directory=directory, shared_write=shared_write), \
                    mock.patch.object(files, 'native', return_value='C:\\safe'), \
                    mock.patch.object(files, 'info', return_value={'directory': directory}):
                with files.handle('unused', directory=directory, shared_write=shared_write):
                    pass
                self.assertEqual(3 if shared_write else 1, files.api.CreateFileW.call_args.args[2])
                expected_access = files.ATTRIBUTES | (files.LIST_DIRECTORY if directory else 0)
                self.assertEqual(expected_access, files.api.CreateFileW.call_args.args[1])

    def test_native_child_open_uses_one_no_follow_component_and_root_handle(self):
        files = object.__new__(p.WindowsFiles)
        captured = []
        def create(output, access, attributes, result, size, flags, sharing, disposition,
                   options, ea, ea_size):
            attributes = ctypes.cast(attributes, ctypes.POINTER(p.ObjectAttributes)).contents
            name = attributes.name.contents
            captured.append((attributes.root, ctypes.string_at(name.buffer, name.length),
                             access, sharing, disposition, options))
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 456
            return 0
        files.nt = types.SimpleNamespace(NtCreateFile=create)
        files.api = types.SimpleNamespace(CloseHandle=mock.Mock())
        with mock.patch.object(files, 'info', return_value={'directory': True}):
            with files.child(123, '目录', directory=True, create=True):
                pass
            self.assertEqual((123, '目录'.encode('utf-16-le')), captured[0][:2])
            self.assertEqual((3, 3), captured[0][3:5])
            self.assertTrue(captured[0][5] & 0x00200000)
            self.assertTrue(captured[0][2] & files.LIST_DIRECTORY)
            for name in ['a/b', '..', 'a\\b', 'C:escape', '/absolute']:
                with self.assertRaises(ValueError):
                    with files.child(123, name):
                        self.fail('unsafe name accepted')
        files.api.CloseHandle.assert_called_once()

    def test_native_rename_uses_parent_handle_and_never_an_absolute_path(self):
        files = object.__new__(p.WindowsFiles)
        def rename(handle, result, buffer, size, kind):
            info = p.RenameInfo.from_buffer(buffer)
            name = bytes(buffer)[p.RenameInfo.name.offset:][:info.length].decode('utf-16-le')
            self.assertEqual((123, 10, 456, '结果.json', 1), (handle, kind, info.root, name, info.flags))
            return 0
        files.nt = types.SimpleNamespace(NtSetInformationFile=rename)
        with mock.patch.object(files, 'info'):
            files.rename_child(123, ctypes.c_void_p(456), '结果.json', replace=True)
            with self.assertRaises(ValueError):
                files.rename_child(123, ctypes.c_void_p(456), 'nested/escape')

    def test_cli_modules_import_without_fcntl(self):
        with mock.patch.dict(sys.modules, fcntl=None):
            for name in ['workflow', 'scheduling', 'retention', 'source_cache', 'import_results']:
                spec = importlib.util.spec_from_file_location('no_fcntl_' + name, MODULE.with_name(name + '.py'))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

    def test_cli_paths_with_spaces_and_unicode_and_non_utf8_parent_environment(self):
        with tempfile.TemporaryDirectory(prefix='e2e portable-') as temp:
            root = Path(temp) / '含空格 project'
            root.mkdir()
            env = dict(os.environ, PYTHONIOENCODING='ascii')
            result = subprocess.run([sys.executable, str(MODULE.with_name('workflow.py')),
                                     'init', '--project', str(root)], capture_output=True,
                                    encoding='utf-8', env=env, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((root / 'test/spec.md').is_file())
            (root / '正文.md').write_text('示例正文', encoding='utf-8')
            request = {'source_id': '来源', 'revision': '1', 'fingerprint': 'fixture',
                       'fingerprint_basis': 'document_bytes', 'files': ['正文.md']}
            input_path = root / 'request.json'
            input_path.write_text(json.dumps(request), encoding='utf-8')
            result = subprocess.run([sys.executable, str(MODULE.with_name('source_cache.py')),
                                     'capture', '--project', str(root), '--input', str(input_path)],
                                    capture_output=True, encoding='utf-8', env=env, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual('来源', json.loads(result.stdout)['source_id'])


@unittest.skipUnless(os.name == 'nt', 'requires native Windows file handles')
class NativeWindowsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='e2e-native-windows-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.files = p.WindowsFiles()

    def test_pinned_parent_cannot_be_renamed(self):
        directory = self.root / 'pinned'
        directory.mkdir()
        with self.files.directories(directory):
            with self.assertRaises(OSError):
                directory.rename(self.root / 'replacement')
        directory.rename(self.root / 'replacement')

    def test_file_read_rename_and_delete_by_verified_handle(self):
        source, target = self.root / 'original', self.root / 'staged'
        source.write_bytes(b'synthetic-only')
        with self.files.handle(source, access=self.files.READ | self.files.DELETE) as handle:
            self.assertEqual(b'synthetic-only', self.files.read(handle))
            self.files.rename(handle, target)
        self.assertFalse(source.exists())
        with self.files.handle(target, access=self.files.READ | self.files.DELETE) as handle:
            self.files.delete(handle)
        self.assertFalse(target.exists())

    def test_junction_without_symlink_privilege_is_rejected(self):
        target, junction = self.root / 'target', self.root / 'junction'
        target.mkdir()
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)],
                                capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        try:
            with self.assertRaises(ValueError):
                with self.files.directories(junction):
                    pass
        finally:
            junction.rmdir()

    def test_native_exclusive_move_never_overwrites(self):
        source, target = self.root / 'source', self.root / 'target'
        source.write_bytes(b'original')
        target.write_bytes(b'keep')
        with self.files.handle(source, access=self.files.READ | self.files.DELETE) as handle:
            with self.assertRaises(OSError):
                self.files.rename(handle, target)
        self.assertEqual(b'keep', target.read_bytes())

    def test_retention_atomic_writes_work_with_pinned_parents(self):
        spec = importlib.util.spec_from_file_location('native_retention', MODULE.with_name('retention.py'))
        retention = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(retention)
        tree = retention.WindowsTree(self.root)
        try:
            relative = 'test/.cache/retention/plans/synthetic.json'
            tree.write(relative, {'version': 1}, exclusive=True)
            self.assertEqual({'version': 1}, json.loads(tree.read(relative)[0]))
            with self.assertRaises(OSError):
                tree.write(relative, {'version': 2}, exclusive=True)
            self.assertEqual({'version': 1}, json.loads(tree.read(relative)[0]))
            tree.write(relative, {'version': 3})
            self.assertEqual({'version': 3}, json.loads(tree.read(relative)[0]))
            staged = 'test/.cache/retention/quarantine/renamed.json'
            tree.move(relative, staged, tree.read(relative)[1])
            self.assertFalse(tree.exists(relative))
            self.assertEqual({'version': 3}, json.loads(tree.read(staged)[0]))
            self.assertIn(staged, tree.walk('test/.cache/retention'))
            tree.restore(staged, relative)
            self.assertFalse(tree.exists(staged))
            self.assertEqual({'version': 3}, json.loads(tree.read(relative)[0]))
        finally:
            tree.close()

    def test_handle_relative_root_cannot_be_renamed(self):
        directory = self.root / 'pinned'
        directory.mkdir()
        with self.files.rooted_directory(directory):
            with self.assertRaises(OSError):
                directory.rename(self.root / 'replacement')

    def mutate_junction(self, directory, target, delete=False):
        import struct
        if delete:
            payload = struct.pack('<IHH', 0xA0000003, 0, 0)
        else:
            substitute = ('\\??\\' + str(target)).encode('utf-16-le')
            display = str(target).encode('utf-16-le')
            names = substitute + b'\0\0' + display + b'\0\0'
            payload = struct.pack('<IHHHHHH', 0xA0000003, 8 + len(names), 0,
                                  0, len(substitute), len(substitute) + 2, len(display)) + names
        operation = self.files.api.DeviceIoControl
        operation.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                              ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                              ctypes.c_void_p]
        operation.restype = ctypes.c_int
        raw = self.files.api.CreateFileW(self.files.native(directory), 0x40000000, 3,
                                         None, 3, 0x02200000, None)
        if raw == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            count = ctypes.c_uint32()
            code = 0x900AC if delete else 0x900A4
            if not operation(raw, code, payload, len(payload), None, 0, ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.files.api.CloseHandle(raw)

    def test_reparse_mutation_after_validation_cannot_redirect_child_creation_or_rename(self):
        for action in ['create', 'rename']:
            with self.subTest(action=action):
                directory, outside = self.root / action, self.root / (action + '-outside')
                directory.mkdir()
                outside.mkdir()
                source = self.root / (action + '-source')
                source.write_bytes(b'keep')
                with self.files.rooted_directory(directory) as parent:
                    original_info = self.files.info
                    changed = False
                    def mutate_after_check(handle):
                        nonlocal changed
                        result = original_info(handle)
                        if handle is parent and not changed:
                            self.mutate_junction(directory, outside)
                            changed = True
                        return result
                    try:
                        with self.files.handle(source, access=self.files.READ | self.files.DELETE) as handle:
                            with mock.patch.object(self.files, 'info', side_effect=mutate_after_check):
                                try:
                                    if action == 'create':
                                        with self.files.child(parent, 'must-stay-local', directory=False,
                                                              access=0xC0000000, exclusive=True):
                                            pass
                                    else:
                                        self.files.rename_child(handle, parent, 'must-stay-local')
                                except (OSError, ValueError):
                                    pass  # Refusal is safe; escaping the pinned object is not.
                        self.assertTrue(changed)
                        self.assertEqual([], list(outside.iterdir()))
                        with self.assertRaises(ValueError):
                            with self.files.child(parent, 'another', directory=False, exclusive=True):
                                self.fail('known reparse parent accepted')
                    finally:
                        if changed:
                            self.mutate_junction(directory, outside, delete=True)

    def test_native_validated_file_cannot_be_written_until_handle_closes(self):
        source = self.root / 'protected'
        source.write_bytes(b'keep')
        with self.files.handle(source, access=self.files.READ | self.files.DELETE):
            with self.assertRaises(OSError):
                source.write_bytes(b'changed')
        self.assertEqual(b'keep', source.read_bytes())

    def test_native_hardlinked_artifact_is_rejected(self):
        source, link = self.root / 'original', self.root / 'alias'
        source.write_bytes(b'keep')
        os.link(source, link)
        with self.assertRaises(ValueError):
            with self.files.handle(link, access=self.files.READ):
                pass
