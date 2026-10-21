"""One durable Jules worker alongside Symphony's independent Codex worker.

CLI output and patches are private. Submission is write-ahead and never replayed
when delivery is uncertain; remote ownership is independent of local process life.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

from controller import Controller, Error, MARKER, Task, controller_lock, now, private_json
import browser
import support


REMOTE_ACTIVE = {'submitting', 'submission_unknown', 'remote'}
TERMINAL = {'review', 'superseded', 'failed'}


def validate_runtime(config, environment):
    """Load the CLI under the exact supervisor environment before admission."""
    directory = Path(config['state_directory']) / 'jules/runtime'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        result = subprocess.run([config['jules']['binary'], '--help'], text=True,
                                capture_output=True, timeout=30, env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Error('Jules CLI cannot start under the configured runtime_bin') from error
    (directory/'startup.stdout').write_text(result.stdout)
    (directory/'startup.stderr').write_text(result.stderr)
    if result.returncode or 'A CLI for Jules' not in result.stdout:
        raise Error('Jules CLI cannot start under the configured runtime_bin; inspect private startup output')
    return {'cli_startup_verified': True}


class JulesCLI:
    def __init__(self, config):
        self.config = config
        self.binary = config['jules']['binary']

    def invoke(self, args, directory, *, prompt=None, timeout=120):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = subprocess.run([self.binary, 'remote', *args], input=prompt, text=True,
                                capture_output=True, timeout=timeout, env=support.github_environment())
        # The CLI sometimes returns zero for API failures. Callers also validate
        # the response shape or reconcile the remote session before proceeding.
        (directory/'cli.stdout').write_text(result.stdout)
        (directory/'cli.stderr').write_text(result.stderr)
        if result.returncode:
            raise Error('Jules CLI failed; inspect private session output')
        return result.stdout

    def submit(self, repository, prompt, directory):
        output = self.invoke(['new', '--repo', repository], directory, prompt=prompt)
        match = re.search(r'(?:jules\.google\.com/session/|Session(?:\s+ID)?\s*[:=]\s*)(\d+)', output, re.I)
        if not match:
            raise Error('Jules submission is uncertain; reconcile remote sessions before retrying')
        return match[1]

    def status(self, session, repository, directory):
        output = self.invoke(['list', '--session'], directory)
        row = next((line for line in output.splitlines() if re.match(r'^\s*' + re.escape(session) + r'\s', line)), None)
        if row is None:
            raise Error('Recorded Jules session is absent from the listing; ownership remains reserved')
        match = re.search(r'\s(Completed|Failed|Cancelled|In Progress|Planning|Awaiting.*)\s*$', row, re.I)
        if not match:
            raise Error('Jules returned an unrecognized session state')
        return match[1].lower()

    def pull(self, session, directory):
        output = self.invoke(['pull', '--session', session], directory)
        if not output.startswith('diff --git '):
            raise Error('Jules did not return a Git patch; inspect private output')
        return output


def owns_cycle(record):
    record = record or {}
    return record.get('backend') == 'jules' and record.get('jules', {}).get('phase') not in TERMINAL


class Superseded(Exception):
    pass


class JulesWorker:
    def __init__(self, config, gh=None, cli=None):
        self.config = config
        self.controller = Controller(config, gh)
        self.cli = cli or JulesCLI(config)

    def close(self):
        self.controller.state.db.close()

    def save(self, item_id, **changes):
        current = self.controller.state.get(item_id)
        return self.controller.state.save(item_id, jules={**current.get('jules', {}), **changes})

    def directory(self, record):
        task = record['task']
        return Path(self.config['state_directory']) / 'jules' / f"GH-{task['number']}" / str(record['cycle'])

    def tick(self):
        c = self.controller
        # Resume existing remote/publication work before selecting another issue.
        owned = [r for r in c.state.all() if r.get('backend') == 'jules'
                 and r.get('jules', {}).get('phase') not in TERMINAL]
        if len(owned) > 1:
            raise Error('Multiple Jules claims require reconciliation before dispatch')
        if owned:
            if not self.config['enabled']:
                return 'paused'
            return self.advance(owned[0])
        if not self.config['enabled'] or not self.config.get('jules', {}).get('enabled'):
            return 'paused'
        _, items = c.snapshot()
        candidates = []
        for item in items:
            if c.eligible(item, backend='jules') and c.normalized(item, True)['dispatchable']:
                candidates.append(item)
        candidates.sort(key=lambda item: (c.task(item).order if c.task(item).order is not None else float('inf'),
                                         {'P0': 0, 'P1': 1, 'P2': 2}.get(c.task(item).priority, 3), c.task(item).number))
        if not candidates:
            return 'idle'
        item = candidates[0]
        task = c.task(item)
        c.claim(item['id'], c.workspace(task), backend='jules')
        record = c.state.get(item['id'])
        self.save(item['id'], phase='claimed', created_at=now())
        return self.advance(c.state.get(item['id']))

    def advance(self, record):
        c = self.controller
        item_id = record['task']['item_id']
        with c.claim_lock(item_id):
            current = c.state.get(item_id)
            if not current or current.get('cycle') != record.get('cycle') or current.get('backend') != 'jules':
                return 'superseded'
            try:
                return self._advance(current)
            except Superseded:
                return 'superseded'

    def require_working(self, record):
        c = self.controller
        item_id = record['task']['item_id']
        current = c.state.get(item_id)
        if not current or current.get('cycle') != record.get('cycle') or current.get('backend') != 'jules':
            raise Superseded()
        _, item, _ = c.find(item_id)
        if c.role(item) != 'working':
            self.save(item_id, phase='superseded')
            raise Superseded()

    def _advance(self, record):
        c = self.controller
        item_id = record['task']['item_id']
        _, items = c.snapshot()
        item = next((row for row in items if row['id'] == item_id), None)
        task = Task(**record['task'])
        path = c.workspace(task)
        state = record.get('jules', {})
        phase = state.get('phase', 'claimed')
        directory = self.directory(record)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if phase in {'submitting', 'submission_unknown'}:
            # No idempotency key in this CLI: a lost response must not cause a
            # second remote job. Retain the intent and exact private prompt.
            return 'submission_unknown'
        if phase == 'claimed':
            _, item, task = c.find(item_id)
            if c.role(item) != 'working':
                raise Error('Jules issue left the working state before submission')
            issue = c.gh.issue(task)
            comments = [row.get('body', '') for row in c.gh.pages(f'repos/{task.repository}/issues/{task.number}/comments')]
            prompt = (f"Implement {task.url} in {task.repository}. Start from {c.base_branch} at {record['base_sha']}.\n"
                      f"Work cycle {task.cycle}; intended branch {task.branch}.\n"
                      "Return a patch only; the trusted controller validates, commits and creates the draft PR.\n"
                      "Read AGENTS.md and repository guidance. Implement only this issue. Run its required validation.\n"
                      "Include a temporary .symphony-jules-report.json in the patch with summary, blocked (boolean), "
                      "blocker, and checks [{command,result,details}], using actual passed/failed/not_run evidence. "
                      "The controller removes that handoff file before committing. Do not include credentials or raw logs.\n"
                      "Do not merge, deploy, change board status or start additional coding agents.\n\n"
                      + issue.get('body', '') + '\n\nDiscussion:\n' + '\n\n'.join(comments))
            (directory/'task.txt').write_text(prompt)
            self.save(item_id, phase='submitting', prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
            try:
                session = self.cli.submit(task.repository, prompt, directory)
            except BaseException:
                self.save(item_id, phase='submission_unknown')
                raise
            self.save(item_id, phase='remote', session_id=session,
                      session_url='https://jules.google.com/session/' + session)
            return 'remote'
        if phase == 'remote':
            status = self.cli.status(state['session_id'], task.repository, directory)
            if status in {'failed', 'cancelled'}:
                self.save(item_id, phase='failed', remote_state=status)
                c.block(task, 'Jules remote session ' + status + '; retained work requires review')
                return status
            if status != 'completed':
                self.save(item_id, remote_state=status)
                return status
            patch = self.cli.pull(state['session_id'], directory)
            (directory/'result.patch').write_text(patch)
            self.save(item_id, phase='patch_ready', patch_sha256=hashlib.sha256(patch.encode()).hexdigest())
            phase = 'patch_ready'
        # Explicit Rework is a new Codex cycle, only after the old remote job is
        # known terminal. Its output is retained privately, never applied here.
        if item is None or not c.routed(item) or c.role(item) != 'working':
            self.save(item_id, phase='superseded')
            return 'superseded'
        _, item, task = c.find(item_id)
        state = c.state.get(item_id)['jules']
        if phase in {'patch_ready', 'applying'}:
            self.require_working(record)
            patch = directory/'result.patch'
            if hashlib.sha256(patch.read_bytes()).hexdigest() != state['patch_sha256']:
                raise Error('The retained Jules patch changed')
            if phase == 'patch_ready':
                if browser.git(path, 'status', '--porcelain'):
                    raise Error('Jules patch application requires its unchanged clean checkout')
                support.run(['git', 'apply', '--check', str(patch)], cwd=path)
                self.save(item_id, phase='applying', starting_head=browser.git(path, 'rev-parse', 'HEAD'))
                support.run(['git', 'apply', '--index', str(patch)], cwd=path)
            else:
                # A crash after report removal is recognized by the pre-recorded
                # source-only index tree, without reapplying a remote patch.
                if state.get('source_tree') == browser.git(path, 'write-tree'):
                    self.save(item_id, phase='applied', tree=state['source_tree'])
                    return 'applied'
                support.run(['git', 'apply', '--reverse', '--check', '--index', str(patch)], cwd=path)
            report_file = path/'.symphony-jules-report.json'
            if report_file.is_symlink() or not report_file.is_file():
                raise Error('Jules must provide the structured handoff report')
            from controller import report_valid
            report = json.loads(report_file.read_text())
            report_valid(report)
            if report['blocked']:
                raise Error('Jules reported an incomplete assignment; inspect its retained report')
            private_json(directory/'report.json', report)
            # Compute the source-only tree using an isolated index before the
            # real handoff entry is removed; this closes the recovery window.
            import os
            import shutil
            temporary_index = directory/'source.index'
            shutil.copyfile(path/'.git/index', temporary_index)
            import host_git
            argv, environment = host_git.prepare(['update-index', '--force-remove', '--', '.symphony-jules-report.json'], cwd=path)
            environment['GIT_INDEX_FILE'] = str(temporary_index)
            subprocess.run(argv, cwd=path, env=environment, check=True, capture_output=True)
            argv, _ = host_git.prepare(['write-tree'], cwd=path)
            source_tree = subprocess.check_output(argv, cwd=path, env=environment, text=True).strip()
            self.save(item_id, source_tree=source_tree)
            support.run(['git', 'rm', '--force', '--', '.symphony-jules-report.json'], cwd=path)
            self.save(item_id, phase='applied', tree=browser.git(path, 'write-tree'))
            phase = 'applied'
        if phase == 'applied':
            report = c.validate(item_id, path)
            if not report['checks']:
                raise Error('Configure independent checks before publishing Jules work')
            # Commit recovery uses the intended tree and parent, never a blind
            # repeat after an interrupted git commit.
            state = c.state.get(item_id)['jules']
            self.save(item_id, phase='committing')
            phase = 'committing'
        if phase == 'committing':
            self.require_working(record)
            state = c.state.get(item_id)['jules']
            head = browser.git(path, 'rev-parse', 'HEAD')
            if head == state['starting_head']:
                support.run(['git', '-c', 'user.name=Symphony', '-c', 'user.email=symphony@users.noreply.github.com',
                             'commit', '-m', f'Implement GH-{task.number}: {task.title}'], cwd=path)
            elif (browser.git(path, 'rev-parse', 'HEAD^{tree}') != state['tree']
                  or browser.git(path, 'rev-parse', 'HEAD^') != state['starting_head']):
                raise Error('Unexpected local changes during Jules commit recovery')
            sha = browser.git(path, 'rev-parse', 'HEAD')
            self.save(item_id, phase='pushing', sha=sha)
            phase = 'pushing'
        if phase == 'pushing':
            self.require_working(record)
            state = c.state.get(item_id)['jules']
            remote = c.gh.remote_head(task.repository, task.branch)
            if remote is None:
                support.run(['git', 'push', 'https://github.com/' + task.repository + '.git',
                             state['sha'] + ':refs/heads/' + task.branch], cwd=path, timeout=240)
            elif remote != state['sha']:
                raise Error('Jules branch changed remotely; refusing overwrite')
            if c.gh.remote_head(task.repository, task.branch) != state['sha']:
                raise Error('GitHub did not confirm the pushed Jules commit')
            self.save(item_id, phase='publishing')
            phase = 'publishing'
        if phase == 'publishing':
            self.require_working(record)
            state = c.state.get(item_id)['jules']
            prs = [pr for pr in c.gh.issue_prs(task) if pr['head']['ref'] == task.branch and pr['state'] == 'open']
            if len(prs) > 1:
                raise Error('Multiple PRs match the Jules cycle')
            if not prs:
                report = json.loads((directory/'report.json').read_text())
                body = (f"{MARKER}\n#### Context\n\nImplements {task.url}.\n\n#### TL;DR\n\n*Publish the independently validated Jules result for human review.*\n\n"
                        "#### Summary\n\n- Jules supplied the retained patch; the host independently validated and published it.\n\n"
                        "#### Alternatives\n\n- Preserve one worker per issue and the existing human review gates.\n\n"
                        "#### Test Plan\n\n" + '\n'.join(f"- {row['command']}: {row['result']}" for row in c.state.get(item_id)['validation']['checks']))
                # Intent precedes create. On ambiguous API response the next tick
                # discovers an existing exact-branch PR before creating again.
                c.gh.api(f'repos/{task.repository}/pulls', method='POST', payload={
                    'title': task.title, 'head': task.branch, 'base': c.base_branch, 'draft': True, 'body': body})
                return 'publishing'
            pr = prs[0]
            c.state.save(item_id, pr_number=pr['number'], pr_url=pr['html_url'], published_sha=state['sha'])
            self.save(item_id, phase='checks')
            phase = 'checks'
        if phase == 'checks':
            state = c.state.get(item_id)['jules']
            # Semantic application evidence and feedback are gates, not waived
            # because the code came from another provider.
            if browser.required(self.config, path, c.base_ref):
                raise Error('Jules changes need configured full-application browser evidence before review')
            c.require_checks(task, state['sha'])
            record = c.state.get(item_id)
            pr_number = record['pr_number']
            feedback = [*c.gh.pages(f'repos/{task.repository}/pulls/{pr_number}/comments'),
                        *c.gh.pages(f'repos/{task.repository}/pulls/{pr_number}/reviews'),
                        *c.gh.pages(f'repos/{task.repository}/issues/{pr_number}/comments')]
            if any(row.get('body', '').strip() and not row.get('body', '').startswith(MARKER) for row in feedback):
                raise Error('Jules PR feedback requires disposition before review handoff')
            c.validate(item_id, path)
            result = c.review(item_id, path, {'pr_number': pr_number, 'sha': state['sha']})
            self.save(item_id, phase='review')
            return result['status']
        return phase


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    while True:
        config = json.loads(args.config.read_text())
        worker = None
        try:
            # Distinct service lock prevents a second companion. Per-issue RPC
            # tools stay independent; Codex candidates exclude Jules ownership.
            with controller_lock(Path(config['state_directory'])/'jules-controller'):
                worker = JulesWorker(config)
                worker.tick()
        except (Error, OSError, ValueError, subprocess.SubprocessError) as error:
            # Detailed CLI output stays in private state. A transient read error
            # leaves the exact remote ownership and phase intact for next tick.
            private_json(Path(config['state_directory'])/'jules-worker-error.json',
                         {'at': now(), 'error': type(error).__name__, 'message': str(error)})
        finally:
            if worker:
                worker.close()
        if args.once:
            break
        time.sleep(config.get('polling_interval_seconds', 30))


if __name__ == '__main__':
    main()
