import json
import unittest

from helpers import LedgerTestCase

from depledger import store


class PackageLookupTest(LedgerTestCase):

    def setUp(self):
        super().setUp()
        with store.write(self.conn):
            store.set_package(self.conn, 'npm', '@tiptap/*', analysed='3.30.1', status='TRAP', gist='prosemirror floor')
            store.set_package(self.conn, 'npm', 'react / react-dom', analysed='19.3.0', status='CHECK', gist='feature minor')
            store.set_package(self.conn, 'composer', 'laravel/framework', analysed='13.2.0', status='routine', gist='churn')
            store.set_package(self.conn, 'images', 'ghcr.io/acme/php-base', analysed='2026-09-21', status='CHECK', gist='rebuilds')

    def test_lookup_matches_exact_names_then_globs_then_the_last_segment(self):
        index = store.package_index(self.conn)

        self.assertEqual(store.lookup(index, 'react-dom', 'npm')['name'], 'react / react-dom')
        self.assertEqual(store.lookup(index, '@tiptap/extension-mention', 'npm')['name'], '@tiptap/*')
        self.assertEqual(store.lookup(index, 'php-base', 'images')['name'], 'ghcr.io/acme/php-base')
        self.assertIsNone(store.lookup(index, 'vue', 'npm'))

    def test_find_package_accepts_any_name_and_keeps_ecosystems_apart(self):
        self.assertEqual(store.find_package(self.conn, 'react')['name'], 'react / react-dom')
        self.assertIsNone(store.find_package(self.conn, 'laravel/framework', 'npm'))

    def test_setting_a_name_covered_by_a_glob_adds_its_own_row(self):
        with store.write(self.conn):
            store.set_package(self.conn, 'npm', '@tiptap/core', analysed='3.31.3', status='TRAP', gist='see tiptap')

        index = store.package_index(self.conn)
        self.assertEqual(store.lookup(index, '@tiptap/core', 'npm')['gist'], 'see tiptap')
        self.assertEqual(store.lookup(index, '@tiptap/react', 'npm')['gist'], 'prosemirror floor')

    def test_a_changed_row_keeps_its_old_version_in_revisions(self):
        with store.write(self.conn):
            store.set_package(self.conn, 'npm', 'react', analysed='19.4.0', gist='another minor')

        package = store.find_package(self.conn, 'react')
        history = store.revisions(self.conn, 'packages', package['id'])
        self.assertEqual(package['analysed'], '19.4.0')
        self.assertEqual(json.loads(history[0]['snapshot'])['analysed'], '19.3.0')

    def test_search_finds_package_gists_and_doc_bodies_by_substring(self):
        with store.write(self.conn):
            package = store.find_package(self.conn, '@tiptap/core')
            doc = store.ensure_package_doc(self.conn, package)
            store.put_doc(self.conn, doc['slug'], 'package', body='Two prosemirror-model copies break Enter.')

        kinds = sorted(h['kind'] for h in store.search(self.conn, 'prosemirror'))
        self.assertEqual(kinds, ['doc', 'package'])
        self.assertEqual(store.search(self.conn, 'model copies')[0]['kind'], 'doc')


class NotesTest(LedgerTestCase):

    def setUp(self):
        super().setUp()
        with store.write(self.conn):
            self.repo_id = store.ensure_repo(self.conn, 'acme/web')
            store.add_note(self.conn, self.repo_id, 'Base branch is staging')
            store.add_note(self.conn, self.repo_id, 'Seven pin sites', when_tags='docker-image', slug='docker', body='detail')
            store.add_note(self.conn, self.repo_id, 'Redis runs on phpredis', when_tags='predis/predis')
            store.add_note(self.conn, self.repo_id, 'Orphaned branches', when_tags='dirty, ahead')
            store.add_note(self.conn, self.repo_id, 'Editor probes', when_tags='@tiptap/*')

    def notes(self, **ctx):
        always, matched, hidden = store.notes_for(self.conn, self.repo_id, ctx)
        return [n['gist'] for n in always], sorted(n['gist'] for n in matched)

    def test_untagged_notes_print_every_run_and_tagged_ones_only_when_they_match(self):
        always, matched = self.notes(buckets=['docker-image'], names=['redis'], text='chore(deps): update docker images')

        self.assertEqual(always, ['Base branch is staging'])
        self.assertEqual(matched, ['Seven pin sites'])

    def test_tags_match_package_globs_and_signals(self):
        _, matched = self.notes(buckets=['frontend'], names=['@tiptap/core', 'predis/predis'], text='update all dirty')

        self.assertEqual(matched, ['Editor probes', 'Orphaned branches', 'Redis runs on phpredis'])

    def test_parentheticals_do_not_split_a_tag(self):
        self.assertEqual(store.split_tags('laravel/framework (SessionGuard, Auth), docker-image'),
                         ['laravel/framework', 'docker-image'])


class ReviewsTest(LedgerTestCase):

    def triage(self, repo, number, name, old, new, head='aaa111'):
        return store.record_triage(self.conn, repo, number, {'title': 'update %s' % name, 'head_sha': head},
                                   [{'kind': 'manifest', 'ecosystem': 'npm', 'name': name, 'from': old, 'to': new, 'type': 'patch'}])

    def test_bump_history_spans_repos_and_shows_the_verdict(self):
        self.triage('acme/web', 1366, '@tiptap/core', '3.31.2', '3.31.3')
        self.triage('acme/api', 801, '@tiptap/react', '3.31.2', '3.31.3')
        web = store.find_repo(self.conn, 'web')
        with store.write(self.conn):
            store.save_review(self.conn, web['id'], [1366], verdict='safe', tier=2, body='clean lock')

        rows = store.bump_history(self.conn, ['@tiptap/*'], exclude_repo_id=store.find_repo(self.conn, 'api')['id'])

        self.assertEqual([(r['repo'], r['number'], r['verdict']) for r in rows], [('acme/web', 1366, 'safe')])

    def test_saving_again_at_the_same_head_replaces_and_a_new_head_adds(self):
        self.triage('acme/api', 722, 'motion', '12.0.0', '13.0.0', head='aaa111')
        repo_id = store.find_repo(self.conn, 'api')['id']
        with store.write(self.conn):
            first, _ = store.save_review(self.conn, repo_id, [722], verdict='not_safe', tier=2, body='breaks the build')
            again, replaced = store.save_review(self.conn, repo_id, [722], verdict='not_safe', tier=2, body='breaks the build, fixed wording')
        self.triage('acme/api', 722, 'motion', '12.0.0', '13.0.0', head='bbb222')
        with store.write(self.conn):
            second, _ = store.save_review(self.conn, repo_id, [722], verdict='safe', tier=2, body='fixed at the new head')

        reviews = store.reviews_for_pr(self.conn, store.get_pr(self.conn, repo_id, 722)['id'])
        self.assertEqual(first, again)
        self.assertTrue(replaced)
        self.assertEqual([(r['head_sha'], r['verdict']) for r in reviews], [('aaa111', 'not_safe'), ('bbb222', 'safe')])

    def test_retriage_replaces_bumps_and_keeps_the_review(self):
        self.triage('acme/api', 803, 'vite', '8.3.3', '8.3.4')
        repo_id = store.find_repo(self.conn, 'api')['id']
        with store.write(self.conn):
            store.save_review(self.conn, repo_id, [803], verdict='safe', tier=1, body='ok')
        self.triage('acme/api', 803, 'vite', '8.3.3', '8.3.5')

        pr = store.get_pr(self.conn, repo_id, 803)
        self.assertEqual([b['to_version'] for b in store.pr_bumps(self.conn, pr['id'])], ['8.3.5'])
        self.assertEqual(len(store.reviews_for_pr(self.conn, pr['id'])), 1)


class ArtifactsTest(LedgerTestCase):

    def test_digests_and_action_tags_are_recorded_once(self):
        with store.write(self.conn):
            _, first = store.add_image(self.conn, 'redis', 'alpine', 'ba6e394f6acc', {'version': '8.10.2'})
            _, second = store.add_image(self.conn, 'redis', 'alpine', 'sha256:ba6e394f6acc')
            _, tag_first = store.add_action(self.conn, 'actions/checkout', 'v6.1.0', 'd23441a48e516b6c34aea4fa41551a30e30af803', 'commit')
            _, tag_second = store.add_action(self.conn, 'actions/checkout', 'v6', 'd23441a48e516b6c34aea4fa41551a30e30af803', 'commit')

        self.assertEqual((first, second, tag_first, tag_second), (True, False, True, True))
        self.assertEqual([a['tag'] for a in store.find_action(self.conn, 'd23441a48e51')], ['v6.1.0', 'v6'])


if __name__ == '__main__':
    unittest.main()
