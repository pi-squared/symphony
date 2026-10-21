"""Container isolation and cleanup ownership, with no live daemon required."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

import docker_worker as d


class DockerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'GH-3'
        self.workspace.mkdir()
        self.auth = self.root / 'auth.json'
        self.auth.write_text('{"synthetic":true}')
        self.config = {'state_directory': str(self.root/'state'), 'python': '/host/python',
                       'docker': {'image': 'sha256:' + 'a'*64, 'auth_file': str(self.auth)}}

    def test_issue_keeps_tracked_catalog_bytes_but_disables_discovery(self):
        skills = self.workspace / '.codex/skills/custom'
        skills.mkdir(parents=True)
        (skills/'SKILL.md').write_text('untrusted custom skill')
        (self.workspace/'.codex/config.toml').touch()
        self.assertIn(str(skills), ' '.join(d.discovery_arguments(self.workspace)))
        self.assertIn('enabled=false', ' '.join(d.discovery_arguments(self.workspace)))
        self.assertTrue((skills/'SKILL.md').is_file())
        home = d.auth_home(self.config, self.workspace)
        self.assertEqual([p.name for p in home.iterdir()], ['auth.json'])
        args = d.command(self.config, self.workspace, ['codex'], home=home)
        self.assertIn('--read-only', args)
        self.assertIn('ALL', args)
        self.assertNotIn('/var/run/docker.sock', ' '.join(args))
        self.assertTrue(any(str(self.workspace)+',dst='+str(self.workspace) == a.split('src=')[-1] for a in args if 'src=' in a))

    def test_validation_has_no_auth_home_network_or_writable_git(self):
        (self.workspace/'.git').mkdir()
        args = d.command(self.config, self.workspace, ['python3', 'test.py'], validation=True)
        text = ' '.join(args)
        self.assertIn('--network none', text)
        self.assertIn(str(self.workspace/'.git')+',readonly', text)
        self.assertNotIn(str(self.auth), text)
        self.assertNotIn('CODEX_HOME', text)

    def test_auth_cache_refresh_is_preserved_and_custom_skill_rejected(self):
        home = d.auth_home(self.config, self.workspace)
        (home/'auth.json').write_text('refreshed')
        d.auth_home(self.config, self.workspace)
        self.assertEqual((home/'auth.json').read_text(), 'refreshed')
        (home/'skills/custom').mkdir(parents=True)
        with self.assertRaises(d.DockerError):
            d.auth_home(self.config, self.workspace)

    def test_foreign_container_is_never_removed(self):
        with patch.object(d, 'inspect', return_value={'Config': {'Labels': {'symphony.workspace': '/other'}}}), \
             patch.object(d.subprocess, 'run') as run:
            with self.assertRaises(d.DockerError):
                d.stop(self.config, 'foreign', self.workspace)
            run.assert_not_called()

    def test_restart_discards_only_automatic_bound_project_trust(self):
        import json
        home = d.auth_home(self.config, self.workspace)
        config = home/'config.toml'
        trust = '[projects.' + json.dumps(str(self.workspace)) + ']\ntrust_level="trusted"\n'
        config.write_text(trust)
        (home/'auth.json').write_text('refreshed')
        self.assertEqual(d.auth_home(self.config, self.workspace), home)
        self.assertFalse(config.exists())
        self.assertEqual((home/'auth.json').read_text(), 'refreshed')
        for custom in ('model="unexpected"\n' + trust, '[projects."/another"]\ntrust_level="trusted"\n'):
            config.write_text(custom)
            with self.assertRaises(d.DockerError):
                d.auth_home(self.config, self.workspace)
            self.assertTrue(config.exists())
        config.unlink()
        config.symlink_to(self.auth)
        with self.assertRaises(d.DockerError):
            d.auth_home(self.config, self.workspace)

    def test_unpinned_image_refused(self):
        self.config['docker']['image'] = 'latest'
        with self.assertRaises(d.DockerError):
            d.command(self.config, self.workspace, ['codex'], validation=True)

    def test_symlinked_configuration_parent_is_rejected(self):
        target = self.workspace/'nested'
        target.mkdir()
        for name in ('.codex', '.agents'):
            alias = self.workspace/name
            alias.symlink_to(target, target_is_directory=True)
            with self.assertRaises(d.DockerError):
                d.repository_skills(self.workspace)
            alias.unlink()
