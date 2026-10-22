"""Worker-controlled Git hooks, helpers and push destinations stay untrusted."""
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import controller as c
import host_git
import test_controller as fixtures


class HostGitTests(unittest.TestCase):
    setUp = fixtures.ControllerTests.setUp
    tearDown = fixtures.ControllerTests.tearDown
    record = fixtures.ControllerTests.record
    checkout = fixtures.ControllerTests.checkout
    git = fixtures.ControllerTests.git

    def hook(self, name):
        marker = self.root/(name+'.executed')
        hook = self.path/'.git/hooks'/name
        hook.write_text('#!/bin/sh\ntouch '+str(marker)+'\n')
        hook.chmod(0o700)
        return marker, hook

    def test_host_status_never_executes_worker_fsmonitor(self):
        self.checkout()
        marker, hook = self.hook('fsmonitor')
        self.git('config', 'core.fsmonitor', str(hook))
        self.assertEqual(c.browser.git(self.path, 'status', '--porcelain'), '')
        self.assertFalse(marker.exists())

    def test_host_push_disables_pre_push_hook_in_actual_local_transport(self):
        sha = self.checkout()
        marker, hook = self.hook('pre-push')
        target = self.root/'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(target)], check=True)
        argv, env = host_git.prepare(['-c', 'protocol.file.allow=always', 'push', str(target), sha+':refs/heads/test'], cwd=self.path)
        subprocess.run(argv, cwd=self.path, env=env, check=True, capture_output=True)
        self.assertFalse(marker.exists())
        actual = subprocess.check_output(['git', '--git-dir', str(target), 'rev-parse', 'refs/heads/test'], text=True).strip()
        self.assertEqual(actual, sha)

    def test_mutable_pushurl_cannot_redirect_the_host_bridge(self):
        sha = self.checkout()
        self.git('config', 'remote.origin.pushurl', 'https://elsewhere.invalid/stolen.git')
        with patch.object(self.controller, 'validate'), \
             patch.object(self.gh, 'remote_head', side_effect=[None, sha]), \
             patch.object(c.support, 'run', return_value='') as run:
            self.controller.git_remote('item-3', self.path, 'push')
        argv = run.call_args.args[0]
        self.assertEqual(argv, ['git', 'push', 'https://github.com/example/project.git', sha+':refs/heads/'+self.task.branch])

    def test_symlinked_metadata_and_url_rewrites_are_rejected(self):
        self.checkout()
        outside = self.root/'outside.git'
        (self.path/'.git').rename(outside)
        (self.path/'.git').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            c.browser.git(self.path, 'status', '--porcelain')
        (self.path/'.git').unlink();outside.rename(self.path/'.git')
        self.git('config', 'url.https://elsewhere.invalid/.insteadOf', 'https://github.com/')
        with self.assertRaises(ValueError):
            host_git.prepare(['push', 'https://github.com/example/project.git', 'HEAD:refs/heads/test'], cwd=self.path)
