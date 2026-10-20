"""One-call triage of a dependency-update PR.

Usage: depledger triage '<PR_URL_OR_NUMBER>' [--out DIR] [--no-docker] [--no-git] [--no-record]

Fetches the PR once, parses the diff, triages buckets, checks lock completeness,
resolves action SHAs, checks image head-of-tag and per-arch manifests, looks every
package / digest / SHA up in the ledger database, and proposes a risk tier. Prints a compact
report; saves the PR JSON, body, diff and per-package release notes under --out. Records the
PR and what it moves in the ledger, so other repos can see it was looked at.

It runs in three stages. gather() makes every GitHub, registry, git and ledger call, starting each
one as soon as what it needs is in, so the slow round trips overlap instead of queueing. decide()
turns those facts into buckets, a tier and the checks to run, with no I/O. report() prints them.

Everything here is mechanical. Nothing it prints is a verdict.
"""
import argparse
import base64
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

from . import checks as checks_mod
from . import ci
from . import probe
from . import repo as repo_mod
from . import store
from .parse import (BUILD_CHAIN, HEX40, bump_type, bumps, collect_changes, coverage, coverage_note, fetch_pr,
                    is_major_like, lock_only_movers, out, parse_arg, parse_diff, release_notes,
                    release_notes_for, renovate_table, run, section, short)

# Enough for every image's three registry calls alongside the GitHub and git calls.
WORKERS = 24
NPM_SECTIONS = ('dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies', 'overrides', 'resolutions', 'engines')
COMPOSER_SECTIONS = ('require', 'require-dev')
FAILED = ('FAILURE', 'ERROR', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE')
PENDING = ('PENDING', 'IN_PROGRESS', 'QUEUED', 'EXPECTED', 'WAITING')


def cwd_repo():
    """(owner/name, toplevel) of the cwd, or ('', None)."""
    try:
        return repo_mod.current_repo()
    except repo_mod.RepoError:
        name = out(*run(['gh', 'repo', 'view', '--json', 'nameWithOwner', '-q', '.nameWithOwner'], timeout=30))
        return name, None


def compact_facts(row, limit=170):
    try:
        facts = json.loads(row.get('facts') or '{}')
    except ValueError:
        facts = {}
    parts = ['%s=%s' % (k, v) for k, v in facts.items() if v not in ('', '-', None)]
    text = '%s:%s %s %s' % (row['image'], row['tag'], row['digest12'], ' '.join(parts))
    if row.get('notes'):
        text += ' | ' + row['notes']
    return text if len(text) <= limit else text[:limit - 3] + '...'


def history_line(h, with_repo):
    verdict = h['verdict'] or ('no verdict' if h['review_id'] else 'triaged only')
    if h['verdict_text'] and h['verdict'] in ('not_safe', 'safe'):
        text = ' '.join(h['verdict_text'].split())
        verdict += ' "%s"' % (text if len(text) <= 90 else text[:87] + '...')
    when = (h['reviewed_at'] or h['triaged_at'] or '')[:10]
    where = '%s#%s' % (h['repo'], h['number']) if with_repo else '#%s' % h['number']
    return '%s %s %s -> %s %s%s' % (where, h['name'], h['from_version'] or '?', h['to_version'] or '?', verdict,
                                    (' (%s)' % when) if when else '')


# ---------------------------------------------------------------- the calls gather() runs in the pool

def head_file(repo, head, path, use_git):
    """A file on the PR head: from the fetched branch when the cwd is the repo, else through the API."""
    if use_git:
        rc, o, _ = run(['git', 'show', 'origin/%s:%s' % (head, path)], timeout=20)
        if rc == 0:
            return o
    rc, o, _ = run(['gh', 'api', 'repos/%s/contents/%s?ref=%s' % (repo, path, head), '--jq', '.content'], timeout=30)
    if rc == 0 and o.strip():
        try:
            return base64.b64decode(o).decode()
        except Exception:
            return ''
    return ''


def sections_of(manifest_text, keys):
    try:
        m = json.loads(manifest_text) if manifest_text else {}
    except Exception:
        return {}
    res = {}
    for key in keys:
        for pkg in (m.get(key) or {}):
            res.setdefault(pkg, key)
    return res


def resolve_tag(action_repo, tag):
    """(commit sha, tag object sha or '', None), or (None, '', error) for an action's tag. An
    annotated tag is followed through to its commit."""
    rc, o1, e1 = run(['gh', 'api', 'repos/%s/git/ref/tags/%s' % (action_repo, tag),
                      '--jq', '.object.sha + " " + .object.type'], timeout=30)
    if rc != 0:
        return None, '', (e1 or o1).strip()[:100]
    sha, typ = (o1.split() + [''])[:2]
    deref = ''
    if typ == 'tag':
        rc2, o2, _ = run(['gh', 'api', 'repos/%s/git/tags/%s' % (action_repo, sha), '--jq', '.object.sha'], timeout=30)
        if rc2 == 0:
            deref = sha
            sha = o2.strip()
    return sha, deref, None


def per_arch(raws):
    """{platform: {'old': manifest digest, 'new': ...}} from each side's `imagetools inspect --raw`."""
    per = {}
    for label, raw in raws:
        try:
            j = json.loads(raw)
            for mm in j.get('manifests', []):
                pl = mm.get('platform', {})
                plat_key = '%s/%s' % (pl.get('os'), pl.get('architecture'))
                if pl.get('os') == 'unknown':
                    continue
                per.setdefault(plat_key, {})[label] = mm['digest']
            if not j.get('manifests') and j.get('config'):
                per.setdefault('single-manifest', {})[label] = 'config:' + j['config'].get('digest', '')[7:19]
        except Exception:
            pass
    return per


def branch_state(head, base, paths):
    """The PR branch against its base: commits behind and ahead, the merge-tree exit code, and the
    files the base changed since the branch was cut that this PR changes too (None without a
    merge base)."""
    behind = out(*run(['git', 'rev-list', '--count', 'origin/%s..origin/%s' % (head, base)], timeout=20))
    ahead = out(*run(['git', 'rev-list', '--count', 'origin/%s..origin/%s' % (base, head)], timeout=20))
    rc, _, e = run(['git', 'merge-tree', '--write-tree', 'origin/' + base, 'origin/' + head], timeout=60)
    mb = out(*run(['git', 'merge-base', 'origin/' + base, 'origin/' + head], timeout=20))
    overlap = None
    if mb:
        _, o, _ = run(['git', 'diff', '--name-only', mb, 'origin/' + base], timeout=30)
        overlap = sorted(set(o.split()) & set(paths))
    return {'behind': behind, 'ahead': ahead, 'merge_rc': rc, 'merge_err': e, 'overlap': overlap}


def grep_hits(pattern_args, ref, *pathspec):
    """Files on a ref that git grep matches."""
    _, o, _ = run(['git', 'grep', '-l'] + list(pattern_args) + [ref] + (['--'] + list(pathspec) if pathspec else []), timeout=20)
    return [line.split(':', 1)[1] for line in o.splitlines()]


def environment():
    """The checkout triage runs in: Sail, the JS package manager and scripts, Docker files, git state."""
    uses_sail = os.path.exists('vendor/bin/sail')
    sail_up = False
    if uses_sail:
        _, o1, _ = run(['./vendor/bin/sail', 'ps'], timeout=30)
        sail_up = 'Up' in o1 or 'running' in o1
    js_pm, js_lock = 'none', 'none'
    for lock, pm in (('bun.lock', 'bun'), ('bun.lockb', 'bun'), ('pnpm-lock.yaml', 'pnpm'), ('yarn.lock', 'yarn'), ('package-lock.json', 'npm')):
        if os.path.exists(lock):
            js_pm, js_lock = pm, lock
            break
    subs = sorted(d for d in (os.listdir('packages') if os.path.isdir('packages') else [])
                  if os.path.exists(os.path.join('packages', d, 'package.json')))
    app_url = 'http://localhost'
    if os.path.exists('.env'):
        for line in open('.env', errors='ignore'):
            if line.startswith('APP_URL='):
                app_url = line.split('=', 1)[1].strip().strip('"') or app_url
    scripts = {}
    if os.path.exists('package.json'):
        try:
            scripts = json.load(open('package.json')).get('scripts', {})
        except Exception:
            scripts = {}
    docker_files = sorted(set([f for f in os.listdir('.') if f.startswith('Dockerfile') or f.startswith('docker-compose')] +
                              [os.path.join('docker', f) for f in (os.listdir('docker') if os.path.isdir('docker') else []) if f.startswith('Dockerfile') or f.startswith('docker-compose')]))
    return {'uses_sail': uses_sail, 'sail_up': sail_up, 'js_pm': js_pm, 'js_lock': js_lock, 'subs': subs, 'app_url': app_url,
            'scripts': scripts, 'docker_files': docker_files, 'has_composer': os.path.exists('composer.json'),
            'has_artisan': os.path.exists('artisan'),
            'git_clean': out(*run(['git', 'status', '--porcelain'], timeout=20)) == '',
            'cur_branch': out(*run(['git', 'branch', '--show-current'], timeout=10))}


def sibling_of(pkgs, name, o, n):
    """The listed package a lock-only mover moves in lockstep with, if any."""
    bare = name.rsplit('/', 1)[-1] if '/' in name and not name.startswith('@') else name
    scope = bare.split('/')[0] if bare.startswith('@') else ''
    for p in pkgs:
        pscope = p['name'].split('/')[0] if p['name'].startswith('@') else '@' + p['name']
        if scope and scope == pscope and (o, n) == (p['old'], p['new']):
            return p['name']
        if (o, n) == (p['old'], p['new']) and p['name'] != bare:
            return p['name']
    return None


def probe_platform(per):
    """The platform `depledger probe` should run both digests on, from the per-arch manifests triage
    read: amd64 first, then arm64, as probe.pick_platform chooses. '' for a single-manifest image
    (run on the default platform), None when triage read no manifests."""
    if not per:
        return None
    if 'single-manifest' in per:
        return ''
    plats = [p for p, dd in per.items() if 'new' in dd]
    for want in ('linux/amd64', 'linux/arm64'):
        if want in plats:
            return want
    return sorted(plats)[0] if plats else None


# ---------------------------------------------------------------- gather

def gather(pool, conn, repo, num, outdir, args, repo_match):
    """Every fact the report needs, as one dict. The PR, its diff and the repo's open PRs are
    fetched at once; CI, the git fetch, action tags and image manifests start as soon as the PR or
    diff they need is in; the git greps and merge checks start once the fetch lands."""
    use_git = repo_match and not args.no_git
    pr_job = pool.submit(fetch_pr, repo, num)
    diff_job = pool.submit(run, ['gh', 'pr', 'diff', num, '--repo', repo], timeout=90)
    open_job = pool.submit(ci.list_open_prs, repo)
    docker_job = None if args.no_docker else pool.submit(run, ['docker', 'info'], timeout=15)

    pr = pr_job.result()
    head, base = pr['headRefName'], pr['baseRefName']
    paths = [x['path'] for x in pr.get('files', [])]
    ci_job = pool.submit(ci.ci_steps, repo, pr['headRefOid']) if pr.get('headRefOid') and pr.get('statusCheckRollup') else None
    fetch_job = (pool.submit(run, ['git', 'fetch', '--quiet', 'origin', base, head], timeout=120)
                 if use_git and os.path.exists('.git') else None)
    f = {'repo': repo, 'num': num, 'outdir': outdir, 'repo_match': repo_match, 'pr': pr, 'paths': paths,
         'meta_path': os.path.join(outdir, 'pr.json'), 'body_path': os.path.join(outdir, 'body.md'),
         'diff_path': os.path.join(outdir, 'pr.diff')}
    body = pr.get('body') or ''
    with open(f['meta_path'], 'w') as fh:
        json.dump(pr, fh, indent=1)
    with open(f['body_path'], 'w') as fh:
        fh.write(body)

    rc, diff_text, e = diff_job.result()
    f['diff_warn'] = None if rc == 0 else e.strip()
    if rc != 0:
        diff_text = ''
    with open(f['diff_path'], 'w') as fh:
        fh.write(diff_text)
    changes = f['changes'] = collect_changes(parse_diff(diff_text))
    npm, composer, images, uses = changes['npm'], changes['composer'], changes['images'], changes['uses']
    f['table'] = renovate_table(body)
    f['notes'], f['truncated'] = release_notes(body, outdir)
    env_job = pool.submit(environment)

    # ---------------- registry calls for every image
    docker_ok = f['docker_ok'] = docker_job is not None and docker_job.result()[0] == 0
    inspect = {}
    for key, v in sorted(images.items()):
        name, o, n = v.get('name', key), v.get('-', {}), v.get('+', {})
        if not n:
            continue
        jobs = inspect[key] = {}
        if docker_ok and n.get('tag') and n.get('digest'):
            jobs['head'] = pool.submit(run, ['docker', 'buildx', 'imagetools', 'inspect', '%s:%s' % (name, n['tag'])], timeout=40)
        if docker_ok and o.get('digest') and n.get('digest'):
            for label, d in (('old', o['digest']), ('new', n['digest'])):
                jobs[label] = pool.submit(run, ['docker', 'buildx', 'imagetools', 'inspect', '%s@sha256:%s' % (name, d), '--raw'], timeout=40)

    # ---------------- ledger lookups, while those run
    index_rows = store.package_index(conn)
    f['traps'] = store.list_traps(conn)
    repo_row = store.find_repo(conn, repo)
    repo_id = f['repo_id'] = repo_row['id'] if repo_row else None
    known_pr = store.get_pr(conn, repo_id, num) if repo_id else None
    this_pr_id = known_pr['id'] if known_pr else None

    def name_patterns(row, name):
        return store.package_names(conn, row['id']) if row else [name]

    def prior_reviews(row, name):
        """This repo's earlier reviews that moved the package, falling back to a text match."""
        if not repo_id:
            return []
        hist = store.bump_history(conn, name_patterns(row, name), repo_id=repo_id, exclude_pr_id=this_pr_id, limit=3, per_pr=True)
        if hist:
            return [history_line(h, False) for h in hist]
        return ['#%s %s (%s)' % (m['number'], m['title'], (m['reviewed_at'] or '')[:10])
                for m in store.reviews_mentioning(conn, repo_id, name, exclude_pr_id=this_pr_id)]

    def elsewhere(row, name):
        hist = store.bump_history(conn, name_patterns(row, name), exclude_repo_id=repo_id, limit=3, per_pr=True)
        return [history_line(h, True) for h in hist]

    pkgs = f['pkgs'] = []
    for eco, found in (('npm', npm), ('composer', composer)):
        for name, v in sorted(found.items()):
            old, new = v.get('-', ''), v.get('+', '')
            row = store.lookup(index_rows, name, eco)
            pkgs.append({'eco': eco, 'name': name, 'old': old, 'new': new, 'type': bump_type(old, new), 'index': row,
                         'reviews': prior_reviews(row, name), 'elsewhere': elsewhere(row, name)})

    lock_movers = lock_only_movers(changes)
    f['lock_all'] = [(lock, key, o, n) for lock, key, _name, o, n in lock_movers]
    f['lock_name'] = dict(((lock, key), name) for lock, key, name, _o, _n in lock_movers)
    bun_lock = changes['bun_lock']
    f['nested_new'] = [k for k, v in bun_lock.items() if '/' in k.rsplit('@', 1)[0] and '+' in v and '-' not in v]
    f['nested_gone'] = [k for k, v in bun_lock.items() if '/' in k.rsplit('@', 1)[0] and '-' in v and '+' not in v]
    f['siblings'], f['lock_independent'], f['lock_rows'] = {}, [], {}
    for lock, name, o, n in sorted(f['lock_all']):
        sib = sibling_of(pkgs, name, o, n)
        if sib:
            f['siblings'].setdefault((sib, o, n), []).append(name)
        else:
            f['lock_independent'].append((lock, name, o, n))
            real = f['lock_name'].get((lock, name), name)
            f['lock_rows'][(lock, name)] = store.lookup(index_rows, real, 'npm' if lock == 'bun.lock' else 'composer')

    # ---------------- action tags not on record, resolved in the pool
    actions = f['actions'] = []
    for action, v in sorted(uses.items()):
        o, n = v.get('-'), v.get('+')
        a = {'action': action, 'old': o, 'new': n, 'row': None}
        actions.append(a)
        if not n:
            a['state'] = 'removed'
            continue
        a['repo'] = '/'.join(action.split('/')[:2])
        row = store.lookup(index_rows, a['repo'], 'actions')
        if row and row['ecosystem'] == 'actions':
            a['row'] = row
        if not HEX40.match(n['ref']):
            a['state'] = 'tag-pinned'
            continue
        a['tag'] = n['comment']
        a['known'] = store.find_action(conn, n['ref'], a['repo'])
        same = [k for k in a['known'] if k['sha'] == n['ref'] and (not a['tag'] or k['tag'] == a['tag'])]
        if same:
            a['state'], a['same'] = 'ledger', same[0]
        elif not a['tag']:
            a['state'] = 'no-comment'
        else:
            a['job'] = pool.submit(resolve_tag, a['repo'], a['tag'])

    f['image_rows'] = dict((key, store.lookup(index_rows, v.get('name', key), 'images')) for key, v in images.items())
    image_facts = f['images'] = {}
    for key, v in sorted(images.items()):
        name, o, n = v.get('name', key), v.get('-', {}), v.get('+', {})
        if not n:
            continue
        row = f['image_rows'][key]
        mentions = []
        for d in (o.get('digest'), n.get('digest')):
            revs = store.reviews_mentioning(conn, None, short(d), exclude_pr_id=this_pr_id) if d else []
            if revs:
                mentions.append((d, revs))
        image_facts[key] = {'row': row, 'mentions': mentions, 'elsewhere': elsewhere(row, name),
                            'hits': dict((label, store.find_digest(conn, d) if d else [])
                                         for label, d in (('old', o.get('digest')), ('new', n.get('digest'))))}

    rows = ([p['index'] for p in pkgs] + list(f['lock_rows'].values()) + [a['row'] for a in actions] +
            [im['row'] for im in image_facts.values()])
    f['exposure'] = dict((r['doc_id'], store.exposure_checks(conn, r['doc_id'])) for r in rows
                         if r and r['doc_id'] and r['status'] in ('CHECK', 'TRAP'))

    # ---------------- everything that reads the fetched branches
    git_ok = f['git_ok'] = False
    f['fetch_warn'] = None
    if fetch_job:
        rc, _, e = fetch_job.result()
        git_ok = f['git_ok'] = rc == 0
        if not git_ok:
            f['fetch_warn'] = e.strip()[:200]
    npm_job = pool.submit(head_file, repo, head, 'package.json', use_git) if npm else None
    composer_job = pool.submit(head_file, repo, head, 'composer.json', use_git) if composer else None
    greps = {}
    for key, v in sorted(images.items()):
        name, n = v.get('name', key), v.get('+', {})
        if git_ok and n.get('digest'):
            greps[key] = (pool.submit(grep_hits, ['-F', short(n['digest'])], 'origin/' + head, '.github/workflows'),
                          pool.submit(grep_hits, ['-E', r'^FROM\s+(--platform=\S+\s+)?' + re.escape(name) + r'[:@]'], 'origin/' + head))
    branch_job = pool.submit(branch_state, head, base, paths) if git_ok else None
    needles = []
    if git_ok:
        for p in pkgs:
            if p['new']:
                needles.append(('%s %s' % (p['name'], p['new']), '"%s": "%s"' % (p['name'], p['new'])))
        for key, v in images.items():
            d = v.get('+', {}).get('digest')
            if d:
                needles.append(('%s@%s' % (key, short(d)), short(d)))
        for action, v in uses.items():
            if '+' in v and HEX40.match(v['+']['ref']):
                needles.append(('%s@%s' % (action, v['+']['ref'][:12]), v['+']['ref'][:12]))
    needle_jobs = [(label, pool.submit(grep_hits, ['-F', needle], 'origin/' + base)) for label, needle in needles]
    prs, unavailable = open_job.result()
    new_digests = [v['+']['digest'] for v in images.values() if v.get('+', {}).get('digest')]
    other_job = (pool.submit(ci.other_open_prs, prs, num, pr.get('headRefOid') or '', base, paths, new_digests, git_ok)
                 if prs is not None else None)

    # ---------------- collect
    f['ci_lines'] = ci_job.result() if ci_job else None
    npm_sections = f['npm_sections'] = sections_of(npm_job.result(), NPM_SECTIONS) if npm_job else {}
    composer_sections = f['composer_sections'] = sections_of(composer_job.result(), COMPOSER_SECTIONS) if composer_job else {}
    for p in pkgs:
        p['section'] = (npm_sections if p['eco'] == 'npm' else composer_sections).get(p['name'], 'not in head manifest')
    for a in actions:
        if 'job' in a:
            a['sha'], a['deref'], a['error'] = a.pop('job').result()
            a['state'] = 'unresolved' if a['error'] else ('ok' if a['sha'] == a['new']['ref'] else 'mismatch')
    platforms = {}
    for key, im in image_facts.items():
        jobs = inspect[key]
        im['head_checked'] = 'head' in jobs
        if im['head_checked']:
            m = re.search(r'Digest:\s*sha256:([0-9a-f]{64})', jobs['head'].result()[1])
            im['head_digest'] = m.group(1) if m else None
        im['per'] = per_arch([(label, jobs[label].result()[1]) for label in ('old', 'new')]) if 'new' in jobs else None
        platform = probe_platform(im['per'])
        if platform is not None:
            platforms[key] = platform
        im['wf'], im['stacked'] = (greps[key][0].result(), greps[key][1].result()) if key in greps else (None, [])
    f['branch'] = branch_job.result() if branch_job else None
    f['base_hits'] = dict((label, job.result()) for label, job in needle_jobs if job.result())
    f['other_lines'], f['other_conflicts'] = other_job.result() if other_job else ([unavailable], [])
    f['env'] = env_job.result()
    if images:
        with open(os.path.join(outdir, 'probes.json'), 'w') as fh:
            json.dump(probe.pairs_from_images(images, platforms), fh, indent=1)
    return f


# ---------------------------------------------------------------- decide

def ci_rollup(pr):
    """([(check, workflow, conclusion)], failed names, pending names, whether renovate/artifacts failed)."""
    checks, failed, pending, artifacts_failed = [], [], [], False
    for c in pr.get('statusCheckRollup') or []:
        name = c.get('name') or c.get('context') or '?'
        concl = str(c.get('conclusion') or c.get('state') or c.get('status') or '?')
        checks.append((name, c.get('workflowName') or '', concl))
        if concl.upper() in FAILED:
            failed.append(name)
            if 'artifacts' in name:
                artifacts_failed = True
        elif concl.upper() in PENDING:
            pending.append(name)
    return checks, failed, pending, artifacts_failed


def decide(f):
    """Buckets, lock completeness, the proposed tier with its reasons, the checks it needs and what
    the repo notes are matched against, from gather()'s facts alone."""
    pr, pkgs, env, changes = f['pr'], f['pkgs'], f['env'], f['changes']
    images, uses, other_files = changes['images'], changes['uses'], changes['other_files']
    title = pr['title'].lower()
    paths = f['paths']
    bns = [os.path.basename(p) for p in paths]
    _, failed, pending, artifacts_failed = ci_rollup(pr)
    d = {'failed': failed, 'pending': pending, 'artifacts_failed': artifacts_failed,
         'lock_file_maintenance': 'lock file maintenance' in title}

    # ---------------- buckets and locks
    buckets = d['buckets'] = []
    npm_sects = set(p['section'] for p in pkgs if p['eco'] == 'npm')
    comp_sects = set(p['section'] for p in pkgs if p['eco'] == 'composer')
    js_touched = any(b in ('package.json', 'bun.lock', 'bun.lockb', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock') for b in bns) \
        or any(p.startswith('resources/') or b.startswith('vite.config') for p, b in zip(paths, bns))
    php_touched = any(b in ('composer.json', 'composer.lock') for b in bns)
    build_chain_hit = d['build_chain_hit'] = [p['name'] for p in pkgs if p['eco'] == 'npm' and p['name'].startswith(BUILD_CHAIN)]
    if js_touched:
        if npm_sects and npm_sects <= {'devDependencies', 'overrides', 'resolutions', 'engines'}:
            buckets.append('js-devtool')
        elif npm_sects:
            buckets.append('frontend')
        else:
            buckets.append('frontend (lock-only)')
    if php_touched:
        if comp_sects and comp_sects <= {'require-dev'}:
            buckets.append('php-devtool')
        elif comp_sects:
            buckets.append('php-runtime')
        else:
            buckets.append('php-runtime (lock-only)')
    if any(p.startswith('.github/') for p in paths) and uses:
        buckets.append('ci-actions')
    if images:
        buckets.append('docker-image')
    if any(b in ('.nvmrc', '.node-version') for b in bns) or 'engines' in npm_sects:
        buckets.append('node-pin')
    if any(b in ('Cargo.toml', 'Cargo.lock') for b in bns):
        buckets.append('cargo')
    if other_files:
        buckets.append('OTHER')
    lock_problems = d['lock_problems'] = []
    if 'package.json' in bns and not any(b in ('bun.lock', 'bun.lockb', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock') for b in bns):
        lock_problems.append('package.json changed without its lock')
    if 'composer.json' in bns and 'composer.lock' not in bns:
        lock_problems.append('composer.json changed without composer.lock')

    # ---------------- actions and images
    d['removed_actions'] = [a for a, v in uses.items() if '-' in v and '+' not in v]
    d['added_actions'] = [a for a, v in uses.items() if '+' in v and '-' not in v]
    action_problems = d['action_problems'] = [a['action'] for a in f['actions'] if a['state'] == 'mismatch']
    if d['removed_actions'] or d['added_actions']:
        action_problems.extend(d['added_actions'])
    action_unresolved = d['action_unresolved'] = [a['action'] for a in f['actions'] if a['state'] in ('no-comment', 'unresolved')]
    action_rows = dict((a['action'], a['row']) for a in f['actions'] if a['row'])
    image_needs_probe = d['image_needs_probe'] = [k for k, im in sorted(f['images'].items())
                                                  if images[k]['+'].get('digest') and not im['hits']['new']]
    image_ci_blind = d['image_ci_blind'] = [k for k, im in sorted(f['images'].items()) if im['wf'] is not None and not im['wf']]
    image_stacked = d['image_stacked'] = [k for k, im in sorted(f['images'].items()) if im['stacked']]
    d['image_only_in_workflows'] = bool(images) and all(p.startswith('.github/') for fs in changes['image_files'].values() for p in fs)

    # ---------------- branch
    b = f['branch']
    conflicts = d['conflicts'] = (b['merge_rc'] == 1) if b else None
    overlap = d['overlap'] = (b['overlap'] or []) if b else []
    behind, ahead = (b['behind'], b['ahead']) if b else (None, None)

    # ---------------- tier
    tier = 0
    reasons = d['reasons'] = []

    def bump(level, why):
        nonlocal tier
        tier = max(tier, level)
        reasons.append('T%d: %s' % (level, why))

    lock_only = f['lock_independent']
    if lock_problems:
        bump(2, 'lock incomplete')
    if (pr.get('mergeStateStatus') or '').upper() in ('CONFLICTING', 'DIRTY') or conflicts:
        bump(2, 'branch conflicts with base: rebase first')
    if failed:
        bump(2, 'CI failed: %s' % ','.join(failed))
    if pending:
        bump(1, 'CI still pending: %s' % ','.join(pending))
    if 'OTHER' in buckets:
        bump(2, 'touches non-dependency files')
    if other_files == [] and not pkgs and not images and not uses and not lock_only and not d['lock_file_maintenance']:
        bump(1, 'parsed nothing recognisable from the diff; read it by hand')
    for p in pkgs:
        idx = p['index']
        if is_major_like(p['type']):
            bump(2, '%s %s is %s' % (p['name'], p['new'], p['type']))
        if idx and idx['status'] == 'TRAP':
            bump(2, '%s is a TRAP row' % p['name'])
        elif idx and idx['status'] == 'CHECK':
            bump(1, '%s is a CHECK row (re-run its exposure check)' % p['name'])
        elif not idx and p['type'].startswith('minor') and p['section'] in ('dependencies', 'require'):
            bump(1, '%s: unknown runtime package, minor bump' % p['name'])
        elif idx and p['type'] != 'digest' and coverage(p['new'], idx['analysed']) in ('past', 'other-line') and p['type'].startswith('minor') and p['section'] in ('dependencies', 'require'):
            bump(1, '%s %s is %s the analysed %s (runtime, minor)' % (
                p['name'], p['new'], 'past' if coverage(p['new'], idx['analysed']) == 'past' else 'on another line than', idx['analysed']))
    for lock, name, o, n in lock_only:
        row = f['lock_rows'].get((lock, name))
        if is_major_like(bump_type(o, n)):
            bump(2, 'lock-only transitive %s %s -> %s is major-like' % (name, o, n))
        elif bump_type(o, n).startswith('minor'):
            bump(1, 'lock-only transitive %s %s -> %s (minor, unlisted by Renovate)' % (name, o, n))
        if row and row['status'] == 'TRAP':
            bump(2, 'lock-only transitive %s is a TRAP row' % name)
        elif row and row['status'] == 'CHECK':
            bump(1, 'lock-only transitive %s is a CHECK row (re-run its exposure check)' % name)
    for action, row in sorted(action_rows.items()):
        if row['status'] == 'TRAP':
            bump(2, 'action %s is a TRAP row' % action)
        elif row['status'] == 'CHECK':
            bump(1, 'action %s is a CHECK row (re-run its exposure check)' % action)
    if action_problems:
        bump(2, 'action SHA mismatch / identity change: %s' % ','.join(action_problems))
    if action_unresolved:
        bump(1, 'action SHA not resolved: %s' % ','.join(action_unresolved))
    for key in images:
        v = images[key]
        name = v.get('name', key)
        row = f['image_rows'][key]
        if row and row['status'] == 'TRAP':
            bump(2, 'image %s is a TRAP row' % name)
        o, n = v.get('-', {}), v.get('+', {})
        if o.get('tag') and n.get('tag') and o['tag'] != n['tag']:
            t = bump_type(o['tag'], n['tag'])
            if is_major_like(t):
                bump(2, 'image %s tag %s -> %s is %s' % (name, o['tag'], n['tag'], t))
            elif t.startswith('minor'):
                bump(1, 'image %s tag %s -> %s (minor)' % (name, o['tag'], n['tag']))
    if image_needs_probe:
        bump(1, 'new digest not on record, probe it: %s' % ','.join(image_needs_probe))
    if image_ci_blind:
        bump(1, 'CI never runs the new image: %s' % ','.join(image_ci_blind))
    if image_stacked:
        bump(1, 'repo stacks layers on: %s' % ','.join(image_stacked))
    if overlap and not conflicts:
        bump(1, 'base moved in the same files since the branch was cut: test the merge result')
    if f['nested_new']:
        bump(1, 'new nested bun.lock copies appeared: triage benign vs troublesome')
    if not reasons:
        reasons.append('T0: all patch/minor-devtool, routine or unknown-patch rows, lock complete, CI green, no image/action concerns')
    d['tier'] = tier

    # ---------------- checks
    scripts, subs = env['scripts'], env['subs']
    checks = d['checks'] = []
    if any(b.startswith('frontend') for b in buckets) or 'js-devtool' in buckets:
        checks.append('install-js(frozen)')
    if any(b.startswith('frontend') for b in buckets) or build_chain_hit:
        checks.append('build')
    if any(b.startswith('frontend') for b in buckets) or 'js-devtool' in buckets:
        if scripts.get('typecheck') or scripts.get('tsc'):
            checks.append('typecheck')
        if scripts.get('lint'):
            checks.append('lint(no --fix)')
    if 'frontend' in buckets and tier >= 1:
        checks.append('chrome')
    if any(b.startswith('php') for b in buckets):
        checks.append('install-php')
        checks.append('tests')
    if 'php-runtime' in buckets and tier >= 1:
        checks.append('chrome(boot)')
    if subs and any(b.startswith('frontend') or b == 'js-devtool' for b in buckets):
        checks.append('submodule-dup')
    if image_needs_probe:
        checks.append('probe-image')
    if image_stacked:
        checks.append('rebuild-stacked')
    if any(p['name'] in ('aws/aws-sdk-php',) or p['name'].startswith('league/flysystem') for p in pkgs):
        checks.append('s3-roundtrip')
    if overlap and not conflicts:
        checks.append('merge-result-install')
    d['needs_checkout'] = any(c for c in checks if c not in ('probe-image', 'rebuild-stacked'))

    # ---------------- what repo notes are matched against
    lock_name = f['lock_name']
    names = [p['name'] for p in pkgs] + list(images) + [v.get('name', '') for v in images.values()] + list(uses)
    names += [lock_name.get((lock, key), key) for lock, key, _o, _n in lock_only] + [key for _lock, key, _o, _n in lock_only]
    signals = [(pr.get('mergeStateStatus') or '').lower()]
    if ahead and ahead.isdigit() and int(ahead) > 1:
        signals.append('ahead')
    if behind and behind.isdigit() and int(behind) > 0:
        signals.append('behind')
    if conflicts:
        signals.append('conflicts')
    if overlap:
        signals.append('base_overlap')
    if lock_problems:
        signals.append('lock_ok=no')
    if failed:
        signals.append('ci_failed')
    if artifacts_failed:
        signals.append('renovate/artifacts')
    d['note_context'] = {'buckets': buckets, 'names': [x for x in names if x],
                         'text': ' '.join([pr['headRefName'], pr['title']] + signals + names).lower()}
    return d


# ---------------------------------------------------------------- report

def report(f, d, plan, repo_notes):
    """Print every section up to the repo notes. repo_notes: store.notes_for()'s (always, matched,
    hidden), or None when the repo is not registered yet."""
    pr, repo, num, changes = f['pr'], f['repo'], f['num'], f['changes']
    head, base = pr['headRefName'], pr['baseRefName']
    images, uses, other_files = changes['images'], changes['uses'], changes['other_files']
    author = (pr.get('author') or {}).get('login', '')
    shown_docs = set()

    def print_ledger_detail(row, indent='    '):
        """Gist, and for CHECK/TRAP rows the banked exposure checks, once per doc."""
        print('%sgist: %s' % (indent, row['gist']))
        if row['status'] not in ('CHECK', 'TRAP'):
            return
        detail = 'depledger show package %s' % json.dumps(row['name']) if ' ' in row['name'] else 'depledger show package %s' % row['name']
        if not row['doc_id']:
            print('%sexposure check: none banked (re-derive it, then `depledger package exposure`)' % indent)
            return
        if row['doc_id'] in shown_docs:
            print('%sexposure check: same doc as above (%s)' % (indent, row['doc_slug']))
            return
        shown_docs.add(row['doc_id'])
        checks = f['exposure'].get(row['doc_id'], [])
        if not checks:
            print('%sexposure check: none banked; the analysis is in `%s`' % (indent, detail))
            return
        for c in checks:
            lines = c['commands'].rstrip('\n').splitlines()
            print('%sexposure check%s:' % (indent, (' (%s)' % c['label']) if c['label'] else ''))
            for line in lines[:12]:
                print('%s    %s' % (indent, line))
            if len(lines) > 12:
                print('%s    ... %d more lines in `%s`' % (indent, len(lines) - 12, detail))
        print('%sanalysis: `%s`' % (indent, detail))

    # ---------------- PR
    if f['diff_warn'] is not None:
        print('WARN: gh pr diff failed: ' + f['diff_warn'])
    section('PR')
    print('PR_REPO=%s' % repo)
    print('PR_NUMBER=%s' % num)
    print('TITLE=%s' % pr['title'])
    print('AUTHOR=%s  (bot-shaped branch: %s)' % (author, 'yes' if head.startswith(('renovate/', 'dependabot/')) else 'NO'))
    print('HEAD=%s' % head)
    print('BASE=%s' % base)
    print('STATE=%s  draft=%s' % (pr.get('state'), pr.get('isDraft')))
    print('MERGE_STATE=%s  mergeable=%s' % (pr.get('mergeStateStatus'), pr.get('mergeable')))
    print('FILES=%s' % ', '.join(f['paths']))
    print('SAVED: META_FILE=%s  BODY_FILE=%s  DIFF_FILE=%s' % (f['meta_path'], f['body_path'], f['diff_path']))

    # ---------------- CI
    section('CI (statusCheckRollup)')
    for name, wf, concl in ci_rollup(pr)[0]:
        print('- %s%s: %s' % (name, ' [%s]' % wf if wf else '', concl))
    if not (pr.get('statusCheckRollup') or []):
        print('- (no checks reported)')
    print('CI_FAILED=%s' % (','.join(d['failed']) or 'none'))
    print('CI_PENDING=%s' % (','.join(d['pending']) or 'none'))
    if f['ci_lines'] is not None:
        print('CI steps on head %s (longest real steps; a short or skipped step is a blind spot):' % pr['headRefOid'][:8])
        for line in f['ci_lines']:
            print(line)
    if d['artifacts_failed']:
        print('NOTE: renovate/artifacts failed -> the lock was probably not generated; all-red frozen installs downstream are NOT test failures.')
    if f['fetch_warn'] is not None:
        print('WARN: git fetch failed: ' + f['fetch_warn'])

    # ---------------- packages
    section('Packages (from the manifest diff)')
    if not f['pkgs']:
        print('(no manifest changes)')
    for p in f['pkgs']:
        idx = p['index']
        status = ('%s @%s%s' % (idx['status'], idx['analysed'], coverage_note(p['new'], idx['analysed']))) if idx else 'not in ledger'
        print('- %s %s: %s -> %s  [%s] section=%s  ledger=%s' % (p['eco'], p['name'], p['old'], p['new'], p['type'], p['section'], status))
        if idx:
            print_ledger_detail(idx)
        if p['reviews']:
            print('    prior reviews here: %s' % ' | '.join(p['reviews']))
        if p['elsewhere']:
            print('    elsewhere: %s' % ' | '.join(p['elsewhere']))
        n = release_notes_for(p['name'], f['notes'])
        if n:
            print('    release notes: %d lines, %d breaking-ish keyword hits -> %s' % (n[0], n[1], n[2]))
        else:
            print('    release notes: none in body')
    if f['truncated']:
        print('NOTE: body says release notes were truncated; fetch the missing changelogs.')

    # ---------------- lock-only movers
    section('Lock-only movers (transitives Renovate does not list)')
    if not f['lock_all']:
        print('(none)')
    for (sib, o, n), names in f['siblings'].items():
        print('- lockstep siblings of %s (%s -> %s): %s' % (sib, o, n, ', '.join(names)))
    for lock, name, o, n in f['lock_independent']:
        row = f['lock_rows'][(lock, name)]
        tag = (' ledger=%s @%s%s' % (row['status'], row['analysed'], coverage_note(n, row['analysed']))) if row else ''
        print('- %s %s: %s -> %s [%s]%s' % (lock, name, o, n, bump_type(o, n), tag))
        if row and row['status'] in ('CHECK', 'TRAP'):
            print_ledger_detail(row)
    nested_new, nested_gone = f['nested_new'], f['nested_gone']
    if nested_new:
        print('NEW nested bun.lock entries (second copies): %s' % ', '.join(sorted(nested_new)[:12]) + (' ...' if len(nested_new) > 12 else ''))
    if nested_gone:
        print('REMOVED nested bun.lock entries (deduped): %s' % ', '.join(sorted(nested_gone)[:12]))

    # ---------------- Renovate's own table, for cross-check
    section('Renovate table (body)')
    if not f['table']:
        print('(no package table found in body)')
    for r in f['table']:
        extra = ' update=%s' % r['update'] if r['update'] else ''
        extra += ' type=%s' % r['type'] if r['type'] else ''
        print('- %s: %s -> %s%s' % (r['pkg'], r['old'], r['new'], extra))

    # ---------------- actions
    section('GitHub Actions `uses:` changes')
    if not uses:
        print('(none)')
    for a in f['actions']:
        action, o, n, state = a['action'], a['old'], a['new'], a['state']
        if state == 'removed':
            print('- %s: removed (%s)' % (action, o['ref'] if o else ''))
            continue
        print('- %s: %s %s -> %s %s' % (action, (o['ref'][:12] if o else '(new)'), (o['comment'] if o else ''), n['ref'][:12], n['comment']))
        if a['row']:
            print('    ledger=%s @%s' % (a['row']['status'], a['row']['analysed']))
            print_ledger_detail(a['row'])
        if state == 'tag-pinned':
            print('    pinned by tag, not SHA')
            continue
        if state == 'ledger':
            same = a['same']
            print('    on record: %s %s = %s (%s)  -> pass (ledger)' % (a['repo'], same['tag'], same['sha'][:12], same['tag_type'] or 'type not recorded'))
            continue
        if a['known']:
            print('    SHA on record as %s, not %s: resolving the comment tag' % (', '.join(k['tag'] for k in a['known']), a['tag'] or '(no comment)'))
        if state == 'no-comment':
            print('    no version comment on the pin; resolve by hand')
            continue
        if state == 'unresolved':
            print('    tag %s could not be resolved (%s)' % (a['tag'], a['error']))
            continue
        sha, deref = a['sha'], a['deref']
        print('    %s resolves to %s%s -> %s' % (a['tag'], sha[:12], (' (annotated %s)' % deref[:12]) if deref else '', 'OK' if state == 'ok' else 'MISMATCH'))
        if state == 'ok':
            print('    bank with: depledger action add %s %s %s%s' % (a['repo'], a['tag'], sha, (' --annotated %s' % deref) if deref else ' --type commit'))
    if d['removed_actions'] or d['added_actions']:
        print('ACTION_IDENTITY_CHANGED: removed=%s added=%s  (fork swap?)' % (d['removed_actions'], d['added_actions']))

    # ---------------- images
    section('Container images')
    if not images:
        print('(none)')
    for key, im in sorted(f['images'].items()):
        v = images[key]
        name, o, n = v.get('name', key), v.get('-', {}), v.get('+', {})
        print('- %s  files=%s' % (key, ','.join(sorted(set(changes['image_files'].get(key, []))))))
        print('    tag: %s -> %s   digest: %s -> %s   [%s]' % (o.get('tag', ''), n.get('tag', ''), short(o.get('digest')), short(n.get('digest')), bump_type(o.get('tag') or short(o.get('digest')), n.get('tag') or short(n.get('digest')))))
        row = im['row']
        if row:
            print('    ledger=%s @%s' % (row['status'], row['analysed']))
            print_ledger_detail(row)
        for label, dg in (('old', o.get('digest')), ('new', n.get('digest'))):
            hits = im['hits'][label]
            print('    digest %s %s: %s' % (label, short(dg), compact_facts(hits[0]) if hits else 'not on record'))
        for dg, revs in im['mentions']:
            print('    prior reviews (%s): %s' % (short(dg), ' | '.join('%s#%s %s' % (r['repo'], r['number'], (r['reviewed_at'] or '')[:10]) for r in revs)))
        for line in im['elsewhere']:
            print('    elsewhere: %s' % line)
        if im['head_checked']:
            if im['head_digest']:
                print('    head-of-tag %s:%s = %s -> %s' % (name, n['tag'], short(im['head_digest']), 'OK (new digest is head)' if im['head_digest'] == n['digest'] else 'STALE (tag has moved on; new digest is not head)'))
            else:
                print('    head-of-tag: imagetools inspect gave no digest')
        if im['per']:
            parts = []
            same_any = False
            for plat, dd in sorted(im['per'].items()):
                if 'old' in dd and 'new' in dd:
                    same = dd['old'] == dd['new']
                    same_any = same_any or same
                    parts.append('%s %s' % (plat, 'IDENTICAL' if same else 'changed'))
                else:
                    parts.append('%s only-in-%s' % (plat, 'new' if 'new' in dd else 'old'))
            print('    per-arch manifests: %s' % ', '.join(parts))
            if same_any:
                print('    -> identical manifest on a platform means no content change there: no probe needed for it')
        if im['wf'] is not None:
            print('    CI runs the new digest: %s' % (', '.join(im['wf']) if im['wf'] else 'NO workflow on the branch pins it -> green CI says nothing about this image'))
            if im['stacked']:
                print('    Dockerfiles with FROM %s: %s  (stacked layers -> local rebuild when the probe says packages moved; CI never builds these)' % (name, ', '.join(im['stacked'])))
    if d['image_needs_probe']:
        print("PROBE: depledger probe --from '%s'   (new digests not on record: %s; probes in parallel, banks facts, skips digests on record)"
              % (f['outdir'], ','.join(d['image_needs_probe'])))
    if d['image_only_in_workflows']:
        print('NOTE: every image change sits in .github/ -> a docker pin wearing a ci-actions path; treat as docker-image, SHA-pin resolution N/A.')

    # ---------------- buckets and locks
    section('Buckets and lock completeness')
    print('BUCKETS=%s' % (','.join(d['buckets']) or 'none'))
    if d['build_chain_hit']:
        print('BUILD_CHAIN_PACKAGES=%s  (devDeps that change shipped output: keep the build)' % ','.join(d['build_chain_hit']))
    if other_files:
        print('NON_DEPENDENCY_FILES=%s  (not a pure dependency PR?)' % ','.join(other_files))
    if d['lock_file_maintenance']:
        print('LOCK_FILE_MAINTENANCE=yes  (no manifest diff expected; breaking-change scan N/A; install+build+tests are the signal)')
    lock_problems = d['lock_problems']
    print('LOCK_OK=%s%s' % ('no' if lock_problems else 'yes', ('  ' + '; '.join(lock_problems)) if lock_problems else ''))

    # ---------------- git state vs base
    section('Branch vs base')
    b = f['branch']
    if b is not None:
        behind, ahead, overlap, conflicts = b['behind'], b['ahead'], d['overlap'], d['conflicts']
        print('BEHIND_BASE=%s commits  AHEAD=%s commits%s' % (behind, ahead, '  (more than 1 ahead: orphaned/rebased base? inspect git log)' if ahead and ahead.isdigit() and int(ahead) > 1 else ''))
        print('MERGE_TREE=%s' % ('CONFLICTS' if conflicts else ('clean' if b['merge_rc'] == 0 else 'unavailable (%s)' % b['merge_err'].strip()[:80])))
        if b['overlap'] is not None:
            print('BASE_TOUCHED_SAME_FILES_SINCE_BRANCH=%s' % (','.join(overlap) if overlap else 'none'))
            if overlap and not conflicts:
                print('    -> logical overlap with a clean textual merge: CI ran against a base that has since moved in these files. Test the merge result, not the branch.')
        if f['base_hits']:
            print('ALREADY_ON_BASE (prior validation by a sibling PR):')
            for k, v in f['base_hits'].items():
                print('    %s -> %s' % (k, ', '.join(v)))
        else:
            print('ALREADY_ON_BASE=none')
    else:
        print('(git checks skipped: %s)' % ('repo mismatch, cwd is %s' % (f['cur_repo'] or 'not a gh repo') if not f['repo_match'] else 'fetch failed or --no-git'))

    # ---------------- other open PRs
    section('Other open PRs on the same files')
    for line in f['other_lines']:
        print(line)
    print('OTHER_PR_CONFLICTS=%s' % (','.join('#%s' % n for n in f['other_conflicts']) or 'none'))

    # ---------------- environment
    section('Environment')
    env = f['env']
    scripts = env['scripts']
    print('REPO_MATCH=%s  (cwd repo: %s)' % ('yes' if f['repo_match'] else 'NO', f['cur_repo'] or 'none'))
    print('USES_SAIL=%s  SAIL_UP=%s' % (('yes' if env['uses_sail'] else 'no'), ('yes' if env['sail_up'] else 'no')))
    print('JS_PM=%s  JS_LOCK=%s  HAS_COMPOSER=%s  JS_SUBPACKAGES=%s' % (env['js_pm'], env['js_lock'], 'yes' if env['has_composer'] else 'no', ','.join(env['subs']) or 'none'))
    print('SCRIPTS: build=%s  typecheck=%s  lint=%s' % (scripts.get('build', '-'), scripts.get('typecheck', scripts.get('tsc', '-')), scripts.get('lint', '-')))
    if '--fix' in str(scripts.get('lint', '')):
        print('    lint script carries --fix: run eslint directly without it for a review')
    print('APP_URL=%s  DOCKER_FILES=%s  HOST_ARCH=%s  DOCKER=%s' % (env['app_url'], ','.join(env['docker_files']) or 'none', os.uname().machine, 'up' if f['docker_ok'] else 'down/skipped'))
    print('GIT_CLEAN=%s  CURRENT_BRANCH=%s' % ('yes' if env['git_clean'] else 'NO', env['cur_branch']))

    # ---------------- tier
    section('Proposed tier')
    print('TIER=%d' % d['tier'])
    for r in d['reasons']:
        print('- ' + r)
    print('CHECKS=%s' % (','.join(d['checks']) or 'none (diff read + CI only)'))
    print('NEEDS_CHECKOUT=%s' % ('yes' if d['needs_checkout'] else 'no'))
    if plan:
        runs = [s['name'] for s in plan['steps'] if s['selected']]
        print("RUN_CHECKS: depledger checks --from '%s' --checkout   (runs %s%s)" % (
            f['outdir'], ', '.join(runs) or 'nothing automatable', '; by hand: ' + ', '.join(plan['manual']) if plan['manual'] else ''))

    if f['traps']:
        section('Standing traps (symptom-keyed; `depledger show pattern <slug>` for the detail)')
        for t in f['traps']:
            print('- %s%s' % (t['headline'], ('  [pattern: %s]' % t['doc_slug']) if t['doc_slug'] else ''))

    # ---------------- repo notes
    section('Repo notes (%s)' % repo)
    if repo_notes is None:
        print('(no notes: this run registers %s; bank facts with `depledger note add --repo %s`)' % (repo, repo))
        return
    always, matched, hidden = repo_notes
    for nt in always:
        print('- [%s] %s%s%s' % (nt['slug'] or nt['id'], nt['gist'], '  (unverified)' if nt['unverified'] else '',
                                 '  (detail: depledger show note %s)' % (nt['slug'] or nt['id']) if nt['body'] else ''))
    if not always:
        print('(no standing notes)')
    for nt in [n for n in matched if not n['body']]:
        print('- [%s] %s  (when: %s)%s' % (nt['id'], nt['gist'], nt['when_tags'], '  (unverified)' if nt['unverified'] else ''))
    for nt in [n for n in matched if n['body']]:
        print('\n### [%s] %s  (when: %s)%s' % (nt['slug'] or nt['id'], nt['gist'], nt['when_tags'], '  (unverified)' if nt['unverified'] else ''))
        print(nt['body'].rstrip('\n'))
    topics = [nt for nt in hidden if nt['body']]
    if hidden:
        print('\nNOT_MATCHED=%d notes tagged for other PRs%s' % (len(hidden), (': ' + ', '.join(
            '%s (when: %s)' % (nt['slug'] or nt['id'], nt['when_tags']) for nt in topics)) if topics else ''))


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog='depledger triage')
    ap.add_argument('pr')
    ap.add_argument('--out', default=None, help='scratch dir for saved PR json/body/diff/notes')
    ap.add_argument('--no-docker', action='store_true')
    ap.add_argument('--no-git', action='store_true')
    ap.add_argument('--no-record', action='store_true', help='do not record the PR in the ledger')
    args = ap.parse_args(argv)

    cur_repo, cur_top = cwd_repo()
    repo, num = parse_arg(args.pr, cur_repo)
    repo_match = bool(cur_repo) and cur_repo.lower() == repo.lower()
    outdir = args.out or os.path.join(os.environ.get('TMPDIR', '/tmp'), 'depledger',
                                      repo.replace('/', '-') + '-' + num)
    os.makedirs(outdir, exist_ok=True)

    conn = store.connect()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        f = gather(pool, conn, repo, num, outdir, args, repo_match)
    f['cur_repo'] = cur_repo
    d = decide(f)
    plan = None
    if d['needs_checkout'] and repo_match:
        env = f['env']
        plan_env = {'top': cur_top or os.getcwd(), 'pm': env['js_pm'], 'lock': env['js_lock'] if env['js_lock'] != 'none' else None,
                    'scripts': env['scripts'], 'uses_sail': env['uses_sail'], 'sail_up': env['sail_up'],
                    'has_composer': env['has_composer'], 'has_artisan': env['has_artisan']}
        plan = checks_mod.write_plan(outdir, checks_mod.plan(plan_env, d['checks'], [p['name'] for p in f['pkgs']]),
                                     {'repo': repo, 'number': num, 'head_sha': f['pr'].get('headRefOid'), 'head_ref': f['pr']['headRefName']})
    repo_notes = store.notes_for(conn, f['repo_id'], d['note_context']) if f['repo_id'] else None
    report(f, d, plan, repo_notes)

    # ---------------- write-back
    if not args.no_record:
        pr = f['pr']
        pr_fields = {'title': pr['title'], 'branch': pr['headRefName'], 'base': pr['baseRefName'], 'url': pr.get('url'),
                     'author': (pr.get('author') or {}).get('login', ''), 'state': pr.get('state'), 'head_sha': pr.get('headRefOid'),
                     'opened_at': pr.get('createdAt'), 'merged_at': pr.get('mergedAt'), 'proposed_tier': d['tier']}
        sections = dict((('npm', k), v) for k, v in f['npm_sections'].items())
        sections.update((('composer', k), v) for k, v in f['composer_sections'].items())
        try:
            store.record_triage(conn, repo, num, pr_fields, bumps(f['changes'], sections),
                                local_path=cur_top if repo_match else None)
            store.export(conn)
        except Exception as e:  # noqa: a ledger write must never cost the triage output
            print('WARN: could not record this triage in the ledger: %s' % e)
    section('Write-back')
    print('REVIEW_KEY=%s#%s' % (repo, num))
    print('save the verdict: depledger review save %s#%s --verdict safe|not-safe --tier N [--verdict-text "..."] < entry.md' % (repo, num))
