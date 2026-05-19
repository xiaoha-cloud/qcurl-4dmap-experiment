#!/usr/bin/env bash
# Fig.7-style throughput evaluation: baseline vs fixed-T vs learn under the same dynamic BW profile.
#
# Bandwidth steps (bw_profile.fig7_200s.env on h2-eth1 server egress):
#   0-50s: 20 Mbps | 50-100s: 30 Mbps | 100s+: 10 Mbps
#
# Usage (VM, repo root = /home/mininet/Project/4D-MAP):
#   chmod +x scripts/mininet/run_fig7_eval_matrix.sh
#   sudo TIMEOUT=220 SAVE_LOGS=1 ./scripts/mininet/run_fig7_eval_matrix.sh
#
# Analyze (on host with logs):
#   python3 scripts/analyze/fig7_throughput_compare.py \\
#     --out derived/fig7_compare \\
#     -r baseline:logs_exp/session_*/fig7_um_baseline \\
#     -r T:logs_exp/session_*/fig7_um_T \\
#     -r learn:logs_exp/session_*/fig7_um_learn

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"
BW_PROFILE="${BW_PROFILE:-scripts/mininet/bw_profile.fig7_200s.env}"
TIMEOUT="${TIMEOUT:-220}"
SAVE_LOGS="${SAVE_LOGS:-1}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

cd "$ROOT"
SESSION_DIR="logs_exp/session_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"

run_one() {
  local um="$1"
  local um_lc
  um_lc="$(printf '%s' "$um" | tr '[:upper:]' '[:lower:]')"
  local label="fig7_um_${um_lc}"
  local -a cmd=(
    python3 "$MP" --run-exp --scenario fig7 --utility-mode "$um"
    --timeout "$TIMEOUT" --log-parent "$SESSION_DIR" --run-label "$label"
    --dynamic-bw-profile "$BW_PROFILE"
  )
  [[ "$SAVE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)
  echo "[fig7_eval] utility-mode=$um → $SESSION_DIR/$label"
  "${cmd[@]}"
}

for um in baseline T learn; do
  run_one "$um"
done

echo "[fig7_eval] done. session=$ROOT/$SESSION_DIR"
echo "[fig7_eval] compare: python3 scripts/analyze/fig7_throughput_compare.py --out derived/fig7_compare \\"
echo "  -r baseline:$SESSION_DIR/fig7_um_baseline -r T:$SESSION_DIR/fig7_um_T -r learn:$SESSION_DIR/fig7_um_learn"
