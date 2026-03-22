#!/usr/bin/env bash
# One-shot experiment logging layout: logs_exp/<RUN_ID>/server|push|pull_<RUN_ID>.log
#
# From the repository root (e.g. ~/Project/4D-MAP):
#   source scripts/vm_experiment_logs.sh
# Then open three terminals (same shell session can export once; or copy RUN_ID/LOGDIR).
#
# Terminal 1 — server:
#   ./qserver -protocol=quic -au=false 2>&1 | tee "${LOGDIR}/server_${RUN_ID}.log"
#
# Terminal 2 — push:
#   ./4dmap -type=false -protocol=quic -multi=false -sch=rr \
#     -file="${HOME}/Videos/push_input.flv" \
#     rtmp://127.0.0.1/live/test 2>&1 | tee "${LOGDIR}/push_${RUN_ID}.log"
#
# Terminal 3 — pull (touch output if main.go lacks O_CREATE on pull path):
#   touch "${HOME}/Videos/pulled_${RUN_ID}.flv"
#   ./4dmap -type=true -protocol=quic -multi=false \
#     -file="${HOME}/Videos/pulled_${RUN_ID}.flv" \
#     rtmp://127.0.0.1/live/test 2>&1 | tee "${LOGDIR}/pull_${RUN_ID}.log"
#
# Extract [utility] lines:
#   grep '\[utility\]' "${LOGDIR}/push_${RUN_ID}.log" > "${LOGDIR}/utility_push_${RUN_ID}.txt"
#   grep '\[utility\]' "${LOGDIR}/pull_${RUN_ID}.log" > "${LOGDIR}/utility_pull_${RUN_ID}.txt"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "${RUN_ID:-}" ]]; then
	export RUN_ID="$(date +%Y%m%d_%H%M%S)"
fi

export LOGDIR="${REPO_ROOT}/logs_exp/${RUN_ID}"
mkdir -p "$LOGDIR"

echo "RUN_ID=$RUN_ID"
echo "LOGDIR=$LOGDIR"
echo "Example: ./qserver -protocol=quic -au=false 2>&1 | tee \"\${LOGDIR}/server_\${RUN_ID}.log\""
