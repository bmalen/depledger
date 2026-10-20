import contextlib
import importlib
import io
import json
import os
import pkgutil
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

import helpers  # noqa: F401

import depledger
from depledger import checks, ci
from depledger.parse import coverage, coverage_note

SYNAPSE_LINT = "bun eslint 'resources/**/*.{ts,tsx,js,jsx,json,mjs}' --fix && bun stylelint 'resources/**/*.css' --fix"


def env(top, **kw):
    e = {'top': top, 'pm': 'bun', 'lock': 'bun.lock', 'scripts': {}, 'uses_sail': True, 'sail_up': True,
         'has_composer': True, 'has_artisan': True}
    e.update(kw)
    return e


class ImportTest(unittest.TestCase):

    def test_every_module_imports(self):
        for m in pkgutil.iter_modules(depledger.__path__):
            importlib.import_module('depledger.' + m.name)


class PlanTest(unittest.TestCase):

    def test_a_bun_sail_laravel_repo_gets_its_own_scripts_without_fix(self):
        scripts = {'build': 'vite build', 'typecheck': 'tsc --noEmit', 'lint': SYNAPSE_LINT}

        p = checks.plan(env('/r', scripts=scripts), ['install-js(frozen)', 'build', 'typecheck', 'lint(no --fix)', 'chrome'])

        cmds = {s['name']: s['cmd'] for s in p['steps']}
        self.assertEqual(cmds['install-js'], 'bun install --frozen-lockfile')
        self.assertEqual(cmds['lint:eslint'], "bun eslint 'resources/**/*.{ts,tsx,js,jsx,json,mjs}' --format json")
        self.assertEqual(cmds['lint:stylelint'], "bun stylelint 'resources/**/*.css'")
        self.assertEqual(cmds['typecheck'], 'bun run typecheck')
        self.assertEqual([s['name'] for s in p['steps'] if s['selected']], ['install-js', 'build', 'typecheck', 'lint:eslint', 'lint:stylelint'])
        self.assertEqual(p['manual'], ['chrome'])

    def test_an_npm_vue_repo_runs_bare_bins_through_npx_and_follows_script_calls(self):
        scripts = {'lint': 'npm run lint:js && npm run format', 'lint:js': 'eslint . --fix --format stylish',
                   'format': 'prettier --write src/', 'type-check': 'vue-tsc --build --force'}

        p = checks.plan(env('/r', pm='npm', lock='package-lock.json', scripts=scripts, uses_sail=False, has_composer=False), [])

        cmds = {s['name']: s['cmd'] for s in p['steps']}
        self.assertEqual(cmds['install-js'], 'npm ci')
        self.assertEqual(cmds['lint:eslint'], 'npx --no-install eslint . --format json')
        self.assertEqual(cmds['lint:prettier'], 'npx --no-install prettier --check src/')
        self.assertEqual(cmds['typecheck'], 'npm run type-check')
        self.assertNotIn('tests', cmds)

    def test_pnpm_and_both_yarn_lock_formats(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'yarn.lock'), 'w') as f:
                f.write('__metadata:\n  version: 8\n')
            self.assertEqual(checks.install_js('yarn', os.path.join(d, 'yarn.lock')), 'yarn install --immutable')
            with open(os.path.join(d, 'yarn.lock'), 'w') as f:
                f.write('# yarn lockfile v1\n')
            self.assertEqual(checks.install_js('yarn', os.path.join(d, 'yarn.lock')), 'yarn install --frozen-lockfile')
        self.assertEqual(checks.install_js('pnpm', 'x'), 'pnpm install --frozen-lockfile')

    def test_a_phpunit_move_adds_a_parallel_run(self):
        p = checks.plan(env('/r', pm='none', lock=None), ['tests'], moved=['phpunit/phpunit'])

        self.assertEqual([(s['name'], s['cmd']) for s in p['steps'] if s['kind'] == 'tests'],
                         [('tests', 'php artisan test --compact'), ('tests-parallel', 'php artisan test --compact --parallel')])


class LibraryAndVueCliTest(unittest.TestCase):

    def test_a_php_library_uses_its_own_test_script_or_pest(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'composer.json'), 'w') as f:
                json.dump({'require-dev': {'pestphp/pest': '^3'}}, f)
            self.assertEqual(checks.php_tests(d), 'vendor/bin/pest')
            with open(os.path.join(d, 'composer.json'), 'w') as f:
                json.dump({'scripts': {'test': 'phpunit'}}, f)
            self.assertEqual(checks.php_tests(d), 'composer test')

    def test_vue_cli_lint_is_told_not_to_fix(self):
        self.assertEqual(checks.no_fix('vue-cli-service lint', 'npm')['cmd'], 'npx --no-install vue-cli-service lint --no-fix')


class JudgeTest(unittest.TestCase):

    def test_eslint_counts_files_and_fails_on_errors_or_an_empty_glob(self):
        clean = json.dumps([{'filePath': '/a/x.ts', 'errorCount': 0, 'warningCount': 1, 'messages': []}])
        dirty = json.dumps([{'filePath': '/a/resources/x.ts', 'errorCount': 1, 'warningCount': 0,
                             'messages': [{'severity': 2, 'line': 4, 'ruleId': 'no-undef'}]}])
        step = {'kind': 'eslint'}

        self.assertEqual(checks.judge(step, 0, clean, '', []), (True, '1 files, 0 errors, 1 warnings'))
        self.assertEqual(checks.judge(step, 1, dirty, '', []), (False, '1 files, 1 errors, 0 warnings: a/resources/x.ts:4 no-undef'))
        self.assertFalse(checks.judge(step, 0, '[]', '', [])[0])

    def test_a_rewritten_lock_and_a_stale_composer_lock_fail(self):
        self.assertEqual(checks.judge({'kind': 'install-js'}, 0, 'done', '', ['bun.lock']),
                         (False, 'install rewrote bun.lock: the lock is not in sync with package.json'))
        self.assertFalse(checks.judge({'kind': 'install-php'}, 0, '', 'Warning: The lock file is not up to date with the latest changes', [])[0])

    def test_summaries_name_the_line_that_matters(self):
        self.assertEqual(checks.judge({'kind': 'install-js'}, 0, 'Checked 643 installs across 718 packages (no changes) [905ms]\n', '$ is-ci || husky', []),
                         (True, 'lock untouched; Checked 643 installs across 718 packages (no changes) [905ms]'))
        self.assertEqual(checks.judge({'kind': 'install-php'}, 0, '', 'Nothing to install, update or remove\nUse the `composer fund` command', []),
                         (True, 'composer.lock in sync; Nothing to install, update or remove'))
        self.assertEqual(checks.judge({'kind': 'generic', 'name': 'build'}, 0, 'x\n\u2713 built in 41.2s\ny\n\u2713 built in 5.61s\n', '$ vite build', []),
                         (True, '2 bundles built (41.2s, 5.61s)'))
        self.assertEqual(checks.judge({'kind': 'generic', 'name': 'lint:stylelint'}, 0, '', '', []), (True, 'clean'))

    def test_tests_report_the_pest_summary(self):
        self.assertEqual(checks.judge({'kind': 'tests'}, 0, '\x1b[32m  Tests:    812 passed (2301 assertions)\n', '', []),
                         (True, '812 passed (2301 assertions)'))


class RunPlanTest(unittest.TestCase):

    def git(self, *args):
        subprocess.run(['git'] + list(args), cwd=self.top, check=True, capture_output=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.top = self.tmp.name
        self.logs = tempfile.TemporaryDirectory()
        self.git('init', '-q')
        with open(os.path.join(self.top, 'bun.lock'), 'w') as f:
            f.write('{}')
        self.git('add', '.')
        self.git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'x')
        self.head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.top, capture_output=True, text=True).stdout.strip()

    def tearDown(self):
        self.tmp.cleanup()
        self.logs.cleanup()

    def run_plan(self, p, executor, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            failures = checks.run_plan(p, self.logs.name, executor=executor, **kw)
        return failures, out.getvalue()

    def make_plan(self, head=None):
        p = checks.plan(env(self.top, uses_sail=False, has_composer=False, scripts={'build': 'vite build', 'lint': 'eslint src --fix'}),
                        ['install-js(frozen)', 'build', 'lint(no --fix)'])
        return dict(p, repo='a/b', number=1, head_sha=head or self.head, head_ref='renovate/x')

    def test_runs_installs_first_and_reports_each_step(self):
        order = []

        def fake(cmd):
            order.append(cmd)
            stdout = json.dumps([{'filePath': '/x.ts', 'errorCount': 0, 'warningCount': 0, 'messages': []}]) if 'eslint' in cmd else 'ok'
            return SimpleNamespace(returncode=0, stdout=stdout, stderr='')

        failures, out = self.run_plan(self.make_plan(), fake)

        self.assertEqual(failures, 0)
        self.assertEqual(order[0], 'bun install --frozen-lockfile')
        self.assertIn('PASS lint:eslint', out)
        self.assertIn('1 files, 0 errors, 0 warnings', out)
        self.assertIn('RESULT=pass', out)

    def test_an_install_that_rewrites_the_lock_fails(self):
        def fake(cmd):
            if cmd.startswith('bun install'):
                with open(os.path.join(self.top, 'bun.lock'), 'w') as f:
                    f.write('{"changed": true}')
            return SimpleNamespace(returncode=0, stdout='[{"filePath": "/x", "messages": []}]', stderr='')

        failures, out = self.run_plan(self.make_plan(), fake, only=['install'])

        self.assertEqual(failures, 1)
        self.assertIn('FAIL install-js', out)
        self.assertIn('install rewrote bun.lock', out)

    def test_test_runs_never_overlap(self):
        p = checks.plan(env(self.top, uses_sail=False), ['tests'], moved=['phpunit/phpunit'])
        p = dict(p, repo='a/b', number=1, head_sha=self.head, head_ref='renovate/x')
        running, overlapped, lock = [], [], threading.Lock()

        def fake(cmd):
            with lock:
                running.append(cmd)
                overlapped.append(len(running) > 1)
            time.sleep(0.05)
            with lock:
                running.remove(cmd)
            return SimpleNamespace(returncode=0, stdout='Tests:    3 passed', stderr='')

        failures, _ = self.run_plan(p, fake)

        self.assertEqual(failures, 0)
        self.assertEqual(overlapped, [False, False])

    def test_refuses_to_run_on_another_head_without_checkout(self):
        failures, out = self.run_plan(self.make_plan(head='f' * 40), lambda cmd: None)

        self.assertEqual(failures, 1)
        self.assertIn('pass --checkout', out)


class CiTest(unittest.TestCase):

    def test_a_job_line_names_its_longest_steps_and_its_skipped_ones(self):
        job = {'name': 'PHPUnit', 'conclusion': 'success', 'started_at': '2026-10-18T21:40:00Z', 'completed_at': '2026-10-18T21:42:50Z',
               'steps': [{'name': 'Set up job', 'conclusion': 'success', 'started_at': '2026-10-18T21:40:00Z', 'completed_at': '2026-10-18T21:40:30Z'},
                         {'name': 'Run unit and feature tests', 'conclusion': 'success', 'started_at': '2026-10-18T21:40:35Z', 'completed_at': '2026-10-18T21:41:37Z'},
                         {'name': 'Run browser tests', 'conclusion': 'success', 'started_at': '2026-10-18T21:42:14Z', 'completed_at': '2026-10-18T21:42:30Z'},
                         {'name': 'Upload browser test screenshots', 'conclusion': 'skipped'}]}

        self.assertEqual(ci.job_line(job), '- PHPUnit success (2m50s): Run unit and feature tests 1m02s | Run browser tests 16s'
                                           '  skipped: Upload browser test screenshots')

    def test_overlap_keeps_shared_files_and_workflow_prs_only_when_digests_move(self):
        prs = [{'number': 1432, 'files': [{'path': 'package.json'}]},
               {'number': 1430, 'files': [{'path': 'package.json'}, {'path': 'Dockerfile.sail'}]},
               {'number': 1433, 'files': [{'path': '.github/workflows/ci.yml'}]},
               {'number': 1434, 'files': [{'path': 'README.md'}]}]

        self.assertEqual([(f['pr']['number'], f['shared']) for f in ci.overlapping(prs, 1432, ['package.json', 'bun.lock'], [])],
                         [(1430, ['package.json'])])
        self.assertEqual([f['pr']['number'] for f in ci.overlapping(prs, 1432, ['Dockerfile'], ['a' * 64])], [1433])


class CoverageTest(unittest.TestCase):

    def test_an_older_major_line_is_not_covered_by_a_newer_analysed_version(self):
        self.assertEqual(coverage('6.4.4', '8.3.4'), 'other-line')
        self.assertEqual(coverage('8.3.2', '8.3.4'), 'covered')
        self.assertEqual(coverage('8.4.0', '8.3.4'), 'past')
        self.assertEqual(coverage('0.5.2', '0.7.0'), 'other-line')
        self.assertEqual(coverage('1.2.0', '2026-09-21'), 'other-line')
        self.assertEqual(coverage('1.2.0', 'any'), 'unknown')
        self.assertEqual(coverage_note('6.4.4', '8.3.4'), ' (that is the 8.x line: 6.4.4 is not covered)')


class ConflictWithTest(unittest.TestCase):

    def git(self, *args):
        return subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t'] + list(args), cwd=self.d, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, files, parent=None):
        if parent:
            self.git('checkout', '-q', parent)
        for name, text in files.items():
            with open(os.path.join(self.d, name), 'w') as f:
                f.write(text)
        self.git('add', '.')
        self.git('commit', '-qm', 'c')
        return self.git('rev-parse', 'HEAD')

    def test_only_conflicts_this_pr_causes_are_blamed_on_it(self):
        with tempfile.TemporaryDirectory() as self.d:
            self.git('init', '-q')
            old = self.commit({'a.txt': '1\n2\n3\n', 'b.txt': 'x\n'})
            base = self.commit({'b.txt': 'y\n'})
            this_pr = self.commit({'a.txt': 'P\n2\n3\n'})
            rival = self.commit({'a.txt': 'O\n2\n3\n'}, parent=base)
            stale = self.commit({'b.txt': 'z\n'}, parent=old)

            merged = ci.merged_base(base, this_pr, cwd=self.d)

            self.assertEqual(ci.conflict_with(base, merged, rival, cwd=self.d), ('new', ['a.txt']))
            self.assertEqual(ci.conflict_with(base, merged, stale, cwd=self.d), ('stale', ['b.txt']))
            self.assertEqual(self.git('status', '--porcelain'), '')


class FetchPrHeadsTest(unittest.TestCase):

    def git(self, cwd, *args):
        return subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t'] + list(args), cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_fetches_the_missing_heads_and_survives_a_bad_one(self):
        with tempfile.TemporaryDirectory() as d:
            origin, clone = os.path.join(d, 'origin'), os.path.join(d, 'clone')
            os.makedirs(origin)
            self.git(origin, 'init', '-q')
            self.git(origin, 'commit', '-q', '--allow-empty', '-m', 'base')
            heads = {}
            for n in (1, 2):
                self.git(origin, 'commit', '-q', '--allow-empty', '-m', 'pr %d' % n)
                heads[n] = self.git(origin, 'rev-parse', 'HEAD')
                self.git(origin, 'update-ref', 'refs/pull/%d/head' % n, 'HEAD')
                self.git(origin, 'reset', '-q', '--hard', 'HEAD~1')
            self.git(d, 'clone', '-q', '--no-local', origin, clone)
            self.assertFalse(ci.have_commit(heads[1], cwd=clone))

            self.assertEqual(ci.fetch_pr_heads({**heads, 3: 'f' * 40}, cwd=clone), {1, 2})
