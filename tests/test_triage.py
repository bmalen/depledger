import base64
import contextlib
import io
import json
import os
import subprocess
import threading
import time
from unittest import mock

from helpers import LedgerTestCase

from depledger import store, triage
from depledger.parse import bump_type

OLD_SHA, NEW_SHA = '1' * 40, '2' * 40
OLD_DIGEST, NEW_DIGEST = 'a' * 64, 'b' * 64

PR = {'title': 'chore(deps): update dependencies', 'author': {'login': 'renovate[bot]'}, 'headRefName': 'renovate/all',
      'headRefOid': 'f' * 40, 'baseRefName': 'main', 'body': '', 'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN',
      'files': [{'path': 'package.json'}, {'path': 'bun.lock'}, {'path': 'docker-compose.yml'}, {'path': '.github/workflows/ci.yml'}],
      'statusCheckRollup': [{'name': 'tests', 'conclusion': 'SUCCESS', 'workflowName': 'CI'}],
      'url': 'https://github.com/acme/web/pull/12', 'isDraft': False, 'state': 'OPEN', 'createdAt': '2026-10-01T00:00:00Z', 'mergedAt': None}

DIFF = '''diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
-    "eslint": "9.1.0",
+    "eslint": "9.1.1",
diff --git a/bun.lock b/bun.lock
--- a/bun.lock
+++ b/bun.lock
-    "eslint": ["eslint@9.1.0", "", {}, "sha512-x"],
+    "eslint": ["eslint@9.1.1", "", {}, "sha512-y"],
diff --git a/docker-compose.yml b/docker-compose.yml
--- a/docker-compose.yml
+++ b/docker-compose.yml
-    image: redis:8-alpine@sha256:%s
+    image: redis:8-alpine@sha256:%s
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
-      - uses: actions/checkout@%s # v6.0.0
+      - uses: actions/checkout@%s # v6.1.0
''' % (OLD_DIGEST, NEW_DIGEST, OLD_SHA, NEW_SHA)


def manifest_list(amd64, arm64):
    return json.dumps({'manifests': [{'platform': {'os': 'linux', 'architecture': 'amd64'}, 'digest': 'sha256:' + amd64},
                                     {'platform': {'os': 'linux', 'architecture': 'arm64'}, 'digest': 'sha256:' + arm64},
                                     {'platform': {'os': 'unknown', 'architecture': 'unknown'}, 'digest': 'sha256:9'}]})


class FakeWorld:
    """Answers triage's gh, docker and git calls for PR acme/web#12, each after a short delay,
    and counts how many were in flight at once."""

    def __init__(self):
        self.lock = threading.Lock()
        self.in_flight = self.most_in_flight = 0

    def answer(self, cmd):
        joined = ' '.join(cmd)
        if cmd[:3] == ['gh', 'pr', 'view']:
            return 0, json.dumps(PR)
        if cmd[:3] == ['gh', 'pr', 'diff']:
            return 0, DIFF
        if cmd[:3] == ['gh', 'pr', 'list']:
            return 0, json.dumps([{'number': 12, 'title': PR['title'], 'author': PR['author'], 'headRefName': PR['headRefName'],
                                   'headRefOid': PR['headRefOid'], 'files': PR['files'], 'isDraft': False}])
        if cmd[:2] == ['docker', 'info']:
            return 0, ''
        if joined == 'docker buildx imagetools inspect redis:8-alpine':
            return 0, 'Name: docker.io/library/redis:8-alpine\nDigest: sha256:%s\n' % NEW_DIGEST
        if joined == 'docker buildx imagetools inspect redis@sha256:%s --raw' % OLD_DIGEST:
            return 0, manifest_list('3' * 64, '4' * 64)
        if joined == 'docker buildx imagetools inspect redis@sha256:%s --raw' % NEW_DIGEST:
            return 0, manifest_list('5' * 64, '4' * 64)
        if joined.startswith('gh api repos/acme/web/commits/'):
            return 0, '{"check_runs": []}'
        if joined.startswith('gh api repos/actions/checkout/git/ref/tags/v6.1.0 '):
            return 0, '%s commit\n' % NEW_SHA
        if joined.startswith('gh api repos/acme/web/contents/package.json?ref=renovate/all '):
            return 0, base64.b64encode(b'{"devDependencies": {"eslint": "9.1.1"}}').decode()
        return 128, ''

    def run(self, cmd, *a, **kw):
        with self.lock:
            self.in_flight += 1
            self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            time.sleep(0.05)
            rc, stdout = self.answer(list(cmd))
            return subprocess.CompletedProcess(cmd, rc, stdout, '')
        finally:
            with self.lock:
                self.in_flight -= 1


class TriageMainTest(LedgerTestCase):

    def test_a_pr_is_triaged_end_to_end_with_its_slow_calls_overlapping(self):
        world = FakeWorld()
        outdir = os.path.join(self.tmp.name, 'out')
        work = os.path.join(self.tmp.name, 'work')
        os.makedirs(work)
        old_cwd, out = os.getcwd(), io.StringIO()
        try:
            os.chdir(work)
            with mock.patch.object(subprocess, 'run', side_effect=world.run), contextlib.redirect_stdout(out):
                triage.main(['https://github.com/acme/web/pull/12', '--out', outdir])
        finally:
            os.chdir(old_cwd)
        text = out.getvalue()

        for line in ('- npm eslint: 9.1.0 -> 9.1.1  [patch] section=devDependencies  ledger=not in ledger',
                     '    v6.1.0 resolves to %s -> OK' % NEW_SHA[:12],
                     '    head-of-tag redis:8-alpine = %s -> OK (new digest is head)' % NEW_DIGEST[:12],
                     '    per-arch manifests: linux/amd64 changed, linux/arm64 IDENTICAL',
                     'BUCKETS=js-devtool,ci-actions,docker-image',
                     '(git checks skipped: repo mismatch, cwd is not a gh repo)',
                     '(0 other open PRs, none touch these files)',
                     'TIER=1',
                     'CHECKS=install-js(frozen),probe-image'):
            self.assertIn(line + '\n', text)
        with open(os.path.join(outdir, 'probes.json')) as f:
            self.assertEqual(json.load(f)[0]['platform'], 'linux/amd64')
        web = store.find_repo(self.conn, 'acme/web')
        pr = store.get_pr(self.conn, web['id'], 12)
        self.assertEqual(pr['proposed_tier'], 1)
        self.assertEqual(sorted(b['name'] for b in store.pr_bumps(self.conn, pr['id'])), ['actions/checkout', 'eslint', 'redis'])
        self.assertGreaterEqual(world.most_in_flight, 3)


def pkg(name, old, new, section='devDependencies', eco='npm', index=None):
    return {'eco': eco, 'name': name, 'old': old, 'new': new, 'type': bump_type(old, new), 'section': section, 'index': index}


def facts(**over):
    f = {'pr': {'title': 'chore(deps): update dependencies', 'headRefName': 'renovate/all', 'mergeStateStatus': 'CLEAN',
                'statusCheckRollup': []},
         'paths': ['package.json', 'bun.lock'], 'pkgs': [], 'env': {'scripts': {}, 'subs': []},
         'changes': {'images': {}, 'uses': {}, 'other_files': [], 'image_files': {}},
         'actions': [], 'images': {}, 'image_rows': {}, 'branch': None,
         'lock_independent': [], 'lock_rows': {}, 'lock_name': {}, 'nested_new': []}
    f.update(over)
    return f


class DecideTest(LedgerTestCase):

    def test_a_devtool_patch_is_tier_0_with_lint_and_typecheck(self):
        d = triage.decide(facts(pkgs=[pkg('eslint', '9.1.0', '9.1.1')],
                                env={'scripts': {'lint': 'eslint . --fix', 'typecheck': 'vue-tsc'}, 'subs': []}))

        self.assertEqual((d['tier'], d['buckets']), (0, ['js-devtool']))
        self.assertEqual(d['checks'], ['install-js(frozen)', 'typecheck', 'lint(no --fix)'])
        self.assertTrue(d['needs_checkout'])

    def test_an_unknown_runtime_minor_is_tier_1_with_a_build_and_chrome(self):
        d = triage.decide(facts(pkgs=[pkg('axios', '1.19.0', '1.20.0', section='dependencies')]))

        self.assertEqual((d['tier'], d['buckets']), (1, ['frontend']))
        self.assertIn('T1: axios: unknown runtime package, minor bump', d['reasons'])
        self.assertEqual(d['checks'], ['install-js(frozen)', 'build', 'chrome'])

    def test_a_trap_row_a_conflict_and_red_ci_are_tier_2(self):
        trap = {'status': 'TRAP', 'analysed': '3.31.2'}
        pr = dict(facts()['pr'], statusCheckRollup=[{'name': 'tests', 'conclusion': 'FAILURE'}])
        branch = {'behind': '3', 'ahead': '1', 'merge_rc': 1, 'merge_err': '', 'overlap': []}

        d = triage.decide(facts(pr=pr, branch=branch, pkgs=[pkg('@tiptap/core', '3.31.2', '3.31.3', section='dependencies', index=trap)]))

        self.assertEqual(d['tier'], 2)
        self.assertEqual([r for r in d['reasons'] if r.startswith('T2')],
                         ['T2: branch conflicts with base: rebase first', 'T2: CI failed: tests', 'T2: @tiptap/core is a TRAP row'])
        self.assertIn('behind', d['note_context']['text'].split())

    def test_a_new_digest_ci_never_runs_needs_a_probe_but_no_checkout(self):
        images = {'redis:8-alpine': {'name': 'redis', '-': {'tag': '8-alpine', 'digest': OLD_DIGEST},
                                     '+': {'tag': '8-alpine', 'digest': NEW_DIGEST}}}
        image = {'row': None, 'hits': {'old': [], 'new': []}, 'wf': [], 'stacked': []}

        d = triage.decide(facts(paths=['docker-compose.yml'],
                                changes={'images': images, 'uses': {}, 'other_files': [], 'image_files': {'redis:8-alpine': ['docker-compose.yml']}},
                                images={'redis:8-alpine': image}, image_rows={'redis:8-alpine': None}))

        self.assertEqual((d['tier'], d['buckets'], d['checks']), (1, ['docker-image'], ['probe-image']))
        self.assertEqual(d['image_ci_blind'], ['redis:8-alpine'])
        self.assertFalse(d['needs_checkout'])
