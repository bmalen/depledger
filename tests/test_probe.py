import contextlib
import io
import json
import os
from unittest import mock

from helpers import LedgerTestCase

from depledger import cli, probe, store

OLD = 'ghcr.io/acme/php:php8.4@sha256:' + 'a' * 64
NEW = 'ghcr.io/acme/php:php8.4@sha256:' + 'b' * 64

CADDY_OLD = '''os=Debian GNU/Linux 13 (trixie)
php=PHP 8.4.26 (cli) (built: Sep 24 2026 19:12:20) (NTS)
bun=1.4.2
composer=Composer version 2.10.2 2026-07-01 11:24:45
caddy=v2.11.4 h1:XKxkMTgNSizEvKG6QHue6cAsFOteU2qA61w2tKkCWi0=
php_modules_md5=c479f6f7d3e4125cc1482c9633e970bf
php_modules=Core ctype curl mailparse
dpkg_count=2
dpkg_md5=5d71c7c7c8f0a3e37f38dfd41c4d36cb
---PACKAGES---
curl 8.14.1-2+deb13u5
openssl 3.5.7-1~deb13u2
'''


def dump(text):
    raw, packages = probe.split_header(text)
    return {'raw': raw, 'packages': packages, 'platform': 'linux/amd64', 'dump': '/tmp/x.txt'}


class NormaliseTest(LedgerTestCase):

    def test_keys_match_the_records_banked_by_hand(self):
        facts = probe.normalise(dump(CADDY_OLD)['raw'])

        self.assertEqual(facts, {
            'os': 'Debian GNU/Linux 13 (trixie)', 'php': '8.4.26', 'bun': '1.4.2', 'composer': '2.10.2',
            'caddy': '2.11.4', 'built': '2026-09-24', 'modules md5': 'c479f6f7d3e4125cc1482c9633e970bf',
            'dpkg md5': '5d71c7c7c8f0a3e37f38dfd41c4d36cb', 'dpkg': '2'})

    def test_banner_versions_extensions_and_a_client_matching_the_server(self):
        raw = {'gotenberg': 'Version: 8.38.1', 'mailpit': '/mailpit v1.31.6 compiled with go1.27.2 on linux/amd64',
               'postgres': 'postgres (PostgreSQL) 17.11 (Debian 17.11-1.pgdg13+2)', 'psql': 'psql (PostgreSQL) 17.11 (Debian)',
               'pg_ext_timescaledb': '2.30.2', 'pg_ext_vector': '0.8.7', 'rpm_md5': 'f' * 32, 'rpm_count': '117'}

        facts = probe.normalise(raw)

        self.assertEqual(facts, {'postgres': '17.11', 'mailpit': '1.31.6', 'gotenberg': '8.38.1', 'timescaledb': '2.30.2',
                                 'pgvector': '0.8.7', 'rpm md5': 'f' * 32, 'rpm': '117'})


class CompareTest(LedgerTestCase):

    def test_a_security_refresh_is_a_package_refresh_not_a_version_move(self):
        new = CADDY_OLD.replace('Sep 24', 'Oct  6').replace('deb13u5', 'deb13u7')

        c = probe.compare(dump(CADDY_OLD), dump(new))

        self.assertEqual(c['kind'], 'PACKAGE REFRESH')
        self.assertEqual(c['changed'], [('curl', '8.14.1-2+deb13u5', '8.14.1-2+deb13u7')])
        self.assertEqual(probe.auto_notes('a' * 64, c), 'vs aaaaaaaaaaaa: PACKAGE REFRESH; 1 packages: curl')

    def test_a_runtime_move_and_a_dropped_module_are_named(self):
        new = CADDY_OLD.replace('bun=1.4.2', 'bun=1.4.3').replace(' mailparse', '')

        c = probe.compare(dump(CADDY_OLD), dump(new))

        self.assertEqual(c['kind'], 'VERSION MOVE')
        self.assertEqual(c['moved'], [('bun', '1.4.2', '1.4.3')])
        self.assertEqual(c['modules_removed'], ['mailparse'])

    def test_only_a_new_build_timestamp_is_a_rebuild(self):
        self.assertEqual(probe.compare(dump(CADDY_OLD), dump(CADDY_OLD.replace('Sep 24', 'Oct  6')))['kind'], 'REBUILD ONLY')

    def test_apk_lines_split_into_name_and_version(self):
        self.assertEqual(probe.package_map(['apk-tools-3.0.8-r0', 'tzdata-2026d-r0']), {'apk-tools': '3.0.8-r0', 'tzdata': '2026d-r0'})


class ProbePairsTest(LedgerTestCase):

    def run_pairs(self, pairs, results, picked='linux/amd64', **kw):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'DEPLEDGER_PROBE_DIR': self.tmp.name}), \
                mock.patch.object(probe, 'pick_platform', return_value=picked) as self.pick, \
                mock.patch.object(probe, 'run_probe', side_effect=lambda ref, *a: dict(results[ref], ref=ref)) as run, \
                contextlib.redirect_stdout(out):
            failures = probe.probe_pairs(self.conn, pairs, **kw)
        return out.getvalue(), failures, run

    def test_banks_both_sides_with_the_delta_on_the_new_record(self):
        new_dump = CADDY_OLD.replace('deb13u5', 'deb13u7')

        out, failures, _ = self.run_pairs([('ghcr.io/acme/php:php8.4', OLD, NEW)], {OLD: dump(CADDY_OLD), NEW: dump(new_dump)})

        self.assertEqual(failures, 0)
        self.assertIn('PACKAGE REFRESH', out)
        new_row = store.find_digest(self.conn, 'b' * 12)[0]
        self.assertEqual((new_row['image'], new_row['tag']), ('ghcr.io/acme/php', 'php8.4'))
        self.assertEqual(json.loads(new_row['facts'])['php'], '8.4.26')
        self.assertIn('vs aaaaaaaaaaaa: PACKAGE REFRESH', new_row['notes'])
        self.assertEqual(store.find_digest(self.conn, 'a' * 12)[0]['notes'], '')

    def test_a_new_digest_already_on_record_is_not_probed(self):
        with store.write(self.conn):
            store.add_image(self.conn, 'ghcr.io/acme/php', 'php8.4', 'b' * 64, {'php': '8.4.26'})

        out, _, run = self.run_pairs([('ghcr.io/acme/php:php8.4', OLD, NEW)], {})

        run.assert_not_called()
        self.assertIn('pass (ledger): php=8.4.26', out)

    def test_print_only_probes_a_digest_on_record_and_writes_nothing(self):
        with store.write(self.conn):
            store.add_image(self.conn, 'ghcr.io/acme/php', 'php8.4', 'b' * 64, {'php': '8.4.26'})

        out, _, run = self.run_pairs([('ghcr.io/acme/php:php8.4', OLD, NEW)], {OLD: dump(CADDY_OLD), NEW: dump(CADDY_OLD)},
                                     do_bank=False)

        self.assertEqual(run.call_count, 2)
        self.assertIn('IDENTICAL', out)
        self.assertEqual(store.find_digest(self.conn, 'a' * 12), [])

    def test_a_missing_docker_is_a_failed_probe_not_a_crash(self):
        with mock.patch.dict(os.environ, {'DEPLEDGER_PROBE_DIR': self.tmp.name}), \
                mock.patch.object(probe.subprocess, 'run', side_effect=FileNotFoundError(2, 'No such file or directory', 'docker')):
            result = probe.run_probe(NEW, platform='linux/amd64')

        self.assertIn('docker did not start', result['error'])

    def test_a_failed_probe_is_reported_and_nothing_is_banked(self):
        out, failures, _ = self.run_pairs([('x', OLD, NEW)], {OLD: dump(CADDY_OLD), NEW: {'error': 'exit 1 (platform linux/amd64): no stderr'}})

        self.assertEqual(failures, 1)
        self.assertIn('PROBE FAILED %s: exit 1' % NEW, out)
        self.assertEqual(store.find_digest(self.conn, 'b' * 12), [])

    def test_both_sides_run_on_the_platform_triage_read_without_asking_the_registry(self):
        _, failures, run = self.run_pairs([('ghcr.io/acme/php:php8.4', OLD, NEW, 'linux/arm64')], {OLD: dump(CADDY_OLD), NEW: dump(CADDY_OLD)})

        self.assertEqual(failures, 0)
        self.assertEqual(sorted(c.args for c in run.call_args_list), [(OLD, 'linux/arm64', False), (NEW, 'linux/arm64', False)])
        self.pick.assert_not_called()

    def test_an_unreadable_manifest_fails_the_pair_instead_of_probing_the_host_platform(self):
        out, failures, run = self.run_pairs([('ghcr.io/acme/php:php8.4', OLD, NEW)], {}, picked=None)

        self.assertEqual(failures, 1)
        run.assert_not_called()
        self.assertIn('could not read the manifest to choose a platform', out)

    def test_a_cached_dump_answers_without_docker_or_the_registry(self):
        with open(os.path.join(self.tmp.name, '%s_linux-amd64.txt' % ('b' * 64)), 'w') as f:
            f.write(CADDY_OLD)

        with mock.patch.dict(os.environ, {'DEPLEDGER_PROBE_DIR': self.tmp.name}), \
                mock.patch.object(probe.subprocess, 'run', side_effect=AssertionError('no subprocess for a cached digest')):
            result = probe.run_probe(NEW)

        self.assertEqual(result['platform'], 'linux/amd64')
        self.assertEqual(probe.normalise(result['raw'])['php'], '8.4.26')

    def test_triage_pairs_carry_full_refs_and_an_untagged_image(self):
        images = {'redis': {'name': 'redis', '-': {'tag': '', 'digest': 'a' * 64}, '+': {'tag': '', 'digest': 'b' * 64}},
                  'valkey/valkey:9.2-alpine': {'name': 'valkey/valkey', '-': {'tag': '9.1-alpine', 'digest': 'c' * 64},
                                               '+': {'tag': '9.2-alpine', 'digest': 'd' * 64}}}

        self.assertEqual(probe.pairs_from_images(images), [
            {'label': 'redis', 'old': 'redis@sha256:' + 'a' * 64, 'new': 'redis@sha256:' + 'b' * 64},
            {'label': 'valkey/valkey:9.2-alpine', 'old': 'valkey/valkey:9.1-alpine@sha256:' + 'c' * 64,
             'new': 'valkey/valkey:9.2-alpine@sha256:' + 'd' * 64}])

    def test_triage_pairs_carry_the_platform_it_read(self):
        images = {'redis': {'name': 'redis', '-': {'tag': '', 'digest': 'a' * 64}, '+': {'tag': '', 'digest': 'b' * 64}},
                  'timescale/timescaledb-ha:pg17': {'name': 'timescale/timescaledb-ha', '+': {'tag': 'pg17', 'digest': 'c' * 64}}}

        pairs = probe.pairs_from_images(images, {'redis': 'linux/amd64'})

        self.assertEqual([(p['label'], p.get('platform')) for p in pairs], [('redis', 'linux/amd64'), ('timescale/timescaledb-ha:pg17', None)])


class ImageEditTest(LedgerTestCase):

    def run_cli(self, *argv, stdin=''):
        old_stdin = cli.sys.stdin
        out = io.StringIO()
        try:
            cli.sys.stdin = stdin if not isinstance(stdin, str) else io.StringIO(stdin)
            with contextlib.redirect_stdout(out):
                cli.main(list(argv))
        finally:
            cli.sys.stdin = old_stdin
        return out.getvalue()

    def test_edit_merges_facts_appends_notes_and_keeps_the_old_row(self):
        self.run_cli('image', 'add', 'axllent/mailpit:latest@sha256:' + 'e' * 64, '--fact', 'version=', '--notes', 'binary unchanged')

        self.run_cli('image', 'edit', 'e' * 12, '--fact', 'mailpit=1.31.6', '--drop-fact', 'version',
                     '--notes', 'binary v1.31.3 -> v1.31.6')

        row = store.find_digest(self.conn, 'e' * 12)[0]
        self.assertEqual(json.loads(row['facts']), {'mailpit': '1.31.6'})
        self.assertEqual(row['notes'], 'binary v1.31.3 -> v1.31.6')
        old = json.loads(store.revisions(self.conn, 'image_digests', row['id'])[0]['snapshot'])
        self.assertEqual(old['notes'], 'binary unchanged')

    def test_adding_a_digest_on_record_points_at_edit(self):
        self.run_cli('image', 'add', 'redis@sha256:' + 'f' * 64)

        self.assertIn('image edit', self.run_cli('image', 'add', 'redis@sha256:' + 'f' * 64))


class NoteAddStdinTest(ImageEditTest):

    def test_a_note_without_slug_never_reads_stdin(self):
        checkout = self.make_git_repo('git@github.com:acme/web.git')
        old_cwd = os.getcwd()
        os.chdir(checkout)
        try:
            self.run_cli('repo', 'add')
            blocking = mock.Mock(isatty=lambda: False, read=mock.Mock(side_effect=AssertionError('read stdin')))
            self.run_cli('note', 'add', 'Gotenberg is mocked in tests', '--when', 'gotenberg/gotenberg', stdin=blocking)
        finally:
            os.chdir(old_cwd)

        web = store.find_repo(self.conn, 'acme/web')
        self.assertEqual([n['gist'] for n in store.list_notes(self.conn, web['id'])], ['Gotenberg is mocked in tests'])
