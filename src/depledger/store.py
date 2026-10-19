"""The only module that touches the database. Everything else calls these functions.

Data lives in $DEPLEDGER_HOME (default ${XDG_DATA_HOME:-~/.local/share}/depledger): the database,
the export/ backup and the UI log. $DEPLEDGER_DB points at a different database file.
"""
import contextlib
import datetime
import fnmatch
import glob
import json
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'schema')
ECOSYSTEMS = ('npm', 'composer', 'actions', 'images', 'cargo', 'other')
STATUSES = ('routine', 'CHECK', 'TRAP')


class StoreError(Exception):
    pass


def data_dir():
    return os.environ.get('DEPLEDGER_HOME') or os.path.join(
        os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share'), 'depledger')


def db_path():
    return os.environ.get('DEPLEDGER_DB') or os.path.join(data_dir(), 'depledger.db')


def export_dir():
    """export/ beside the database file, so a test or scratch database never overwrites the
    backup of the real one."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path())), 'export')


def now():
    return datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')


def connect(path=None):
    path = path or db_path()
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA busy_timeout = 10000')
    conn.execute('PRAGMA foreign_keys = ON')
    migrate(conn)
    return conn


def migrate(conn):
    current = conn.execute('PRAGMA user_version').fetchone()[0]
    for path in sorted(glob.glob(os.path.join(SCHEMA_DIR, '*.sql'))):
        number = int(os.path.basename(path).split('_', 1)[0])
        if number <= current:
            continue
        with open(path) as f:
            sql = f.read()
        conn.executescript('BEGIN IMMEDIATE;\n%s\nPRAGMA user_version = %d;\nCOMMIT;' % (sql, number))


@contextlib.contextmanager
def write(conn):
    """One write transaction. BEGIN IMMEDIATE takes the write lock up front, so two sessions
    writing at once queue on busy_timeout instead of failing halfway."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield conn
    except BaseException:
        conn.execute('ROLLBACK')
        raise
    conn.execute('COMMIT')


def rows(cursor):
    return [dict(r) for r in cursor.fetchall()]


def one(cursor):
    r = cursor.fetchone()
    return dict(r) if r else None


def slugify(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


# ---------------------------------------------------------------- repos

def find_repo(conn, name):
    """A registered repo by `owner/name`, or by bare name when that is unambiguous."""
    r = one(conn.execute('SELECT * FROM repos WHERE full_name = ?', (name,)))
    if r or '/' in name:
        return r
    matches = rows(conn.execute('SELECT * FROM repos WHERE full_name LIKE ?', ('%/' + name,)))
    if len(matches) > 1:
        raise StoreError('%r matches several repos: %s' % (name, ', '.join(m['full_name'] for m in matches)))
    return matches[0] if matches else None


def ensure_repo(conn, full_name, local_path=None):
    r = find_repo(conn, full_name) if '/' in full_name else None
    if r:
        if local_path and r['local_path'] != local_path:
            conn.execute('UPDATE repos SET local_path = ? WHERE id = ?', (local_path, r['id']))
        return r['id']
    if '/' not in full_name:
        raise StoreError('a repo is registered as owner/name, got %r' % full_name)
    return conn.execute('INSERT INTO repos (full_name, local_path) VALUES (?, ?)', (full_name, local_path)).lastrowid


def require_repo(conn, name):
    r = find_repo(conn, name)
    if not r:
        raise StoreError('%s is not a registered repo. Triage one of its PRs first, or run `depledger repo add %s`.' % (name, name))
    return r


def list_repos(conn):
    return rows(conn.execute(
        'SELECT r.*, '
        '(SELECT count(*) FROM reviews v WHERE v.repo_id = r.id) AS review_count, '
        '(SELECT max(reviewed_at) FROM reviews v WHERE v.repo_id = r.id) AS last_reviewed, '
        '(SELECT count(*) FROM notes n WHERE n.repo_id = r.id) AS note_count '
        'FROM repos r ORDER BY r.full_name'))


# ---------------------------------------------------------------- docs

def get_doc(conn, slug):
    return one(conn.execute('SELECT * FROM docs WHERE slug = ?', (slug,)))


def doc_by_id(conn, doc_id):
    return one(conn.execute('SELECT * FROM docs WHERE id = ?', (doc_id,))) if doc_id else None


def put_doc(conn, slug, kind, title=None, body=None, append=False):
    """Create or update a doc. With append, body is added as a new section at the end."""
    d = get_doc(conn, slug)
    if d is None:
        conn.execute('INSERT INTO docs (slug, kind, title, body) VALUES (?, ?, ?, ?)',
                     (slug, kind, title or slug, (body or '').strip('\n') + ('\n' if body else '')))
        return get_doc(conn, slug)
    new_body = d['body']
    if body is not None:
        if append and d['body'].strip():
            new_body = d['body'].rstrip('\n') + '\n\n' + body.strip('\n') + '\n'
        else:
            new_body = body.strip('\n') + '\n'
    conn.execute('UPDATE docs SET title = ?, body = ?, updated_at = ? WHERE id = ?',
                 (title or d['title'], new_body, now(), d['id']))
    return get_doc(conn, slug)


def exposure_checks(conn, doc_id):
    return rows(conn.execute('SELECT * FROM exposure_checks WHERE doc_id = ? ORDER BY position, id', (doc_id,)))


def set_exposure_check(conn, doc_id, commands, label='', replace_id=None):
    if replace_id:
        conn.execute('UPDATE exposure_checks SET commands = ?, label = ?, updated_at = ? WHERE id = ? AND doc_id = ?',
                     (commands, label, now(), replace_id, doc_id))
        return replace_id
    pos = conn.execute('SELECT coalesce(max(position), 0) + 1 FROM exposure_checks WHERE doc_id = ?', (doc_id,)).fetchone()[0]
    return conn.execute('INSERT INTO exposure_checks (doc_id, position, label, commands) VALUES (?, ?, ?, ?)',
                        (doc_id, pos, label, commands)).lastrowid


# ---------------------------------------------------------------- packages

PACKAGE_SELECT = ('SELECT p.*, d.slug AS doc_slug FROM packages p LEFT JOIN docs d ON d.id = p.doc_id ')


def package_index(conn):
    """[(pattern, package)] in ledger order, for lookup()."""
    out = []
    for r in conn.execute(
            'SELECT n.pattern, p.id, p.ecosystem, p.name, p.analysed, p.status, p.gist, p.doc_id, d.slug AS doc_slug '
            'FROM package_names n JOIN packages p ON p.id = n.package_id LEFT JOIN docs d ON d.id = p.doc_id '
            'ORDER BY p.position, p.id, n.id'):
        r = dict(r)
        out.append((r.pop('pattern'), r))
    return out


def lookup(index, name, ecosystem=None):
    """The package row a name belongs to: an exact name first, then a glob, then (for unscoped
    names) the last path segment. A row in the same ecosystem wins a tie."""
    if not name:
        return None

    def pick(cands):
        if not cands:
            return None
        for c in cands:
            if ecosystem and c['ecosystem'] == ecosystem:
                return c
        return cands[0]

    exact = [p for pat, p in index if pat == name]
    if exact:
        return pick(exact)
    globbed = [p for pat, p in index if '*' in pat and fnmatch.fnmatchcase(name, pat)]
    if globbed:
        return pick(globbed)
    if '@' not in name:
        base = name.rsplit('/', 1)[-1]
        based = [p for pat, p in index if '@' not in pat and '*' not in pat and pat.rsplit('/', 1)[-1] == base]
        if based:
            return pick(based)
    return None


def find_package(conn, name, ecosystem=None):
    """A package by display name, any of its names, or its doc slug."""
    by_name = rows(conn.execute(PACKAGE_SELECT + 'WHERE p.name = ? ORDER BY p.position, p.id', (name,)))
    for r in by_name:
        if not ecosystem or r['ecosystem'] == ecosystem:
            return r
    hit = lookup(package_index(conn), name, ecosystem)
    if hit and (not ecosystem or hit['ecosystem'] == ecosystem):
        return one(conn.execute(PACKAGE_SELECT + 'WHERE p.id = ?', (hit['id'],)))
    if ecosystem:
        return None
    return one(conn.execute(PACKAGE_SELECT + 'WHERE d.slug = ? ORDER BY p.position, p.id', (name,)))


def exact_package(conn, ecosystem, name):
    """The row whose display name, or one of whose exact (non-glob) names, is this name. Never
    a glob or last-segment match, so writing `@tiptap/core` cannot land on the `@tiptap/*` row."""
    r = one(conn.execute(PACKAGE_SELECT + 'WHERE p.ecosystem = ? AND p.name = ?', (ecosystem, name)))
    if r:
        return r
    return one(conn.execute(PACKAGE_SELECT + 'JOIN package_names n ON n.package_id = p.id '
                            'WHERE p.ecosystem = ? AND n.pattern = ? ORDER BY p.position, p.id', (ecosystem, name)))


def package_names(conn, package_id):
    return [r[0] for r in conn.execute('SELECT pattern FROM package_names WHERE package_id = ? ORDER BY id', (package_id,))]


def split_names(display):
    """The names a ledger name cell answers to: `react / react-dom`, `@sentry/vue, @unhead/vue`,
    `node (.nvmrc / engines)`."""
    bare = re.sub(r'\s*\(.*?\)', '', display).strip()
    return [n.strip() for n in re.split(r'\s+/\s+|\s+\+\s+|,\s*', bare) if n.strip()] or [display.strip()]


def set_package(conn, ecosystem, name, analysed=None, status=None, gist=None, aliases=(), doc_slug=None, position=None):
    if ecosystem not in ECOSYSTEMS:
        raise StoreError('ecosystem must be one of %s' % ', '.join(ECOSYSTEMS))
    if status is not None and status not in STATUSES:
        raise StoreError('status must be one of %s' % ', '.join(STATUSES))
    existing = exact_package(conn, ecosystem, name)
    doc_id = None
    if doc_slug:
        d = get_doc(conn, doc_slug)
        if not d:
            raise StoreError('no doc %r' % doc_slug)
        doc_id = d['id']
    if existing:
        conn.execute(
            'UPDATE packages SET analysed = ?, status = ?, gist = ?, doc_id = ?, updated_at = ? WHERE id = ?',
            (analysed if analysed is not None else existing['analysed'],
             status or existing['status'],
             gist if gist is not None else existing['gist'],
             doc_id if doc_slug else existing['doc_id'],
             now(), existing['id']))
        pid = existing['id']
    else:
        if status is None:
            raise StoreError('a new package needs --status')
        if position is None:
            position = conn.execute('SELECT coalesce(max(position), 0) + 1 FROM packages').fetchone()[0]
        pid = conn.execute(
            'INSERT INTO packages (ecosystem, name, analysed, status, gist, doc_id, position) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (ecosystem, name, analysed or '', status, gist or '', doc_id, position)).lastrowid
        for n in split_names(name):
            conn.execute('INSERT OR IGNORE INTO package_names (package_id, pattern) VALUES (?, ?)', (pid, n))
    for a in aliases:
        conn.execute('INSERT OR IGNORE INTO package_names (package_id, pattern) VALUES (?, ?)', (pid, a))
    return one(conn.execute(PACKAGE_SELECT + 'WHERE p.id = ?', (pid,)))


def ensure_package_doc(conn, package):
    """The doc behind a package row, created (and linked) when the row has none."""
    if package['doc_id']:
        return doc_by_id(conn, package['doc_id'])
    slug = slugify(package['name'])
    if get_doc(conn, slug):
        slug = '%s-%s' % (package['ecosystem'], slug)
    d = put_doc(conn, slug, 'package', title=package['name'], body='')
    conn.execute('UPDATE packages SET doc_id = ?, updated_at = ? WHERE id = ?', (d['id'], now(), package['id']))
    return d


def list_packages(conn, status=None, ecosystem=None):
    sql = ('SELECT p.*, d.slug AS doc_slug, '
           '(SELECT count(DISTINCT pr.repo_id) FROM package_names n JOIN pr_bumps b ON b.name GLOB n.pattern '
           'JOIN prs pr ON pr.id = b.pr_id WHERE n.package_id = p.id) AS repo_count '
           'FROM packages p LEFT JOIN docs d ON d.id = p.doc_id WHERE 1 = 1')
    args = []
    if status:
        sql += ' AND p.status = ?'
        args.append(status)
    if ecosystem:
        sql += ' AND p.ecosystem = ?'
        args.append(ecosystem)
    return rows(conn.execute(sql + ' ORDER BY p.ecosystem, p.position, p.id', args))


def packages_for_doc(conn, doc_id):
    return rows(conn.execute('SELECT * FROM packages WHERE doc_id = ? ORDER BY position, id', (doc_id,)))


# ---------------------------------------------------------------- traps

def list_traps(conn):
    return rows(conn.execute('SELECT t.*, d.slug AS doc_slug FROM traps t LEFT JOIN docs d ON d.id = t.doc_id ORDER BY t.position, t.id'))


def add_trap(conn, headline, detail='', doc_slug=None):
    doc_id = None
    if doc_slug:
        d = get_doc(conn, doc_slug)
        if not d:
            raise StoreError('no doc %r' % doc_slug)
        doc_id = d['id']
    pos = conn.execute('SELECT coalesce(max(position), 0) + 1 FROM traps').fetchone()[0]
    return conn.execute('INSERT INTO traps (position, headline, detail, doc_id) VALUES (?, ?, ?, ?)',
                        (pos, headline, detail, doc_id)).lastrowid


def edit_trap(conn, trap_id, headline=None, detail=None, doc_slug=None):
    t = one(conn.execute('SELECT * FROM traps WHERE id = ?', (trap_id,)))
    if not t:
        raise StoreError('no trap %s' % trap_id)
    doc_id = t['doc_id']
    if doc_slug is not None:
        doc_id = get_doc(conn, doc_slug)['id'] if doc_slug else None
    conn.execute('UPDATE traps SET headline = ?, detail = ?, doc_id = ?, updated_at = ? WHERE id = ?',
                 (headline if headline is not None else t['headline'], detail if detail is not None else t['detail'],
                  doc_id, now(), trap_id))


# ---------------------------------------------------------------- notes

def split_tags(text):
    """Tags from a comma list. Parentheticals are dropped first, so
    `laravel/framework (SessionGuard, Auth)` is one tag."""
    text = re.sub(r'\s*\([^)]*\)', '', text or '')
    return [t.strip() for t in text.split(',') if t.strip()]


def list_notes(conn, repo_id):
    return rows(conn.execute('SELECT * FROM notes WHERE repo_id = ? ORDER BY position, id', (repo_id,)))


def get_note(conn, repo_id, ref):
    if str(ref).isdigit():
        return one(conn.execute('SELECT * FROM notes WHERE id = ? AND repo_id = ?', (int(ref), repo_id)))
    return one(conn.execute('SELECT * FROM notes WHERE slug = ? AND repo_id = ?', (ref, repo_id)))


def add_note(conn, repo_id, gist, when_tags='', slug=None, body=None, append=False, unverified=False):
    """Create a note, or update the note with this slug."""
    existing = get_note(conn, repo_id, slug) if slug else None
    if existing:
        new_body = existing['body']
        if body:
            new_body = (existing['body'].rstrip('\n') + '\n\n' + body.strip('\n') + '\n') if (append and existing['body']) else body.strip('\n') + '\n'
        conn.execute('UPDATE notes SET gist = ?, when_tags = ?, body = ?, updated_at = ? WHERE id = ?',
                     (gist or existing['gist'], when_tags if when_tags is not None else existing['when_tags'], new_body, now(), existing['id']))
        return existing['id']
    pos = conn.execute('SELECT coalesce(max(position), 0) + 1 FROM notes WHERE repo_id = ?', (repo_id,)).fetchone()[0]
    return conn.execute(
        'INSERT INTO notes (repo_id, slug, gist, body, when_tags, unverified, position) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (repo_id, slug, gist, (body.strip('\n') + '\n') if body else None, when_tags or '', 1 if unverified else 0, pos)).lastrowid


def edit_note(conn, note_id, gist=None, when_tags=None, body=None, append=False, slug=None, verified=None):
    n = one(conn.execute('SELECT * FROM notes WHERE id = ?', (note_id,)))
    if not n:
        raise StoreError('no note %s' % note_id)
    new_body = n['body']
    if body is not None:
        new_body = (n['body'].rstrip('\n') + '\n\n' + body.strip('\n') + '\n') if (append and n['body']) else (body.strip('\n') + '\n' if body.strip() else None)
    conn.execute(
        'UPDATE notes SET gist = ?, when_tags = ?, body = ?, slug = ?, unverified = ?, updated_at = ? WHERE id = ?',
        (gist if gist is not None else n['gist'], when_tags if when_tags is not None else n['when_tags'], new_body,
         slug if slug is not None else n['slug'],
         n['unverified'] if verified is None else (0 if verified else 1), now(), note_id))


def tag_matches(tag, ctx):
    """ctx: buckets (list), names (package, image and action names in the PR), text (title,
    branch and signals, lowercased)."""
    t = tag.strip().lower()
    if not t:
        return False
    for b in ctx.get('buckets', ()):
        b = b.lower()
        if t == b or t == b.split(' ')[0]:
            return True
    for n in ctx.get('names', ()):
        n = n.lower()
        if n == t or ('*' in t and fnmatch.fnmatchcase(n, t)):
            return True
    return t in ctx.get('text', '')


def notes_for(conn, repo_id, ctx):
    """(always, matched, hidden) for one PR."""
    always, matched, hidden = [], [], []
    for n in list_notes(conn, repo_id):
        tags = split_tags(n['when_tags'])
        if not tags:
            always.append(n)
        elif any(tag_matches(t, ctx) for t in tags):
            matched.append(n)
        else:
            hidden.append(n)
    return always, matched, hidden


def notes_tagged_for(conn, names, exclude_repo_id=None):
    """Notes in any repo whose tags name one of these packages (exact or glob), for show package."""
    out = []
    lowered = [n.lower() for n in names]
    for n in rows(conn.execute('SELECT n.*, r.full_name AS repo FROM notes n JOIN repos r ON r.id = n.repo_id '
                               "WHERE n.when_tags != '' ORDER BY r.full_name, n.position")):
        if exclude_repo_id and n['repo_id'] == exclude_repo_id:
            continue
        for t in split_tags(n['when_tags']):
            t = t.lower()
            if any(t == x or ('*' in t and fnmatch.fnmatchcase(x, t)) or ('*' in x and fnmatch.fnmatchcase(t, x)) for x in lowered):
                out.append(n)
                break
    return out


# ---------------------------------------------------------------- PRs and reviews

def get_pr(conn, repo_id, number):
    return one(conn.execute('SELECT * FROM prs WHERE repo_id = ? AND number = ?', (repo_id, int(number))))


def upsert_pr(conn, repo_id, number, **fields):
    """Insert or update a PR row. Only the fields given (and not None) are written."""
    allowed = ('title', 'branch', 'base', 'url', 'author', 'state', 'head_sha', 'opened_at', 'merged_at',
               'triaged_at', 'proposed_tier', 'synced_at')
    fields = dict((k, v) for k, v in fields.items() if k in allowed and v is not None)
    pr = get_pr(conn, repo_id, number)
    if pr is None:
        cols = ['repo_id', 'number'] + list(fields)
        conn.execute('INSERT INTO prs (%s) VALUES (%s)' % (', '.join(cols), ', '.join('?' * len(cols))),
                     [repo_id, int(number)] + list(fields.values()))
    elif fields:
        conn.execute('UPDATE prs SET %s WHERE id = ?' % ', '.join('%s = ?' % k for k in fields),
                     list(fields.values()) + [pr['id']])
    return get_pr(conn, repo_id, number)


def set_bumps(conn, pr_id, bumps):
    """Replace a PR's bumps. bumps: dicts with kind, ecosystem, name, section, from, to, type."""
    conn.execute('DELETE FROM pr_bumps WHERE pr_id = ?', (pr_id,))
    for b in bumps:
        conn.execute(
            'INSERT OR REPLACE INTO pr_bumps (pr_id, kind, ecosystem, name, section, from_version, to_version, bump_type) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (pr_id, b['kind'], b['ecosystem'], b['name'], b.get('section'), b.get('from'), b.get('to'), b.get('type')))


def pr_bumps(conn, pr_id):
    return rows(conn.execute('SELECT * FROM pr_bumps WHERE pr_id = ? ORDER BY kind, name', (pr_id,)))


def record_triage(conn, repo_full_name, number, pr_fields, bumps, local_path=None):
    with write(conn):
        repo_id = ensure_repo(conn, repo_full_name, local_path)
        pr_fields = dict(pr_fields, triaged_at=now(), synced_at=now())
        pr = upsert_pr(conn, repo_id, number, **pr_fields)
        set_bumps(conn, pr['id'], bumps)
    return pr


def reviews_for_pr(conn, pr_id):
    return rows(conn.execute(
        'SELECT v.* FROM reviews v JOIN review_prs rp ON rp.review_id = v.id WHERE rp.pr_id = ? '
        'ORDER BY v.reviewed_at, v.id', (pr_id,)))


def save_review(conn, repo_id, numbers, verdict=None, verdict_text=None, tier=None, title=None, body='',
                head_sha=None, reviewed_at=None, source='skill'):
    """Record a review of one or more PRs (the first is primary). A review of the same primary PR
    at the same head is replaced, so re-saving after a correction does not add a second entry."""
    if verdict not in (None, 'safe', 'not_safe'):
        raise StoreError('verdict must be safe or not_safe')
    prs = [upsert_pr(conn, repo_id, n) for n in numbers]
    primary = prs[0]
    head_sha = head_sha or primary['head_sha']
    reviewed_at = reviewed_at or now()
    title = title or primary['title'] or ('PR #%s' % primary['number'])
    existing = None
    for v in reviews_for_pr(conn, primary['id']):
        if v['source'] == source and (v['head_sha'] or None) == (head_sha or None):
            existing = v
    if existing:
        conn.execute(
            'UPDATE reviews SET verdict = ?, verdict_text = ?, tier = ?, title = ?, body = ?, reviewed_at = ?, updated_at = ? WHERE id = ?',
            (verdict, verdict_text, tier, title, body, reviewed_at, now(), existing['id']))
        review_id = existing['id']
    else:
        review_id = conn.execute(
            'INSERT INTO reviews (repo_id, reviewed_at, head_sha, verdict, verdict_text, tier, title, body, source) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (repo_id, reviewed_at, head_sha, verdict, verdict_text, tier, title, body, source)).lastrowid
    for i, pr in enumerate(prs):
        conn.execute('INSERT OR REPLACE INTO review_prs (review_id, pr_id, is_primary) VALUES (?, ?, ?)',
                     (review_id, pr['id'], 1 if i == 0 else 0))
    return review_id, bool(existing)


def get_review(conn, review_id):
    v = one(conn.execute('SELECT v.*, r.full_name AS repo FROM reviews v JOIN repos r ON r.id = v.repo_id WHERE v.id = ?', (review_id,)))
    if v:
        v['prs'] = rows(conn.execute(
            'SELECT p.*, r.full_name AS repo, rp.is_primary FROM review_prs rp JOIN prs p ON p.id = rp.pr_id '
            'JOIN repos r ON r.id = p.repo_id WHERE rp.review_id = ? ORDER BY rp.is_primary DESC, p.number', (review_id,)))
    return v


BUMP_HISTORY_SQL = (
    'SELECT b.kind, b.ecosystem, b.name, b.from_version, b.to_version, b.bump_type, b.section, '
    'p.id AS pr_id, p.number, p.title AS pr_title, p.state, p.url, p.triaged_at, r.full_name AS repo, r.id AS repo_id, '
    'v.id AS review_id, v.verdict, v.verdict_text, v.tier, v.reviewed_at '
    'FROM pr_bumps b JOIN prs p ON p.id = b.pr_id JOIN repos r ON r.id = p.repo_id '
    'LEFT JOIN review_prs rp ON rp.pr_id = p.id LEFT JOIN reviews v ON v.id = rp.review_id ')


def bump_history(conn, patterns, repo_id=None, exclude_repo_id=None, exclude_pr_id=None, limit=None, per_pr=False):
    """Every PR that moved a name matching one of the patterns, newest first, with its latest
    review (if any). per_pr keeps one row per PR (react and react-dom moving together)."""
    if not patterns:
        return []
    where = ['(%s)' % ' OR '.join(['b.name GLOB ?'] * len(patterns))]
    args = list(patterns)
    if repo_id:
        where.append('p.repo_id = ?')
        args.append(repo_id)
    if exclude_repo_id:
        where.append('p.repo_id != ?')
        args.append(exclude_repo_id)
    if exclude_pr_id:
        where.append('p.id != ?')
        args.append(exclude_pr_id)
    found = rows(conn.execute(BUMP_HISTORY_SQL + 'WHERE ' + ' AND '.join(where) +
                              ' ORDER BY coalesce(v.reviewed_at, p.triaged_at, p.opened_at) DESC, v.id DESC', args))
    seen = set()
    out = []
    for f in found:
        key = f['pr_id'] if per_pr else (f['pr_id'], f['name'])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out[:limit] if limit else out


def reviews_mentioning(conn, repo_id, text, exclude_pr_id=None, limit=3):
    """Reviews (in one repo, or all when repo_id is None) whose text mentions a string: the
    fallback for legacy reviews whose bumps could not be backfilled, and for digests named in prose."""
    args = ['%' + text + '%', '%' + text + '%']
    sql = ('SELECT DISTINCT v.id, v.title, v.reviewed_at, v.verdict, p.number, r.full_name AS repo FROM reviews v '
           'JOIN repos r ON r.id = v.repo_id '
           'JOIN review_prs rp ON rp.review_id = v.id AND rp.is_primary = 1 JOIN prs p ON p.id = rp.pr_id '
           'WHERE (v.body LIKE ? OR v.title LIKE ?)')
    if repo_id:
        sql += ' AND v.repo_id = ?'
        args.append(repo_id)
    if exclude_pr_id:
        sql += ' AND p.id != ?'
        args.append(exclude_pr_id)
    return rows(conn.execute(sql + ' ORDER BY v.reviewed_at DESC LIMIT ?', args + [limit]))


def list_reviews(conn, repo_id=None, verdict=None, limit=200):
    sql = ('SELECT v.id, v.reviewed_at, v.verdict, v.verdict_text, v.tier, v.title, v.head_sha, r.full_name AS repo, '
           'p.number, p.state, p.url FROM reviews v JOIN repos r ON r.id = v.repo_id '
           'LEFT JOIN review_prs rp ON rp.review_id = v.id AND rp.is_primary = 1 LEFT JOIN prs p ON p.id = rp.pr_id WHERE 1 = 1')
    args = []
    if repo_id:
        sql += ' AND v.repo_id = ?'
        args.append(repo_id)
    if verdict:
        sql += ' AND v.verdict = ?'
        args.append(verdict)
    return rows(conn.execute(sql + ' ORDER BY v.reviewed_at DESC, v.id DESC LIMIT ?', args + [limit]))


# ---------------------------------------------------------------- images and actions

def find_digest(conn, digest):
    d = (digest or '').replace('sha256:', '')[:12]
    if len(d) < 12:
        return []
    return rows(conn.execute('SELECT * FROM image_digests WHERE digest12 = ? ORDER BY id', (d,)))


def add_image(conn, image, tag, digest, facts=None, notes='', line=''):
    digest = digest.replace('sha256:', '')
    if not re.fullmatch(r'[0-9a-f]{12,64}', digest):
        raise StoreError('digest must be at least 12 hex characters, got %r' % digest)
    existing = find_digest(conn, digest)
    if existing:
        return existing[0], False
    pos = conn.execute('SELECT coalesce(max(position), 0) + 1 FROM image_digests').fetchone()[0]
    rid = conn.execute(
        'INSERT INTO image_digests (image, tag, digest, digest12, line, facts, notes, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (image, tag or '', digest, digest[:12], line or '', json.dumps(facts or {}), notes or '', pos)).lastrowid
    return one(conn.execute('SELECT * FROM image_digests WHERE id = ?', (rid,))), True


def edit_image(conn, image_id, facts=None, drop_facts=(), notes=None, append_notes=None, line=None, tag=None):
    """Merge facts, replace or append notes, retag. Every change keeps the old row in revisions."""
    row = one(conn.execute('SELECT * FROM image_digests WHERE id = ?', (image_id,)))
    if not row:
        raise StoreError('no image record %s' % image_id)
    merged = json.loads(row['facts'] or '{}')
    merged.update(facts or {})
    for k in drop_facts or ():
        merged.pop(k, None)
    new_notes = row['notes'] if notes is None else notes
    if append_notes:
        new_notes = (new_notes.rstrip() + ' ' + append_notes.strip()).strip()
    conn.execute('UPDATE image_digests SET facts = ?, notes = ?, line = ?, tag = ?, updated_at = ? WHERE id = ?',
                 (json.dumps(merged), new_notes, row['line'] if line is None else line,
                  row['tag'] if tag is None else tag, now(), image_id))
    return one(conn.execute('SELECT * FROM image_digests WHERE id = ?', (image_id,)))


def find_action(conn, sha, action=None):
    sha = (sha or '')[:12]
    if len(sha) < 7:
        return []
    sql = 'SELECT * FROM action_shas WHERE substr(sha, 1, 12) = ?'
    args = [sha]
    if action:
        sql += ' AND action = ?'
        args.append(action)
    return rows(conn.execute(sql + ' ORDER BY id', args))


def add_action(conn, action, tag, sha, tag_type='', annotated_sha=None, note=''):
    existing = one(conn.execute('SELECT * FROM action_shas WHERE action = ? AND tag = ?', (action, tag)))
    if existing:
        return existing, False
    pos = conn.execute('SELECT coalesce(max(position), 0) + 1 FROM action_shas').fetchone()[0]
    rid = conn.execute(
        'INSERT INTO action_shas (action, tag, sha, tag_type, annotated_sha, note, position) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (action, tag, sha, tag_type, annotated_sha, note, pos)).lastrowid
    return one(conn.execute('SELECT * FROM action_shas WHERE id = ?', (rid,))), True


# ---------------------------------------------------------------- search and history

def fts_query(text):
    """Quote each word so FTS syntax characters in package names (`-`, `/`, `@`) are literal.
    Trigram needs three characters, so shorter words are dropped."""
    words = [w for w in text.split() if len(w) >= 3]
    return ' '.join('"%s"' % w.replace('"', '""') for w in words)


def search(conn, text, repo=None, kind=None, limit=30):
    q = fts_query(text)
    if not q:
        raise StoreError('search words need at least three characters')
    sql = ("SELECT kind, ref_id, repo, title, snippet(search, -1, '[', ']', '...', 64) AS snippet, rank "
           'FROM search WHERE search MATCH ?')
    args = [q]
    if repo:
        sql += ' AND (repo = ? OR repo IS NULL)'
        args.append(repo)
    if kind:
        sql += ' AND kind = ?'
        args.append(kind)
    return rows(conn.execute(sql + ' ORDER BY rank LIMIT ?', args + [limit]))


def export(conn, out_dir=None):
    """One JSON Lines file per table, in id order, so the ledger can be backed up and diffed in
    git. Every CLI write calls this, so export/ is always current."""
    out_dir = out_dir or export_dir()
    os.makedirs(out_dir, exist_ok=True)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'search%' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:
        path = os.path.join(out_dir, t + '.jsonl')
        with open(path + '.tmp', 'w') as f:
            for r in conn.execute('SELECT * FROM %s ORDER BY rowid' % t):
                f.write(json.dumps(dict(r), ensure_ascii=False, sort_keys=True) + '\n')
        os.replace(path + '.tmp', path)
    return out_dir


def revisions(conn, table_name, row_id):
    return rows(conn.execute('SELECT * FROM revisions WHERE table_name = ? AND row_id = ? ORDER BY id DESC', (table_name, row_id)))
