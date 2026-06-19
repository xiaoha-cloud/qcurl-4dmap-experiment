#!/usr/bin/env bash
# Run inside the Mininet h2 namespace after starting the fig8 topology.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TC_SCRIPT="$ROOT/scripts/mininet/tc_deterioration_steps.sh"
PROFILE="$ROOT/scripts/mininet/combined_deterioration_integration_profile.env"
LOG="$(mktemp)"

cleanup() {
  rm -f "$LOG"
}
trap cleanup EXIT

set +e
bash "$TC_SCRIPT" "$PROFILE" >"$LOG" 2>&1
status=$?
set -e
cat "$LOG"

for expected in \
  "step 1/3" \
  "step 2/3" \
  "step 3/3" \
  "verified root=" \
  "loss=0.05%" \
  "finished all steps" \
  "exiting status=0 current_step=3 completed=1"; do
  if ! grep -Fq "$expected" "$LOG"; then
    echo "FAIL: missing expected log: $expected" >&2
    exit 1
  fi
done
if [[ "$status" -ne 0 ]]; then
  echo "FAIL: deterioration script exit status $status" >&2
  exit "$status"
fi
echo "exit status 0"
