#!/usr/bin/env bash
# Copy the BenchPod features table (features/features.json, generated in benchpod-firmware from
# features/features.toml) into the SDK, which decides from it what a pod with features_rev can do.
#
#   scripts/sync-feature-map.sh [path/to/benchpod-firmware]   # default: ../benchpod-firmware
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
fw="${1:-$here/../benchpod-firmware}"
src="$fw/features/features.json"
dst="$here/packages/embeddedci/src/embeddedci/benchpod/feature_map.json"
[ -f "$src" ] || { echo "sync-feature-map: no $src (pass the benchpod-firmware checkout)" >&2; exit 1; }
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["schema"]==1 and d["features"]' "$src"
cp "$src" "$dst"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("sync-feature-map:", len(d["features"]), "features, revision", d["revision"], "table", d["table_sha256"][:12])' "$dst"
