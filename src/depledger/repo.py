"""Which GitHub repo a command is about. Taken from the git remote, never from the cwd's path,
so a write can only land on a real repo."""
import os
import re
import subprocess

from . import store



class RepoError(Exception):
    pass


def parse_remote(url):
    """`owner/name` from an ssh or https GitHub remote, or None."""
    url = (url or '').strip()
    m = re.match(r'^(?:git@github\.com:|ssh://git@github\.com/|https?://(?:[^@/]+@)?github\.com/)([^/]+)/(.+?)(?:\.git)?/?$', url)
    return '%s/%s' % (m.group(1), m.group(2)) if m else None


def toplevel(cwd=None):
    try:
        p = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=cwd, capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def current_repo(cwd=None):
    """(owner/name, toplevel path) for the git repo at cwd. Raises RepoError outside a GitHub
    repo, and inside this tool's code, its data directory or ~/.claude (where Claude Code skills
    live), where a write would be a stray."""
    cwd = os.path.realpath(cwd or os.getcwd())
    for root in (store.ROOT, store.data_dir(), os.path.expanduser('~/.claude')):
        root = os.path.realpath(root)
        if cwd == root or cwd.startswith(root + os.sep):
            raise RepoError('%s is not a project repo; cd into the repo the PR belongs to, or pass --repo owner/name' % cwd)
    top = toplevel(cwd)
    if not top:
        raise RepoError('%s is not inside a git repo; pass --repo owner/name' % cwd)
    p = subprocess.run(['git', 'remote', 'get-url', 'origin'], cwd=top, capture_output=True, text=True, timeout=10)
    name = parse_remote(p.stdout) if p.returncode == 0 else None
    if not name:
        raise RepoError('%s has no GitHub origin remote; pass --repo owner/name' % top)
    return name, top


def resolve(conn, explicit=None, cwd=None, register=False):
    """The repo row a command targets: --repo when given (owner/name, or a registered bare name),
    else the cwd's remote. Only registered repos are accepted unless register is set."""
    if explicit:
        r = store.find_repo(conn, explicit)
        if r:
            return r
        if not register or '/' not in explicit:
            raise RepoError('%s is not a registered repo (see `depledger repos`)' % explicit)
        with store.write(conn):
            store.ensure_repo(conn, explicit)
        return store.find_repo(conn, explicit)
    name, top = current_repo(cwd)
    r = store.find_repo(conn, name)
    if r:
        return r
    if not register:
        raise RepoError('%s is not a registered repo yet. Triage one of its PRs first, or run `depledger repo add`.' % name)
    with store.write(conn):
        store.ensure_repo(conn, name, top)
    return store.find_repo(conn, name)


def parse_pr_ref(ref):
    """`owner/name#123`, `name#123`, `#123`, `123` or a PR URL -> (repo or None, number)."""
    ref = ref.strip()
    m = re.search(r'github\.com/([^/]+/[^/]+)/pull/(\d+)', ref)
    if m:
        return m.group(1), int(m.group(2))
    m = re.fullmatch(r'(?:([\w.-]+(?:/[\w.-]+)?)#)?#?(\d+)', ref)
    if m:
        return m.group(1), int(m.group(2))
    raise RepoError('cannot read a PR from %r (use owner/name#123)' % ref)
