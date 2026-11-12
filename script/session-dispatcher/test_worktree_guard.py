"""Cross-server checks use disposable state and fake read-only API responses."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from dispatch import DispatchError
from worktree_guard import connections, inspect


class WorktreeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='worktree-guard-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / 'worktree'
        self.worktree.mkdir()
        self.services = self.root / 'services'
        self.services.mkdir()
        self.ledger = self.root / 'ledger.sqlite3'
        with sqlite3.connect(self.ledger) as db:
            db.execute('PRAGMA application_id=0x4F434450')
            db.execute('CREATE TABLE registry (name TEXT, snapshot TEXT)')
            db.execute('INSERT INTO registry VALUES (?,?)', ('task', json.dumps({'connection': '/fixture/dedicated.json'})))
            db.execute('CREATE TABLE pool (connection TEXT)')
            db.execute('INSERT INTO pool VALUES (?)', ('/fixture/other.json',))
        self.calls = []

    def factory(self, hosts):
        calls = self.calls

        class FakeClient:
            def __init__(self, connection):
                self.host = hosts[connection]
                self.connection = connection

            def json(self, method, path):
                calls.append((self.connection, method, path))
                if isinstance(self.host, Exception):
                    raise self.host
                if path == '/api/session/active':
                    return {'data': list(self.host)}
                sid = path.rsplit('/', 1)[1]
                return {'data': {'id': sid, 'location': {'directory': self.host[sid]}}}

        return FakeClient

    def test_regular_server_blocks_even_when_dedicated_server_is_idle(self):
        result = inspect([self.worktree], ['dedicated', 'regular'], client_factory=self.factory({
            'dedicated': {}, 'regular': {'ses_cpu': str(self.worktree)}}))
        self.assertFalse(result['all_sessions_idle'])
        self.assertEqual(result['worktrees'][0]['session_check'], 'blocked')
        self.assertEqual(result['worktrees'][0]['active'][0]['session_id'], 'ses_cpu')
        self.assertTrue(all(call[1] == 'GET' for call in self.calls))

    def test_nested_session_and_symlink_alias_block_the_real_worktree(self):
        nested = self.worktree / 'component'
        nested.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(nested, target_is_directory=True)
        result = inspect([self.worktree], ['regular'], client_factory=self.factory({
            'regular': {'ses_cpu': str(alias)}}))
        self.assertEqual(result['worktrees'][0]['session_check'], 'blocked')

    def test_similar_path_prefix_does_not_block_independent_worktree(self):
        other = self.root / 'worktree-other'
        other.mkdir()
        result = inspect([self.worktree], ['regular'], client_factory=self.factory({
            'regular': {'ses_other': str(other)}}))
        self.assertTrue(result['all_sessions_idle'])

    def test_unavailable_host_is_unknown_and_private_errors_are_not_reported(self):
        result = inspect([self.worktree], ['dedicated', 'regular'], client_factory=self.factory({
            'dedicated': {}, 'regular': DispatchError('SECRET PRIVATE URL')}))
        self.assertFalse(result['all_sessions_idle'])
        self.assertEqual(result['worktrees'][0]['session_check'], 'unknown')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_blocked_stays_blocked_when_another_host_cannot_be_observed(self):
        result = inspect([self.worktree], ['a', 'b'], client_factory=self.factory({
            'a': {'ses_cpu': str(self.worktree)}, 'b': OSError('offline')}))
        self.assertEqual(result['worktrees'][0]['session_check'], 'blocked')

    def test_unresolvable_session_placement_is_unknown_not_idle(self):
        result = inspect([self.worktree], ['regular'], client_factory=self.factory({
            'regular': {'ses_cpu': 'relative/path'}}))
        self.assertEqual(result['worktrees'][0]['session_check'], 'unknown')

    def test_discovery_includes_all_ledger_hosts_and_managed_channels_without_mutation(self):
        for name in ['service.json', 'service-local.json']:
            (self.services / name).write_text(json.dumps({'pid': os.getpid(), 'password': 'SECRET'}))
        before = hashlib.sha256(self.ledger.read_bytes()).hexdigest()
        files, managed = connections(self.ledger, self.services, ['/fixture/standalone.json'])
        self.assertEqual(len(files), 5)
        self.assertIn('/fixture/other.json', files)
        self.assertIn(str(self.services / 'service.json'), files)
        self.assertEqual(len(managed), 2)
        self.assertEqual(hashlib.sha256(self.ledger.read_bytes()).hexdigest(), before)
        self.assertEqual(len(list(self.services.iterdir())), 2)

    @unittest.skipUnless(Path('/proc/self').exists(), 'Linux managed-owner observation')
    def test_only_definitely_absent_managed_owner_is_excluded(self):
        path = self.services / 'service-old.json'
        path.write_text(json.dumps({'pid': 2147483647}))
        files, managed = connections(self.ledger, self.services)
        self.assertIn(str(path), files)
        result = inspect([self.worktree], [str(path)], managed=managed, client_factory=self.factory({
            str(path): DispatchError('API GET connection failed; outcome may be unknown')}))
        self.assertTrue(result['all_sessions_idle'])
        self.assertEqual(result['servers'][0]['state'], 'stale_owner_absent')
        path.write_text(json.dumps({'pid': os.getpid()}))
        result = inspect([self.worktree], [str(path)], managed=managed, client_factory=self.factory({
            str(path): DispatchError('API GET connection failed; outcome may be unknown')}))
        self.assertEqual(result['worktrees'][0]['session_check'], 'unknown')

    def test_reachable_managed_endpoint_is_checked_even_if_registration_owner_is_gone(self):
        path = self.services / 'service-old.json'
        path.write_text(json.dumps({'pid': 2147483647}))
        result = inspect([self.worktree], [str(path)], managed=[str(path)], client_factory=self.factory({
            str(path): {'ses_cpu': str(self.worktree)}}))
        self.assertEqual(result['worktrees'][0]['session_check'], 'blocked')

    def test_missing_managed_state_directory_does_not_silently_skip_discovery(self):
        with self.assertRaisesRegex(DispatchError, 'unavailable'):
            connections(self.ledger, self.root / 'absent')


if __name__ == '__main__':
    unittest.main()
