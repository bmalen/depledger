import contextlib
import io
import json
import os
import subprocess
import unittest
from unittest import mock

from helpers import LedgerTestCase

from depledger import cli, repo, store


class CliTest(LedgerTestCase):

    def run_cli(self, *argv, cwd=None, stdin=''):
        old_cwd, old_stdin = os.getcwd(), cli.sys.stdin
        out = io.StringIO()
        try:
            if cwd:
                os.chdir(cwd)
            cli.sys.stdin = io.StringIO(stdin)
            with contextlib.redirect_stdout(out), mock.patch.object(cli, 'refresh_pr_states', return_value={}):
                cli.main(list(argv))
        finally:
            os.chdir(old_cwd)
            cli.sys.stdin = old_stdin
        return out.getvalue()

    def test_a_note_lands_on_the_cwd_remote_repo_and_in_the_export(self):
        checkout = self.make_git_repo('git@github.com:acme/web.git')
        self.run_cli('repo', 'add', cwd=checkout)

        self.run_cli('note', 'add', 'Base branch is staging', '--always', cwd=checkout)

        web = store.find_repo(self.conn, 'acme/web')
        self.assertEqual([n['gist'] for n in store.list_notes(self.conn, web['id'])], ['Base branch is staging'])
        with open(os.path.join(self.tmp.name, 'export', 'notes.jsonl')) as f:
            self.assertIn('Base branch is staging', f.read())

    def test_writes_refuse_an_unregistered_repo_and_this_tool_repo(self):
        checkout = self.make_git_repo('https://github.com/acme/docs.git')

        with self.assertRaises(SystemExit):
            self.run_cli('note', 'add', 'x', '--always', cwd=checkout)
        with self.assertRaises(SystemExit):
            self.run_cli('note', 'add', 'x', '--always', cwd=store.ROOT)
        self.assertEqual(store.list_repos(self.conn), [])

    def test_a_note_must_say_when_it_matters(self):
        checkout = self.make_git_repo('git@github.com:acme/web.git')
        self.run_cli('repo', 'add', cwd=checkout)

        with self.assertRaises(SystemExit):
            self.run_cli('note', 'add', 'x', cwd=checkout)

    def test_review_save_reads_the_entry_from_stdin(self):
        self.run_cli('review', 'save', 'acme/web#1402', '--verdict', 'safe', '--tier', '1', stdin='- Tier 1, safe.\n')

        r = store.find_repo(self.conn, 'web')
        reviews = store.reviews_for_pr(self.conn, store.get_pr(self.conn, r['id'], 1402)['id'])
        self.assertEqual([(v['verdict'], v['tier'], v['body']) for v in reviews], [('safe', 1, '- Tier 1, safe.\n')])

    def test_backup_commits_only_the_export_and_skips_when_nothing_changed(self):
        subprocess.run(['git', 'init', '-q'], cwd=self.tmp.name, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=self.tmp.name, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=self.tmp.name, check=True)
        self.run_cli('review', 'save', 'acme/web#7', '--verdict', 'safe', '--tier', '0', stdin='- fine\n')

        first = self.run_cli('backup')
        second = self.run_cli('backup')

        committed = subprocess.run(['git', 'show', '--name-only', '--format=%s', 'HEAD'], cwd=self.tmp.name,
                                   capture_output=True, text=True).stdout.split()
        self.assertIn('no upstream branch', first)
        self.assertIn('export/reviews.jsonl', committed)
        self.assertNotIn('test.db', committed)
        self.assertIn('nothing to back up', second)

    def test_pr_states_refresh_asks_only_about_prs_not_merged_yet_in_one_query(self):
        with store.write(self.conn):
            web = store.ensure_repo(self.conn, 'acme/web')
            store.upsert_pr(self.conn, web, 7, state='MERGED')
            store.upsert_pr(self.conn, web, 8, state='OPEN')
            store.upsert_pr(self.conn, web, 9)
        reply = {'data': {'r0': {'p8': {'state': 'MERGED', 'mergedAt': '2026-10-19T03:04:05Z'}, 'p9': {'state': 'OPEN', 'mergedAt': None}}}}

        with mock.patch.object(cli.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(reply), '')) as run:
            changed = cli.refresh_pr_states(self.conn, [store.find_repo(self.conn, 'acme/web')])

        query = run.call_args.args[0][-1]
        self.assertEqual(run.call_count, 1)
        self.assertIn('p8: pullRequest(number: 8)', query)
        self.assertNotIn('number: 7', query)
        self.assertEqual(changed, {'acme/web': [(8, 'OPEN', 'MERGED'), (9, None, 'OPEN')]})
        self.assertEqual(store.get_pr(self.conn, web, 8)['merged_at'], '2026-10-19T03:04:05Z')

    def test_pr_states_refresh_makes_no_call_when_every_recorded_pr_is_merged(self):
        with store.write(self.conn):
            store.upsert_pr(self.conn, store.ensure_repo(self.conn, 'acme/web'), 7, state='MERGED')

        with mock.patch.object(cli.subprocess, 'run') as run:
            self.assertEqual(cli.refresh_pr_states(self.conn, [store.find_repo(self.conn, 'acme/web')]), {'acme/web': []})
        run.assert_not_called()

    def test_parse_remote_and_pr_refs(self):
        self.assertEqual(repo.parse_remote('git@github.com:acme/api.git'), 'acme/api')
        self.assertEqual(repo.parse_remote('https://github.com/acme/ui-kit'), 'acme/ui-kit')
        self.assertEqual(repo.parse_pr_ref('123'), (None, 123))
        self.assertEqual(repo.parse_pr_ref('web#1402'), ('web', 1402))
        self.assertEqual(repo.parse_pr_ref('https://github.com/acme/docs/pull/956'), ('acme/docs', 956))


if __name__ == '__main__':
    unittest.main()
