#!/usr/bin/env bash
# Probe one or two image digests and print what is inside.
#
#   probe-image.sh <image>@sha256:<digest> [<image>@sha256:<digest>] [--platform linux/amd64] [--out DIR]
#
# One ref: prints runtime versions, module/package fingerprints and the base OS.
# Two refs: prints both headers side by side, then the package-list diff.
# Full per-ref dumps are saved under --out (default $TMPDIR/depledger/probes).
# The platform is picked from the manifest (amd64 first, then arm64) unless given.
set -uo pipefail

PLATFORM=""
OUT="${TMPDIR:-/tmp}/depledger/probes"
REFS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform) PLATFORM="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) REFS+=("$1"); shift ;;
  esac
done
if [[ ${#REFS[@]} -lt 1 || ${#REFS[@]} -gt 2 ]]; then
  echo "usage: probe-image.sh <image>@sha256:<digest> [<image>@sha256:<digest>] [--platform P] [--out DIR]" >&2
  exit 2
fi
mkdir -p "$OUT"

PROBE="$OUT/probe.sh"
cat > "$PROBE" <<'SH'
echo "os=$(grep PRETTY_NAME /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d '"')"
for c in php node bun composer caddy psql postgres redis-server valkey-server mysqld nginx mailpit gotenberg chromium google-chrome soffice; do
  if command -v "$c" >/dev/null 2>&1; then
    case "$c" in
      php) v=$(php -v 2>/dev/null | head -1) ;;
      node) v=$(node -v 2>/dev/null) ;;
      bun) v=$(bun -v 2>/dev/null) ;;
      composer) v=$(composer --version 2>/dev/null | head -1) ;;
      caddy) v=$(caddy version 2>/dev/null | head -1) ;;
      psql) v=$(psql --version 2>/dev/null) ;;
      postgres) v=$(postgres --version 2>/dev/null) ;;
      redis-server|valkey-server) v=$("$c" --version 2>/dev/null | sed 's/ build=.*//') ;;
      mysqld) v=$(mysqld --version 2>/dev/null) ;;
      nginx) v=$(nginx -v 2>&1) ;;
      mailpit) v=$(mailpit version 2>/dev/null | head -1) ;;
      gotenberg) v=$(gotenberg --version 2>/dev/null | head -1) ;;
      chromium|google-chrome) v=$("$c" --version 2>/dev/null | head -1) ;;
      soffice) v=$(soffice --version 2>/dev/null | head -1) ;;
    esac
    echo "$c=$v"
  fi
done
if command -v php >/dev/null 2>&1; then
  echo "php_modules_md5=$(php -m 2>/dev/null | md5sum | cut -c1-32)"
  echo "php_modules=$(php -m 2>/dev/null | grep -v '^\[' | grep -v '^$' | tr '\n' ' ')"
fi
if command -v dpkg-query >/dev/null 2>&1; then
  dpkg-query -W -f='${Package} ${Version}\n' 2>/dev/null | sort > /tmp/pkgs
  echo "dpkg_count=$(wc -l < /tmp/pkgs | tr -d ' ')"
  echo "dpkg_md5=$(md5sum < /tmp/pkgs | cut -c1-32)"
elif command -v apk >/dev/null 2>&1; then
  apk info -v 2>/dev/null | sort > /tmp/pkgs
  echo "apk_count=$(wc -l < /tmp/pkgs | tr -d ' ')"
  echo "apk_md5=$(md5sum < /tmp/pkgs | cut -c1-32)"
fi
echo "---PACKAGES---"
cat /tmp/pkgs 2>/dev/null
SH

pick_platform() {
  local ref="$1"
  if [[ -n "$PLATFORM" ]]; then echo "$PLATFORM"; return; fi
  docker buildx imagetools inspect "$ref" --raw 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); sys.exit()
plats = ["%s/%s" % (m.get("platform", {}).get("os"), m.get("platform", {}).get("architecture")) for m in d.get("manifests", []) if m.get("platform", {}).get("os") not in (None, "unknown")]
for want in ("linux/amd64", "linux/arm64"):
    if want in plats:
        print(want); break
else:
    print(plats[0] if plats else "")
'
}

slug() { echo "$1" | sed -E 's#.*/##; s/@sha256:/_/; s/[^A-Za-z0-9._-]/_/g' | cut -c1-80; }

HEADERS=()
PKGFILES=()
for ref in "${REFS[@]}"; do
  plat="$(pick_platform "$ref")"
  args=()
  [[ -n "$plat" ]] && args=(--platform "$plat")
  s="$(slug "$ref")"
  dump="$OUT/$s.txt"
  if ! docker run --rm -i ${args[@]+"${args[@]}"} --entrypoint sh "$ref" -s < "$PROBE" > "$dump" 2> "$OUT/$s.err"; then
    echo "probe failed for $ref (platform '${plat:-default}'):" >&2
    tail -5 "$OUT/$s.err" >&2
    exit 1
  fi
  hdr="$(sed '/^---PACKAGES---$/,$d' "$dump")"
  sed '1,/^---PACKAGES---$/d' "$dump" > "$OUT/$s.pkgs"
  HEADERS+=("$hdr")
  PKGFILES+=("$OUT/$s.pkgs")
  echo "### $ref  (platform ${plat:-default})"
  echo "$hdr"
  echo "full dump: $dump"
  echo
done

if [[ ${#REFS[@]} -eq 2 ]]; then
  echo "### field comparison"
  python3 - "$OUT" <<PY
import sys
a = """${HEADERS[0]}""".splitlines()
b = """${HEADERS[1]}""".splitlines()
da = dict(l.split('=', 1) for l in a if '=' in l)
db = dict(l.split('=', 1) for l in b if '=' in l)
same = True
for k in sorted(set(da) | set(db)):
    if k == 'php_modules':
        continue
    va, vb = da.get(k, '<absent>'), db.get(k, '<absent>')
    if va != vb:
        same = False
        print('CHANGED %s: %s  ->  %s' % (k, va, vb))
print('all fields identical' if same else '')
PY
  echo "### package diff (old -> new)"
  if diff "${PKGFILES[0]}" "${PKGFILES[1]}" > "$OUT/pkgs.diff"; then
    echo "package lists are byte-identical"
  else
    grep -E '^[<>]' "$OUT/pkgs.diff" | sed 's/^</- /; s/^>/+ /'
    echo "($(grep -c '^<' "$OUT/pkgs.diff") removed/changed-from, $(grep -c '^>' "$OUT/pkgs.diff") added/changed-to)"
  fi
fi
