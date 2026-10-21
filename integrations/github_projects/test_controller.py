"""Exercise tracker lifecycle, recovery, and native Git publication boundaries."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import controller as c
from checks import verify_changes

REPO = 'example/project'
REPORT = {'summary': 'Implemented behavior', 'blocked': False, 'blocker': '',
          'checks': [{'command': 'python -m unittest', 'result': 'passed', 'details': 'passed'}]}


def item(number=3, status='Ready', labels=('ready-for-agent',)):
    return {'id': f'item-{number}', 'isArchived': False,
            'fieldValues': {'nodes': [{'name': status, 'field': {'name': 'Status'}}]},
            'content': {'__typename': 'Issue', 'id': f'issue-{number}', 'number': number,
                        'title': f'Issue {number}', 'state': 'OPEN',
                        'url': f'https://github.com/{REPO}/issues/{number}',
                        'repository': {'nameWithOwner': REPO, 'isArchived': False},
                        'labels': {'nodes': [{'name': name} for name in labels]}}}


class FakeGitHub:
    def __init__(self, items=None):
        self.items = items or [item()]
        self.pr = None
        self.comment_rows = []
        self.check_rows = []
        self.dependencies = []
        self.assignees = []
        self.calls = []
        self.fail_status = False

    def project(self, *_):
        return {'id': 'project-6', 'viewerCanUpdate': True}, deepcopy(self.items)

    def issue(self, task):
        row = next(i for i in self.items if i['content']['number'] == task.number)['content']
        return {'state': row['state'].lower(), 'body': 'Acceptance criteria',
                'labels': deepcopy(row['labels']['nodes']), 'assignees': self.assignees}

    def item_status(self, item_id):
        return c.fields(next(i for i in self.items if i['id'] == item_id))['Status']

    def status(self, _, item_id, status):
        if self.fail_status:
            raise c.Error('Status API unavailable')
        next(i for i in self.items if i['id'] == item_id)['fieldValues']['nodes'][0]['name'] = status

    def pages(self, endpoint):
        if '/dependencies/' in endpoint:
            return iter(deepcopy(self.dependencies))
        return iter(deepcopy(self.comment_rows))

    def checks(self, *_):
        return deepcopy(self.check_rows)

    def remote_head(self, _, branch):
        return self.pr['head']['sha'] if self.pr and branch == self.pr['head']['ref'] else None

    def pull_request(self, _):
        return deepcopy(self.pr)

    def issue_prs(self, _):
        return [deepcopy(self.pr)] if self.pr else []

    def api(self, endpoint, method='GET', payload=None):
        self.calls.append((endpoint, method, payload))
        if '/pulls/' in endpoint:
            if method == 'PATCH':
                self.pr.update(payload)
            if endpoint.endswith('/merge'):
                self.pr.update(state='closed', merged=True, merge_commit_sha='merge-sha')
                return {'merged': True}
            return deepcopy(self.pr)
        if '/issues/comments/' in endpoint:
            number = int(endpoint.rsplit('/', 1)[-1])
            row = next(r for r in self.comment_rows if r['id'] == number)
            if method == 'DELETE':
                self.comment_rows.remove(row)
                return None
            if method == 'PATCH':
                row.update(payload)
            return deepcopy(row)
        if endpoint.endswith('/comments') and method == 'POST':
            row = {'id': len(self.comment_rows) + 1, **payload}
            self.comment_rows.append(row)
            return deepcopy(row)
        if '/issues/' in endpoint and method == 'PATCH':
            n = int(endpoint.rsplit('/', 1)[-1])
            next(i for i in self.items if i['content']['number'] == n)['content']['state'] = payload['state'].upper()
            return payload
        if endpoint == 'graphql':
            return {'data': {'ok': True}}
        raise AssertionError((endpoint, method))

    def publish_artifacts(self, task, rows, target):
        return [{**row, 'url': 'https://github.com/evidence/image.png'} for row in rows]


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {'repository': REPO, 'project_owner': 'example', 'project_number': 17,
                       'enabled': True, 'only_issue': None, 'workspace_root': str(self.root/'workspaces'),
                       'state_directory': str(self.root/'state'), 'checks': [], 'python': 'python3', 'runtime_roots': []}
        self.gh = FakeGitHub()
        self.controller = c.Controller(self.config, self.gh)
        self.task = c.task_for(item())
        self.path = self.controller.workspace(self.task)

    def tearDown(self):
        self.controller.state.db.close()
        self.temp.cleanup()

    def record(self, **values):
        return self.controller.state.save('item-3', task=self.task.__dict__, phase='running',
                                         cycle=1, branch=self.task.branch, workspace=str(self.path), **values)

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.path, stderr=subprocess.DEVNULL).decode().strip()

    def checkout(self):
        self.path.mkdir(parents=True)
        self.git('init', '-b', self.task.branch)
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('remote', 'add', 'origin', f'https://github.com/{REPO}.git')
        (self.path/'hello.py').write_text('print("hello")\n')
        self.git('add', 'hello.py')
        self.git('commit', '-m', 'Base')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        sha = self.git('rev-parse', 'HEAD')
        self.record(base_sha=sha)
        self.gh.status({}, 'item-3', 'In progress')
        return sha

    def pr(self, **changes):
        self.gh.pr = {'number': 20, 'state': 'open', 'draft': True, 'body': c.MARKER,
                      'html_url': f'https://github.com/{REPO}/pull/20', 'merged': False,
                      'mergeable': True, 'mergeable_state': 'clean',
                      'head': {'ref': self.task.branch, 'sha': 'head-1', 'repo': {'full_name': REPO}},
                      'base': {'ref': 'main'}, **changes}
        self.record(pr_number=20, pr_url=self.gh.pr['html_url'], published_sha=self.gh.pr['head']['sha'])
        return self.gh.pr

    def success(self):
        self.gh.check_rows = [{'name': name, 'state': 'success'} for name in self.controller.policy['validation']['required_checks']]

    def test_host_git_remote_is_bound_and_checks_publication(self):
        sha = self.checkout()
        with patch.object(self.controller, 'validate') as validate, \
             patch.object(self.gh, 'remote_head', return_value=sha), \
             patch.object(c.support, 'run') as run:
            result = self.controller.git_remote('item-3', self.path, 'push')
            self.assertTrue(result['remote_verified'])
            validate.assert_called_once_with('item-3', self.path)
            run.assert_not_called()
        self.git('remote', 'set-url', 'origin', 'https://github.com/other/project.git')
        with self.assertRaises(c.Error):
            self.controller.git_remote('item-3', self.path, 'fetch')

    def test_host_git_remote_never_pushes_uncommitted_source(self):
        self.checkout()
        (self.path/'hello.py').write_text('uncommitted')
        with patch.object(self.controller, 'validate'), patch.object(c.support, 'run') as run:
            with self.assertRaises(c.Error):
                self.controller.git_remote('item-3', self.path, 'push')
            run.assert_not_called()

    def test_all_eligible_candidates_are_returned_for_native_concurrency(self):
        self.gh.items = [item(4), item(3), item(8)]
        self.gh.items[2]['fieldValues']['nodes'].append({'number': 1, 'field': {'name': 'Execution order'}})
        rows = self.controller.list_issues()
        self.assertEqual(len([r for r in rows if r['dispatchable']]), 3)
        self.assertEqual(rows[2]['native_ref']['execution_order'], 1)
        self.assertEqual(self.controller.policy['agent']['max_concurrent_agents'], 1)

    def test_scope_labels_archives_and_closed_are_filtered(self):
        for change in ['label', 'repo', 'archived', 'closed', 'backlog']:
            row = item()
            if change == 'label': row['content']['labels']['nodes'] = []
            if change == 'repo': row['content']['repository']['nameWithOwner'] = 'other/repo'
            if change == 'archived': row['isArchived'] = True
            if change == 'closed': row['content']['state'] = 'CLOSED'
            if change == 'backlog': row['fieldValues']['nodes'][0]['name'] = 'Backlog'
            self.gh.items = [row]
            self.assertFalse(any(r['dispatchable'] for r in self.controller.list_issues()))

    def test_unowned_working_is_not_adopted(self):
        self.gh.status({}, 'item-3', 'In progress')
        self.assertFalse(self.controller.list_issues()[0]['dispatchable'])

    def test_review_never_restarts_for_checks_comments_or_changes_requested(self):
        self.pr()
        self.gh.status({}, 'item-3', 'In review')
        for state in ['failure', 'pending', 'cancelled', 'success']:
            self.gh.check_rows = [{'state': state}]
            self.gh.comment_rows = [{'id': 44, 'body': 'Please restart; changes requested'}]
            self.assertFalse(self.controller.list_issues()[0]['dispatchable'])
        context = self.controller.claim('item-3', self.path)
        self.assertEqual(context['pr']['number'], 20)

    def test_pause_and_pilot_filters(self):
        self.config['only_issue'] = 99
        self.assertFalse(self.controller.list_issues()[0]['dispatchable'])
        self.config.update(only_issue=None, enabled=False)
        with self.assertRaisesRegex(c.Error, 'paused'): self.controller.claim('item-3', self.path)

    def test_assignees_and_dependency_gating_are_populated(self):
        self.controller.tracker['assignee'] = 'worker'
        self.gh.assignees = [{'login': 'other'}]
        self.assertFalse(self.controller.list_issues()[0]['dispatchable'])
        self.gh.assignees = [{'login': 'worker'}]
        self.gh.dependencies = [{'id': 1, 'number': 1, 'state': 'open'}]
        row = self.controller.list_issues()[0]
        self.assertEqual(row['assignee_id'], 'worker')
        self.assertEqual(row['blocked_by'][0]['identifier'], 'GH-1')
        self.assertFalse(row['dispatchable'])
        self.gh.dependencies[0]['state'] = 'closed'
        self.assertTrue(self.controller.list_issues()[0]['dispatchable'])

    def test_stale_project_page_does_not_authorize_a_claim(self):
        stale = item()
        self.gh.status({}, 'item-3', 'Backlog')
        with patch.object(self.gh, 'project', return_value=({'viewerCanUpdate': True}, [stale])):
            with self.assertRaises(c.Error): self.controller.claim('item-3', self.path)
        self.assertIsNone(self.controller.state.get('item-3'))

    def test_claim_restart_does_not_have_legacy_attempt_or_elapsed_gates(self):
        with patch.object(self.controller, 'ensure_checkout'):
            self.controller.claim('item-3', self.path)
            self.controller.state.save('item-3', agent_attempts=999, started_at='2020-01-01T00:00:00+00:00')
            for _ in range(5):
                self.controller.after_run(self.path)
                self.controller.claim('item-3', self.path, new_attempt=True)
        self.assertTrue(self.controller.list_issues()[0]['dispatchable'])
        self.assertEqual(len(self.gh.comment_rows), 1)
        self.assertEqual(self.controller.state.get('item-3')['cycle'], 1)

    def test_workpad_notes_preserve_checklists_and_evidence(self):
        record = self.record()
        self.controller.workpad(self.task, record, 'start')
        body = '## Codex Workpad\n### Plan\n- [x] implemented\n### Acceptance Criteria\n- [x] correct\n### Validation\nPassed; video URL\n### Notes\nReproduced'
        self.controller.update_workpad(self.task, body)
        self.controller.workpad(self.task, self.controller.state.get('item-3'), 'resumed')
        self.assertIn(body, self.gh.comment_rows[0]['body'])
        self.assertEqual(len(self.gh.comment_rows), 1)

    def test_additive_cycle_migration_is_idempotent_and_lossless(self):
        original = self.record(visual_evidence=[{'url': 'evidence'}], report=REPORT)
        self.controller.state.db.close()
        self.controller = c.Controller(self.config, self.gh)
        self.assertEqual(self.controller.state.get('item-3'), original)
        self.assertEqual(self.controller.state.history('item-3'), [original])
        self.controller.state.db.close()
        self.controller = c.Controller(self.config, self.gh)
        self.assertEqual(len(self.controller.state.history('item-3')), 1)

    def test_rework_reserves_one_replacement_cycle_across_duplicate_events(self):
        self.pr()
        self.controller.workpad(self.task, self.controller.state.get('item-3'), 'prior evidence')
        self.gh.status({}, 'item-3', 'Rework')
        with patch.object(self.controller, 'ensure_checkout'):
            first = self.controller.claim('item-3', self.path)
            second = self.controller.claim('item-3', self.path)
        self.assertEqual(first['branch'], 'codex/symphony-issue-3-cycle-2')
        self.assertEqual(second['cycle'], 2)
        self.assertEqual(self.controller.state.history('item-3')[0]['pr_number'], 20)
        task = self.controller.task(self.gh.items[0])
        with self.assertRaisesRegex(c.Error, 'Close the previous'): self.controller.restart(task)
        self.gh.pr['state'] = 'closed'
        with self.assertRaisesRegex(c.Error, 'Remove the previous'): self.controller.restart(task)
        self.gh.comment_rows.clear()
        self.controller.restart(task)
        self.assertEqual(self.gh.item_status('item-3'), 'In progress')
        self.assertEqual(len(self.gh.comment_rows), 1)

    def test_rework_does_not_adopt_an_older_legacy_workpad(self):
        self.pr()
        self.gh.comment_rows = [{'id': 9, 'body': c.MARKER+'\nolder workpad'},
                                {'id': 10, 'body': c.MARKER+'\ncurrent complete plan and evidence'}]
        self.controller.state.save('item-3', comment_id=10)
        self.gh.status({}, 'item-3', 'Rework')
        with patch.object(self.controller, 'ensure_checkout'):
            self.controller.claim('item-3', self.path)
        self.gh.pr['state'] = 'closed'
        self.gh.comment_rows.pop()
        self.controller.restart(self.controller.task(self.gh.items[0]))
        self.assertNotEqual(self.controller.state.get('item-3')['comment_id'], 9)
        self.assertEqual(self.gh.comment_rows[0]['body'], c.MARKER+'\nolder workpad')
        self.assertIn('current complete plan', self.controller.state.history('item-3')[0]['workpad_snapshot'])

    def test_closed_pr_ready_starts_fresh_cycle(self):
        self.pr(state='closed')
        with patch.object(self.controller, 'ensure_checkout'):
            result = self.controller.claim('item-3', self.path)
        self.assertEqual(result['cycle'], 2)
        self.assertEqual(self.controller.state.history('item-3')[0]['pr_number'], 20)

    def test_claim_discovers_existing_open_pr(self):
        self.pr()
        self.controller.state.db.execute('DELETE FROM issues')
        self.controller.state.db.commit()
        with patch.object(self.controller, 'ensure_checkout'):
            result = self.controller.claim('item-3', self.path)
        self.assertEqual(result['pr']['number'], 20)

    def test_lost_ledger_discovers_closed_pr_and_reserves_fresh_cycle(self):
        self.pr(state='closed')
        self.controller.state.db.execute('DELETE FROM issues')
        self.controller.state.db.commit()
        with patch.object(self.controller, 'ensure_checkout'):
            result = self.controller.claim('item-3', self.path)
        self.assertEqual(result['cycle'], 2)
        self.assertEqual(result['branch'], 'codex/symphony-issue-3-cycle-2')
        self.assertEqual(self.controller.state.history('item-3')[0]['pr_number'], 20)

    def test_prepare_allows_native_commits_and_configures_checkout_auth(self):
        self.checkout()
        (self.path/'hello.py').write_text('print("changed")\n')
        self.git('add', 'hello.py'); self.git('commit', '-m', 'Agent commit')
        self.controller.ensure_checkout(self.task, self.controller.state.get('item-3'), self.path)
        self.assertIn('auth git-credential', self.git('config', '--local', 'credential.helper'))
        self.assertEqual(self.git('log', '-1', '--format=%s'), 'Agent commit')

    def test_push_scanner_catches_already_committed_secrets(self):
        self.checkout()
        (self.path/'bad.txt').write_text('ghp_' + 'a'*36)
        self.git('add', 'bad.txt'); self.git('commit', '-m', 'Bad')
        with self.assertRaisesRegex(c.Error, 'credential'): verify_changes(self.path)

    def test_push_scanner_catches_frozen_changes(self):
        self.checkout()
        p = self.path/'protected/file'; p.parent.mkdir(parents=True); p.write_text('changed')
        self.git('add', str(p)); self.git('commit', '-m', 'Frozen')
        with self.assertRaisesRegex(c.Error, 'Frozen'): verify_changes(self.path, protected_paths=['protected/**'])

    def test_validation_does_not_commit_or_publish(self):
        self.checkout()
        head = self.git('rev-parse', 'HEAD')
        (self.path/'hello.py').write_text('print("changed")\n')
        self.controller.validate('item-3', self.path)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertTrue(self.git('status', '--porcelain'))
        self.assertFalse(any('/pulls' in call[0] for call in self.gh.calls))

    def test_exact_head_checks_reject_missing_pending_cancelled_and_stale(self):
        self.record()
        for state in ['pending', 'cancelled', 'failure', 'skipped']:
            self.success(); self.gh.check_rows[0]['state'] = state
            with self.assertRaises(c.Error): self.controller.require_checks(self.task, 'head')
        self.gh.check_rows = []
        with self.assertRaises(c.Error): self.controller.require_checks(self.task, 'head')
        self.success(); self.controller.require_checks(self.task, 'head')
        pr = self.pr()
        with self.assertRaises(c.Error): self.controller.verify_pr(self.task, pr, 'old-head')

    def test_complete_native_git_cycle_hands_off_without_publication_mutations(self):
        sha = self.checkout()
        pr = self.pr(); pr['head']['sha'] = sha
        self.controller.validate('item-3', self.path)
        self.success()
        result = self.controller.review('item-3', self.path, {'pr_number': 20, 'sha': sha})
        self.assertEqual(result['status'], 'in_review')
        self.assertEqual(self.gh.item_status('item-3'), 'In review')
        self.assertFalse(any(method == 'POST' and '/pulls' in endpoint for endpoint, method, _ in self.gh.calls))

    def test_review_rechecks_remote_head_before_transition(self):
        sha = self.checkout(); pr = self.pr(); pr['head']['sha'] = sha
        self.controller.validate('item-3', self.path); self.success()
        original = self.controller.require_checks
        def changed(*args):
            value = original(*args); self.gh.pr['head']['sha'] = 'external'; return value
        with patch.object(self.controller, 'require_checks', side_effect=changed):
            with self.assertRaises(c.Error): self.controller.review('item-3', self.path, {'pr_number': 20, 'sha': sha})
        self.assertEqual(self.gh.item_status('item-3'), 'In progress')

    def test_landing_requires_human_state_selected_pr_sha_and_ci(self):
        self.pr(draft=False); self.success()
        args = {'method': 'PUT', 'path': f'/repos/{REPO}/pulls/20/merge',
                'body': {'merge_method': 'squash', 'sha': 'head-1'}}
        with self.assertRaisesRegex(c.Error, 'human'): self.controller.github_request(self.task, self.gh.items[0], args)
        self.gh.status({}, 'item-3', 'Merging')
        self.gh.check_rows = []
        with self.assertRaises(c.Error): self.controller.github_request(self.task, self.gh.items[0], args)
        self.success()
        self.controller.github_request(self.task, self.gh.items[0], args)
        self.controller.done('item-3')
        self.assertEqual(self.gh.item_status('item-3'), 'Done')
        self.assertEqual(self.gh.items[0]['content']['state'], 'CLOSED')
        self.controller.done('item-3') # recovery after an ambiguous successful response

    def test_landing_never_bypasses_repository_rules_or_unknown_mergeability(self):
        self.success()
        self.gh.status({}, 'item-3', 'Merging')
        args = {'method': 'PUT', 'path': f'/repos/{REPO}/pulls/20/merge',
                'body': {'merge_method': 'squash', 'sha': 'head-1'}}
        for values in [{'draft': True}, {'draft': False, 'mergeable': None},
                       {'draft': False, 'mergeable_state': 'blocked'},
                       {'draft': False, 'mergeable_state': 'dirty'},
                       {'draft': False, 'mergeable_state': 'unknown'}]:
            self.pr(**values)
            with self.subTest(values=values), self.assertRaisesRegex(c.Error, 'no admin bypass'):
                self.controller.github_request(self.task, self.gh.items[0], args)
        self.assertFalse(any(endpoint.endswith('/merge') for endpoint, _, _ in self.gh.calls))

    def test_human_can_revoke_landing_while_checks_are_read(self):
        self.pr(draft=False); self.success()
        self.gh.status({}, 'item-3', 'Merging')
        args = {'method': 'PUT', 'path': f'/repos/{REPO}/pulls/20/merge',
                'body': {'merge_method': 'squash', 'sha': 'head-1'}}
        original = self.controller.require_checks
        def revoke(*values):
            result = original(*values)
            self.gh.status({}, 'item-3', 'In review')
            return result
        with patch.object(self.controller, 'require_checks', side_effect=revoke):
            with self.assertRaisesRegex(c.Error, 'board moved'):
                self.controller.github_request(self.task, self.gh.items[0], args)
        self.assertFalse(self.gh.pr['merged'])

    def test_optional_bot_check_is_not_an_expected_ci_prerequisite(self):
        self.success()
        self.gh.check_rows.append({'name': 'Optional bot review', 'state': 'neutral'})
        self.controller.require_checks(self.task, 'head-1')

    def test_graphql_uses_native_api_and_rejects_merge_bypass(self):
        self.record()
        value = self.controller.github_request(self.task, self.gh.items[0], {'method': 'POST', 'path': '/graphql', 'body': {'query': '{viewer{login}}'}})
        self.assertEqual(value['status'], 200)
        with self.assertRaises(c.Error):
            self.controller.github_request(self.task, self.gh.items[0], {'method': 'POST', 'path': '/graphql', 'body': {'query': 'mutation { mergePullRequest }'}})

    def test_terminal_cleanup_preserves_dirty_and_unpublished_work(self):
        self.checkout()
        (self.path/'unfinished.txt').write_text('retain me')
        self.controller.cleanup(self.path)
        saved = Path(self.controller.state.get('item-3')['preserved_workspace'])
        self.assertEqual((saved/'unfinished.txt').read_text(), 'retain me')
        self.assertFalse(self.path.exists())

    def test_completion_recovers_after_board_update_or_close_response_is_interrupted(self):
        self.pr(state='closed', merged=True)
        self.gh.status({}, 'item-3', 'Merging')
        self.gh.fail_status = True
        with self.assertRaises(c.Error): self.controller.done('item-3')
        self.assertEqual(self.controller.state.get('item-3')['phase'], 'completing')
        self.gh.fail_status = False
        rows = self.controller.list_issues()
        self.assertEqual(self.gh.item_status('item-3'), 'Done')
        self.assertEqual(self.gh.items[0]['content']['state'], 'CLOSED')
        self.assertEqual(rows[0]['state'], 'Done')
        self.assertEqual(self.controller.state.get('item-3')['phase'], 'done')

    def test_lost_merge_response_resumes_selected_merged_pr(self):
        self.pr(state='closed', merged=True)
        self.gh.status({}, 'item-3', 'Merging')
        with patch.object(self.controller, 'ensure_checkout'):
            context = self.controller.claim('item-3', self.path)
        self.assertTrue(context['pr']['merged'])
        self.controller.done('item-3')
        self.assertEqual(self.controller.state.get('item-3')['cycle'], 1)

    def test_terminal_cleanup_preserves_unowned_nonempty_checkout(self):
        self.path.mkdir(parents=True)
        (self.path/'untracked').write_text('unfinished')
        result = self.controller.cleanup(self.path)
        self.assertEqual(result['status'], 'preserved_unowned')
        self.assertEqual((Path(result['workspace'])/'untracked').read_text(), 'unfinished')

    def test_sync_conflict_preserves_native_git_work_and_can_be_resolved(self):
        self.checkout()
        self.git('branch', 'main')
        (self.path/'hello.py').write_text('issue change\n')
        self.git('add', 'hello.py'); self.git('commit', '-m', 'Issue')
        self.git('switch', 'main')
        (self.path/'hello.py').write_text('main change\n')
        self.git('add', 'hello.py'); self.git('commit', '-m', 'Main')
        self.git('switch', self.task.branch)
        conflict = subprocess.run(['git', 'merge', 'main'], cwd=self.path, capture_output=True)
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn('UU hello.py', self.git('status', '--porcelain'))
        (self.path/'hello.py').write_text('resolved issue and main change\n')
        self.git('add', 'hello.py'); self.git('commit', '-m', 'Resolve synchronization')
        self.controller.ensure_checkout(self.task, self.controller.state.get('item-3'), self.path)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(len(self.git('rev-list', '--parents', '-1', 'HEAD').split()), 3)

    def test_workspace_escape_rejected(self):
        with self.assertRaises(c.Error): self.controller.workspace(self.task, self.root/'outside')


if __name__ == '__main__': unittest.main()
