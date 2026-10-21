"""Observable cycle selection, including retry after leaving Rework."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import controller as c
import test_controller as fixtures
from worker import codex_command, execution_settings


class ExecutionTests(unittest.TestCase):
    setUp = fixtures.ControllerTests.setUp
    tearDown = fixtures.ControllerTests.tearDown
    checkout = fixtures.ControllerTests.checkout
    git = fixtures.ControllerTests.git
    record = fixtures.ControllerTests.record
    pr = fixtures.ControllerTests.pr

    def test_explicit_rework_keeps_xhigh_after_restart_and_retry(self):
        self.checkout()
        self.pr()
        self.gh.status({}, 'item-3', 'Rework')
        with patch.object(self.controller, 'ensure_checkout'):
            context = self.controller.claim('item-3', self.path)
        self.assertEqual(context['execution'], {'model': 'gpt-6.1-sol', 'reasoning': 'xhigh'})
        self.gh.pr['state'] = 'closed'
        self.gh.comment_rows.clear()
        self.controller.restart(self.controller.task(self.gh.items[0]))
        self.controller.after_run(self.path)
        with patch.object(self.controller, 'ensure_checkout'):
            context = self.controller.claim('item-3', self.path)
        self.assertEqual(context['execution']['reasoning'], 'xhigh')
        self.assertEqual(context['cycle'], 2)

    def test_ordinary_settings_preserved_without_rework(self):
        self.config.update(model='ordinary-model', reasoning='high')
        self.checkout()
        with patch.object(self.controller, 'ensure_checkout'):
            context = self.controller.claim('item-3', self.path)
        self.assertEqual(context['execution'], {'model': 'ordinary-model', 'reasoning': 'high'})


class CommandTests(unittest.TestCase):
    def test_effective_command_uses_cycle_settings_and_keeps_permissions(self):
        config = {'codex_argv': ['codex', '-c', 'model="old"', '-c', 'model_reasoning_effort="medium"',
                                 '-c', 'permissions.symphony.network.enabled=true', 'app-server']}
        command = codex_command(config, {'execution': {'model': 'gpt-6.1-sol', 'reasoning': 'xhigh'}})
        self.assertIn('model="gpt-6.1-sol"', command)
        self.assertIn('model_reasoning_effort="xhigh"', command)
        self.assertIn('permissions.symphony.network.enabled=true', command)
