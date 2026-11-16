#!/usr/bin/env bash
# Copy the BenchPod error catalog (errors/errors.json, generated in benchpod-firmware from
# errors/catalog.toml) into the SDK, which renders a refusal's message and fix from it.
#
#   scripts/sync-error-catalog.sh [path/to/benchpod-firmware]   # default: ../benchpod-firmware
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
fw="${1:-$here/../benchpod-firmware}"
src="$fw/errors/errors.json"
dst="$here/packages/embeddedci/src/embeddedci/benchpod/error_catalog.json"
[ -f "$src" ] || { echo "sync-error-catalog: no $src (pass the benchpod-firmware checkout)" >&2; exit 1; }
cp "$src" "$dst"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("sync-error-catalog:", len(d["codes"]), "codes, catalog", d["catalog_sha256"][:12])' "$dst"
