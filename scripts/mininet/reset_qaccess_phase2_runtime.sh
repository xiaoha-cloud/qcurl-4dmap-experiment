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
DERIVED="$ROOT/derived"
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

rm -f \
  "$DERIVED/qaccess_runtime_samples.csv" \
  "$DERIVED/qaccess_update_request.json" \
  "$DERIVED/qaccess_update_response.json"

cp "$INITIAL" "$RUNTIME"
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
