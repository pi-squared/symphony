"""Request remote Jules PRs through GitHub's native issue-label integration.

The independent receipt prevents repeated dispatch. No checkout, patch, remote
session polling, local validation or PR publication belongs to this process.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import time

from controller import Controller, Error, controller_lock, now, private_json

NATIVE_LABEL = 'jules'


class JulesDispatcher:
    def __init__(self, config, gh=None):
        self.config = config
        self.controller = Controller(config, gh)
        self.db = self.controller.state.db
        self.db.execute('CREATE TABLE IF NOT EXISTS jules_requests '
                        '(issue TEXT PRIMARY KEY, record TEXT NOT NULL)')
        # Preserve old managed attempts, including archived cycles. An upgrade
        # must not silently start another remote job or import an old patch.
        for (raw,) in self.db.execute('SELECT record FROM issues UNION SELECT record FROM cycles').fetchall():
            record = json.loads(raw)
            task = record.get('task', {})
            if record.get('backend') == 'jules' and task.get('issue_id'):
                self.db.execute('INSERT OR IGNORE INTO jules_requests VALUES (?,?)',
                                (task['issue_id'], json.dumps({
                                    'status': 'legacy', 'issue_url': task.get('url'),
                                    'session_url': record.get('jules', {}).get('session_url'),
                                    'pr_url': record.get('pr_url'), 'recorded_at': now()})))
        self.db.commit()

    def close(self):
        self.db.close()

    def receipt(self, issue_id):
        row = self.db.execute('SELECT record FROM jules_requests WHERE issue=?', (issue_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, issue_id, record):
        self.db.execute('UPDATE jules_requests SET record=? WHERE issue=?', (json.dumps(record), issue_id))
        self.db.commit()

    def tick(self):
        if not self.config['enabled'] or not self.config.get('jules', {}).get('enabled'):
            return {'status': 'paused'}
        c = self.controller
        label = self.config['jules'].get('label', 'runner:jules').strip().lower()
        _, items = c.snapshot()
        candidates = [item for item in items if c.routed(item)
                      and c.role(item) in {'queued', 'working', 'review', 'rework'}]
        candidates.sort(key=lambda item: (
            c.task(item).order if c.task(item).order is not None else float('inf'),
            {'P0': 0, 'P1': 1, 'P2': 2}.get(c.task(item).priority, 3), c.task(item).number))
        for item in candidates:
            task = c.task(item)
            if self.receipt(task.issue_id):
                continue
            # A board page can lag a human status/membership edit.
            _, item, task = c.find(item['id'])
            if c.role(item) not in {'queued', 'working', 'review', 'rework'}:
                continue
            # Recheck live issue labels/state, assignee and native blockers.
            current = c.normalized(item, True)
            if not current['dispatchable'] or label not in current['labels']:
                continue
            record = {'status': 'requesting', 'issue_url': task.url, 'repository': task.repository,
                      'number': task.number, 'requested_at': now(), 'native_label': NATIVE_LABEL}
            inserted = self.db.execute('INSERT OR IGNORE INTO jules_requests VALUES (?,?)',
                                       (task.issue_id, json.dumps(record))).rowcount
            self.db.commit()  # Intent is durable before the only remote mutation.
            if not inserted:
                continue
            try:
                if NATIVE_LABEL in current['labels']:
                    record['delivery'] = 'existing_label'
                else:
                    labels = c.gh.api(f'repos/{task.repository}/issues/{task.number}/labels',
                                      method='POST', payload={'labels': [NATIVE_LABEL]})
                    if not isinstance(labels, list) or not any(
                            isinstance(row, dict) and row.get('name', '').lower() == NATIVE_LABEL for row in labels):
                        raise Error('GitHub did not confirm the native Jules label; inspect the issue before retrying')
                    record['delivery'] = 'added_label'
            except Exception:
                self.save(task.issue_id, {**record, 'status': 'submission_unknown'})
                raise
            record.update(status='requested', confirmed_at=now())
            self.save(task.issue_id, record)
            # A label receipt confirms the request, not remote task completion.
            # Jules itself posts session/PR links on the original GitHub issue.
            return record
        return {'status': 'idle'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    while True:
        config = json.loads(args.config.read_text())
        dispatcher = None
        try:
            with controller_lock(Path(config['state_directory'])/'jules-controller'):
                dispatcher = JulesDispatcher(config)
                result = dispatcher.tick()
                if args.once:
                    print(json.dumps(result))
        except (Error, OSError, ValueError, KeyError, sqlite3.Error) as error:
            private_json(Path(config['state_directory'])/'jules-dispatcher-error.json',
                         {'error': str(error), 'at': now()})
            if args.once:
                raise
        finally:
            if dispatcher:
                dispatcher.close()
        if args.once:
            return
        time.sleep(config.get('poll_interval', 30))


if __name__ == '__main__':
    main()
