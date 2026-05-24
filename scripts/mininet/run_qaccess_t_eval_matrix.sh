#!/usr/bin/env bash
# Q-ACCeSS-T final evaluation: baseline/off vs qaccess_t (Fig7 dynamic BW profile).
#
# Bandwidth steps (bw_profile.fig7_200s.env on h2-eth1 server egress):
#   0-50s: 20 Mbps | 50-100s: 30 Mbps | 100s+: 10 Mbps
#
# Usage (VM, repo root):
#   chmod +x scripts/mininet/run_qaccess_t_eval_matrix.sh
#   sudo TIMEOUT=220 SAVE_LOGS=1 ./scripts/mininet/run_qaccess_t_eval_matrix.sh
#
# Analyze (host with logs):
#   python3 scripts/analyze/qaccess_t_throughput_compare.py \\
#     -r baseline:logs_exp/session_qaccess_t_*/fig7_baseline \\
#     -r qaccess_t:logs_exp/session_qaccess_t_*/fig7_qaccess_t

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
SESSION_DIR="logs_exp/session_qaccess_t_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"

run_one() {
  local um="$1"
  local label="$2"
  local -a cmd=(
    python3 "$MP" --run-exp --scenario fig7 --utility-mode "$um"
    --timeout "$TIMEOUT" --log-parent "$SESSION_DIR" --run-label "$label"
    --dynamic-bw-profile "$BW_PROFILE"
  )
  [[ "$SAVE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)
  echo "[qaccess_t_eval] utility-mode=$um → $SESSION_DIR/$label"
  "${cmd[@]}"
}

run_one baseline fig7_baseline
run_one qaccess_t fig7_qaccess_t

echo "[qaccess_t_eval] done. session=$ROOT/$SESSION_DIR"
echo "[qaccess_t_eval] compare:"
echo "  python3 scripts/analyze/qaccess_t_throughput_compare.py \\"
echo "    -r baseline:$SESSION_DIR/fig7_baseline \\"
echo "    -r qaccess_t:$SESSION_DIR/fig7_qaccess_t"
