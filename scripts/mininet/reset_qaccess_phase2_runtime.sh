#!/usr/bin/env bash
# Reset Phase 2 runtime state before a new qaccess_t_dynamic experiment.
#
# - derived/qaccess_t_initial_coefficients.json  read-only Phase 1 baseline (never written by worker)
# - derived/qaccess_t_runtime_coefficients.json  mutable copy used by Go + worker for one run
#
# Usage (repo root):
#   ./scripts/mininet/reset_qaccess_phase2_runtime.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DERIVED="${QACCESS_PHASE2_STATE_DIR:-$ROOT/derived}"
if [[ "$DERIVED" != /* ]]; then
  echo "[reset] QACCESS_PHASE2_STATE_DIR must be absolute: $DERIVED" >&2
  exit 2
fi
INITIAL="$DERIVED/qaccess_t_initial_coefficients.json"
RUNTIME="$DERIVED/qaccess_t_runtime_coefficients.json"

mkdir -p "$DERIVED"

if [[ ! -f "$INITIAL" ]]; then
  cat >"$INITIAL" <<'EOF'
{
  "alpha": 0.6,
  "beta": 0.3,
  "gamma": 0.1,
  "source": "phase1_stable_initial_coefficients",
  "metric": "predicted_next_bw_bps"
}
EOF
  echo "[reset] created $INITIAL"
fi

# Phase 2 artifacts are often root-owned after sudo Mininet; remove with sudo when needed.
_phase2_rm() {
  local f
  for f in "$@"; do
    if [[ -e "$f" ]]; then
      rm -f "$f" 2>/dev/null || sudo rm -f "$f" 2>/dev/null || true
    fi
  done
}
_phase2_rm \
  "$DERIVED/qaccess_runtime_samples.csv" \
  "$DERIVED/qaccess_update_request.json" \
  "$DERIVED/qaccess_update_response.json" \
	"$DERIVED/qaccess_trigger_audit.jsonl" \
	"$DERIVED/qaccess_owner_audit.jsonl" \
	"$DERIVED/qaccess_worker_state.json" \
	"$DERIVED/qaccess_update_audit.csv"

# Processed buffers are copied into each completed session. Keeping this shared
# working directory across runs contaminates the next session with old PIDs,
# request IDs, samples, and candidate artifacts.
if [[ -d "$DERIVED/qaccess_processed_buffers" ]]; then
  rm -rf "$DERIVED/qaccess_processed_buffers" 2>/dev/null \
    || sudo rm -rf "$DERIVED/qaccess_processed_buffers" 2>/dev/null \
    || { echo "[reset] failed to clear $DERIVED/qaccess_processed_buffers" >&2; exit 1; }
fi
mkdir -p "$DERIVED/qaccess_processed_buffers"

# Prior sudo Mininet runs may leave root-owned Phase 2 files the worker cannot truncate.
for f in qaccess_runtime_samples.csv qaccess_update_request.json qaccess_update_response.json; do
  if [[ -f "$DERIVED/$f" && ! -w "$DERIVED/$f" ]]; then
    if [[ -n "${SUDO_UID:-}" ]]; then
      chown "${SUDO_UID}:${SUDO_GID}" "$DERIVED/$f"
    elif command -v sudo >/dev/null 2>&1; then
      sudo chown "$(id -u):$(id -g)" "$DERIVED/$f" 2>/dev/null || true
    fi
  fi
done

cp "$INITIAL" "$RUNTIME"
chmod 0666 "$RUNTIME" 2>/dev/null || true
echo "[reset] copied initial -> runtime coefficients"
echo "  initial: $INITIAL"
echo "  runtime: $RUNTIME"
python3 -c "import json; c=json.load(open('$RUNTIME')); print('  runtime coeffs: alpha={} beta={} gamma={} source={}'.format(c['alpha'], c['beta'], c['gamma'], c.get('source','')))"

echo "Start worker (separate terminal, repo root):"
echo "  python3 scripts/analyze/qaccess_t_update_worker.py --poll-interval 5 \\"
echo "    --model derived/qaccess_t_model.pkl \\"
echo "    --coeffs-out derived/qaccess_t_runtime_coefficients.json \\"
echo "    --min-improvement-pct 1.0"
echo ""
echo "Then run combined deterioration eval:"
echo "  sudo -E ./scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh"
