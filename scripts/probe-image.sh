#!/usr/bin/env bash
# Probe one or two image digests and print what is inside, without writing to the ledger.
#
#   probe-image.sh <image>@sha256:<digest> [<image>@sha256:<digest>] [--platform linux/amd64]
#
# Kept for docs that name it: the probe lives in `depledger probe`, which also banks the facts
# (`depledger probe --from <triage out dir>` probes a whole PR). Dumps are cached by digest under
# <data dir>/probes. --out is accepted and ignored.
set -euo pipefail
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) args+=("$1"); shift ;;
  esac
done
exec "$(dirname "$(realpath "$0")")/../bin/depledger" probe --no-bank "${args[@]}"
