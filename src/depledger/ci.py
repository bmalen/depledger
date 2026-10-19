"""What CI actually ran on a PR head, and which other open PRs touch the same files.

Both answer questions a green board cannot: whether the step that exercises a bump ran (and for
how long), and whether another open PR will conflict with this one or already runs its new image.
"""
import datetime
import json
import re
from concurrent.futures import ThreadPoolExecutor

from .parse import run

BOILERPLATE = re.compile(r'^(Set up job|Set up runner|Complete job|Complete runner|Initialize containers|'
                         r'Stop containers|Post |Run actions/checkout)')


def seconds(start, end):
    try:
        a = datetime.datetime.strptime(start, '%Y-%m-%dT%H:%M:%SZ')
        b = datetime.datetime.strptime(end, '%Y-%m-%dT%H:%M:%SZ')
    except (TypeError, ValueError):
        return None
    return int((b - a).total_seconds())


def fmt(sec):
    if sec is None:
        return '?'
    return '%ds' % sec if sec < 60 else '%dm%02ds' % divmod(sec, 60)


def job_line(job, longest=4):
    """One line per job: conclusion, wall time, its longest real steps, then anything failed or skipped."""
    real = [s for s in job.get('steps') or [] if not BOILERPLATE.match(s.get('name', ''))]
    timed = [(s, seconds(s.get('started_at'), s.get('completed_at'))) for s in real]
    ran = sorted([(s, d) for s, d in timed if s.get('conclusion') == 'success'], key=lambda x: -(x[1] or 0))[:longest]
    failed = [s['name'] for s in real if s.get('conclusion') == 'failure']
    skipped = [s['name'] for s in real if s.get('conclusion') == 'skipped']
    line = '- %s %s (%s): %s' % (job.get('name'), job.get('conclusion') or job.get('status'),
                                 fmt(seconds(job.get('started_at'), job.get('completed_at'))),
                                 ' | '.join('%s %s' % (s['name'][:48], fmt(d)) for s, d in ran) or 'no steps reported')
    if failed:
        line += '  FAILED: ' + ', '.join(failed)
    if skipped:
        line += '  skipped: ' + ', '.join(n[:40] for n in skipped)
    return line


def ci_steps(repo, head_sha):
    """Lines describing every GitHub Actions job that ran on head_sha."""
    rc, o, e = run(['gh', 'api', 'repos/%s/commits/%s/check-runs?per_page=100' % (repo, head_sha)], timeout=60)
    if rc != 0:
        return ['(check runs unavailable: %s)' % (e.strip().splitlines() or ['?'])[-1][:120]]
    runs = [r for r in json.loads(o).get('check_runs', []) if (r.get('app') or {}).get('slug') == 'github-actions']
    if not runs:
        return ['(no GitHub Actions jobs on this head)']

    def job(r):
        rc, o, _ = run(['gh', 'api', 'repos/%s/actions/jobs/%s' % (repo, r['id'])], timeout=60)
        return json.loads(o) if rc == 0 else {'name': r.get('name'), 'conclusion': r.get('conclusion'), 'steps': []}
    with ThreadPoolExecutor(max_workers=6) as ex:
        jobs = list(ex.map(job, runs))
    return [job_line(j) for j in sorted(jobs, key=lambda j: j.get('name') or '')]


def overlapping(prs, number, files, new_digests):
    """Other open PRs worth a look: those sharing a changed file, and (when this PR moves image
    digests) those changing a workflow, which may pin the same digest. Pure, for testing."""
    mine = set(files)
    found = []
    for p in prs:
        if int(p['number']) == int(number):
            continue
        theirs = {f['path'] for f in p.get('files') or []}
        shared = sorted(mine & theirs)
        workflows = sorted(f for f in theirs if f.startswith('.github/workflows/')) if new_digests else []
        if shared or workflows:
            found.append({'pr': p, 'shared': shared, 'workflows': workflows})
    return found


def fetch_pr_head(number):
    rc, _, _ = run(['git', 'fetch', '-q', 'origin', 'pull/%s/head' % number], timeout=120)
    if rc != 0:
        return None
    rc, o, _ = run(['git', 'rev-parse', 'FETCH_HEAD'], timeout=10)
    return o.strip() if rc == 0 else None


def merge_conflicts(a, b, cwd=None):
    """Files that conflict when merging commits a and b, [] when clean, None when merge-tree failed."""
    rc, o, _ = run(['git', 'merge-tree', '--write-tree', '--name-only', a, b], timeout=120, cwd=cwd)
    if rc == 0:
        return []
    if rc != 1:
        return None
    files = []
    for line in o.splitlines()[1:]:
        if not line.strip():
            break
        files.append(line.strip())
    return files


def merged_base(base_sha, head_sha, cwd=None):
    """A commit of the base with this PR merged in, so another PR can be tested against the state it
    will actually meet. The commit is a loose object: no ref, index or working tree changes."""
    rc, o, _ = run(['git', 'merge-tree', '--write-tree', base_sha, head_sha], timeout=120, cwd=cwd)
    if rc != 0:
        return None
    rc, sha, _ = run(['git', '-c', 'user.name=depledger', '-c', 'user.email=depledger@localhost', 'commit-tree',
                      o.splitlines()[0], '-p', base_sha, '-p', head_sha, '-m', 'depledger merge check'], timeout=30, cwd=cwd)
    return sha.strip() if rc == 0 else None


def conflict_with(base_sha, merged_sha, other_sha, cwd=None):
    """('new', files) when merging this PR creates conflicts for the other one, ('stale', files) when
    the other PR already conflicts with the base regardless, ('clean', []), or ('unknown', [])."""
    before = merge_conflicts(base_sha, other_sha, cwd)
    after = merge_conflicts(merged_sha, other_sha, cwd) if merged_sha else None
    if before is None or after is None:
        return 'unknown', []
    new = sorted(set(after) - set(before))
    if new:
        return 'new', new
    return ('stale', before) if before else ('clean', [])


def other_open_prs(repo, number, head_sha, base_ref, files, new_digests, git_ok):
    """Lines for the 'Other open PRs' section, plus the numbers of PRs this one conflicts with."""
    rc, o, e = run(['gh', 'pr', 'list', '--repo', repo, '--state', 'open', '--limit', '60',
                    '--json', 'number,title,author,headRefName,files,isDraft'], timeout=120)
    if rc != 0:
        return ['(open PRs unavailable: %s)' % (e.strip().splitlines() or ['?'])[-1][:120]], []
    prs = json.loads(o)
    found = overlapping(prs, number, files, new_digests)
    if not found:
        return ['(%d other open PRs, none touch these files)' % (len(prs) - 1)], []
    base_sha = merged = None
    if git_ok:
        if run(['git', 'cat-file', '-e', head_sha + '^{commit}'], timeout=10)[0] != 0:
            fetch_pr_head(number)
        rc, o, _ = run(['git', 'rev-parse', 'origin/' + base_ref], timeout=10)
        base_sha = o.strip() if rc == 0 else None
        merged = merged_base(base_sha, head_sha) if base_sha else None
    lines, conflicting = [], []
    for f in found:
        p = f['pr']
        who = (p.get('author') or {}).get('login', '?')
        head = fetch_pr_head(p['number']) if git_ok else None
        parts = []
        if f['shared']:
            parts.append('shares %s' % ', '.join(f['shared']))
            kind, c = conflict_with(base_sha, merged, head) if head and base_sha else ('unknown', [])
            if kind == 'new':
                conflicting.append(p['number'])
                parts.append('CONFLICT with this PR in %s: whichever merges second needs a rebase' % ', '.join(c))
            elif kind == 'stale':
                parts.append('already conflicts with %s in %d files, not because of this PR' % (base_ref, len(c)))
            elif kind == 'clean':
                parts.append('merges cleanly alongside this one')
            else:
                parts.append('merge check unavailable')
        if f['workflows'] and head:
            pinned = [d for d in new_digests
                      if run(['git', 'grep', '-q', '-F', d[:12], head, '--', '.github/workflows'], timeout=30)[0] == 0]
            if pinned:
                parts.append("pins this PR's new digest %s in its workflows: that PR's CI runs it" % ', '.join(d[:12] for d in pinned))
        if parts:
            lines.append('- #%s %s (%s%s, branch %s): %s' % (p['number'], p['title'][:70], who, ', draft' if p.get('isDraft') else '',
                                                          p['headRefName'], '; '.join(parts)))
    return lines or ['(%d other open PRs, none relevant)' % (len(prs) - 1)], conflicting
