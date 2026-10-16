"""One-call triage of a dependency-update PR.

Usage: depledger triage '<PR_URL_OR_NUMBER>' [--out DIR] [--no-docker] [--no-git] [--no-record]

Fetches the PR once, parses the diff, triages buckets, checks lock completeness,
resolves action SHAs, checks image head-of-tag and per-arch manifests, looks every
package / digest / SHA up in the ledger database, and proposes a risk tier. Prints a compact
report; saves the PR JSON, body, diff and per-package release notes under --out. Records the
PR and what it moves in the ledger, so other repos can see it was looked at.

Everything here is mechanical. Nothing it prints is a verdict.
"""
import argparse
import base64
import json
import os
import re

from . import repo as repo_mod
from . import store
from .parse import (BUILD_CHAIN, HEX40, bump_type, bumps, collect_changes, fetch_pr, is_major_like,
                    lock_only_movers, out, parse_arg, parse_diff, release_notes, renovate_table, run,
                    section, short, ver_parts)

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

    pr = fetch_pr(repo, num)
    body = pr.get('body') or ''
    meta_path = os.path.join(outdir, 'pr.json')
    body_path = os.path.join(outdir, 'body.md')
    diff_path = os.path.join(outdir, 'pr.diff')
    json.dump(pr, open(meta_path, 'w'), indent=1)
    open(body_path, 'w').write(body)
    rc, diff_text, e = run(['gh', 'pr', 'diff', num, '--repo', repo], timeout=90)
    if rc != 0:
        diff_text = ''
        print('WARN: gh pr diff failed: ' + e.strip())
    open(diff_path, 'w').write(diff_text)
    files = parse_diff(diff_text)
    head, base = pr['headRefName'], pr['baseRefName']
    author = (pr.get('author') or {}).get('login', '')

    # ---------------- PR
    section('PR')
    print('PR_REPO=%s' % repo)
    print('PR_NUMBER=%s' % num)
    print('TITLE=%s' % pr['title'])
    print('AUTHOR=%s  (bot-shaped branch: %s)' % (author, 'yes' if head.startswith(('renovate/', 'dependabot/')) else 'NO'))
    print('HEAD=%s' % head)
    print('BASE=%s' % base)
    print('STATE=%s  draft=%s' % (pr.get('state'), pr.get('isDraft')))
    print('MERGE_STATE=%s  mergeable=%s' % (pr.get('mergeStateStatus'), pr.get('mergeable')))
    print('FILES=%s' % ', '.join(f['path'] for f in pr.get('files', [])))
    print('SAVED: META_FILE=%s  BODY_FILE=%s  DIFF_FILE=%s' % (meta_path, body_path, diff_path))

    # ---------------- CI
    section('CI (statusCheckRollup)')
    failed, pending, artifacts_failed = [], [], False
    for c in pr.get('statusCheckRollup') or []:
        name = c.get('name') or c.get('context') or '?'
        concl = c.get('conclusion') or c.get('state') or c.get('status') or '?'
        concl = str(concl)
        wf = c.get('workflowName') or ''
        print('- %s%s: %s' % (name, ' [%s]' % wf if wf else '', concl))
        if concl.upper() in ('FAILURE', 'ERROR', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE'):
            failed.append(name)
            if 'artifacts' in name:
                artifacts_failed = True
        elif concl.upper() in ('PENDING', 'IN_PROGRESS', 'QUEUED', 'EXPECTED', 'WAITING'):
            pending.append(name)
    if not (pr.get('statusCheckRollup') or []):
        print('- (no checks reported)')
    print('CI_FAILED=%s' % (','.join(failed) or 'none'))
    print('CI_PENDING=%s' % (','.join(pending) or 'none'))
    if artifacts_failed:
        print('NOTE: renovate/artifacts failed -> the lock was probably not generated; all-red frozen installs downstream are NOT test failures.')

    # ---------------- packages from the Renovate table and from the diff
    table = renovate_table(body)
    notes, truncated = release_notes(body, outdir)

    changes = collect_changes(files)
    npm, composer = changes['npm'], changes['composer']
    bun_lock, uses = changes['bun_lock'], changes['uses']
    images, image_files = changes['images'], changes['image_files']   # name -> {'-':..., '+':...}, merged across files
    other_files = changes['other_files']

    # head manifests for section lookup
    def head_file(path):
        if repo_match and not args.no_git:
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

    # fetch refs first so head_file can use git
    git_ok = False
    if repo_match and not args.no_git and os.path.exists('.git'):
        rc, _, e = run(['git', 'fetch', '--quiet', 'origin', base, head], timeout=120)
        git_ok = rc == 0
        if not git_ok:
            print('WARN: git fetch failed: ' + e.strip()[:200])

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

    npm_sections = sections_of(head_file('package.json'), ('dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies', 'overrides', 'resolutions', 'engines')) if npm else {}
    composer_sections = sections_of(head_file('composer.json'), ('require', 'require-dev')) if composer else {}

    conn = store.connect()
    index_rows = store.package_index(conn)
    traps = store.list_traps(conn)
    repo_row = store.find_repo(conn, repo)
    repo_id = repo_row['id'] if repo_row else None
    known_pr = store.get_pr(conn, repo_id, num) if repo_id else None
    this_pr_id = known_pr['id'] if known_pr else None
    shown_docs = set()

    def name_patterns(row, name):
        return store.package_names(conn, row['id']) if row else [name]

    def history_line(h, with_repo):
        verdict = h['verdict'] or ('no verdict' if h['review_id'] else 'triaged only')
        when = (h['reviewed_at'] or h['triaged_at'] or '')[:10]
        where = '%s#%s' % (h['repo'], h['number']) if with_repo else '#%s' % h['number']
        return '%s %s %s -> %s %s%s' % (where, h['name'], h['from_version'] or '?', h['to_version'] or '?', verdict,
                                        (' (%s)' % when) if when else '')

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
        checks = store.exposure_checks(conn, row['doc_id'])
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

    pkgs = []  # unified list of dicts

    def add_pkg(eco, name, old, new, sect):
        t = bump_type(old, new)
        row = store.lookup(index_rows, name, eco)
        pkgs.append({'eco': eco, 'name': name, 'old': old, 'new': new, 'type': t, 'section': sect,
                     'index': row, 'reviews': prior_reviews(row, name), 'elsewhere': elsewhere(row, name)})

    for name, v in sorted(npm.items()):
        add_pkg('npm', name, v.get('-', ''), v.get('+', ''), npm_sections.get(name, 'not in head manifest'))
    for name, v in sorted(composer.items()):
        add_pkg('composer', name, v.get('-', ''), v.get('+', ''), composer_sections.get(name, 'not in head manifest'))

    section('Packages (from the manifest diff)')
    if not pkgs:
        print('(no manifest changes)')
    for p in pkgs:
        idx = p['index']
        status = ('%s @%s' % (idx['status'], idx['analysed'])) if idx else 'not in ledger'
        print('- %s %s: %s -> %s  [%s] section=%s  ledger=%s' % (p['eco'], p['name'], p['old'], p['new'], p['type'], p['section'], status))
        if idx:
            print_ledger_detail(idx)
        if p['reviews']:
            print('    prior reviews here: %s' % ' | '.join(p['reviews']))
        if p['elsewhere']:
            print('    elsewhere: %s' % ' | '.join(p['elsewhere']))
        n = None
        for k, v in notes.items():
            if p['name'].lower() in k.lower() or k.lower().split(' ')[0] in p['name'].lower():
                n = v
                break
        if n:
            print('    release notes: %d lines, %d breaking-ish keyword hits -> %s' % (n[0], n[1], n[2]))
        else:
            print('    release notes: none in body')
    if truncated:
        print('NOTE: body says release notes were truncated; fetch the missing changelogs.')

    # lock-only movers
    lock_movers = lock_only_movers(changes)
    lock_only = [(lock, key, o, n) for lock, key, _name, o, n in lock_movers]
    lock_name = dict(((lock, key), name) for lock, key, name, _o, _n in lock_movers)
    lock_rows = {}
    nested_new = [k for k, v in bun_lock.items() if '/' in k.rsplit('@', 1)[0] and '+' in v and '-' not in v]
    nested_gone = [k for k, v in bun_lock.items() if '/' in k.rsplit('@', 1)[0] and '-' in v and '+' not in v]
    section('Lock-only movers (transitives Renovate does not list)')
    if not lock_only:
        print('(none)')

    def sibling_of(name, o, n):
        bare = name.rsplit('/', 1)[-1] if '/' in name and not name.startswith('@') else name
        scope = bare.split('/')[0] if bare.startswith('@') else ''
        for p in pkgs:
            pscope = p['name'].split('/')[0] if p['name'].startswith('@') else '@' + p['name']
            if scope and scope == pscope and (o, n) == (p['old'], p['new']):
                return p['name']
            if (o, n) == (p['old'], p['new']) and p['name'] != bare:
                return p['name']
        return None

    siblings = {}
    independent = []
    for lock, name, o, n in sorted(lock_only):
        sib = sibling_of(name, o, n)
        if sib:
            siblings.setdefault((sib, o, n), []).append(name)
        else:
            independent.append((lock, name, o, n))
    for (sib, o, n), names in siblings.items():
        print('- lockstep siblings of %s (%s -> %s): %s' % (sib, o, n, ', '.join(names)))
    for lock, name, o, n in independent:
        real = lock_name.get((lock, name), name)
        row = store.lookup(index_rows, real, 'npm' if lock == 'bun.lock' else 'composer')
        lock_rows[(lock, name)] = row
        tag = (' ledger=%s @%s' % (row['status'], row['analysed'])) if row else ''
        print('- %s %s: %s -> %s [%s]%s' % (lock, name, o, n, bump_type(o, n), tag))
        if row and row['status'] in ('CHECK', 'TRAP'):
            print_ledger_detail(row)
    lock_only = independent
    if nested_new:
        print('NEW nested bun.lock entries (second copies): %s' % ', '.join(sorted(nested_new)[:12]) + (' ...' if len(nested_new) > 12 else ''))
    if nested_gone:
        print('REMOVED nested bun.lock entries (deduped): %s' % ', '.join(sorted(nested_gone)[:12]))

    # Renovate's own table, for cross-check
    section('Renovate table (body)')
    if not table:
        print('(no package table found in body)')
    for r in table:
        extra = ' update=%s' % r['update'] if r['update'] else ''
        extra += ' type=%s' % r['type'] if r['type'] else ''
        print('- %s: %s -> %s%s' % (r['pkg'], r['old'], r['new'], extra))

    # ---------------- actions
    section('GitHub Actions `uses:` changes')
    action_problems = []
    action_rows = {}
    action_unresolved = []
    if not uses:
        print('(none)')
    for action, v in sorted(uses.items()):
        o, n = v.get('-'), v.get('+')
        if not n:
            print('- %s: removed (%s)' % (action, o['ref'] if o else ''))
            continue
        line = '- %s: %s %s -> %s %s' % (action, (o['ref'][:12] if o else '(new)'), (o['comment'] if o else ''), n['ref'][:12], n['comment'])
        print(line)
        action_repo = '/'.join(action.split('/')[:2])
        row = store.lookup(index_rows, action_repo, 'actions')
        if row and row['ecosystem'] == 'actions':
            action_rows[action] = row
            print('    ledger=%s @%s' % (row['status'], row['analysed']))
            print_ledger_detail(row)
        if HEX40.match(n['ref']):
            tag = n['comment']
            known = store.find_action(conn, n['ref'], action_repo)
            same = [k for k in known if k['sha'] == n['ref'] and (not tag or k['tag'] == tag)]
            if same:
                print('    on record: %s %s = %s (%s)  -> pass (ledger)' % (action_repo, same[0]['tag'], same[0]['sha'][:12], same[0]['tag_type'] or 'type not recorded'))
                continue
            if known:
                print('    SHA on record as %s, not %s: resolving the comment tag' % (', '.join(k['tag'] for k in known), tag or '(no comment)'))
            if not tag:
                print('    no version comment on the pin; resolve by hand')
                action_unresolved.append(action)
                continue
            rc, o1, e1 = run(['gh', 'api', 'repos/%s/git/ref/tags/%s' % (action.split('/', 2)[0] + '/' + action.split('/', 2)[1], tag),
                              '--jq', '.object.sha + " " + .object.type'], timeout=30)
            if rc != 0:
                print('    tag %s could not be resolved (%s)' % (tag, (e1 or o1).strip()[:100]))
                action_unresolved.append(action)
                continue
            sha, typ = (o1.split() + [''])[:2]
            deref = ''
            if typ == 'tag':
                rc2, o2, _ = run(['gh', 'api', 'repos/%s/git/tags/%s' % ('/'.join(action.split('/')[:2]), sha), '--jq', '.object.sha'], timeout=30)
                if rc2 == 0:
                    deref = sha
                    sha = o2.strip()
            ok = sha == n['ref']
            print('    %s resolves to %s%s -> %s' % (tag, sha[:12], (' (annotated %s)' % deref[:12]) if deref else '', 'OK' if ok else 'MISMATCH'))
            if not ok:
                action_problems.append(action)
            else:
                print('    bank with: depledger action add %s %s %s%s' % (action_repo, tag, sha, (' --annotated %s' % deref) if deref else ' --type commit'))
        else:
            print('    pinned by tag, not SHA')
    # owner/repo swaps: a '-' with no '+' and a '+' with no '-' in the same file is suspicious
    removed = [a for a, v in uses.items() if '-' in v and '+' not in v]
    added = [a for a, v in uses.items() if '+' in v and '-' not in v]
    if removed or added:
        print('ACTION_IDENTITY_CHANGED: removed=%s added=%s  (fork swap?)' % (removed, added))
        action_problems.extend(added)

    # ---------------- images
    section('Container images')
    docker_ok = not args.no_docker and run(['docker', 'info'], timeout=15)[0] == 0
    image_needs_probe = []
    image_ci_blind = []
    image_stacked = []
    image_only_in_workflows = bool(images) and all(f.startswith('.github/') for fs in image_files.values() for f in fs)
    if not images:
        print('(none)')
    for key, v in sorted(images.items()):
        name = v.get('name', key)
        o, n = v.get('-', {}), v.get('+', {})
        if not n:
            continue
        print('- %s  files=%s' % (key, ','.join(sorted(set(image_files.get(key, []))))))
        print('    tag: %s -> %s   digest: %s -> %s   [%s]' % (o.get('tag', ''), n.get('tag', ''), short(o.get('digest')), short(n.get('digest')), bump_type(o.get('tag') or short(o.get('digest')), n.get('tag') or short(n.get('digest')))))
        row = store.lookup(index_rows, name, 'images')
        if row:
            print('    ledger=%s @%s' % (row['status'], row['analysed']))
            print_ledger_detail(row)
        for label, d in (('old', o.get('digest')), ('new', n.get('digest'))):
            hits = store.find_digest(conn, d) if d else []
            print('    digest %s %s: %s' % (label, short(d), compact_facts(hits[0]) if hits else 'not on record'))
            if label == 'new' and d and not hits:
                image_needs_probe.append(key)
        for d in (o.get('digest'), n.get('digest')):
            revs = store.reviews_mentioning(conn, None, short(d), exclude_pr_id=this_pr_id) if d else []
            if revs:
                print('    prior reviews (%s): %s' % (short(d), ' | '.join('%s#%s %s' % (r['repo'], r['number'], (r['reviewed_at'] or '')[:10]) for r in revs)))
        for line in elsewhere(row, name):
            print('    elsewhere: %s' % line)
        if docker_ok and n.get('tag') and n.get('digest'):
            rc, o1, _ = run(['docker', 'buildx', 'imagetools', 'inspect', '%s:%s' % (name, n['tag'])], timeout=40)
            m = re.search(r'Digest:\s*sha256:([0-9a-f]{64})', o1)
            if m:
                print('    head-of-tag %s:%s = %s -> %s' % (name, n['tag'], short(m.group(1)), 'OK (new digest is head)' if m.group(1) == n['digest'] else 'STALE (tag has moved on; new digest is not head)'))
            else:
                print('    head-of-tag: imagetools inspect gave no digest')
        if docker_ok and o.get('digest') and n.get('digest'):
            per = {}
            for label, d in (('old', o['digest']), ('new', n['digest'])):
                rc, raw, _ = run(['docker', 'buildx', 'imagetools', 'inspect', '%s@sha256:%s' % (name, d), '--raw'], timeout=40)
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
            if per:
                parts = []
                same_any = False
                for plat, dd in sorted(per.items()):
                    if 'old' in dd and 'new' in dd:
                        same = dd['old'] == dd['new']
                        same_any = same_any or same
                        parts.append('%s %s' % (plat, 'IDENTICAL' if same else 'changed'))
                    else:
                        parts.append('%s only-in-%s' % (plat, 'new' if 'new' in dd else 'old'))
                print('    per-arch manifests: %s' % ', '.join(parts))
                if same_any:
                    print('    -> identical manifest on a platform means no content change there: no probe needed for it')
        if git_ok and n.get('digest'):
            rc, o1, _ = run(['git', 'grep', '-l', '-F', short(n['digest']), 'origin/' + head, '--', '.github/workflows'], timeout=20)
            wf = [l.split(':', 1)[1] for l in o1.splitlines()]
            print('    CI runs the new digest: %s' % (', '.join(wf) if wf else 'NO workflow on the branch pins it -> green CI says nothing about this image'))
            if not wf:
                image_ci_blind.append(key)
            rc, o2, _ = run(['git', 'grep', '-l', '-E', r'^FROM\s+(--platform=\S+\s+)?' + re.escape(name) + r'[:@]', 'origin/' + head], timeout=20)
            stacked = [l.split(':', 1)[1] for l in o2.splitlines()]
            if stacked:
                print('    Dockerfiles with FROM %s: %s  (stacked layers -> probe-image.sh / local rebuild; CI never builds these)' % (name, ', '.join(stacked)))
                image_stacked.append(key)
    if image_only_in_workflows:
        print('NOTE: every image change sits in .github/ -> a docker pin wearing a ci-actions path; treat as docker-image, SHA-pin resolution N/A.')

    # ---------------- buckets and locks
    section('Buckets and lock completeness')
    paths = [f['path'] for f in pr.get('files', [])]
    bns = [os.path.basename(p) for p in paths]
    buckets = []
    npm_sects = set(p['section'] for p in pkgs if p['eco'] == 'npm')
    comp_sects = set(p['section'] for p in pkgs if p['eco'] == 'composer')
    js_touched = any(b in ('package.json', 'bun.lock', 'bun.lockb', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock') for b in bns) \
        or any(p.startswith('resources/') or b.startswith('vite.config') for p, b in zip(paths, bns))
    php_touched = any(b in ('composer.json', 'composer.lock') for b in bns)
    build_chain_hit = [p['name'] for p in pkgs if p['eco'] == 'npm' and p['name'].startswith(BUILD_CHAIN)]
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
    print('BUCKETS=%s' % (','.join(buckets) or 'none'))
    if build_chain_hit:
        print('BUILD_CHAIN_PACKAGES=%s  (devDeps that change shipped output: keep the build)' % ','.join(build_chain_hit))
    if other_files:
        print('NON_DEPENDENCY_FILES=%s  (not a pure dependency PR?)' % ','.join(other_files))
    lock_problems = []
    if 'package.json' in bns and not any(b in ('bun.lock', 'bun.lockb', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock') for b in bns):
        lock_problems.append('package.json changed without its lock')
    if 'composer.json' in bns and 'composer.lock' not in bns:
        lock_problems.append('composer.json changed without composer.lock')
    if 'lock file maintenance' in pr['title'].lower():
        print('LOCK_FILE_MAINTENANCE=yes  (no manifest diff expected; breaking-change scan N/A; install+build+tests are the signal)')
    print('LOCK_OK=%s%s' % ('no' if lock_problems else 'yes', ('  ' + '; '.join(lock_problems)) if lock_problems else ''))

    # ---------------- git state vs base
    section('Branch vs base')
    behind = ahead = None
    conflicts = None
    overlap = []
    base_hits = {}
    if git_ok:
        behind = out(*run(['git', 'rev-list', '--count', 'origin/%s..origin/%s' % (head, base)], timeout=20))
        ahead = out(*run(['git', 'rev-list', '--count', 'origin/%s..origin/%s' % (base, head)], timeout=20))
        print('BEHIND_BASE=%s commits  AHEAD=%s commits%s' % (behind, ahead, '  (more than 1 ahead: orphaned/rebased base? inspect git log)' if ahead and ahead.isdigit() and int(ahead) > 1 else ''))
        rc, o1, e1 = run(['git', 'merge-tree', '--write-tree', 'origin/' + base, 'origin/' + head], timeout=60)
        conflicts = rc == 1
        print('MERGE_TREE=%s' % ('CONFLICTS' if conflicts else ('clean' if rc == 0 else 'unavailable (%s)' % e1.strip()[:80])))
        mb = out(*run(['git', 'merge-base', 'origin/' + base, 'origin/' + head], timeout=20))
        if mb:
            rc, o2, _ = run(['git', 'diff', '--name-only', mb, 'origin/' + base], timeout=30)
            base_changed = set(o2.split())
            overlap = sorted(base_changed & set(paths))
            print('BASE_TOUCHED_SAME_FILES_SINCE_BRANCH=%s' % (','.join(overlap) if overlap else 'none'))
            if overlap and not conflicts:
                print('    -> logical overlap with a clean textual merge: CI ran against a base that has since moved in these files. Test the merge result, not the branch.')
        needles = []
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
        for label, needle in needles:
            rc, o3, _ = run(['git', 'grep', '-l', '-F', needle, 'origin/' + base], timeout=20)
            hits = [l.split(':', 1)[1] for l in o3.splitlines()]
            if hits:
                base_hits[label] = hits
        if base_hits:
            print('ALREADY_ON_BASE (prior validation by a sibling PR):')
            for k, v in base_hits.items():
                print('    %s -> %s' % (k, ', '.join(v)))
        else:
            print('ALREADY_ON_BASE=none')
    else:
        print('(git checks skipped: %s)' % ('repo mismatch, cwd is %s' % (cur_repo or 'not a gh repo') if not repo_match else 'fetch failed or --no-git'))

    # ---------------- environment
    section('Environment')
    uses_sail = os.path.exists('vendor/bin/sail')
    sail_up = False
    if uses_sail:
        rc, o1, _ = run(['./vendor/bin/sail', 'ps'], timeout=30)
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
    git_clean = out(*run(['git', 'status', '--porcelain'], timeout=20)) == ''
    cur_branch = out(*run(['git', 'branch', '--show-current'], timeout=10))
    print('REPO_MATCH=%s  (cwd repo: %s)' % ('yes' if repo_match else 'NO', cur_repo or 'none'))
    print('USES_SAIL=%s  SAIL_UP=%s' % (('yes' if uses_sail else 'no'), ('yes' if sail_up else 'no')))
    print('JS_PM=%s  JS_LOCK=%s  HAS_COMPOSER=%s  JS_SUBPACKAGES=%s' % (js_pm, js_lock, 'yes' if os.path.exists('composer.json') else 'no', ','.join(subs) or 'none'))
    print('SCRIPTS: build=%s  typecheck=%s  lint=%s' % (scripts.get('build', '-'), scripts.get('typecheck', scripts.get('tsc', '-')), scripts.get('lint', '-')))
    if '--fix' in str(scripts.get('lint', '')):
        print('    lint script carries --fix: run eslint directly without it for a review')
    print('APP_URL=%s  DOCKER_FILES=%s  HOST_ARCH=%s  DOCKER=%s' % (app_url, ','.join(docker_files) or 'none', os.uname().machine, 'up' if docker_ok else 'down/skipped'))
    print('GIT_CLEAN=%s  CURRENT_BRANCH=%s' % ('yes' if git_clean else 'NO', cur_branch))

    # ---------------- tier
    section('Proposed tier')
    tier = 0
    reasons = []

    def bump(level, why):
        nonlocal tier
        tier = max(tier, level)
        reasons.append('T%d: %s' % (level, why))

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
    if other_files == [] and not pkgs and not images and not uses and not lock_only and 'lock file maintenance' not in pr['title'].lower():
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
        elif idx and p['type'] != 'digest' and idx['analysed'] not in ('any', 'see gist') and ver_parts(p['new']) > ver_parts(idx['analysed']) and p['type'].startswith('minor') and p['section'] in ('dependencies', 'require'):
            bump(1, '%s %s is past the analysed %s (runtime, minor)' % (p['name'], p['new'], idx['analysed']))
    for lock, name, o, n in lock_only:
        row = lock_rows.get((lock, name))
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
        row = store.lookup(index_rows, name, 'images')
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
    if nested_new:
        bump(1, 'new nested bun.lock copies appeared: triage benign vs troublesome')
    if not reasons:
        reasons.append('T0: all patch/minor-devtool, routine or unknown-patch rows, lock complete, CI green, no image/action concerns')

    checks = []
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
    print('TIER=%d' % tier)
    for r in reasons:
        print('- ' + r)
    print('CHECKS=%s' % (','.join(checks) or 'none (diff read + CI only)'))
    print('NEEDS_CHECKOUT=%s' % ('yes' if any(c for c in checks if c not in ('probe-image', 'rebuild-stacked')) else 'no'))

    if traps:
        section('Standing traps (symptom-keyed; `depledger show pattern <slug>` for the detail)')
        for t in traps:
            print('- %s%s' % (t['headline'], ('  [pattern: %s]' % t['doc_slug']) if t['doc_slug'] else ''))

    # ---------------- repo notes
    section('Repo notes (%s)' % repo)
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
    ctx = {'buckets': buckets, 'names': [x for x in names if x],
           'text': ' '.join([head, pr['title']] + signals + names).lower()}
    if not repo_id:
        print('(no notes: this run registers %s; bank facts with `depledger note add --repo %s`)' % (repo, repo))
    else:
        always, matched, hidden = store.notes_for(conn, repo_id, ctx)
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

    # ---------------- write-back
    if not args.no_record:
        pr_fields = {'title': pr['title'], 'branch': head, 'base': base, 'url': pr.get('url'), 'author': author,
                     'state': pr.get('state'), 'head_sha': pr.get('headRefOid'), 'opened_at': pr.get('createdAt'),
                     'merged_at': pr.get('mergedAt'), 'proposed_tier': tier}
        sections = dict((('npm', k), v) for k, v in npm_sections.items())
        sections.update((('composer', k), v) for k, v in composer_sections.items())
        try:
            store.record_triage(conn, repo, num, pr_fields, bumps(changes, sections),
                                local_path=cur_top if repo_match else None)
            store.export(conn)
        except Exception as e:  # noqa: a ledger write must never cost the triage output
            print('WARN: could not record this triage in the ledger: %s' % e)
    section('Write-back')
    print('REVIEW_KEY=%s#%s' % (repo, num))
    print('save the verdict: depledger review save %s#%s --verdict safe|not-safe --tier N [--verdict-text "..."] < entry.md' % (repo, num))
