"""Probe image digests: what runtime versions, extensions and packages are inside, and what moved.

Each digest is probed once per machine. The in-container script's output is cached by digest and
platform under $DEPLEDGER_PROBE_DIR (default <data dir>/probes), so the old side of a PR (usually
the new side of the previous PR) costs no pull. Facts are normalised to the same keys every time
and banked in image_digests, so the next triage anywhere prints them instead of asking for a probe.
"""
import datetime
import glob
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

from . import store

INSIDE = r'''
echo "os=$(grep PRETTY_NAME /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d '"')"
find_bin() { command -v "$1" 2>/dev/null || { [ -x "/$1" ] && echo "/$1"; }; }
for c in php node bun composer caddy postgres psql redis-server valkey-server mysqld nginx mailpit gotenberg chromium google-chrome soffice; do
  b=$(find_bin "$c")
  [ -n "$b" ] || continue
  case "$c" in
    php) v=$("$b" -v 2>/dev/null | head -1) ;;
    node|bun) v=$("$b" -v 2>/dev/null) ;;
    caddy|mailpit) v=$("$b" version 2>/dev/null | head -1) ;;
    nginx) v=$("$b" -v 2>&1) ;;
    gotenberg) v=$(timeout 10 "$b" 2>&1 | grep -m1 '^Version:') ;;
    redis-server|valkey-server) v=$("$b" --version 2>/dev/null | sed 's/ build=.*//') ;;
    *) v=$("$b" --version 2>/dev/null | head -1) ;;
  esac
  echo "$c=$v"
done
if command -v php >/dev/null 2>&1; then
  echo "php_modules_md5=$(php -m 2>/dev/null | md5sum | cut -c1-32)"
  echo "php_modules=$(php -m 2>/dev/null | grep -v '^\[' | grep -v '^$' | tr '\n' ' ')"
fi
for d in /usr/share/postgresql/*/extension /usr/local/share/postgresql/extension; do
  [ -d "$d" ] || continue
  for e in timescaledb vector postgis pg_cron; do
    f="$d/$e.control"
    [ -f "$f" ] && echo "pg_ext_$e=$(sed -n "s/^default_version *= *'\(.*\)'.*/\1/p" "$f")"
  done
done
if command -v dpkg-query >/dev/null 2>&1; then
  dpkg-query -W -f='${Package} ${Version}\n' 2>/dev/null | sort > /tmp/pkgs
  m=dpkg
elif command -v apk >/dev/null 2>&1; then
  apk info -v 2>/dev/null | sort > /tmp/pkgs
  m=apk
elif command -v rpm >/dev/null 2>&1; then
  rpm -qa --qf '%{NAME} %{VERSION}-%{RELEASE}\n' 2>/dev/null | sort > /tmp/pkgs
  m=rpm
fi
if [ -n "${m:-}" ]; then
  echo "${m}_count=$(wc -l < /tmp/pkgs | tr -d ' ')"
  echo "${m}_md5=$(md5sum < /tmp/pkgs | cut -c1-32)"
fi
echo "---PACKAGES---"
[ -f /tmp/pkgs ] && cat /tmp/pkgs
exit 0
'''

VERSION_KEYS = (('php', 'php'), ('node', 'node'), ('bun', 'bun'), ('composer', 'composer'), ('caddy', 'caddy'),
                ('postgres', 'postgres'), ('psql', 'psql'), ('redis-server', 'redis'), ('valkey-server', 'valkey'),
                ('mysqld', 'mysql'), ('nginx', 'nginx'), ('mailpit', 'mailpit'), ('gotenberg', 'gotenberg'),
                ('chromium', 'chromium'), ('google-chrome', 'chrome'), ('soffice', 'libreoffice'))
EXT_KEYS = (('pg_ext_timescaledb', 'timescaledb'), ('pg_ext_vector', 'pgvector'), ('pg_ext_postgis', 'postgis'),
            ('pg_ext_pg_cron', 'pg_cron'))
PACKAGE_MANAGERS = ('dpkg', 'apk', 'rpm')
REF_RE = re.compile(r'^(.+?)(?::([^@:/]+))?@(?:sha256:)?([0-9a-f]{64})$')


def probe_dir():
    return os.environ.get('DEPLEDGER_PROBE_DIR') or os.path.join(store.data_dir(), 'probes')


def parse_ref(ref):
    m = REF_RE.match(ref)
    if not m:
        raise store.StoreError('expected <image>[:<tag>]@sha256:<64 hex>, got %r' % ref)
    return m.group(1), m.group(2) or '', m.group(3)


def first_version(text):
    m = re.search(r'(\d+(?:\.\d+)+)', text or '')
    return m.group(1) if m else ''


def normalise(raw):
    """The probe header as banked facts: one version per runtime, extension versions, the PHP
    module and package fingerprints. Keys match the records banked by hand before this existed."""
    facts = {}
    if raw.get('os'):
        facts['os'] = raw['os']
    for src, key in VERSION_KEYS:
        if src in raw:
            facts[key] = first_version(raw[src]) or raw[src]
    if facts.get('psql') and facts.get('psql') == facts.get('postgres'):
        del facts['psql']
    built = re.search(r'built: (\w{3}) +(\d+) (\d{4})', raw.get('php', ''))
    if built:
        facts['built'] = datetime.datetime.strptime(' '.join(built.groups()), '%b %d %Y').strftime('%Y-%m-%d')
    for src, key in EXT_KEYS:
        if raw.get(src):
            facts[key] = raw[src]
    if raw.get('php_modules_md5'):
        facts['modules md5'] = raw['php_modules_md5']
    for pm in PACKAGE_MANAGERS:
        if raw.get(pm + '_md5'):
            facts[pm + ' md5'] = raw[pm + '_md5']
            facts[pm] = raw[pm + '_count']
    return facts


def split_header(dump):
    head, _, pkgs = dump.partition('---PACKAGES---\n')
    raw = dict(l.split('=', 1) for l in head.splitlines() if '=' in l)
    return raw, [l for l in pkgs.splitlines() if l.strip()]


def package_map(lines):
    out = {}
    for l in lines:
        if ' ' in l:
            name, ver = l.split(' ', 1)
        else:
            m = re.match(r'^(.+?)-(\d[^-]*-r\d+)$', l)
            name, ver = (m.group(1), m.group(2)) if m else (l, '')
        out[name] = ver
    return out


def pick_platform(ref):
    """amd64, then arm64, then the first platform in the ref's manifest list; '' for a
    single-manifest image; None when the manifest cannot be read."""
    try:
        raw = subprocess.run(['docker', 'buildx', 'imagetools', 'inspect', ref, '--raw'],
                             capture_output=True, text=True, timeout=60).stdout
        manifests = json.loads(raw).get('manifests', [])
    except (ValueError, subprocess.SubprocessError, OSError):
        return None
    plats = ['%s/%s' % (m['platform']['os'], m['platform']['architecture'])
             for m in manifests if m.get('platform', {}).get('os') not in (None, 'unknown')]
    for want in ('linux/amd64', 'linux/arm64'):
        if want in plats:
            return want
    return plats[0] if plats else ''


def cached_platform(digest):
    """The platform of a dump already cached for this digest (amd64, then arm64, then any), or None."""
    found = [os.path.basename(p)[len(digest) + 1:-len('.txt')] for p in glob.glob(os.path.join(probe_dir(), digest + '_*.txt'))]
    for want in ('linux-amd64', 'linux-arm64') + tuple(sorted(found)):
        if want in found:
            return '' if want == 'default' else want.replace('-', '/')
    return None


def choose_platform(ref, platform=None):
    """The platform to probe a ref on: the one given, else that of a dump already cached for its
    digest (no registry call), else the manifest's. None when the manifest cannot be read, so a
    probe never silently runs on the host's own platform (arm64 on a Mac) instead."""
    if platform is not None:
        return platform
    cached = cached_platform(parse_ref(ref)[2])
    return cached if cached is not None else pick_platform(ref)


def run_probe(ref, platform=None, refresh=False):
    """{'ref', 'platform', 'raw', 'packages', 'dump'} or {'ref', 'error'}. Cached by digest+platform.
    platform: as choose_platform() takes it; '' runs on the default platform."""
    _, _, digest = parse_ref(ref)
    plat = choose_platform(ref, platform)
    if plat is None:
        return {'ref': ref, 'error': 'could not read the manifest to choose a platform (pass --platform)'}
    os.makedirs(probe_dir(), exist_ok=True)
    dump_path = os.path.join(probe_dir(), '%s_%s.txt' % (digest, (plat or 'default').replace('/', '-')))
    if refresh or not os.path.exists(dump_path):
        args = ['docker', 'run', '--rm', '-i'] + (['--platform', plat] if plat else []) + ['--entrypoint', 'sh', ref, '-s']
        try:
            p = subprocess.run(args, input=INSIDE, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            return {'ref': ref, 'error': 'timed out after 900s'}
        except OSError as e:
            return {'ref': ref, 'error': 'docker did not start: %s' % e}
        if p.returncode != 0 or '---PACKAGES---' not in p.stdout:
            tail = ' | '.join(l for l in p.stderr.strip().splitlines()[-3:]) or 'no stderr'
            return {'ref': ref, 'error': 'exit %d (platform %s): %s' % (p.returncode, plat or 'default', tail)}
        with open(dump_path + '.tmp', 'w') as f:
            f.write(p.stdout)
        os.replace(dump_path + '.tmp', dump_path)
    with open(dump_path) as f:
        raw, packages = split_header(f.read())
    return {'ref': ref, 'platform': plat or 'default', 'raw': raw, 'packages': packages, 'dump': dump_path}


def compare(old, new):
    """What moved between two probes: a one-word class, runtime moves, module and package deltas."""
    fo, fn = normalise(old['raw']), normalise(new['raw'])
    fingerprint = {'modules md5'} | {pm + s for pm in PACKAGE_MANAGERS for s in (' md5', '')}
    moved = [(k, fo.get(k, '-'), fn.get(k, '-')) for k in list(dict.fromkeys(list(fo) + list(fn)))
             if k not in fingerprint and k != 'built' and fo.get(k) != fn.get(k)]
    mo = set(old['raw'].get('php_modules', '').split())
    mn = set(new['raw'].get('php_modules', '').split())
    po, pn = package_map(old['packages']), package_map(new['packages'])
    changed = [(k, po[k], pn[k]) for k in sorted(po) if k in pn and po[k] != pn[k]]
    added = sorted(set(pn) - set(po))
    removed = sorted(set(po) - set(pn))
    if moved:
        kind = 'VERSION MOVE'
    elif changed or added or removed or mo != mn:
        kind = 'PACKAGE REFRESH'
    elif fo.get('built') != fn.get('built'):
        kind = 'REBUILD ONLY'
    else:
        kind = 'IDENTICAL'
    return {'kind': kind, 'moved': moved, 'modules_added': sorted(mn - mo), 'modules_removed': sorted(mo - mn),
            'changed': changed, 'added': added, 'removed': removed, 'old_facts': fo, 'new_facts': fn}


def package_summary(c, limit):
    parts = ['%s %s -> %s' % x for x in c['changed'][:limit]]
    rest = len(c['changed']) - limit
    if rest > 0:
        parts.append('+%d more changed' % rest)
    if c['added']:
        parts.append('added: ' + ', '.join(c['added'][:limit]) + (' +%d' % (len(c['added']) - limit) if len(c['added']) > limit else ''))
    if c['removed']:
        parts.append('removed: ' + ', '.join(c['removed'][:limit]) + (' +%d' % (len(c['removed']) - limit) if len(c['removed']) > limit else ''))
    return '; '.join(parts)


def auto_notes(old_digest, c):
    """The deterministic half of an image record's notes; the reviewer appends the interpretation."""
    bits = ['vs %s: %s' % (old_digest[:12], c['kind'])]
    if c['moved']:
        bits.append(', '.join('%s %s -> %s' % m for m in c['moved']))
    if c['modules_added'] or c['modules_removed']:
        bits.append('php modules +%s -%s' % (','.join(c['modules_added']) or '0', ','.join(c['modules_removed']) or '0'))
    n = len(c['changed']) + len(c['added']) + len(c['removed'])
    if n:
        names = [x[0] for x in c['changed']] + ['+' + a for a in c['added']] + ['-' + r for r in c['removed']]
        bits.append('%d packages: %s%s' % (n, ', '.join(names[:10]), ' ...' if n > 10 else ''))
    return '; '.join(bits)


def bank(conn, ref, facts, notes, refresh=False):
    """'added', 'merged' (with --refresh), or 'on record'."""
    image, tag, digest = parse_ref(ref)
    existing = store.find_digest(conn, digest)
    if existing:
        if not refresh:
            return 'on record'
        with store.write(conn):
            store.edit_image(conn, existing[0]['id'], facts=facts)
        return 'merged'
    with store.write(conn):
        store.add_image(conn, image, tag, digest, facts, notes)
    return 'added'


def fact_line(facts):
    return ' '.join('%s=%s' % (k, v) for k, v in facts.items() if not k.endswith(' md5'))


def probe_pairs(conn, pairs, platform=None, refresh=False, do_bank=True, workers=4):
    """pairs: [(label, old_ref or None, new_ref)], optionally with a fourth item, the platform
    triage read from the manifests. Prints one block per pair and returns the number of failed
    probes. When banking, a pair whose new digest is already on record is skipped."""
    todo, failures = [], 0
    for pair in pairs:
        label, old, new = pair[:3]
        hits = store.find_digest(conn, parse_ref(new)[2])
        if hits and do_bank and not refresh:
            print('### %s  pass (ledger): %s' % (label, fact_line(json.loads(hits[0]['facts'] or '{}')) or hits[0]['notes']))
        else:
            todo.append((label, old, new, platform if platform is not None else (pair[3] if len(pair) > 3 else None)))
    # Both sides of a pair run on one platform, chosen from the new side, so the delta never
    # compares an amd64 dump with an arm64 one.
    with ThreadPoolExecutor(max_workers=workers) as ex:
        plats = list(ex.map(lambda t: choose_platform(t[2], t[3]), todo))
    refs = list(dict.fromkeys((r, plat) for (_, o, n, _), plat in zip(todo, plats) if plat is not None for r in (o, n) if r))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = dict(zip(refs, ex.map(lambda rp: run_probe(rp[0], rp[1], refresh), refs)))
    for (label, old, new, _), plat in zip(todo, plats):
        if plat is None:
            o, n = None, {'ref': new, 'error': 'could not read the manifest to choose a platform (pass --platform)'}
        else:
            o, n = results.get((old, plat)) if old else None, results[(new, plat)]
        _, _, nd = parse_ref(new)
        print('### %s  %s -> %s  (%s)' % (label, parse_ref(old)[2][:12] if old else '-', nd[:12], n.get('platform', '?')))
        errors = [r for r in (o, n) if r and r.get('error')]
        for r in errors:
            failures += 1
            print('PROBE FAILED %s: %s' % (r['ref'], r['error']))
        if n.get('error'):
            continue
        nf = normalise(n['raw'])
        print('new: ' + fact_line(nf))
        notes = ''
        if o and not o.get('error'):
            c = compare(o, n)
            notes = auto_notes(parse_ref(old)[2], c)
            print('%s%s' % (c['kind'], (': ' + ', '.join('%s %s -> %s' % m for m in c['moved'])) if c['moved'] else ''))
            if c['modules_added'] or c['modules_removed']:
                print('php modules: added %s, removed %s' % (c['modules_added'] or 'none', c['modules_removed'] or 'none'))
            n_pkgs = len(c['changed']) + len(c['added']) + len(c['removed'])
            if n_pkgs:
                print('packages (%d): %s' % (n_pkgs, package_summary(c, 12)))
            print('dumps: %s %s' % (o['dump'], n['dump']))
        else:
            print('dump: %s' % n['dump'])
        if do_bank:
            states = []
            for r, rnotes in ((o, ''), (n, notes)):
                if r and not r.get('error'):
                    states.append('%s %s' % (parse_ref(r['ref'])[2][:12], bank(conn, r['ref'], normalise(r['raw']), rnotes, refresh)))
            print('banked: ' + ', '.join(states) + '  (add the interpretation: depledger image edit %s --append-notes "...")' % nd[:12])
        print()
    return failures


def pairs_from_images(images, platforms=None):
    """Probe list for triage: [{'label', 'old', 'new'}] with full refs, from parse.image_changes output.
    platforms: {key: platform} triage read from the manifests, carried as 'platform' so the probe
    runs both sides on it without asking the registry again ('' is a single-manifest image)."""
    out = []
    for key, v in sorted(images.items()):
        o, n = v.get('-', {}), v.get('+', {})
        if not n.get('digest'):
            continue

        def ref(side):
            return '%s%s@sha256:%s' % (v.get('name', key), (':' + side['tag']) if side.get('tag') else '', side['digest'])
        pair = {'label': key, 'old': ref(o) if o.get('digest') else None, 'new': ref(n)}
        if platforms and key in platforms:
            pair['platform'] = platforms[key]
        out.append(pair)
    return out
