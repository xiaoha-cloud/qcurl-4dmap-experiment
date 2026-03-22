#!/usr/bin/env bash
# One-shot Mininet/VM experiment: server + pull + push, logs under logs_exp/vm_run_<RUN_ID>/
# Usage: ./run_exp.sh
# Optional: INPUT_FLV=/path/to/file.flv ./run_exp.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$ROOT/server"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
LOGDIR="$ROOT/logs_exp/vm_run_${RUN_ID}"
OUTFILE="${HOME}/Videos/pulled_${RUN_ID}.flv"
INPUT_FLV="${INPUT_FLV:-$HOME/Videos/push_input.flv}"

mkdir -p "$LOGDIR"
mkdir -p "$(dirname "$OUTFILE")"
touch "$OUTFILE"

if [[ ! -f "$INPUT_FLV" ]]; then
	echo "[error] input FLV not found: $INPUT_FLV"
	echo "        set INPUT_FLV=/path/to/your.flv or place file at $HOME/Videos/push_input.flv"
	exit 1
fi

if [[ ! -x "$ROOT/qserver" ]] && [[ ! -f "$ROOT/qserver" ]]; then
	echo "[warn] $ROOT/qserver missing; build from repo root: GO111MODULE=on go build -o qserver ./server"
fi
if [[ ! -x "$ROOT/4dmap" ]] && [[ ! -f "$ROOT/4dmap" ]]; then
	echo "[warn] $ROOT/4dmap missing; build: GO111MODULE=on go build -o 4dmap ."
fi

export RUN_ID
export LOGDIR
export QUIC_GO_LOG_LEVEL="${QUIC_GO_LOG_LEVEL:-info}"

echo "RUN_ID=$RUN_ID"
echo "LOGDIR=$LOGDIR"
echo "INPUT_FLV=$INPUT_FLV"
echo "$RUN_ID" >"$ROOT/.last_run_id"

file_size() {
	if stat -c%s "$1" >/dev/null 2>&1; then
		stat -c%s "$1"
	else
		stat -f%z "$1" 2>/dev/null || echo 0
	fi
}

cleanup() {
	echo "[cleanup] stopping processes..."
	for pid in "${PUSH_PID:-}" "${PULL_PID:-}" "${SERVER_PID:-}"; do
		if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
			kill "$pid" 2>/dev/null || true
		fi
	done
	sleep 2
	for pid in "${PUSH_PID:-}" "${PULL_PID:-}" "${SERVER_PID:-}"; do
		if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
			kill -9 "$pid" 2>/dev/null || true
		fi
	done
}
trap cleanup EXIT

echo "[start] launching server..."
(
	cd "$SERVER_DIR"
	exec > >(tee -a "${LOGDIR}/server_${RUN_ID}.log") 2>&1
	echo "[server] cwd=$(pwd)"
	echo "[server] start $(date -Iseconds 2>/dev/null || date)"
	exec "$ROOT/qserver" -protocol=quic -au=false
) &
SERVER_PID=$!

sleep 3

echo "[start] launching pull..."
(
	cd "$ROOT"
	exec > >(tee -a "${LOGDIR}/pull_${RUN_ID}.log") 2>&1
	echo "[pull] cwd=$(pwd)"
	echo "[pull] outfile=$OUTFILE"
	echo "[pull] start $(date -Iseconds 2>/dev/null || date)"
	exec ./4dmap -type=true -protocol=quic -multi=false -file="$OUTFILE" rtmp://127.0.0.1/live/test
) &
PULL_PID=$!

sleep 3

PUSH_TIMEOUT="${PUSH_TIMEOUT:-180}"
echo "[start] launching push (timeout ${PUSH_TIMEOUT}s)..."
(
	cd "$ROOT"
	exec > >(tee -a "${LOGDIR}/push_${RUN_ID}.log") 2>&1
	echo "[push] cwd=$(pwd)"
	echo "[push] input=$INPUT_FLV"
	echo "[push] start $(date -Iseconds 2>/dev/null || date)"
	if command -v timeout >/dev/null 2>&1; then
		exec timeout "$PUSH_TIMEOUT" ./4dmap -type=false -protocol=quic -multi=false -sch=rr -file="$INPUT_FLV" rtmp://127.0.0.1/live/test
	else
		exec ./4dmap -type=false -protocol=quic -multi=false -sch=rr -file="$INPUT_FLV" rtmp://127.0.0.1/live/test
	fi
) &
PUSH_PID=$!

echo "$SERVER_PID" >"${LOGDIR}/server.pid"
echo "$PULL_PID" >"${LOGDIR}/pull.pid"
echo "$PUSH_PID" >"${LOGDIR}/push.pid"

GRACE_SEC="${WATCHDOG_GRACE_SEC:-45}"
echo "[watchdog] grace ${GRACE_SEC}s for streams to start..."
sleep "$GRACE_SEC"

echo "[watchdog] monitoring output file growth: $OUTFILE"
last_size=0
stable_count=0
max_stable_rounds="${WATCHDOG_STALL_ROUNDS:-15}"
poll_interval="${WATCHDOG_POLL_SEC:-2}"

while true; do
	push_alive=0
	pull_alive=0

	if kill -0 "$PUSH_PID" 2>/dev/null; then push_alive=1; fi
	if kill -0 "$PULL_PID" 2>/dev/null; then pull_alive=1; fi

	current_size=$(file_size "$OUTFILE")
	echo "[watchdog] $(date '+%F %T') size=${current_size}B stable=${stable_count} push_alive=${push_alive} pull_alive=${pull_alive}" |
		tee -a "${LOGDIR}/watchdog_${RUN_ID}.log"

	if [[ "$current_size" -gt "$last_size" ]]; then
		stable_count=0
		last_size="$current_size"
	else
		stable_count=$((stable_count + 1))
	fi

	if [[ "$push_alive" -eq 0 ]]; then
		echo "[watchdog] push exited" | tee -a "${LOGDIR}/watchdog_${RUN_ID}.log"
		break
	fi

	if [[ "$stable_count" -ge "$max_stable_rounds" ]]; then
		echo "[watchdog] output stalled (${max_stable_rounds}x${poll_interval}s), terminating run" |
			tee -a "${LOGDIR}/watchdog_${RUN_ID}.log"
		break
	fi

	sleep "$poll_interval"
done

echo "[finish] collecting tails..."
tail -n 30 "${LOGDIR}/server_${RUN_ID}.log" >"${LOGDIR}/server_tail_${RUN_ID}.log" 2>/dev/null || true
tail -n 30 "${LOGDIR}/pull_${RUN_ID}.log" >"${LOGDIR}/pull_tail_${RUN_ID}.log" 2>/dev/null || true
tail -n 30 "${LOGDIR}/push_${RUN_ID}.log" >"${LOGDIR}/push_tail_${RUN_ID}.log" 2>/dev/null || true

echo "[done] logs saved in $LOGDIR"
echo "[done] exit codes: check push/pull/server in logs; watchdog_${RUN_ID}.log for stall decision"
