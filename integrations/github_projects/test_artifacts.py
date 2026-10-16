"""Exercise the visual evidence boundary with real local files."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import controller as c
from launch import worker_environment


class EvidenceTests(unittest.TestCase):
    def test_rejects_ancestor_swap_during_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'workspace'
            directory = workspace / 'captures'
            directory.mkdir(parents=True)
            outside = root / 'outside'
            outside.mkdir()
            content = b'\x89PNG\r\n\x1a\noutside-image'
            (outside / 'capture.png').write_bytes(content)
            (directory / 'capture.png').write_bytes(b'\x89PNG\r\n\x1a\ninside-image')
            row = {'path': 'captures/capture.png', 'sha256': hashlib.sha256(content).hexdigest(), 'caption': 'Browser'}
            original_open = c.artifacts.os.open

            def swap_before_parent(path, flags, **kwargs):
                if path == 'captures':
                    directory.rename(workspace / 'preserved')
                    directory.symlink_to(outside, target_is_directory=True)
                return original_open(path, flags, **kwargs)

            with patch.object(c.artifacts.os, 'open', side_effect=swap_before_parent), self.assertRaises(ValueError):
                c.artifacts.stage(workspace, [row], root / 'staged', c.support.SECRET_PATTERN)
            self.assertFalse((root / 'staged').exists())

    def test_rejects_escape_symlink_hash_type_and_credentials_before_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'workspace'
            workspace.mkdir()
            source = workspace / 'capture.png'
            content = b'\x89PNG\r\n\x1a\nevidence'
            source.write_bytes(content)
            base = {'path': 'capture.png', 'sha256': hashlib.sha256(content).hexdigest(), 'caption': 'Local browser'}
            destination = root / 'staged'
            for changes in [{'path': '../capture.png'}, {'path': './capture.png'}, {'path': str(source)}, {'path': 'capture.zip'},
                            {'sha256': '0' * 64}]:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    c.artifacts.stage(workspace, [{**base, **changes}], destination, c.support.SECRET_PATTERN)
                self.assertFalse(destination.exists())
            link = workspace / 'link.png'
            link.symlink_to(source)
            with self.assertRaises(ValueError):
                c.artifacts.stage(workspace, [{**base, 'path': 'link.png'}], destination, c.support.SECRET_PATTERN)
            import os
            fifo = workspace / 'pipe.png'
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                c.artifacts.stage(workspace, [{**base, 'path': 'pipe.png'}], destination, c.support.SECRET_PATTERN)
            source.write_bytes(b'not an image')
            with self.assertRaises(ValueError):
                c.artifacts.stage(workspace, [base], destination, c.support.SECRET_PATTERN)
            secret = content + b' sk-' + b'x' * 40
            source.write_bytes(secret)
            with self.assertRaises(ValueError):
                c.artifacts.stage(workspace, [{**base, 'sha256': hashlib.sha256(secret).hexdigest()}],
                                  destination, c.support.SECRET_PATTERN)
            source.write_bytes(content)
            first = c.artifacts.stage(workspace, [base], destination, c.support.SECRET_PATTERN)
            second = c.artifacts.stage(workspace, [base], destination, c.support.SECRET_PATTERN)
            self.assertEqual(first, second)
            self.assertEqual(Path(first[0]['staged_path']).read_bytes(), content)

    def test_worker_receives_test_key_and_luna_without_github_credentials(self):
        source = {'OPENAI_API_KEY': 'fixture-only', 'GH_TOKEN': 'github-fixture',
                  'GITHUB_TOKEN': 'github-fixture', 'UNRELATED_SECRET': 'fixture'}
        env = worker_environment({'runtime_bin': []}, '/tmp/install', source)
        self.assertEqual(env['OPENAI_API_KEY'], source['OPENAI_API_KEY'])
        self.assertEqual(env['OPENAI_TEST_MODEL'], 'gpt-6-luna')
        self.assertFalse({'GH_TOKEN', 'GITHUB_TOKEN', 'UNRELATED_SECRET'} & env.keys())
        source['OPENAI_TEST_MODEL'] = 'explicit-test-model'
        self.assertEqual(worker_environment({'runtime_bin': []}, '/tmp/install', source)['OPENAI_TEST_MODEL'],
                         'explicit-test-model')


if __name__ == '__main__':
    unittest.main()
