"""Backend ownership and remote submission recovery across real ledger restarts."""
from copy import deepcopy
from pathlib import Path
import subprocess
import json
import tempfile
import unittest
from unittest.mock import patch

import controller as c
import jules
import test_controller as fixtures


class FakeCLI:
    def __init__(self):
        self.submissions = 0
        self.current = 'planning'
        self.ambiguous = False
        self.patch = ''

    def submit(self, repository, prompt, directory):
        self.submissions += 1
        if self.ambiguous:
            raise c.Error('ambiguous response')
        return '101'

    def status(self, *_):
        return self.current

    def pull(self, *_):
        return self.patch


class RoutingTests(unittest.TestCase):
    setUp = fixtures.ControllerTests.setUp
    tearDown = fixtures.ControllerTests.tearDown
    record = fixtures.ControllerTests.record
    checkout = fixtures.ControllerTests.checkout
    git = fixtures.ControllerTests.git

    def settings(self):
        self.config['jules'] = {'enabled': True, 'binary': '/configured/jules', 'label': 'runner:jules'}
        self.gh.items[0]['content']['labels']['nodes'].append({'name': 'runner:jules'})

    def worker(self, cli):
        worker = jules.JulesWorker(self.config, self.gh, cli)
        self.addCleanup(worker.close)
        return worker

    def test_codex_and_jules_route_different_issues(self):
        self.settings()
        self.gh.items.append(fixtures.item(4))
        rows = self.controller.list_issues()
        self.assertFalse(rows[0]['dispatchable'])
        self.assertTrue(rows[1]['dispatchable'])
        self.assertTrue(self.controller.eligible(self.gh.items[0], backend='jules'))

    def test_existing_owner_survives_label_change(self):
        self.settings()
        self.record(backend='codex')
        self.assertEqual(self.controller.backend_for(self.gh.items[0]), 'codex')
        self.controller.state.save('item-3', backend='jules', jules={'phase': 'remote'})
        self.gh.items[0]['content']['labels']['nodes'].pop()
        self.assertEqual(self.controller.backend_for(self.gh.items[0]), 'jules')

    def test_rework_waits_for_remote_terminal_then_selects_codex(self):
        self.settings()
        self.record(backend='jules', jules={'phase': 'remote'})
        self.gh.status({}, 'item-3', 'Rework')
        self.assertFalse(self.controller.eligible(self.gh.items[0]))
        self.controller.state.save('item-3', jules={'phase': 'superseded'})
        self.assertTrue(self.controller.eligible(self.gh.items[0]))

    def prepare_checkout(self):
        self.checkout()
        self.controller.state.db.execute('DELETE FROM issues')
        self.controller.state.db.commit()
        self.gh.status({}, 'item-3', 'Ready')

    def test_lost_submission_response_never_dispatches_again_after_restart(self):
        self.settings()
        self.prepare_checkout()
        cli = FakeCLI()
        cli.ambiguous = True
        with self.assertRaises(c.Error):
            self.worker(cli).tick()
        self.assertEqual(self.worker(cli).tick(), 'submission_unknown')
        self.assertEqual(cli.submissions, 1)
        record = self.controller.state.get('item-3')
        self.assertEqual(record['backend'], 'jules')
        self.assertFalse(self.controller.list_issues()[0]['dispatchable'])

    def test_remote_ownership_resumes_without_second_submission(self):
        self.settings()
        self.prepare_checkout()
        cli = FakeCLI()
        self.assertEqual(self.worker(cli).tick(), 'remote')
        self.assertEqual(self.worker(cli).tick(), 'planning')
        self.assertEqual(cli.submissions, 1)
        self.assertEqual(self.controller.state.get('item-3')['jules']['session_id'], '101')

    def test_rework_during_every_publication_phase_waits_for_safe_supersession(self):
        self.settings()
        self.prepare_checkout()
        for phase in ('patch_ready', 'applying', 'applied', 'committing', 'pushing', 'publishing', 'checks'):
            with self.subTest(phase=phase):
                self.record(backend='jules', jules={'phase': phase})
                self.gh.status({}, 'item-3', 'Rework')
                self.assertFalse(self.controller.eligible(self.gh.items[0]))
                worker = self.worker(FakeCLI())
                with patch.object(c.support, 'run', side_effect=AssertionError('Must not mutate old checkout')):
                    self.assertEqual(worker.tick(), 'superseded')
                self.assertTrue(self.controller.eligible(self.gh.items[0]))
                self.assertEqual((self.path/'hello.py').read_text(), 'print("hello")\n')

    def test_stale_jules_snapshot_cannot_write_into_new_codex_cycle(self):
        self.settings()
        self.record(backend='jules', jules={'phase': 'pushing'})
        old = self.controller.state.get('item-3')
        self.controller.state.save('item-3', backend='codex', cycle=2, jules={})
        worker = self.worker(FakeCLI())
        with patch.object(c.support, 'run', side_effect=AssertionError('No cross-cycle mutation')):
            self.assertEqual(worker.advance(old), 'superseded')
        self.assertEqual(self.controller.state.get('item-3')['backend'], 'codex')
        self.assertEqual(self.controller.state.get('item-3')['cycle'], 2)


class CLITests(unittest.TestCase):
    def test_supervisor_rejects_missing_cli_runtime_before_any_worker_starts(self):
        import launch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.json'
            path.write_text(json.dumps({'state_directory': directory, 'enabled': False,
                'runtime_bin': [], 'symphony': '/configured/symphony', 'workflow': '/configured/WORKFLOW.md',
                'jules': {'enabled': True, 'binary': '/configured/jules'}}))
            failed = subprocess.CompletedProcess([], 1, '', "Cannot find module 'fs/promises'")
            with patch('sys.argv', ['launch', '--config', str(path)]), \
                    patch.object(jules.subprocess, 'run', return_value=failed) as probe, \
                    patch.object(launch.os, 'chdir'), \
                    patch.object(launch.subprocess, 'Popen') as start:
                with self.assertRaises(c.Error):
                    launch.main()
                start.assert_not_called()
                self.assertEqual(probe.call_args.kwargs['env']['PATH'],
                                 launch.worker_environment({'runtime_bin': []}, path.parent)['PATH'])
            self.assertIn('fs/promises', (Path(directory)/'jules/runtime/startup.stderr').read_text())

    def test_zero_exit_api_error_is_not_submission_success(self):
        cli = jules.JulesCLI({'jules': {'binary': 'jules'}})
        with patch.object(cli, 'invoke', return_value='API error: 401'):
            with self.assertRaises(c.Error):
                cli.submit('example/project', 'prompt', Path('/tmp'))

    def test_status_requires_the_recorded_session(self):
        cli = jules.JulesCLI({'jules': {'binary': 'jules'}})
        with patch.object(cli, 'invoke', return_value=' 101 task example/repo 1m ago Completed\n'):
            self.assertEqual(cli.status('101', 'example/repo', Path('/tmp')), 'completed')
            with self.assertRaises(c.Error):
                cli.status('202', 'example/repo', Path('/tmp'))

class PublicationTests(unittest.TestCase):
    setUp = RoutingTests.setUp
    tearDown = RoutingTests.tearDown
    record = RoutingTests.record
    checkout = RoutingTests.checkout
    git = RoutingTests.git
    settings = RoutingTests.settings
    worker = RoutingTests.worker
    prepare_checkout = RoutingTests.prepare_checkout

    def test_completed_remote_patch_becomes_one_verified_draft_and_review(self):
        import json
        from controller import browser
        self.settings()
        self.prepare_checkout()
        (self.path/'hello.py').write_text('print("changed")\n')
        (self.path/'.symphony-jules-report.json').write_text(json.dumps(fixtures.REPORT))
        self.git('add', '--', 'hello.py', '.symphony-jules-report.json')
        returned_patch = self.git('diff', '--cached', '--binary') + '\n'
        self.git('reset', '--hard', 'HEAD')
        cli = FakeCLI()
        cli.current = 'completed'
        cli.patch = returned_patch
        self.config['checks'] = [['python3', '-m', 'unittest']]
        worker = self.worker(cli)
        remote = {'sha': None}
        original_run = c.support.run
        original_api = self.gh.api
        self.gh.remote_head = lambda *_: remote['sha']
        def run(argv, **kwargs):
            if argv[:2] == ['git', 'push']:
                remote['sha'] = self.git('rev-parse', 'HEAD')
                return ''
            return original_run(argv, **kwargs)
        def api(endpoint, method='GET', payload=None):
            if endpoint.endswith('/pulls') and method == 'POST':
                self.gh.pr = {'number': 20, 'state': 'open', 'draft': True, 'body': payload['body'],
                              'html_url': 'https://github.com/example/project/pull/20',
                              'head': {'ref': self.task.branch, 'sha': remote['sha'], 'repo': {'full_name': fixtures.REPO}},
                              'base': {'ref': 'main'}}
                return deepcopy(self.gh.pr)
            return original_api(endpoint, method, payload)
        self.gh.api = api
        self.gh.check_rows = [{'name': name, 'state': 'success'} for name in self.controller.policy['validation']['required_checks']]
        with patch.object(c.support, 'run', side_effect=run), \
             patch.object(c.runtime.Sandbox, 'check', return_value=b'Independent check passed'):
            self.assertEqual(worker.tick(), 'remote')
            self.assertEqual(worker.tick(), 'publishing')
            self.assertEqual(worker.tick(), 'in_review')
            self.assertEqual(worker.tick(), 'idle')
        self.assertEqual(cli.submissions, 1)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual((self.path/'hello.py').read_text(), 'print("changed")\n')
        self.assertFalse((self.path/'.symphony-jules-report.json').exists())
        self.assertEqual(self.controller.state.get('item-3')['jules']['phase'], 'review')
        self.assertEqual(c.fields(self.gh.items[0])['Status'], 'In review')
