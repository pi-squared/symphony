"""Issue-only remote dispatch never claims, imports or grades Jules's work."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import controller as c
import install
import jules
import test_controller as fixtures


class LabelGitHub(fixtures.FakeGitHub):
    delivery_error = None
    malformed = False
    current_labels = None

    def issue(self, task):
        result = super().issue(task)
        if self.current_labels is not None:
            result['labels'] = [{'name': name} for name in self.current_labels]
        return result

    def api(self, endpoint, method='GET', payload=None):
        self.calls.append((endpoint, method, deepcopy(payload)))
        if not endpoint.endswith('/labels') or method != 'POST':
            raise AssertionError('Dispatch must only add the native issue label')
        if self.delivery_error:
            raise self.delivery_error
        if self.malformed:
            return {'error': 'unconfirmed response'}
        number = int(endpoint.split('/')[-2])
        issue = next(i['content'] for i in self.items if i['content']['number'] == number)
        labels = issue['labels']['nodes']
        for name in payload['labels']:
            if name not in [row['name'] for row in labels]:
                labels.append({'name': name})
        return deepcopy(labels)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.gh = LabelGitHub()
        self.controller.gh = self.gh
        self.config['jules'] = {'enabled': True, 'label': 'runner:jules'}
        self.gh.items[0]['content']['labels']['nodes'].append({'name': 'runner:jules'})

    tearDown = fixtures.ControllerTests.tearDown
    record = fixtures.ControllerTests.record

    def dispatcher(self):
        dispatcher = jules.JulesDispatcher(self.config, self.gh)
        self.addCleanup(dispatcher.close)
        return dispatcher

    def test_jules_label_keeps_same_issue_dispatchable_to_codex(self):
        self.assertTrue(self.controller.list_issues()[0]['dispatchable'])

    def test_dispatch_only_adds_label_without_local_work_or_status_changes(self):
        before = deepcopy(self.gh.items)
        with patch.object(c.Controller, 'workspace', side_effect=AssertionError('No checkout')), \
                patch.object(c.Controller, 'claim', side_effect=AssertionError('No primary claim')), \
                patch.object(c.Controller, 'validate', side_effect=AssertionError('No grading')), \
                patch.object(c.support, 'run', side_effect=AssertionError('No Git or CLI')):
            result = self.dispatcher().tick()
        self.assertEqual(result['status'], 'requested')
        self.assertEqual(self.gh.calls, [(f'repos/{fixtures.REPO}/issues/3/labels', 'POST', {'labels': ['jules']})])
        self.assertEqual(self.controller.state.all(), [])
        self.assertFalse(Path(self.config['workspace_root']).exists())
        self.assertEqual(c.fields(self.gh.items[0]), c.fields(before[0]))
        self.assertEqual(self.gh.comment_rows, [])
        self.assertTrue(self.controller.list_issues()[0]['dispatchable'])

    def test_default_running_cycle_and_rework_do_not_wait_for_jules(self):
        self.record(backend='codex', execution={'model': 'gpt-6.1-sol', 'reasoning': 'xhigh'})
        self.gh.status({}, 'item-3', self.controller.roles['working'])
        before = self.controller.state.get('item-3')
        self.dispatcher().tick()
        self.assertEqual(self.controller.state.get('item-3'), before)
        self.assertTrue(self.controller.eligible(self.gh.items[0]))
        self.gh.status({}, 'item-3', 'Rework')
        self.assertTrue(self.controller.eligible(self.gh.items[0]))

    def test_restart_and_label_edits_do_not_resubmit(self):
        self.assertEqual(self.dispatcher().tick()['status'], 'requested')
        self.gh.items[0]['content']['labels']['nodes'] = [{'name': 'ready-for-agent'}, {'name': 'runner:jules'}]
        self.assertEqual(self.dispatcher().tick()['status'], 'idle')
        self.assertEqual(len(self.gh.calls), 1)
        self.assertEqual(self.dispatcher().receipt('issue-3')['status'], 'requested')

    def test_second_issue_dispatches_while_first_is_remote(self):
        self.gh.items.append(fixtures.item(4, labels=('ready-for-agent', 'runner:jules')))
        self.dispatcher().tick()
        self.dispatcher().tick()
        self.assertEqual([row[0] for row in self.gh.calls], [
            f'repos/{fixtures.REPO}/issues/3/labels', f'repos/{fixtures.REPO}/issues/4/labels'])

    def test_existing_native_label_is_recorded_without_retrigger(self):
        self.gh.items[0]['content']['labels']['nodes'].append({'name': 'JuLeS'})
        dispatcher = self.dispatcher()
        self.assertEqual(dispatcher.tick()['status'], 'requested')
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(dispatcher.receipt('issue-3')['delivery'], 'existing_label')

    def test_lost_response_is_not_retried_and_does_not_block_codex(self):
        self.gh.delivery_error = c.Error('response lost after delivery')
        with self.assertRaises(c.Error):
            self.dispatcher().tick()
        self.assertEqual(self.dispatcher().receipt('issue-3')['status'], 'submission_unknown')
        self.assertEqual(self.dispatcher().tick()['status'], 'idle')
        self.assertEqual(len(self.gh.calls), 1)
        self.assertTrue(self.controller.list_issues()[0]['dispatchable'])

    def test_malformed_success_is_not_reported_as_success_or_retried(self):
        self.gh.malformed = True
        with self.assertRaises(c.Error):
            self.dispatcher().tick()
        self.assertEqual(self.dispatcher().receipt('issue-3')['status'], 'submission_unknown')
        self.dispatcher().tick()
        self.assertEqual(len(self.gh.calls), 1)

    def test_crash_after_intent_does_not_repeat_request(self):
        self.gh.delivery_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.dispatcher().tick()
        self.assertEqual(self.dispatcher().receipt('issue-3')['status'], 'requesting')
        self.assertEqual(self.dispatcher().tick()['status'], 'idle')
        self.assertEqual(len(self.gh.calls), 1)

    def test_current_issue_admission_and_label_are_rechecked(self):
        self.gh.current_labels = ['ready-for-agent']
        self.assertEqual(self.dispatcher().tick()['status'], 'idle')
        self.gh.current_labels = ['runner:jules']
        with self.assertRaises(c.Error):
            self.dispatcher().tick()
        self.assertEqual(self.gh.calls, [])

    def test_disabled_archived_closed_backlog_and_done_are_not_submitted(self):
        changes = [lambda: self.config.update(enabled=False),
                   lambda: self.config['jules'].update(enabled=False),
                   lambda: self.gh.items[0].update(isArchived=True),
                   lambda: self.gh.items[0]['content'].update(state='CLOSED'),
                   lambda: self.gh.status({}, 'item-3', 'Backlog'),
                   lambda: self.gh.status({}, 'item-3', 'Done')]
        original = deepcopy(self.gh.items)
        for change in changes:
            with self.subTest(change=change):
                self.config['enabled'] = self.config['jules']['enabled'] = True
                self.gh.items = deepcopy(original)
                change()
                self.assertIn(self.dispatcher().tick()['status'], {'idle', 'paused'})
        self.assertEqual(self.gh.calls, [])

    def test_assignee_and_native_dependencies_are_preserved(self):
        dispatcher = self.dispatcher()
        dispatcher.controller.tracker['assignee'] = 'allowed'
        self.assertEqual(dispatcher.tick()['status'], 'idle')
        self.gh.assignees = [{'login': 'allowed'}]
        dispatcher.controller.tracker['provider']['dependency_gating'] = True
        self.gh.dependencies = [{'id': 'blocked', 'number': 2, 'state': 'open'}]
        self.assertEqual(dispatcher.tick()['status'], 'idle')
        self.gh.dependencies = []
        self.assertEqual(dispatcher.tick()['status'], 'requested')

    def test_duplicate_project_items_do_not_duplicate_dispatch(self):
        duplicate = deepcopy(self.gh.items[0]); duplicate['id'] = 'another-item'
        self.gh.items.append(duplicate)
        self.dispatcher().tick()
        self.assertEqual(self.dispatcher().tick()['status'], 'idle')
        self.assertEqual(len(self.gh.calls), 1)

    def test_legacy_session_is_preserved_without_redispatch_or_rework_wait(self):
        original = self.record(backend='jules', jules={'phase': 'remote', 'session_id': '101',
                                                     'session_url': 'https://jules.google.com/session/101'})
        dispatcher = self.dispatcher()
        self.assertEqual(dispatcher.tick()['status'], 'idle')
        self.assertEqual(dispatcher.receipt('issue-3')['status'], 'legacy')
        self.assertEqual(dispatcher.receipt('issue-3')['session_url'], original['jules']['session_url'])
        self.assertEqual(self.controller.state.get('item-3'), original)
        self.gh.status({}, 'item-3', 'Rework')
        self.assertTrue(self.controller.eligible(self.gh.items[0]))

    def test_human_rework_starts_fresh_xhigh_and_archives_legacy_remote_attempt(self):
        self.record(backend='jules', jules={'phase': 'remote', 'session_id': '101'})
        self.gh.status({}, 'item-3', 'Rework')
        with patch.object(self.controller, 'ensure_checkout'):
            result = self.controller.claim('item-3', self.path)
        self.assertEqual(result['cycle'], 2)
        self.assertEqual(result['execution'], {'model': 'gpt-6.1-sol', 'reasoning': 'xhigh'})
        self.assertEqual(self.controller.state.get('item-3')['backend'], 'codex')
        self.assertEqual(self.controller.state.history('item-3')[0]['jules']['session_id'], '101')
        self.assertEqual(self.dispatcher().tick()['status'], 'idle')

    def test_live_board_transition_prevents_dispatch_from_stale_snapshot(self):
        with patch.object(self.gh, 'item_status', return_value='Backlog'):
            self.assertEqual(self.dispatcher().tick()['status'], 'idle')
        self.assertEqual(self.gh.calls, [])


class InstallationTests(unittest.TestCase):
    def test_native_dispatch_does_not_require_jules_cli_or_node(self):
        settings = {'repository': 'example/project', 'project_owner': 'example', 'project_number': 17,
                    'required_checks': ['tests'], 'checks': [['python', '-m', 'unittest']],
                    'jules': {'enabled': True, 'label': 'runner:jules'}}
        install.validate_settings(settings)

    def test_companion_failure_does_not_stop_default_runner(self):
        import launch
        with tempfile.TemporaryDirectory() as directory:
            config = {'state_directory': directory, 'enabled': False, 'runtime_bin': [],
                      'symphony': '/configured/symphony', 'workflow': '/configured/WORKFLOW.md',
                      'python': '/configured/python', 'jules': {'enabled': True}}
            config_path = Path(directory)/'config.json'; config_path.write_text(json.dumps(config))
            primary = Mock(pid=100, returncode=0)
            count = iter([None, None, 0]); primary.poll.side_effect = lambda: next(count, 0)
            companion = Mock(pid=200, returncode=1); companion.poll.return_value = 1
            with patch('sys.argv', ['launch', '--config', str(config_path)]), \
                    patch.object(launch.os, 'chdir'), patch.object(launch.os, 'killpg') as kill, \
                    patch.object(launch.signal, 'signal'), patch.object(launch.time, 'sleep') as sleep, \
                    patch.object(launch.subprocess, 'Popen', side_effect=[primary, companion]) as start:
                with self.assertRaises(SystemExit) as result:
                    launch.main()
            self.assertEqual(result.exception.code, 0)
            self.assertEqual(start.call_count, 2)
            self.assertGreaterEqual(sleep.call_count, 2)
            kill.assert_not_called()
