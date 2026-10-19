"""`depledger`: a shared ledger for dependency-update reviews across many repos.

Built for an AI agent (Claude Code) reviewing Renovate or Dependabot PRs, so reads print compact
text sized for a model's context. Writes take one fact per call, with any
markdown body on stdin. Repo-scoped commands resolve the repo from the cwd's git remote (or
--repo) and refuse anything that is not a registered repo, so nothing lands in the wrong place.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

from . import repo as repo_mod
from . import store

UI_PORT = int(os.environ.get('DEPLEDGER_UI_PORT', '7420'))


def stdin_text():
    return '' if sys.stdin.isatty() else sys.stdin.read()


def fail(msg):
    sys.exit('depledger: %s' % msg)


def heading(text):
    print('\n## ' + text)


def indent(text, by='    '):
    return '\n'.join(by + line if line else line for line in text.rstrip('\n').splitlines())


def verdict_label(v):
    return {'safe': 'safe', 'not_safe': 'not safe'}.get(v or '', 'no verdict')


# ---------------------------------------------------------------- show

def show_package(conn, a):
    p = store.find_package(conn, a.name, a.eco)
    if not p:
        fail('no package matches %r (try `depledger search %s`)' % (a.name, a.name))
    names = store.package_names(conn, p['id'])
    print('# %s  [%s]  %s @%s' % (p['name'], p['ecosystem'], p['status'], p['analysed']))
    print('names: %s' % ', '.join(names))
    print('gist: %s' % p['gist'])
    doc = store.doc_by_id(conn, p['doc_id'])
    if doc:
        shared = [x['name'] for x in store.packages_for_doc(conn, doc['id']) if x['id'] != p['id']]
        print('doc: %s (updated %s)%s' % (doc['slug'], doc['updated_at'][:10], ('  shared with: ' + ', '.join(shared)) if shared else ''))
        checks = store.exposure_checks(conn, doc['id'])
        if checks:
            heading('Exposure checks (re-run every review)')
            for c in checks:
                print('[%s]%s' % (c['id'], (' ' + c['label']) if c['label'] else ''))
                print(indent(c['commands']))
    history = store.bump_history(conn, names)
    heading('Reviews across repos (%d)' % len(history))
    if not history:
        print('(none recorded)')
    for h in history[:a.limit]:
        when = (h['reviewed_at'] or h['triaged_at'] or '')[:10]
        print('- %s#%s %s %s -> %s  %s%s  %s' % (h['repo'], h['number'], h['name'], h['from_version'] or '?', h['to_version'] or '?',
                                             verdict_label(h['verdict']) if h['review_id'] else 'triaged only',
                                             (' tier %s' % h['tier']) if h['tier'] is not None else '', when))
    if len(history) > a.limit:
        print('... %d more (--limit)' % (len(history) - a.limit))
    notes = store.notes_tagged_for(conn, names)
    if notes:
        heading('Repo notes tagged for it')
        for n in notes:
            print('- %s [%s] %s  (when: %s)' % (n['repo'], n['slug'] or n['id'], n['gist'], n['when_tags']))
    if doc and doc['body'].strip() and not a.brief:
        heading('Analysis (%s)' % doc['slug'])
        print(doc['body'].rstrip('\n'))


def show_doc(conn, a):
    d = store.get_doc(conn, a.slug)
    if not d:
        fail('no doc %r' % a.slug)
    print('# %s  [%s doc %s, updated %s]' % (d['title'], d['kind'], d['slug'], d['updated_at'][:10]))
    pk = store.packages_for_doc(conn, d['id'])
    if pk:
        print('backs: %s' % ', '.join('%s (%s)' % (x['name'], x['status']) for x in pk))
    traps = [t for t in store.list_traps(conn) if t['doc_id'] == d['id']]
    for t in traps:
        print('trap [%s]: %s' % (t['id'], t['headline']))
    for c in store.exposure_checks(conn, d['id']):
        print('exposure check [%s]%s:' % (c['id'], (' ' + c['label']) if c['label'] else ''))
        print(indent(c['commands']))
    print()
    print(d['body'].rstrip('\n'))


def show_traps(conn, a):
    for t in store.list_traps(conn):
        print('[%s] %s%s' % (t['id'], t['headline'], ('  [pattern: %s]' % t['doc_slug']) if t['doc_slug'] else ''))
        if a.full and t['detail']:
            print(indent(t['detail']))


def show_repo(conn, a):
    r = repo_mod.resolve(conn, a.repo or a.name)
    print('# %s%s' % (r['full_name'], ('  (%s)' % r['local_path']) if r['local_path'] else ''))
    notes = store.list_notes(conn, r['id'])
    heading('Notes printed on every triage')
    for n in notes:
        if not store.split_tags(n['when_tags']):
            print('- [%s] %s%s' % (n['slug'] or n['id'], n['gist'], '  (unverified)' if n['unverified'] else ''))
    heading('Tagged notes')
    for n in notes:
        if store.split_tags(n['when_tags']):
            print('- [%s] %s  (when: %s)%s%s' % (n['slug'] or n['id'], n['gist'], n['when_tags'], '  +body' if n['body'] else '',
                                              '  (unverified)' if n['unverified'] else ''))
    heading('Recent reviews')
    for v in store.list_reviews(conn, repo_id=r['id'], limit=a.limit):
        print('- #%s %s  %s%s  %s' % (v['number'], v['title'], verdict_label(v['verdict']),
                                      (' tier %s' % v['tier']) if v['tier'] is not None else '', v['reviewed_at'][:10]))


def show_note(conn, a):
    r = repo_mod.resolve(conn, a.repo)
    n = store.get_note(conn, r['id'], a.ref)
    if not n:
        fail('no note %r in %s' % (a.ref, r['full_name']))
    print('# [%s] %s' % (n['slug'] or n['id'], n['gist']))
    print('repo: %s  when: %s  updated: %s%s' % (r['full_name'], n['when_tags'] or '(always)', n['updated_at'][:10],
                                                '  (unverified)' if n['unverified'] else ''))
    if n['body']:
        print()
        print(n['body'].rstrip('\n'))


def print_review(conn, v, full=True):
    prs = ', '.join('%s#%s%s' % (p['repo'], p['number'], '' if p['is_primary'] else ' (also)') for p in v['prs'])
    print('# review %s: %s' % (v['id'], v['title']))
    print('prs: %s' % prs)
    print('verdict: %s%s  tier: %s  reviewed: %s  head: %s  source: %s' % (
        verdict_label(v['verdict']), (' (%s)' % v['verdict_text']) if v['verdict_text'] else '',
        v['tier'] if v['tier'] is not None else '?', v['reviewed_at'][:10], (v['head_sha'] or '?')[:12], v['source']))
    if full and v['body'].strip():
        print()
        print(v['body'].rstrip('\n'))


def show_review(conn, a):
    if a.ref.startswith('r') and a.ref[1:].isdigit():
        v = store.get_review(conn, int(a.ref[1:]))
        if not v:
            fail('no review %s' % a.ref)
        print_review(conn, v)
        return
    repo_name, number = repo_mod.parse_pr_ref(a.ref)
    r = repo_mod.resolve(conn, repo_name or a.repo)
    pr = store.get_pr(conn, r['id'], number)
    if not pr:
        fail('%s#%s has not been triaged or reviewed' % (r['full_name'], number))
    print('# %s#%s %s' % (r['full_name'], pr['number'], pr['title']))
    print('state: %s  branch: %s  head: %s  triaged: %s  proposed tier: %s' % (
        pr['state'] or '?', pr['branch'] or '?', (pr['head_sha'] or '?')[:12], (pr['triaged_at'] or '-')[:10],
        pr['proposed_tier'] if pr['proposed_tier'] is not None else '?'))
    bumps = store.pr_bumps(conn, pr['id'])
    if bumps:
        heading('Moves')
        for b in bumps:
            print('- %s %s %s: %s -> %s [%s]' % (b['kind'], b['ecosystem'], b['name'], b['from_version'] or '?', b['to_version'] or '?', b['bump_type'] or '?'))
    for v in store.reviews_for_pr(conn, pr['id']):
        print()
        print_review(conn, store.get_review(conn, v['id']))


def show_image(conn, a):
    hits = store.find_digest(conn, a.digest)
    if not hits:
        print('not on record')
    for h in hits:
        facts = json.loads(h['facts'] or '{}')
        print('%s:%s %s  line: %s  recorded: %s' % (h['image'], h['tag'], h['digest'], h['line'] or '-', h['recorded_at'][:10]))
        for k, v in facts.items():
            if v not in ('', None):
                print('    %s: %s' % (k, v))
        if h['notes']:
            print('    notes: %s' % h['notes'])


def show_action(conn, a):
    hits = store.find_action(conn, a.sha)
    if not hits:
        print('not on record')
    for h in hits:
        print('%s %s = %s  %s%s%s' % (h['action'], h['tag'], h['sha'], h['tag_type'] or 'type not recorded',
                                     (' (tag object %s)' % h['annotated_sha']) if h['annotated_sha'] else '',
                                     ('  ' + h['note']) if h['note'] else ''))


def cmd_search(conn, a):
    repo_name = None
    if a.repo:
        repo_name = repo_mod.resolve(conn, a.repo)['full_name']
    try:
        hits = store.search(conn, ' '.join(a.words), repo=repo_name, kind=a.kind, limit=a.limit)
    except store.StoreError as e:
        fail(str(e))
    if not hits:
        print('no matches')
    for h in hits:
        print('- %s' % search_ref(conn, h))
        print('    %s' % re.sub(r'\s+', ' ', h['snippet'] or '').strip()[:300])


def search_ref(conn, h):
    """How to open a search hit."""
    k, i = h['kind'], h['ref_id']
    h = dict(h, title=h['title'] if len(h['title']) <= 110 else h['title'][:107] + '...')
    if k == 'doc':
        d = store.doc_by_id(conn, i)
        return 'doc %s: %s  (depledger show doc %s)' % (d['slug'], d['title'], d['slug'])
    if k == 'package':
        return 'package %s  (depledger show package %s)' % (h['title'], json.dumps(h['title']) if ' ' in h['title'] else h['title'])
    if k == 'trap':
        return 'trap [%s] %s' % (i, h['title'])
    if k == 'note':
        n = store.one(conn.execute('SELECT slug FROM notes WHERE id = ?', (i,)))
        return 'note %s [%s] %s  (depledger show note %s --repo %s)' % (h['repo'], (n or {}).get('slug') or i, h['title'], (n or {}).get('slug') or i, h['repo'])
    if k == 'review':
        return 'review r%s %s: %s  (depledger show review r%s)' % (i, h['repo'], h['title'], i)
    if k == 'image':
        return 'image %s  (depledger show image <digest>)' % h['title']
    return '%s %s' % (k, i)


# ---------------------------------------------------------------- writes

def cmd_repos(conn, a):
    for r in store.list_repos(conn):
        print('%s  reviews=%s notes=%s last=%s  %s' % (r['full_name'], r['review_count'], r['note_count'],
                                                    (r['last_reviewed'] or '-')[:10], r['local_path'] or ''))


def cmd_repo_add(conn, a):
    if a.name:
        name, top = a.name, a.path
    else:
        name, top = repo_mod.current_repo()
    with store.write(conn):
        store.ensure_repo(conn, name, top)
    print('registered %s' % name)


def cmd_package_set(conn, a):
    with store.write(conn):
        before = store.find_package(conn, a.name, a.eco)
        p = store.set_package(conn, a.eco, a.name, analysed=a.analysed, status=a.status, gist=a.gist,
                              aliases=a.alias or (), doc_slug=a.doc)
        if p['status'] in ('CHECK', 'TRAP') and not p['doc_id']:
            d = store.ensure_package_doc(conn, p)
            print('created doc %s: bank the analysis with `depledger package doc %s %s < analysis.md` and the check with `depledger package exposure ...`' % (d['slug'], a.eco, json.dumps(p['name'])))
        elif p['status'] == 'routine' and p['doc_id']:
            print('note: the row is routine but still links doc %s; drop it with `depledger package drop-doc %s %s` if it says nothing a review needs' % (p['doc_slug'], a.eco, json.dumps(p['name'])))
    print('%s %s [%s] %s @%s: %s' % ('updated' if before else 'added', p['name'], p['ecosystem'], p['status'], p['analysed'], p['gist']))


def package_or_fail(conn, a):
    p = store.find_package(conn, a.name, a.eco)
    if not p:
        fail('no %s package %r; add it with `depledger package set` first' % (a.eco, a.name))
    return p


def cmd_package_doc(conn, a):
    body = stdin_text()
    if not body.strip():
        fail('pass the analysis on stdin')
    with store.write(conn):
        p = package_or_fail(conn, a)
        d = store.ensure_package_doc(conn, p)
        d = store.put_doc(conn, d['slug'], 'package', body=body, append=a.append)
    print('%s doc %s (%d lines)' % ('appended to' if a.append else 'wrote', d['slug'], d['body'].count('\n')))


def cmd_package_exposure(conn, a):
    commands = stdin_text()
    if not commands.strip():
        fail('pass the commands on stdin')
    with store.write(conn):
        p = package_or_fail(conn, a)
        d = store.ensure_package_doc(conn, p)
        cid = store.set_exposure_check(conn, d['id'], commands.strip('\n') + '\n', label=a.label or '', replace_id=a.replace)
    print('%s exposure check %s on doc %s' % ('replaced' if a.replace else 'added', cid, d['slug']))


def cmd_package_drop_doc(conn, a):
    with store.write(conn):
        p = package_or_fail(conn, a)
        if not p['doc_id']:
            print('%s has no doc' % p['name'])
            return
        conn.execute('UPDATE packages SET doc_id = NULL, updated_at = ? WHERE id = ?', (store.now(), p['id']))
        if not store.packages_for_doc(conn, p['doc_id']):
            conn.execute('DELETE FROM docs WHERE id = ?', (p['doc_id'],))
            print('deleted doc %s (kept in revisions)' % p['doc_slug'])
        else:
            print('unlinked doc %s (other rows still use it)' % p['doc_slug'])


def cmd_doc_get(conn, a):
    d = store.get_doc(conn, a.slug)
    if not d:
        fail('no doc %r' % a.slug)
    if a.out:
        with open(a.out, 'w') as f:
            f.write(d['body'])
        print('wrote %s (%d lines); edit it, then `depledger doc put %s %s`' % (a.out, d['body'].count('\n'), a.slug, a.out))
    else:
        sys.stdout.write(d['body'])


def cmd_doc_put(conn, a):
    body = open(a.path).read() if a.path else stdin_text()
    if not body.strip():
        fail('pass the body as a file path or on stdin')
    with store.write(conn):
        existing = store.get_doc(conn, a.slug)
        if not existing and not a.kind:
            fail('no doc %r yet; pass --kind package|pattern|image|action to create it' % a.slug)
        d = store.put_doc(conn, a.slug, (existing or {}).get('kind') or a.kind, title=a.title, body=body, append=a.append)
    print('%s doc %s (%d lines)' % ('updated' if existing else 'created', d['slug'], d['body'].count('\n')))


def cmd_pattern_set(conn, a):
    a.kind, a.path = 'pattern', None
    cmd_doc_put(conn, a)


def cmd_trap_add(conn, a):
    with store.write(conn):
        tid = store.add_trap(conn, a.headline, a.detail or '', a.pattern)
    print('added trap %s' % tid)


def cmd_trap_edit(conn, a):
    with store.write(conn):
        store.edit_trap(conn, a.id, a.headline, a.detail, a.pattern)
    print('updated trap %s' % a.id)


def cmd_trap_rm(conn, a):
    with store.write(conn):
        conn.execute('DELETE FROM traps WHERE id = ?', (a.id,))
    print('removed trap %s (kept in revisions)' % a.id)


def cmd_action_add(conn, a):
    tag_type = a.type or ('annotated' if a.annotated else '')
    with store.write(conn):
        row, added = store.add_action(conn, a.action, a.tag, a.sha, tag_type, a.annotated, a.note or '')
    if added:
        print('added %s %s = %s' % (a.action, a.tag, a.sha))
    elif row['sha'] == a.sha:
        print('already on record: %s %s = %s' % (row['action'], row['tag'], row['sha']))
    else:
        fail('%s %s is on record as %s, not %s: a moved tag? Check it before changing the ledger' % (row['action'], row['tag'], row['sha'], a.sha))


def parse_facts(pairs):
    facts = {}
    for f in pairs or ():
        if '=' not in f:
            fail('--fact takes key=value, got %r' % f)
        k, v = f.split('=', 1)
        facts[k.strip()] = v.strip()
    return facts


def cmd_image_add(conn, a):
    m = re.match(r'^(.+?)(?::([^@:/]+))?@(?:sha256:)?([0-9a-f]{12,64})$', a.ref)
    if not m:
        fail('expected <image>[:<tag>]@<digest>, got %r' % a.ref)
    with store.write(conn):
        row, added = store.add_image(conn, m.group(1), m.group(2) or '', m.group(3), parse_facts(a.fact), a.notes or '', a.line or '')
    print('%s %s:%s %s' % ('added' if added else 'already on record (change it with `depledger image edit`):', row['image'], row['tag'], row['digest12']))


def cmd_image_edit(conn, a):
    hits = store.find_digest(conn, a.ref.rsplit('@', 1)[-1])
    if not hits:
        fail('no image record for %r (12 or more hex characters of the digest, or the full ref)' % a.ref)
    if a.notes is not None and a.append_notes:
        fail('use --notes or --append-notes, not both')
    with store.write(conn):
        row = store.edit_image(conn, hits[0]['id'], facts=parse_facts(a.fact), drop_facts=a.drop_fact or (),
                               notes=a.notes, append_notes=a.append_notes, line=a.line, tag=a.tag)
    print('updated %s:%s %s' % (row['image'], row['tag'], row['digest12']))


def cmd_checks(conn, a):
    from . import checks
    path = os.path.join(a.from_dir, 'checks.json')
    if not os.path.exists(path):
        fail('%s not found: run `depledger triage <pr> --out %s` from the repo first (it writes the plan when NEEDS_CHECKOUT=yes)' % (path, a.from_dir))
    with open(path) as f:
        p = json.load(f)
    if a.list:
        for s in p['steps']:
            print('%s %-16s phase %d  %s' % ('*' if s['selected'] else ' ', s['name'], s['phase'], s['cmd']))
        print('(* = selected by triage; run others with --only <name>)')
        return
    only = [o.strip() for o in a.only.split(',')] if a.only else None
    if a.here and a.checkout:
        fail('use --checkout (the PR head) or --here (a baseline on what is checked out), not both')
    if checks.run_plan(p, a.from_dir, only=only, do_checkout=a.checkout, here=a.here):
        sys.exit(1)


def cmd_probe(conn, a):
    from . import probe
    if a.from_dir:
        path = os.path.join(a.from_dir, 'probes.json')
        if not os.path.exists(path):
            fail('%s not found: run `depledger triage <pr> --out %s` first' % (path, a.from_dir))
        with open(path) as f:
            pairs = [(p['label'], p['old'], p['new']) for p in json.load(f)]
    elif len(a.refs) == 1:
        pairs = [(a.refs[0].rsplit('@', 1)[0], None, a.refs[0])]
    elif len(a.refs) == 2:
        pairs = [(a.refs[1].rsplit('@', 1)[0], a.refs[0], a.refs[1])]
    else:
        fail('give --from <triage out dir>, or one ref, or an old and a new ref')
    if not pairs:
        print('no image changes to probe')
        return
    failures = probe.probe_pairs(conn, pairs, platform=a.platform, refresh=a.refresh, do_bank=not a.no_bank)
    if failures:
        print('FAILED=%d' % failures)
        sys.exit(1)


def note_tags(a):
    if a.always and a.when:
        fail('use --always or --when, not both')
    return '' if a.always else (a.when or None)


def cmd_note_add(conn, a):
    tags = note_tags(a)
    if tags is None:
        fail('say when the note matters: --when "<bucket, package or glob, branch, text>" or --always (printed on every triage of the repo, keep these few)')
    r = repo_mod.resolve(conn, a.repo)
    body = stdin_text() if (a.slug or a.append) else ''
    with store.write(conn):
        existing = store.get_note(conn, r['id'], a.slug) if a.slug else None
        nid = store.add_note(conn, r['id'], a.gist, tags, slug=a.slug, body=body or None, append=a.append)
    print('%s note %s in %s' % ('updated' if existing else 'added', a.slug or nid, r['full_name']))


def cmd_note_edit(conn, a):
    r = repo_mod.resolve(conn, a.repo)
    n = store.get_note(conn, r['id'], a.ref)
    if not n:
        fail('no note %r in %s' % (a.ref, r['full_name']))
    body = stdin_text() if (a.body or a.append) else None
    with store.write(conn):
        store.edit_note(conn, n['id'], gist=a.gist, when_tags=note_tags(a), body=body, append=a.append, slug=a.slug,
                        verified=True if a.verified else None)
    print('updated note %s in %s' % (a.ref, r['full_name']))


def cmd_note_rm(conn, a):
    r = repo_mod.resolve(conn, a.repo)
    n = store.get_note(conn, r['id'], a.ref)
    if not n:
        fail('no note %r in %s' % (a.ref, r['full_name']))
    with store.write(conn):
        conn.execute('DELETE FROM notes WHERE id = ?', (n['id'],))
    print('removed note %s (kept in revisions)' % a.ref)


def cmd_review_save(conn, a):
    body = stdin_text()
    if not body.strip():
        fail('pass the review entry on stdin (ten lines at most)')
    repo_name, number = repo_mod.parse_pr_ref(a.ref)
    r = repo_mod.resolve(conn, repo_name or a.repo, register=bool(repo_name))
    numbers = [number]
    for extra in a.also or ():
        extra_repo, extra_number = repo_mod.parse_pr_ref(extra)
        if extra_repo and extra_repo.lower() != r['full_name'].lower():
            fail('--also must be a PR in the same repo')
        numbers.append(extra_number)
    verdict = {'safe': 'safe', 'not-safe': 'not_safe', 'not_safe': 'not_safe'}.get(a.verdict)
    with store.write(conn):
        rid, replaced = store.save_review(conn, r['id'], numbers, verdict=verdict, verdict_text=a.verdict_text,
                                          tier=a.tier, title=a.title, body=body.strip('\n') + '\n', head_sha=a.head)
    print('%s review r%s for %s#%s (%s)' % ('replaced' if replaced else 'saved', rid, r['full_name'], number, verdict_label(verdict)))
    try:
        changed = refresh_pr_states(conn, r)
        if changed:
            print('PR states refreshed: %s' % ', '.join('#%s %s' % (n, new.lower()) for n, _old, new in changed))
    except Exception as e:  # noqa: the review is saved; a stale PR state is not worth failing for
        print('warning: could not refresh PR states (%s)' % e)
    if ui_running():
        print('view: http://127.0.0.1:%d/reviews/%s' % (UI_PORT, rid))


def refresh_pr_states(conn, repo_row):
    """Bring the recorded PRs of one repo up to date (open, merged, closed) with one gh call.
    Returns the PRs whose state changed."""
    out = subprocess.run(['gh', 'pr', 'list', '--repo', repo_row['full_name'], '--state', 'all', '--limit', '500',
                          '--json', 'number,state,mergedAt'], capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        raise store.StoreError('gh pr list failed for %s: %s' % (repo_row['full_name'], out.stderr.strip()[:160]))
    live = dict((p['number'], p) for p in json.loads(out.stdout))
    changed = []
    with store.write(conn):
        for pr in store.rows(conn.execute('SELECT * FROM prs WHERE repo_id = ?', (repo_row['id'],))):
            p = live.get(pr['number'])
            if p and (p['state'] != pr['state'] or (p.get('mergedAt') or None) != pr['merged_at']):
                store.upsert_pr(conn, repo_row['id'], pr['number'], state=p['state'], merged_at=p.get('mergedAt'), synced_at=store.now())
                changed.append((pr['number'], pr['state'], p['state']))
    return changed


def cmd_review_sync(conn, a):
    """Refresh PR state (open, merged, closed) from GitHub."""
    repos = [repo_mod.resolve(conn, a.repo)] if a.repo else store.list_repos(conn)
    for r in repos:
        try:
            changed = refresh_pr_states(conn, r)
        except store.StoreError as e:
            print('skip %s' % e)
            continue
        for number, old, new in changed:
            print('%s#%s %s -> %s' % (r['full_name'], number, old or '?', new))
        print('%s: %d changed' % (r['full_name'], len(changed)))


# ---------------------------------------------------------------- ui and export

def ui_running(port=UI_PORT):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/api/health' % port, timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def cmd_ui(conn, a):
    os.makedirs(store.data_dir(), exist_ok=True)
    pid_path = os.path.join(store.data_dir(), 'ui.pid')
    if a.stop:
        try:
            pid = int(open(pid_path).read().strip())
            os.kill(pid, 15)
            os.remove(pid_path)
            print('stopped the UI (pid %d)' % pid)
        except (OSError, ValueError):
            print('the UI is not running from this checkout')
        return
    url = 'http://127.0.0.1:%d' % a.port
    if not ui_running(a.port):
        bun = shutil.which('bun') or os.path.expanduser('~/.bun/bin/bun')
        if not os.path.exists(bun):
            fail('bun is not installed, so the UI cannot start')
        ui_dir = os.path.join(store.ROOT, 'ui')
        if not os.path.isdir(os.path.join(ui_dir, 'node_modules')):
            subprocess.run([bun, 'install', '--frozen-lockfile'], cwd=ui_dir, check=True)
        log_path = os.path.join(store.data_dir(), 'ui.log')
        log = open(log_path, 'a')
        env = dict(os.environ, DEPLEDGER_DB=store.db_path(), DEPLEDGER_UI_PORT=str(a.port))
        proc = subprocess.Popen([bun, 'server.ts'], cwd=ui_dir, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        with open(pid_path, 'w') as f:
            f.write(str(proc.pid))
        for _ in range(40):
            if ui_running(a.port):
                break
            if proc.poll() is not None:
                fail('the UI exited at startup; see %s' % log_path)
            time.sleep(0.25)
        else:
            fail('the UI did not answer on %s within 10s; see %s' % (url, log_path))
        print('started the UI: %s' % url)
    else:
        print('the UI is running: %s' % url)
    if a.open:
        subprocess.run(['open', url])


def cmd_export(conn, a):
    out_dir = store.export(conn, a.dir)
    print('exported to %s' % out_dir)


def git(args, cwd):
    return subprocess.run(['git'] + args, cwd=cwd, capture_output=True, text=True, timeout=120)


def cmd_backup(conn, a):
    """Commit export/ in the git repo that holds it, then push. Only export/ is staged, so
    nothing else in that repo is swept into the commit."""
    out_dir = os.path.realpath(store.export(conn))
    top = repo_mod.toplevel(out_dir)
    if not top:
        fail('%s is not inside a git repo; run `git init` in %s to back it up' % (out_dir, os.path.dirname(out_dir)))
    rel = os.path.relpath(out_dir, os.path.realpath(top))
    git(['add', '--', rel], top)
    if git(['diff', '--cached', '--quiet', '--', rel], top).returncode == 0:
        print('nothing to back up: %s matches the last commit' % rel)
        return
    stat = git(['diff', '--cached', '--shortstat', '--', rel], top).stdout.strip()
    message = 'chore: ledger backup %s' % store.now()
    r = git(['commit', '-q', '-m', message, '--', rel], top)
    if r.returncode:
        fail('git commit failed: %s' % (r.stderr or r.stdout).strip())
    print('committed "%s" (%s)' % (message, stat))
    if a.no_push:
        return
    upstream = git(['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}'], top)
    if upstream.returncode:
        print('no upstream branch in %s; the commit stays local' % top)
        return
    r = git(['push', '-q'], top)
    if r.returncode:
        fail('git push failed: %s' % (r.stderr or r.stdout).strip())
    print('pushed to %s' % upstream.stdout.strip())


# ---------------------------------------------------------------- parser

def build_parser():
    ap = argparse.ArgumentParser(prog='depledger', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('triage', add_help=False, help='triage a PR (see `depledger triage --help`)')

    p = sub.add_parser('show', help='read one thing')
    show = p.add_subparsers(dest='what', required=True)
    s = show.add_parser('package', help='a package across every repo')
    s.add_argument('name')
    s.add_argument('--eco', choices=store.ECOSYSTEMS)
    s.add_argument('--brief', action='store_true', help='leave out the analysis body')
    s.add_argument('--limit', type=int, default=15)
    s.set_defaults(f=show_package)
    s = show.add_parser('doc', help='a doc (package analysis, pattern, image or action notes)')
    s.add_argument('slug')
    s.set_defaults(f=show_doc)
    s = show.add_parser('pattern', help='a tooling pattern doc')
    s.add_argument('slug')
    s.set_defaults(f=show_doc)
    s = show.add_parser('traps', help='the standing traps')
    s.add_argument('--full', action='store_true')
    s.set_defaults(f=show_traps)
    s = show.add_parser('repo', help="a repo's notes and recent reviews")
    s.add_argument('name', nargs='?')
    s.add_argument('--repo')
    s.add_argument('--limit', type=int, default=15)
    s.set_defaults(f=show_repo)
    s = show.add_parser('note', help='one repo note')
    s.add_argument('ref', help='note id or slug')
    s.add_argument('--repo')
    s.set_defaults(f=show_note)
    s = show.add_parser('review', help='a PR (owner/name#N) with its reviews, or one review (r123)')
    s.add_argument('ref')
    s.add_argument('--repo')
    s.set_defaults(f=show_review)
    s = show.add_parser('image', help='a digest')
    s.add_argument('digest')
    s.set_defaults(f=show_image)
    s = show.add_parser('action', help='a pinned action SHA')
    s.add_argument('sha')
    s.set_defaults(f=show_action)

    p = sub.add_parser('search', help='full text search over every note, doc, review and digest')
    p.add_argument('words', nargs='+')
    p.add_argument('--repo', help='this repo plus global entries')
    p.add_argument('--kind', choices=('doc', 'package', 'trap', 'note', 'review', 'image'))
    p.add_argument('--limit', type=int, default=20)
    p.set_defaults(f=cmd_search)

    p = sub.add_parser('repos', help='registered repos')
    p.set_defaults(f=cmd_repos)
    p = sub.add_parser('repo', help='register a repo')
    rs = p.add_subparsers(dest='action', required=True)
    s = rs.add_parser('add')
    s.add_argument('name', nargs='?', help='owner/name (default: the cwd remote)')
    s.add_argument('--path')
    s.set_defaults(f=cmd_repo_add)

    p = sub.add_parser('package', help='write a package row, its analysis or its exposure check')
    ps = p.add_subparsers(dest='action', required=True)
    s = ps.add_parser('set', help='add or update a row')
    s.add_argument('eco', choices=store.ECOSYSTEMS)
    s.add_argument('name')
    s.add_argument('--analysed')
    s.add_argument('--status', choices=store.STATUSES)
    s.add_argument('--gist')
    s.add_argument('--alias', action='append', help='another name or glob the row answers to')
    s.add_argument('--doc', help='link an existing doc by slug')
    s.set_defaults(f=cmd_package_set)
    for name, f, helptext in (('doc', cmd_package_doc, 'write the analysis doc from stdin'),
                              ('exposure', cmd_package_exposure, 'add an exposure check from stdin'),
                              ('drop-doc', cmd_package_drop_doc, 'unlink (and delete if unused) the doc')):
        s = ps.add_parser(name, help=helptext)
        s.add_argument('eco', choices=store.ECOSYSTEMS)
        s.add_argument('name')
        if name == 'doc':
            s.add_argument('--append', action='store_true', help='add a section instead of replacing')
        if name == 'exposure':
            s.add_argument('--label')
            s.add_argument('--replace', type=int, help='replace the check with this id')
        s.set_defaults(f=f)

    p = sub.add_parser('doc', help='export a doc to edit it, or write one')
    ds = p.add_subparsers(dest='action', required=True)
    s = ds.add_parser('get')
    s.add_argument('slug')
    s.add_argument('--out', help='write to this file instead of stdout')
    s.set_defaults(f=cmd_doc_get)
    s = ds.add_parser('put')
    s.add_argument('slug')
    s.add_argument('path', nargs='?', help='file to read (default: stdin)')
    s.add_argument('--kind', choices=('package', 'pattern', 'image', 'action'))
    s.add_argument('--title')
    s.add_argument('--append', action='store_true')
    s.set_defaults(f=cmd_doc_put)

    p = sub.add_parser('pattern', help='write a tooling pattern doc')
    pts = p.add_subparsers(dest='action', required=True)
    s = pts.add_parser('set')
    s.add_argument('slug')
    s.add_argument('--title')
    s.add_argument('--append', action='store_true')
    s.set_defaults(f=cmd_pattern_set)

    p = sub.add_parser('trap', help='standing traps')
    ts = p.add_subparsers(dest='action', required=True)
    s = ts.add_parser('add')
    s.add_argument('headline')
    s.add_argument('--detail')
    s.add_argument('--pattern', help='pattern doc slug')
    s.set_defaults(f=cmd_trap_add)
    s = ts.add_parser('edit')
    s.add_argument('id', type=int)
    s.add_argument('--headline')
    s.add_argument('--detail')
    s.add_argument('--pattern')
    s.set_defaults(f=cmd_trap_edit)
    s = ts.add_parser('rm')
    s.add_argument('id', type=int)
    s.set_defaults(f=cmd_trap_rm)

    p = sub.add_parser('action', help='bank a resolved action tag/SHA pair')
    acs = p.add_subparsers(dest='action_cmd', required=True)
    s = acs.add_parser('add')
    s.add_argument('action', help='owner/repo')
    s.add_argument('tag')
    s.add_argument('sha')
    s.add_argument('--annotated', help='the tag object SHA of an annotated tag')
    s.add_argument('--type', choices=('commit', 'annotated'))
    s.add_argument('--note')
    s.set_defaults(f=cmd_action_add)

    p = sub.add_parser('image', help='bank a probed digest')
    ims = p.add_subparsers(dest='action', required=True)
    s = ims.add_parser('add')
    s.add_argument('ref', help='<image>[:<tag>]@<digest>')
    s.add_argument('--fact', action='append', help='key=value (php=8.4.26, os=Debian 13 trixie, ...)')
    s.add_argument('--notes')
    s.add_argument('--line', help='image line, e.g. "php8.4 line"')
    s.set_defaults(f=cmd_image_add)
    s = ims.add_parser('edit', help='change a banked digest: merge facts, replace or append notes')
    s.add_argument('ref', help='the digest (12+ hex) or the full <image>[:<tag>]@<digest>')
    s.add_argument('--fact', action='append', help='key=value, added or overwritten')
    s.add_argument('--drop-fact', action='append', help='a fact key to remove')
    s.add_argument('--notes', help='replace the notes')
    s.add_argument('--append-notes', help='add to the end of the notes')
    s.add_argument('--line')
    s.add_argument('--tag')
    s.set_defaults(f=cmd_image_edit)

    p = sub.add_parser('checks', help="run the PR's local checks from the plan triage wrote (install, build, typecheck, lint without --fix, tests)")
    p.add_argument('--from', dest='from_dir', required=True, help='the triage --out dir holding checks.json')
    p.add_argument('--checkout', action='store_true', help='check out the PR head first (refuses on a dirty tree or local-only commits)')
    p.add_argument('--only', help='comma list of step or check names (prefixes work: lint, install), selected or not')
    p.add_argument('--here', action='store_true', help='run on whatever is checked out (the base, for a baseline) instead of requiring the PR head')
    p.add_argument('--list', action='store_true', help='print the plan without running it')
    p.set_defaults(f=cmd_checks)

    p = sub.add_parser('probe', help='probe image digests, bank their facts and print what moved')
    p.add_argument('refs', nargs='*', help='<image>[:<tag>]@sha256:<digest>: one to describe it, old and new to compare')
    p.add_argument('--from', dest='from_dir', help="a triage --out dir: probe every pair in its probes.json whose new digest is not on record")
    p.add_argument('--platform', help='default: amd64, then arm64, from the manifest')
    p.add_argument('--refresh', action='store_true', help='re-run cached probes and merge their facts into existing records')
    p.add_argument('--no-bank', action='store_true', help='print only, write nothing to the ledger')
    p.set_defaults(f=cmd_probe)

    p = sub.add_parser('note', help="a repo's standing facts")
    ns = p.add_subparsers(dest='action', required=True)
    s = ns.add_parser('add', help='add a note (or update the note with --slug, body on stdin)')
    s.add_argument('gist')
    s.add_argument('--when', help='comma list: bucket, package name or glob, branch, or text in the title/signals')
    s.add_argument('--always', action='store_true', help='print on every triage of the repo')
    s.add_argument('--slug')
    s.add_argument('--append', action='store_true')
    s.add_argument('--repo')
    s.set_defaults(f=cmd_note_add)
    s = ns.add_parser('edit')
    s.add_argument('ref', help='note id or slug')
    s.add_argument('--gist')
    s.add_argument('--when')
    s.add_argument('--always', action='store_true')
    s.add_argument('--slug')
    s.add_argument('--body', action='store_true', help='replace the body from stdin')
    s.add_argument('--append', action='store_true', help='append stdin to the body')
    s.add_argument('--verified', action='store_true', help='clear the unverified flag')
    s.add_argument('--repo')
    s.set_defaults(f=cmd_note_edit)
    s = ns.add_parser('rm')
    s.add_argument('ref')
    s.add_argument('--repo')
    s.set_defaults(f=cmd_note_rm)

    p = sub.add_parser('review', help='reviews')
    rvs = p.add_subparsers(dest='action', required=True)
    s = rvs.add_parser('save', help='save a verdict, entry on stdin')
    s.add_argument('ref', help='owner/name#N')
    s.add_argument('--also', action='append', help='another PR this review covers')
    s.add_argument('--verdict', required=True, choices=('safe', 'not-safe'))
    s.add_argument('--verdict-text', help='the condition, when the call is conditional')
    s.add_argument('--tier', type=int, choices=(0, 1, 2), required=True)
    s.add_argument('--title')
    s.add_argument('--head', help='head SHA reviewed (default: the one triage recorded)')
    s.add_argument('--repo')
    s.set_defaults(f=cmd_review_save)
    s = rvs.add_parser('sync', help='refresh PR state from GitHub (every registered repo, or --repo)')
    s.add_argument('--repo')
    s.set_defaults(f=cmd_review_sync)

    p = sub.add_parser('ui', help='start the web UI if it is not running, and print its URL')
    p.add_argument('--ensure', action='store_true', help='(the default) start only when nothing answers')
    p.add_argument('--stop', action='store_true')
    p.add_argument('--open', action='store_true', help='open it in the browser')
    p.add_argument('--port', type=int, default=UI_PORT)
    p.set_defaults(f=cmd_ui)

    p = sub.add_parser('export', help='write every table as JSON Lines for a backup (every write does this too)')
    p.add_argument('--dir')
    p.set_defaults(f=cmd_export)

    p = sub.add_parser('backup', help='commit export/ in the git repo that holds it, and push')
    p.add_argument('--no-push', action='store_true', help='commit only')
    p.set_defaults(f=cmd_backup)
    return ap


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == 'triage':
        from . import triage
        return triage.main(argv[1:])
    a = build_parser().parse_args(argv)
    conn = store.connect()
    before = conn.total_changes
    try:
        a.f(conn, a)
    except (store.StoreError, repo_mod.RepoError) as e:
        fail(str(e))
    finally:
        if conn.total_changes != before and a.f is not cmd_export:
            store.export(conn)
