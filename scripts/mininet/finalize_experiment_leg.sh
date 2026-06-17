#!/usr/bin/env bash
# Finalize one experiment leg: diagnostics, retention cleanup, size summary.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LEG_DIR="${1:?leg directory required}"
LEG_LABEL="${2:-$(basename "$LEG_DIR")}"

export KEEP_PCAP="${KEEP_PCAP:-0}"
export KEEP_RAW_RUNTIME="${KEEP_RAW_RUNTIME:-0}"
export SAVE_OUTPUT_FLV="${SAVE_OUTPUT_FLV:-0}"
export KEEP_ALL_PROCESSED_BUFFERS="${KEEP_ALL_PROCESSED_BUFFERS:-0}"

python3 "$ROOT/scripts/analyze/finalize_control_law_leg.py" \
  --repo "$ROOT" \
  --leg-dir "$LEG_DIR" \
  --leg-label "$LEG_LABEL" \
  --keep-pcap "$KEEP_PCAP" \
  --keep-raw-runtime "$KEEP_RAW_RUNTIME" \
  --save-output-flv "$SAVE_OUTPUT_FLV" \
  --keep-all-processed-buffers "$KEEP_ALL_PROCESSED_BUFFERS"
