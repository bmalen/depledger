"""Pure parsing helpers shared by triage and the markdown migration: no ledger access."""
import json
import os
import re
import subprocess
import sys


HEX40 = re.compile(r'^[0-9a-f]{40}$')

BUILD_CHAIN = (
    'vite', '@vitejs/', 'postcss', 'autoprefixer', 'tailwindcss', '@tailwindcss/',
    'laravel-vite-plugin', '@vue/compiler-sfc', 'sass', 'esbuild', 'rollup', 'rolldown',
    'vite-plugin-', 'cssnano', 'lightningcss', 'terser', '@sentry/vite-plugin',
)
LINT_ONLY = (
    'eslint', '@eslint/', 'typescript-eslint', '@typescript-eslint/', 'eslint-plugin-',
    'eslint-config-', 'prettier', 'prettier-plugin-', 'globals', '@types/', 'vue-tsc',
    'typescript', 'stylelint', 'lint-staged', 'husky', 'baseline-browser-mapping',
)


def run(cmd, timeout=60, cwd=None):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, '', 'timeout after %ss' % timeout
    except Exception as e:  # noqa
        return 1, '', str(e)


def out(rc, o, _e=None):
    return o.strip() if rc == 0 else ''


def short(d):
    return d[:12] if d else ''


def section(title):
    print('\n## ' + title)


# ---------------------------------------------------------------- versions

def norm_ver(v):
    v = v.strip().strip('"').strip("'")
    v = re.sub(r'^[\^~=v]+', '', v)
    v = re.sub(r'^(>=|<=|>|<)', '', v)
    return v


def ver_parts(v):
    parts = []
    for p in re.split(r'[.\-+]', norm_ver(v)):
        if p.isdigit():
            parts.append(int(p))
        else:
            break
    return parts


def bump_type(old, new):
    if not old or not new:
        return 'unknown'
    if re.fullmatch(r'[0-9a-f]{7,64}', old) and re.fullmatch(r'[0-9a-f]{7,64}', new):
        return 'digest'
    a, b = ver_parts(old), ver_parts(new)
    if not a or not b:
        return 'other'
    a += [0] * (3 - len(a))
    b += [0] * (3 - len(b))
    if a[:3] == b[:3]:
        return 'none' if norm_ver(old) == norm_ver(new) else 'prerelease/build'
    if a[0] != b[0]:
        return 'major'
    if a[1] != b[1]:
        return 'minor (0.x = major)' if a[0] == 0 else 'minor'
    return 'patch (0.0.x = breaking-capable)' if a[0] == 0 and a[1] == 0 else 'patch'


def is_major_like(t):
    return t.startswith('major') or '0.x' in t or '0.0.x' in t


# ---------------------------------------------------------------- PR fetch

def parse_arg(arg, cur_repo):
    m = re.search(r'github\.com/([^/]+/[^/]+)/pull/(\d+)', arg)
    if m:
        return m.group(1), m.group(2)
    m = re.fullmatch(r'#?(\d+)', arg.strip())
    if m:
        return cur_repo, m.group(1)
    sys.exit('cannot parse PR from %r' % arg)


def fetch_pr(repo, num):
    fields = ('title,author,headRefName,headRefOid,baseRefName,body,files,mergeable,mergeStateStatus,'
              'statusCheckRollup,url,isDraft,state,additions,deletions,commits,createdAt,mergedAt')
    rc, o, e = run(['gh', 'pr', 'view', num, '--repo', repo, '--json', fields], timeout=90)
    if rc != 0:
        sys.exit('gh pr view failed: ' + e.strip())
    return json.loads(o)


# ---------------------------------------------------------------- diff parsing

def parse_diff(text):
    files = {}
    cur = None
    for line in text.splitlines():
        if line.startswith('diff --git'):
            m = re.match(r'diff --git a/(.*?) b/(.*)$', line)
            cur = m.group(2) if m else line
            files[cur] = []
        elif cur is not None and (line.startswith('+') or line.startswith('-')) \
                and not line.startswith('+++') and not line.startswith('---'):
            files[cur].append((line[0], line[1:]))
    return files


def manifest_changes(lines):
    """{name: {'-': ver, '+': ver}} from +/- '"name": "ver"' lines."""
    found = {}
    for sign, l in lines:
        m = re.match(r'^\s*"([^"]+)":\s*"([^"]+)",?\s*$', l)
        if m:
            found.setdefault(m.group(1), {})[sign] = m.group(2)
    return found


def bun_lock_changes(lines):
    found = {}
    for sign, l in lines:
        m = re.match(r'^\s+"([^"]+)": \["((?:@[^"/]+/)?[^"@/]+)@([^"]+)"', l)
        if m:
            key, name, ver = m.groups()
            found.setdefault(key, {'name': name})[sign] = ver
    return found


def composer_lock_changes(lines):
    found = {}
    pending = {'-': None, '+': None}
    for sign, l in lines:
        m = re.match(r'^\s+"name": "([^"]+)"', l)
        if m:
            pending[sign] = m.group(1)
            continue
        m = re.match(r'^\s+"version": "([^"]+)"', l)
        if m and pending[sign]:
            found.setdefault(pending[sign], {})[sign] = m.group(1)
            pending[sign] = None
    return found


def uses_changes(lines):
    found = {}
    for sign, l in lines:
        m = re.match(r'^\s*-?\s*uses:\s*["\']?([^@\s"\']+)@([^\s#"\']+)["\']?\s*(?:#\s*(\S+))?', l)
        if m:
            action, ref, comment = m.groups()
            key = action
            found.setdefault(key, {})[sign] = {'ref': ref, 'comment': comment or ''}
    return found


def image_changes(lines):
    found = {}
    for sign, l in lines:
        m = re.match(r'^\s*(?:-\s*)?(?:image:|FROM)\s+(.*)$', l)
        if not m:
            continue
        toks = [t for t in m.group(1).replace('"', '').replace("'", '').split() if not t.startswith('--')]
        if not toks:
            continue
        ref = toks[0]
        digest = ''
        if '@sha256:' in ref:
            ref, digest = ref.split('@sha256:', 1)
        name, tag = ref, ''
        last = ref.rsplit('/', 1)[-1]
        if ':' in last:
            name = ref[: len(ref) - len(last)] + last.split(':', 1)[0]
            tag = last.split(':', 1)[1]
        key = '%s:%s' % (name, tag) if tag else name
        found.setdefault(key, {'name': name})[sign] = {'tag': tag, 'digest': digest}
    # a tag move (postgres:17.10 -> 17.11) lands as a '-' under one key and a '+' under another
    for k, v in list(found.items()):
        if '+' in v and '-' not in v:
            for k2, v2 in list(found.items()):
                if k2 != k and v2.get('name') == v['name'] and '-' in v2 and '+' not in v2:
                    v['-'] = v2.pop('-')
                    if not any(x in v2 for x in '+-'):
                        found.pop(k2)
                    break
    return found


def renovate_table(body):
    rows = []
    header = None
    for line in body.splitlines():
        if not line.startswith('|'):
            if header and rows:
                break
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if header is None:
            if 'Package' in cells[0]:
                header = [re.sub(r'\[([^\]]+)\].*', r'\1', c).lower() for c in cells]
            continue
        if set(line.strip()) <= set('|-: '):
            continue
        row = dict(zip(header, cells))
        pkg = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', row.get('package', ''))
        pkg = re.sub(r'\s*\(source\)\s*', '', pkg).strip()
        pkg = pkg.split(' (')[0].strip()
        ch = row.get('change', '')
        m = re.search(r'`([^`]+)`\s*(?:→|->)\s*`([^`]+)`', ch)
        rows.append({'pkg': pkg, 'old': m.group(1) if m else '', 'new': m.group(2) if m else '',
                     'update': row.get('update', ''), 'type': row.get('type', '')})
    return rows


def release_notes(body, outdir):
    """Split <details> blocks into per-package files; return {name: (lines, hits, truncated)}."""
    res = {}
    kw = re.compile(r'(?i)\bbreaking\b|\bremov(ed|es|al)\b|\bdropp?(ed|s)? support\b|\brenamed?\b|'
                    r'no longer|minimum (supported )?(php|node|version)|requires? (php|node) |deprecat')
    for m in re.finditer(r'<details>\s*<summary>(.*?)</summary>(.*?)</details>', body, re.S):
        name = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        text = m.group(2)
        slug = re.sub(r'[^A-Za-z0-9.-]+', '-', name).strip('-')[:60]
        path = os.path.join(outdir, 'notes-%s.md' % slug)
        with open(path, 'w') as f:
            f.write(text)
        hits = len(kw.findall(text))
        res[name] = (text.count('\n'), hits, path)
    truncated = bool(re.search(r'(?i)truncated|release notes.*too long', body))
    return res, truncated


# ---------------------------------------------------------------- whole-diff summaries

LOCK_AND_PIN_BASENAMES = ('package.json', 'composer.json', 'bun.lock', 'bun.lockb', 'composer.lock',
                          'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', '.nvmrc', '.node-version',
                          'Cargo.toml', 'Cargo.lock')
NOT_PACKAGES = ('name', 'version', 'type', 'description', 'license', 'main', 'module', 'private', 'packageManager')


def collect_changes(files):
    """Everything a PR moves, from parse_diff() output."""
    c = {'npm': {}, 'composer': {}, 'bun_lock': {}, 'composer_lock': {}, 'uses': {}, 'images': {},
         'image_files': {}, 'other_files': []}
    for path, lines in files.items():
        bn = os.path.basename(path)
        if bn == 'package.json':
            for k, v in manifest_changes(lines).items():
                c['npm'].setdefault(k, {}).update(v)
        elif bn == 'composer.json':
            for k, v in manifest_changes(lines).items():
                c['composer'].setdefault(k, {}).update(v)
        elif bn == 'bun.lock':
            c['bun_lock'].update(bun_lock_changes(lines))
        elif bn == 'composer.lock':
            c['composer_lock'].update(composer_lock_changes(lines))
        if path.startswith('.github/'):
            for k, v in uses_changes(lines).items():
                c['uses'].setdefault(k, {}).update(v)
        for k, v in image_changes(lines).items():
            c['images'].setdefault(k, {}).update(v)
            c['image_files'].setdefault(k, []).append(path)
        if not (bn in LOCK_AND_PIN_BASENAMES
                or path.startswith('.github/') or bn.startswith('Dockerfile')
                or bn.startswith('docker-compose') or path.startswith('docker/')
                or path.startswith('.devcontainer/')):
            c['other_files'].append(path)
    # manifest top-level keys that are not packages
    for d in (c['npm'], c['composer']):
        for k in list(d):
            if k in NOT_PACKAGES:
                d.pop(k)
    return c


def lock_only_movers(c):
    """[(lock, key, package name, old, new)] for transitives that moved in a lock without a
    manifest entry. For a nested bun.lock key (`@tiptap/react/prosemirror-view`) the package
    name is the entry's own name, not the key's last segment."""
    manifest_names = set(c['npm']) | set(c['composer'])
    out = []
    for key, v in c['bun_lock'].items():
        if v['name'] not in manifest_names and '-' in v and '+' in v and v['-'] != v['+']:
            out.append(('bun.lock', key, v['name'], v['-'], v['+']))
    for name, v in c['composer_lock'].items():
        if name not in manifest_names and '-' in v and '+' in v and v['-'] != v['+']:
            out.append(('composer.lock', name, name, v['-'], v['+']))
    return sorted(out)


def bumps(c, sections=None):
    """Rows for the ledger's pr_bumps table. sections: {(ecosystem, name): manifest section}."""
    sections = sections or {}
    out = []
    for eco in ('npm', 'composer'):
        for name, v in sorted(c[eco].items()):
            o, n = v.get('-', ''), v.get('+', '')
            out.append({'kind': 'manifest', 'ecosystem': eco, 'name': name, 'section': sections.get((eco, name)),
                        'from': o, 'to': n, 'type': bump_type(o, n)})
    for lock, key, name, o, n in lock_only_movers(c):
        out.append({'kind': 'lock', 'ecosystem': 'npm' if lock == 'bun.lock' else 'composer', 'name': name,
                    'section': lock if key == name else '%s %s' % (lock, key), 'from': o, 'to': n, 'type': bump_type(o, n)})
    for action, v in sorted(c['uses'].items()):
        o, n = v.get('-'), v.get('+')

        def label(x):
            if not x:
                return ''
            return ('%s %s' % (x['comment'], x['ref'][:12])).strip() if HEX40.match(x['ref']) else x['ref']
        out.append({'kind': 'action', 'ecosystem': 'actions', 'name': action, 'section': None,
                    'from': label(o), 'to': label(n), 'type': 'removed' if not n else ('added' if not o else 'pin')})
    for key, v in sorted(c['images'].items()):
        o, n = v.get('-', {}), v.get('+', {})
        if not n:
            continue

        def ref(x):
            return ('%s@%s' % (x.get('tag', ''), short(x.get('digest', '')))).strip('@') if x else ''
        out.append({'kind': 'image', 'ecosystem': 'images', 'name': v.get('name', key), 'section': key,
                    'from': ref(o), 'to': ref(n),
                    'type': bump_type(o.get('tag') or short(o.get('digest')), n.get('tag') or short(n.get('digest')))})
    seen = set()
    unique = []
    for b in out:
        if (b['kind'], b['name']) not in seen:
            seen.add((b['kind'], b['name']))
            unique.append(b)
    return unique
