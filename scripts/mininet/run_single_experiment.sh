#!/usr/bin/env bash
# Minimal single-run wrapper for mp_topo.py
# Goal: run one fig7 experiment quickly, with optional dynamic profile.
#
# Usage:
#   chmod +x scripts/mininet/run_single_experiment.sh
#   sudo ./scripts/mininet/run_single_experiment.sh
#
# Optional env overrides:
#   TIMEOUT=220 UTILITY_MODE=learn LOG_CONTROL=1 \
#   BW_PROFILE=scripts/mininet/bw_profile.fig7_200s.env \
#   sudo ./scripts/mininet/run_single_experiment.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"

TIMEOUT="${TIMEOUT:-220}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"
UTILITY_MODE="${UTILITY_MODE:-T}"
BW_PROFILE="${BW_PROFILE:-scripts/mininet/bw_profile.fig7_200s.env}"
SAVE_LOGS="${SAVE_LOGS:-0}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] please run with sudo (Mininet requires root)" >&2
  exit 1
fi

if [[ ! -f "$MP" ]]; then
  echo "[error] missing script: $MP" >&2
  exit 1
fi

if [[ -n "$BW_PROFILE" && ! -f "$ROOT/$BW_PROFILE" && ! -f "$BW_PROFILE" ]]; then
  echo "[error] missing BW_PROFILE: $BW_PROFILE" >&2
  exit 1
fi

cd "$ROOT"

SESSION_DIR="logs_exp/session_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"

um_lc="$(printf '%s' "$UTILITY_MODE" | tr '[:upper:]' '[:lower:]')"
RUN_LABEL="fig7_um_${um_lc}"

CMD=(
  python3 "$MP"
  --run-exp
  --scenario fig7
  --utility-mode "$UTILITY_MODE"
  --timeout "$TIMEOUT"
  --log-parent "$SESSION_DIR"
  --run-label "$RUN_LABEL"
)

if [[ "$SAVE_LOGS" != "1" ]]; then
  CMD+=(--disable-logs)
fi

if [[ -n "$INPUT_FLV" ]]; then
  CMD+=(--input-flv "$INPUT_FLV")
fi

if [[ "$LOG_CONTROL" == "1" ]]; then
  CMD+=(--log-control)
fi

if [[ -n "$BW_PROFILE" ]]; then
  CMD+=(--dynamic-bw-profile "$BW_PROFILE")
fi

echo "[info] running single experiment (fig7 + optional dynamic bw profile)"
echo "[info] output root: $ROOT/$SESSION_DIR"
echo "[info] timeout: $TIMEOUT"
echo "[info] utility-mode: $UTILITY_MODE"
echo "[info] bw-profile: ${BW_PROFILE:-<disabled>}"
echo "[info] save-logs: $SAVE_LOGS"
echo "[info] command: ${CMD[*]}"
"${CMD[@]}"

echo "[info] done"
echo "[info] logs: $ROOT/$SESSION_DIR/$RUN_LABEL"
