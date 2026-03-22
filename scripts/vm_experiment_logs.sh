#!/usr/bin/env bash
# One-shot experiment logging layout: logs_exp/vm_run_<RUN_ID>/server|push|pull_<RUN_ID>.log
#
# From the repository root (e.g. ~/Project/4D-MAP):
#   source scripts/vm_experiment_logs.sh
# Then open three terminals (same shell session can export once; or copy RUN_ID/LOGDIR).
#
# Terminal 1 — server:
#   ./qserver -protocol=quic -au=false 2>&1 | tee "${LOGDIR}/server_${RUN_ID}.log"
#
# QUIC_GO_LOG_LEVEL=info is exported below so [utility] / [m]monitor (utils.Infof) appear in 4dmap logs.
#
# Run order: Terminal 1 server, Terminal 2 pull, Terminal 3 push (copy RUN_ID and LOGDIR into each).
#
# Terminal 2 — pull first:
#   touch "${HOME}/Videos/pulled_${RUN_ID}.flv"
#   ./4dmap -type=true -protocol=quic -multi=false \
#     -file="${HOME}/Videos/pulled_${RUN_ID}.flv" \
#     rtmp://127.0.0.1/live/test 2>&1 | tee "${LOGDIR}/pull_${RUN_ID}.log"
#
# Terminal 3 — push:
#   ./4dmap -type=false -protocol=quic -multi=false -sch=rr \
#     -file="${HOME}/Videos/push_input.flv" \
#     rtmp://127.0.0.1/live/test 2>&1 | tee "${LOGDIR}/push_${RUN_ID}.log"
#
# After run:
#   grep '\[utility\]' "${LOGDIR}/pull_${RUN_ID}.log" > "${LOGDIR}/utility_pull_${RUN_ID}.txt"
#   grep '\[m]monitor' "${LOGDIR}/pull_${RUN_ID}.log" > "${LOGDIR}/monitor_pull_${RUN_ID}.txt"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "${RUN_ID:-}" ]]; then
	export RUN_ID="$(date +%Y%m%d_%H%M%S)"
fi

export LOGDIR="${REPO_ROOT}/logs_exp/vm_run_${RUN_ID}"
mkdir -p "$LOGDIR"

export QUIC_GO_LOG_LEVEL=info

echo "RUN_ID=$RUN_ID"
echo "LOGDIR=$LOGDIR"
echo "QUIC_GO_LOG_LEVEL=$QUIC_GO_LOG_LEVEL"
echo "Example: ./qserver -protocol=quic -au=false 2>&1 | tee \"\${LOGDIR}/server_\${RUN_ID}.log\""
