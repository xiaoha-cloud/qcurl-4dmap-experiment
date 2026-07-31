#!/usr/bin/env bash
# Fixed-coefficient clean-D collection for offline per-path RTT prediction.
# Historical collectors and the intervention smoke workflow are not modified.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TIMEOUT="${TIMEOUT:-220}"
SAMPLE_INTERVAL_MS="${QACCESS_RUNTIME_SAMPLE_INTERVAL_MS:-100}"
INPUT_FLV="${INPUT_FLV:-/home/mininet/Videos/push_input.flv}"

args=(
  --controller-variant qaccess_d
  --profile-kind delay_clean
  --scenario clean_equal_paths
  --timeout "$TIMEOUT"
  --sample-interval-ms "$SAMPLE_INTERVAL_MS"
  --input-flv "$INPUT_FLV"
)

exec python3 "$ROOT/scripts/mininet/run_qaccess_qserver_sender_sweep.py" "${args[@]}" "$@"
