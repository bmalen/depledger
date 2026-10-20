"""Plan and run a PR's local checks with the repo's own tools.

Triage writes the plan (checks.json in its --out dir) from what it already knows: the package
manager and lock, the package.json scripts, whether the repo uses Sail, composer and artisan. The
runner executes it on the checked-out PR head: installs first, then build, typecheck and lint
(without --fix) side by side, then tests one run at a time, inside Sail when the repo uses it. Each check prints
one PASS/FAIL line with the evidence (lock untouched, files and errors linted, tests passed).
"""
import json
import os
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

RUNNERS = ('bun', 'bunx', 'npx', 'pnpm', 'yarn', 'npm', 'node', 'php', 'composer')
EXEC = {'bun': 'bun x', 'pnpm': 'pnpm exec', 'npm': 'npx --no-install', 'yarn': 'yarn'}
RUN = {'bun': 'bun run', 'pnpm': 'pnpm run', 'npm': 'npm run', 'yarn': 'yarn run'}
TYPECHECK_SCRIPTS = ('typecheck', 'type-check', 'check-types', 'types', 'tsc', 'lint:types')
WRAPPERS = ('bun', 'bunx', 'npx', 'pnpm', 'yarn', 'exec', 'x', 'dlx', '--no-install', 'run')


def install_js(pm, lock):
    if pm == 'yarn':
        with open(lock) as f:
            return 'yarn install --immutable' if '__metadata:' in f.read(4096) else 'yarn install --frozen-lockfile'
    return {'bun': 'bun install --frozen-lockfile', 'pnpm': 'pnpm install --frozen-lockfile', 'npm': 'npm ci'}.get(pm)


def tool_of(segment):
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    while tokens and (tokens[0] in WRAPPERS or '=' in tokens[0]):
        tokens.pop(0)
    return os.path.basename(tokens[0]) if tokens else ''


def no_fix(segment, pm):
    """One lint command as a review runs it: no --fix, prettier --check, bare bins through the
    package manager, eslint as JSON so the runner can count files and errors."""
    tool = tool_of(segment)
    cmd = re.sub(r'\s--fix(=\S+)?(?=\s|$)', '', segment)
    if tool == 'prettier':
        cmd = re.sub(r'\s(--write|-w)(?=\s|$)', ' --check', cmd)
    if tool == 'vue-cli-service' and re.search(r'\slint(\s|$)', cmd) and '--no-fix' not in cmd:
        cmd += ' --no-fix'
    if tool == 'biome':
        cmd = re.sub(r'\s--(write|apply|apply-unsafe|unsafe)(?=\s|$)', '', cmd)
    first = cmd.split()[0]
    if first not in RUNNERS and not first.startswith(('vendor/bin/', './')):
        cmd = '%s %s' % (EXEC.get(pm, 'npx --no-install'), cmd)
    if tool == 'eslint':
        cmd = re.sub(r'\s(--format|-f)(=|\s+)\S+', '', cmd) + ' --format json'
    return {'name': 'lint:%s' % (tool or 'script'), 'cmd': cmd, 'kind': 'eslint' if tool == 'eslint' else 'generic',
            'check': 'lint(no --fix)', 'phase': 2}


def lint_steps(scripts, pm, name='lint', depth=0):
    """The lint script split at && and ;, following calls to other scripts (`bun run lint:css`)."""
    steps = []
    for seg in re.split(r'\s*(?:&&|;)\s*', scripts.get(name) or ''):
        seg = seg.strip()
        if not seg:
            continue
        m = re.fullmatch(r'(?:bun|npm|pnpm|yarn)(?:\s+run)?\s+([\w:.-]+)(?:\s+--(?:\s.*)?)?', seg)
        if m and m.group(1) in scripts and m.group(1) != name and depth < 3:
            steps += lint_steps(scripts, pm, m.group(1), depth + 1)
        else:
            steps.append(no_fix(seg, pm))
    return steps


def php_tests(top):
    """A library's own test command: its composer `test` script, else Pest, else PHPUnit."""
    try:
        with open(os.path.join(top, 'composer.json')) as f:
            composer = json.load(f)
    except (OSError, ValueError):
        composer = {}
    if 'test' in (composer.get('scripts') or {}):
        return 'composer test'
    return 'vendor/bin/pest' if 'pestphp/pest' in (composer.get('require-dev') or {}) else 'vendor/bin/phpunit'


def plan(env, selected, moved=()):
    """env: top, pm, lock, scripts, uses_sail, sail_up, has_composer, has_artisan.
    selected: triage's CHECKS. moved: package names the PR moves."""
    pm, scripts = env['pm'], env['scripts']
    steps = []
    if pm in RUN and env.get('lock'):
        steps.append({'name': 'install-js', 'cmd': install_js(pm, os.path.join(env['top'], env['lock'])), 'kind': 'install-js',
                      'check': 'install-js(frozen)', 'phase': 1})
    if env['has_composer']:
        steps.append({'name': 'install-php', 'cmd': 'composer install --no-interaction --no-progress', 'kind': 'install-php',
                      'check': 'install-php', 'phase': 1})
    if pm in RUN and scripts.get('build'):
        steps.append({'name': 'build', 'cmd': '%s build' % RUN[pm], 'kind': 'generic', 'check': 'build', 'phase': 2})
    tc = next((s for s in TYPECHECK_SCRIPTS if s in scripts), None)
    if pm in RUN and tc:
        steps.append({'name': 'typecheck', 'cmd': '%s %s' % (RUN[pm], tc), 'kind': 'typecheck', 'check': 'typecheck', 'phase': 2})
    if pm in RUN:
        steps += lint_steps(scripts, pm)
    if env['has_composer']:
        test = 'php artisan test --compact' if env['has_artisan'] else php_tests(env['top'])
        steps.append({'name': 'tests', 'cmd': test, 'kind': 'tests', 'check': 'tests', 'phase': 3})
        if env['has_artisan'] and any(m in ('phpunit/phpunit', 'brianium/paratest', 'pestphp/pest') for m in moved):
            steps.append({'name': 'tests-parallel', 'cmd': test + ' --parallel', 'kind': 'tests', 'check': 'tests', 'phase': 3})
    for s in steps:
        s['selected'] = s['check'] in selected
    automated = {s['check'] for s in steps}
    return {'env': {k: env[k] for k in ('top', 'pm', 'lock', 'uses_sail', 'sail_up')}, 'steps': steps,
            'manual': [c for c in selected if c not in automated and c not in ('probe-image', 'rebuild-stacked')]}


def write_plan(out_dir, p, pr):
    p = dict(p, repo=pr['repo'], number=pr['number'], head_sha=pr['head_sha'], head_ref=pr['head_ref'])
    with open(os.path.join(out_dir, 'checks.json'), 'w') as f:
        json.dump(p, f, indent=1)
    return p


# ---------------------------------------------------------------- results

def eslint_summary(stdout):
    start = stdout.find('[')
    try:
        files = json.loads(stdout[start:]) if start >= 0 else None
    except ValueError:
        files = None
    if files is None:
        return None
    errors = sum(f.get('errorCount', 0) + f.get('fatalErrorCount', 0) for f in files)
    warnings = sum(f.get('warningCount', 0) for f in files)
    first = ['%s:%s %s' % ('/'.join(f['filePath'].split('/')[-3:]), m.get('line'), m.get('ruleId') or m.get('message', '')[:60])
             for f in files for m in f.get('messages', []) if m.get('severity') == 2][:5]
    return {'files': len(files), 'errors': errors, 'warnings': warnings, 'first': first}


def last_line(text):
    lines = [l.strip() for l in re.sub(r'\x1b\[[0-9;]*m', '', text).splitlines() if l.strip()]
    return lines[-1][:160] if lines else ''


def find_line(text, pattern):
    hits = re.findall(pattern, re.sub(r'\x1b\[[0-9;]*m', '', text))
    return hits[-1].strip()[:120] if hits else ''


def judge(step, rc, stdout, stderr, lock_dirty):
    """(ok, summary) for one finished step."""
    both = stdout + '\n' + stderr
    kind = step['kind']
    if kind == 'install-js':
        if rc == 0 and lock_dirty:
            return False, 'install rewrote %s: the lock is not in sync with package.json' % ', '.join(lock_dirty)
        if rc != 0:
            return False, last_line(stderr) or last_line(stdout)
        done = find_line(both, r'(?m)^.*(?:\d+ packages? installed|Checked \d+ installs.*|added \d+ packages.*|up to date.*|Done in .*)$')
        return True, 'lock untouched' + ('; ' + done if done else '')
    if kind == 'install-php':
        if re.search(r'(?i)lock file is not up to date', both):
            return False, 'composer.lock is not up to date with composer.json'
        if rc != 0:
            return False, last_line(stderr) or last_line(stdout)
        ops = find_line(both, r'(?m)^.*(?:Package operations: .*|Nothing to install, update or remove)$')
        return True, 'composer.lock in sync' + ('; ' + ops if ops else '')
    if kind == 'eslint':
        s = eslint_summary(stdout)
        if s is None:
            return False, 'no eslint JSON (exit %d): %s' % (rc, last_line(stderr) or last_line(stdout))
        text = '%d files, %d errors, %d warnings' % (s['files'], s['errors'], s['warnings'])
        if s['files'] == 0:
            return False, text + ': the glob matched nothing'
        return s['errors'] == 0 and rc in (0, 1), text + ('' if not s['first'] else ': ' + '; '.join(s['first']))
    if kind == 'typecheck':
        n = len(re.findall(r'error TS\d+', both))
        if rc != 0 and not n:
            return False, last_line(both)
        return rc == 0, '%d errors' % n
    if kind == 'tests':
        m = re.findall(r'Tests:\s+(.+)', re.sub(r'\x1b\[[0-9;]*m', '', both))
        return rc == 0, (m[-1].strip()[:160] if m else last_line(both))
    if rc == 0 and step['name'] == 'build':
        built = re.findall(r'built in ([\d.]+m?s)', re.sub(r'\x1b\[[0-9;]*m', '', both))
        if built:
            return True, '%d bundle%s built (%s)' % (len(built), '' if len(built) == 1 else 's', ', '.join(built))
    if rc == 0:
        return True, last_line(stdout) or 'clean'
    return False, last_line(stderr) or last_line(stdout)


# ---------------------------------------------------------------- running

def git(top, *args, timeout=120):
    p = subprocess.run(['git'] + list(args), cwd=top, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout.rstrip('\n'), p.stderr.strip()


def checkout(p):
    """Put the PR head in the working tree, refusing anything that could lose work."""
    top, ref = p['env']['top'], p['head_ref']
    if git(top, 'status', '--porcelain')[1]:
        return 'the working tree is not clean: commit or stash first'
    if git(top, 'rev-parse', '--verify', '--quiet', 'refs/heads/' + ref)[0] == 0:
        git(top, 'fetch', '-q', 'origin', ref)
        if git(top, 'rev-list', 'origin/%s..%s' % (ref, ref))[1]:
            return 'local branch %s has commits that are not on origin: check it out by hand' % ref
        for args in (('checkout', '-q', ref), ('reset', '-q', '--hard', 'origin/' + ref)):
            rc, _, e = git(top, *args)
            if rc != 0:
                return 'git %s failed: %s' % (args[0], e)
    else:
        r = subprocess.run(['gh', 'pr', 'checkout', str(p['number']), '--repo', p['repo']], cwd=top, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            return 'gh pr checkout failed: %s' % r.stderr.strip()
    return None


def wrap(env, cmd):
    return ['./vendor/bin/sail', 'bash', '-c', cmd] if env['uses_sail'] else ['bash', '-c', cmd]


def run_plan(p, out_dir, only=None, do_checkout=False, here=False, executor=None):
    """Run the plan and print one line per step. Returns the number of failed steps."""
    env, top = p['env'], p['env']['top']
    head = git(top, 'rev-parse', 'HEAD')[1]
    if here:
        print('running on %s at %s (--here: a baseline, not the PR head)' % (git(top, 'branch', '--show-current')[1] or 'detached', head[:8]))
    elif head != p['head_sha']:
        if not do_checkout:
            print('HEAD is %s, the PR head is %s: pass --checkout, or check it out first' % (head[:8], p['head_sha'][:8]))
            return 1
        problem = checkout(p)
        if problem:
            print('CHECKOUT REFUSED: ' + problem)
            return 1
        head = git(top, 'rev-parse', 'HEAD')[1]
        print('checked out %s at %s%s' % (p['head_ref'], head[:8], '' if head == p['head_sha'] else
                                          '  (the PR moved since triage, which saw %s: re-triage before trusting this)' % p['head_sha'][:8]))
    hot = os.path.join(top, 'public', 'hot')
    if os.path.exists(hot):
        os.remove(hot)
        print('removed public/hot (a stale dev-server marker renders blank pages)')
    if env['uses_sail'] and subprocess.run(['./vendor/bin/sail', 'ps', '-q'], cwd=top, capture_output=True, text=True,
                                           stdin=subprocess.DEVNULL).stdout.strip() == '':
        print('starting Sail: ' + ('ok' if subprocess.run(['./vendor/bin/sail', 'up', '-d'], cwd=top, capture_output=True,
                                                            stdin=subprocess.DEVNULL).returncode == 0 else 'FAILED'))
    if only:
        steps = [s for s in p['steps'] if any(s['name'].startswith(o) or s['check'].startswith(o) for o in only)]
    else:
        steps = [s for s in p['steps'] if s['selected']]
    dirty_before = set(git(top, 'status', '--porcelain', '--untracked-files=no')[1].splitlines())
    execute = executor or (lambda cmd: subprocess.run(wrap(env, cmd), cwd=top, capture_output=True, text=True,
                                                      stdin=subprocess.DEVNULL, timeout=3600))

    def one(step):
        t0 = time.time()
        try:
            r = execute(step['cmd'])
            rc, so, se = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            rc, so, se = 124, '', 'timed out after 3600s'
        log = os.path.join(out_dir, 'check-%s%s.log' % (step['name'].replace(':', '-'), '.here' if here else ''))
        with open(log, 'w') as f:
            f.write('$ %s\n# exit %d\n%s\n--- stderr ---\n%s' % (step['cmd'], rc, so, se))
        lock_dirty = []
        if step['kind'] == 'install-js':
            lock_dirty = [l[3:] for l in git(top, 'status', '--porcelain', '--', env['lock'], 'package.json')[1].splitlines()]
        ok, summary = judge(step, rc, so, se, lock_dirty)
        return step, ok, summary, int(time.time() - t0), log

    # Test runs go one at a time: a serial and a --parallel run share the app's Redis, storage and CPU.
    results = []
    for phase in (1, 2, 3):
        batch = [s for s in steps if s['phase'] == phase]
        with ThreadPoolExecutor(max_workers=1 if phase == 3 else 4) as ex:
            results += list(ex.map(one, batch))
    failures = 0
    for step, ok, summary, secs, log in results:
        failures += 0 if ok else 1
        print('%s %-16s %4ds  %s%s' % ('PASS' if ok else 'FAIL', step['name'], secs, summary, '' if ok else '  -> ' + log))
    touched = sorted(set(git(top, 'status', '--porcelain', '--untracked-files=no')[1].splitlines()) - dirty_before)
    if touched:
        print('NOTE: the checks changed tracked files (a lint that fixes by default?): ' + ', '.join(l[3:] for l in touched))
    if p.get('manual'):
        print('MANUAL: %s (see the skill)' % ', '.join(p['manual']))
    print('RESULT=%s' % ('pass' if not failures else 'fail (%d)' % failures))
    return failures
