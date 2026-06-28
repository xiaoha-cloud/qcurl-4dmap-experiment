#!/usr/bin/env bash
# Run or post-process the final Q-ACCeSS-T evaluation pipeline.
#
# This script intentionally keeps the experiment runner separate from the final
# evaluation builder. The paired Mininet runners collect logs and per-leg
# diagnostics; this script adds QoE summaries, visual-fidelity metrics when
# possible, final tables, plots, and an audit-friendly README.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

PROFILE="fig8_combined"
SESSION=""
FIG7_SESSION=""
FIG8_SESSION=""
MODE=""
OUTPUT_ROOT="derived/final_eval"
OVERWRITE=0
SKIP_VISUAL=0
GAP_THRESHOLD_MS=500
LAST_RUN_SESSION=""

usage() {
  cat <<'EOF'
usage:
  # Post-process an existing Fig.8 session.
  bash scripts/eval/run_final_eval_pipeline.sh \
    --session logs_exp/<session> \
    --profile fig8_combined \
    --postprocess-only

  # Run one experiment and then post-process it.
  sudo env INPUT_FLV=/home/mininet/Videos/push_input.flv \
    bash scripts/eval/run_final_eval_pipeline.sh \
      --profile fig8_combined \
      --run-experiment

  # Run Fig.8 and Fig.7, then build a combined final evaluation.
  sudo env INPUT_FLV=/home/mininet/Videos/push_input.flv \
    bash scripts/eval/run_final_eval_pipeline.sh \
      --profile all \
      --run-experiment

options:
  --profile <fig8_combined|fig7_bandwidth|all>
  --session <path>              Existing session for single-profile postprocess.
  --fig7-session <path>         Existing Fig.7 session for --profile all.
  --fig8-session <path>         Existing Fig.8 session for --profile all.
  --postprocess-only
  --run-experiment
  --output-root <path>          Default: derived/final_eval
  --overwrite                   Replace only the selected derived output dir.
  --skip-visual                 Skip FFmpeg SSIM/PSNR/VMAF.
  --gap-threshold-ms <number>   Default: 500
  -h, --help
EOF
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="${2:?missing profile}"; shift 2 ;;
    --session) SESSION="${2:?missing session}"; shift 2 ;;
    --fig7-session) FIG7_SESSION="${2:?missing Fig.7 session}"; shift 2 ;;
    --fig8-session) FIG8_SESSION="${2:?missing Fig.8 session}"; shift 2 ;;
    --postprocess-only) MODE="postprocess"; shift ;;
    --run-experiment) MODE="run"; shift ;;
    --output-root) OUTPUT_ROOT="${2:?missing output root}"; shift 2 ;;
    --overwrite) OVERWRITE=1; shift ;;
    --skip-visual) SKIP_VISUAL=1; shift ;;
    --gap-threshold-ms) GAP_THRESHOLD_MS="${2:?missing threshold}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[final-eval] unsupported argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$PROFILE" in
  fig8_combined|fig7_bandwidth|all) ;;
  *) echo "[final-eval] unsupported profile: $PROFILE" >&2; exit 2 ;;
esac

if [[ -z "$MODE" ]]; then
  echo "[final-eval] choose exactly one mode: --postprocess-only or --run-experiment" >&2
  exit 2
fi

require_file() {
  local path="$1"
  local label="$2"
  if [[ -z "$path" || ! -e "$path" ]]; then
    echo "[preflight] missing ${label}: ${path:-<unset>}" >&2
    return 1
  fi
}

profile_to_runner() {
  case "$1" in
    fig8_combined) printf '%s\n' "scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh" ;;
    fig7_bandwidth) printf '%s\n' "scripts/mininet/run_qaccess_t_fig7_baseline_vs_dynamic_hybrid.sh" ;;
  esac
}

profile_to_session_arg() {
  case "$1" in
    fig8_combined) printf '%s\n' "--fig8-session" ;;
    fig7_bandwidth) printf '%s\n' "--fig7-session" ;;
  esac
}

profile_to_session_glob() {
  case "$1" in
    fig8_combined) printf '%s\n' "logs_exp/session_combined_deterioration_*" ;;
    fig7_bandwidth) printf '%s\n' "logs_exp/session_fig7_capacity_hybrid_*" ;;
  esac
}

profile_to_legs() {
  case "$1" in
    fig8_combined) printf '%s\n' "combined_baseline combined_qaccess_t_dynamic" ;;
    fig7_bandwidth) printf '%s\n' "fig7_baseline fig7_qaccess_t_dynamic" ;;
  esac
}

preflight_common() {
  mkdir -p "$OUTPUT_ROOT"
  echo "[preflight] pwd=$(pwd)"
  echo "[preflight] python=$(python3 --version 2>&1)"
  echo "[preflight] git status --short:"
  git status --short || true

  require_file "scripts/analyze/qoe_from_events.py" "qoe_from_events.py"
  require_file "scripts/analyze/visual_fidelity_ffmpeg.sh" "visual_fidelity_ffmpeg.sh"
  require_file "scripts/analyze/build_final_evaluation.py" "build_final_evaluation.py"

  if command -v ffmpeg >/dev/null 2>&1; then
    echo "[preflight] ffmpeg filters:"
    ffmpeg -hide_banner -filters 2>/dev/null | grep -E '(^| )ssim |(^| )psnr |libvmaf' || true
  else
    echo "[preflight] ffmpeg unavailable; visual fidelity will be skipped"
  fi
}

preflight_run() {
  export QACCESS_ENABLE_QOE_LOG="${QACCESS_ENABLE_QOE_LOG:-1}"
  export SAVE_OUTPUT_FLV="${SAVE_OUTPUT_FLV:-1}"
  export TIMEOUT="${TIMEOUT:-220}"
  export QACCESS_POST_UPDATE_OBSERVE_SEC="${QACCESS_POST_UPDATE_OBSERVE_SEC:-15}"
  export QACCESS_EXECUTION_MODE="${QACCESS_EXECUTION_MODE:-active}"
  export QACCESS_GATE_MODE="${QACCESS_GATE_MODE:-hybrid}"
  export QACCESS_GATE_BPS="${QACCESS_GATE_BPS:-100000}"
  export QACCESS_MIN_RELATIVE_GAIN="${QACCESS_MIN_RELATIVE_GAIN:-0.03}"
  export KEEP_RAW_RUNTIME="${KEEP_RAW_RUNTIME:-1}"
  export KEEP_ALL_PROCESSED_BUFFERS="${KEEP_ALL_PROCESSED_BUFFERS:-1}"
  export KEEP_PCAP="${KEEP_PCAP:-0}"
  export SAVE_VERBOSE_LOGS="${SAVE_VERBOSE_LOGS:-1}"
  export LOG_CONTROL="${LOG_CONTROL:-1}"
  export QACCESS_WORKER_MODEL="${QACCESS_WORKER_MODEL:-$ROOT/derived/qaccess_t_qserver_sender/qaccess_t_model_delta_bw_1s.pkl}"
  export QACCESS_WORKER_MODEL_METADATA="${QACCESS_WORKER_MODEL_METADATA:-$ROOT/derived/qaccess_t_qserver_sender/qaccess_t_qserver_sender_report.json}"
  export WORKER_PYTHON="${WORKER_PYTHON:-$ROOT/.venv/bin/python3}"

  require_file "${INPUT_FLV:-}" "INPUT_FLV"
  require_file "$QACCESS_WORKER_MODEL" "QACCESS_WORKER_MODEL"
  require_file "$QACCESS_WORKER_MODEL_METADATA" "QACCESS_WORKER_MODEL_METADATA"
  require_file "$WORKER_PYTHON" "WORKER_PYTHON"

  echo "[preflight] run defaults:"
  echo "  QACCESS_ENABLE_QOE_LOG=$QACCESS_ENABLE_QOE_LOG"
  echo "  SAVE_OUTPUT_FLV=$SAVE_OUTPUT_FLV"
  echo "  TIMEOUT=$TIMEOUT"
  echo "  QACCESS_POST_UPDATE_OBSERVE_SEC=$QACCESS_POST_UPDATE_OBSERVE_SEC"
  echo "  QACCESS_EXECUTION_MODE=$QACCESS_EXECUTION_MODE"
  echo "  QACCESS_GATE_MODE=$QACCESS_GATE_MODE"
  echo "  QACCESS_GATE_BPS=$QACCESS_GATE_BPS"
}

run_one_profile() {
  local profile="$1"
  local runner
  runner="$(profile_to_runner "$profile")"
  require_file "$runner" "$profile runner"
  echo "[final-eval] running experiment profile=$profile runner=$runner"
  bash "$runner"
  local latest
  latest="$(cat logs_exp/.last_session)"
  if [[ ! -d "$latest" ]]; then
    echo "[final-eval] runner finished but latest session is missing: $latest" >&2
    exit 1
  fi
  LAST_RUN_SESSION="$latest"
  echo "[final-eval] latest session for $profile: $LAST_RUN_SESSION"
}

latest_or_fail() {
  local pattern="$1"
  local latest
  latest="$(ls -dt $pattern 2>/dev/null | head -1 || true)"
  if [[ -z "$latest" ]]; then
    echo "[final-eval] no session matched: $pattern" >&2
    exit 1
  fi
  printf '%s\n' "$latest"
}

prepare_output_dir() {
  local name="$1"
  local out="$OUTPUT_ROOT/$name"
  if [[ -e "$out" ]]; then
    if [[ "$OVERWRITE" == "1" ]]; then
      rm -rf -- "$out"
    else
      out="${out}_rerun_$(date +%Y%m%d_%H%M%S)"
    fi
  fi
  mkdir -p "$out"
  printf '%s\n' "$out"
}

package_name_for() {
  local profile="$1"
  local session="$2"
  case "$profile" in
    fig8_combined) printf 'fig8_%s\n' "$(basename "$session")" ;;
    fig7_bandwidth) printf 'fig7_%s\n' "$(basename "$session")" ;;
    *) printf '%s\n' "$(basename "$session")" ;;
  esac
}

collect_missing_for_session() {
  local profile="$1"
  local session="$2"
  local missing=()
  if [[ ! -d "$session" ]]; then
    missing+=("session_dir")
  fi
  local legs
  legs="$(profile_to_legs "$profile")"
  for leg in $legs; do
    local leg_dir="$session/$leg"
    [[ -d "$leg_dir" ]] || { missing+=("$leg/dir"); continue; }
    compgen -G "$leg_dir/qoe/qoe_events_*.csv" >/dev/null || missing+=("$leg/qoe_events")
    compgen -G "$leg_dir/output_*.flv" >/dev/null || missing+=("$leg/output_flv")
    [[ -s "$leg_dir/throughput_all_down.csv" ]] || missing+=("$leg/throughput_all_down.csv")
    [[ -s "$leg_dir/throughput_pathA_down.csv" ]] || missing+=("$leg/throughput_pathA_down.csv")
    [[ -s "$leg_dir/throughput_pathB_down.csv" ]] || missing+=("$leg/throughput_pathB_down.csv")
    [[ -s "$leg_dir/leg_status.json" ]] || missing+=("$leg/leg_status.json")
    compgen -G "$leg_dir/tc_deterioration.log" >/dev/null || compgen -G "$leg_dir/logs/tc_deterioration_*.log" >/dev/null || missing+=("$leg/tc_deterioration_log")
  done
  if [[ "$profile" == "fig8_combined" ]]; then
    [[ -s "$session/combined_qaccess_t_dynamic/control_law_diagnostics.csv" ]] || missing+=("combined_qaccess_t_dynamic/control_law_diagnostics.csv")
  elif [[ "$profile" == "fig7_bandwidth" ]]; then
    [[ -s "$session/fig7_qaccess_t_dynamic/control_law_diagnostics.csv" ]] || missing+=("fig7_qaccess_t_dynamic/control_law_diagnostics.csv")
  fi
  if [[ "${#missing[@]}" -gt 0 ]]; then
    printf '%s\n' "${missing[@]}"
  fi
}

run_qoe_summary() {
  local session="$1"
  local out_dir="$2"
  local out="$out_dir/qoe_summary.csv"
  echo "[final-eval] qoe summary -> $out"
  python3 scripts/analyze/qoe_from_events.py \
    --input "$session" \
    --output "$out" \
    --gap-threshold-ms "$GAP_THRESHOLD_MS" || return 1
}

run_visual_for_leg() {
  local leg_dir="$1"
  local visual_dir="$2"
  if [[ "$SKIP_VISUAL" == "1" ]]; then
    echo "[visual] skipped by --skip-visual"
    return 0
  fi
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[visual] ffmpeg unavailable; skipping $leg_dir"
    return 0
  fi
  if [[ -z "${INPUT_FLV:-}" || ! -f "${INPUT_FLV:-}" ]]; then
    echo "[visual] INPUT_FLV unavailable on this machine; skipping $leg_dir"
    return 0
  fi
  local received
  received="$(ls "$leg_dir"/output_*.flv 2>/dev/null | head -1 || true)"
  if [[ -z "$received" ]]; then
    echo "[visual] no received FLV under $leg_dir; skipping"
    return 0
  fi
  mkdir -p "$visual_dir"
  echo "[visual] $leg_dir -> $visual_dir"
  if ! bash scripts/analyze/visual_fidelity_ffmpeg.sh "$INPUT_FLV" "$received" "$visual_dir"; then
    echo "[visual] visual fidelity failed for $leg_dir; continuing"
    return 0
  fi
}

run_visual_for_session() {
  local profile="$1"
  local session="$2"
  local out_dir="$3"
  local baseline qaccess
  case "$profile" in
    fig8_combined)
      baseline="combined_baseline"
      qaccess="combined_qaccess_t_dynamic"
      ;;
    fig7_bandwidth)
      baseline="fig7_baseline"
      qaccess="fig7_qaccess_t_dynamic"
      ;;
    *)
      return 0
      ;;
  esac
  run_visual_for_leg "$session/$baseline" "$out_dir/visual/baseline"
  run_visual_for_leg "$session/$qaccess" "$out_dir/visual/qaccess"
}

build_tables_and_plots() {
  local out_dir="$1"
  shift
  local args=("$@")
  echo "[final-eval] final tables and plots -> $out_dir"
  python3 scripts/analyze/build_final_evaluation.py \
    "${args[@]}" \
    --output "$out_dir" \
    --gap-threshold-ms "$GAP_THRESHOLD_MS"
}

write_pipeline_readme_appendix() {
  local out_dir="$1"
  local session_text="$2"
  local missing_text="$3"
  {
    echo ""
    echo "## Pipeline execution summary"
    echo ""
    echo "* Sessions: $session_text"
    echo "* Pipeline mode: $MODE"
    echo "* Profile: $PROFILE"
    echo "* Gap threshold: ${GAP_THRESHOLD_MS} ms"
    echo "* Visual fidelity requested: $([[ "$SKIP_VISUAL" == "1" ]] && echo no || echo yes)"
    echo ""
    echo "### Missing or partial inputs"
    echo ""
    if [[ -n "$missing_text" ]]; then
      printf '%s\n' "$missing_text" | sed 's/^/* /'
    else
      echo "* none detected by the pipeline preflight"
    fi
  } >> "$out_dir/README_evaluation_logic.md"
}

refresh_root_audit_index() {
  local root="$OUTPUT_ROOT"
  mkdir -p "$root"
  local mapping_out="$root/evaluation_figure_data_mapping.csv"
  local report_out="$root/evaluation_figure_plan_audit.md"
  local first=1
  : > "$mapping_out"
  for mapping in "$root"/*/figure_data_mapping.csv; do
    [[ -f "$mapping" ]] || continue
    local package
    package="$(basename "$(dirname "$mapping")")"
    if [[ "$first" == "1" ]]; then
      awk -v pkg="$package" 'NR==1{print "package," $0; next} {print pkg "," $0}' "$mapping" >> "$mapping_out"
      first=0
    else
      awk -v pkg="$package" 'NR>1{print pkg "," $0}' "$mapping" >> "$mapping_out"
    fi
  done
  if [[ "$first" == "1" ]]; then
    echo "package,figure_name,required_input_file,required_columns,current_project_produces_it,producer_script,exists_in_selected_session,output_path,missing_items" > "$mapping_out"
  fi
  {
    echo "# Final evaluation figure plan audit"
    echo ""
    echo "A. Fig.7 graph structure: correct when a fig7_* package is generated."
    echo "B. Fig.8 graph structure: correct when a fig8_* package is generated."
    echo "C. Per-figure data sources are listed in evaluation_figure_data_mapping.csv."
    echo "D. Experiment runners generate raw logs, FLV, throughput CSVs, and controller diagnostics; final QoE summaries and plots require this post-processing pipeline."
    echo "E. QoE summaries, visual fidelity, final tables, mappings, README files, and PNG plots are post-processing outputs."
    echo "F. Output packages are stored under $root/."
    echo "G. Strict aSSIM is not implemented; current visual fidelity is SSIM / PSNR / VMAF."
    echo "H. p95/max frame gap and p95 stream delay are supporting metrics only."
    echo "I. Unified final evaluation script: scripts/eval/run_final_eval_pipeline.sh."
    echo ""
    echo "## Packages"
    for d in "$root"/*/; do
      [[ -d "$d" ]] || continue
      echo "* $(basename "$d")"
    done
  } > "$report_out"
}

postprocess_single() {
  local profile="$1"
  local session="$2"
  [[ -n "$session" ]] || { echo "[final-eval] --session is required for single-profile postprocess" >&2; exit 2; }
  [[ -d "$session" ]] || { echo "[final-eval] session not found: $session" >&2; exit 1; }

  local session_name out_dir missing_text session_arg
  session_name="$(package_name_for "$profile" "$session")"
  out_dir="$(prepare_output_dir "$session_name")"
  missing_text="$(collect_missing_for_session "$profile" "$session" | sed '/^$/d' || true)"

  if ! run_qoe_summary "$session" "$out_dir"; then
    missing_text="${missing_text}"$'\n'"qoe_summary_generation_failed"
  fi
  run_visual_for_session "$profile" "$session" "$out_dir"
  session_arg="$(profile_to_session_arg "$profile")"
  build_tables_and_plots "$out_dir" "$session_arg" "$session"

  missing_text="$(printf '%s\n' "$missing_text" | sed '/^$/d' || true)"
  write_pipeline_readme_appendix "$out_dir" "$session" "$missing_text"
  refresh_root_audit_index
  print_summary "$session" "$out_dir" "$missing_text"
}

postprocess_all() {
  local fig7="$1"
  local fig8="$2"
  [[ -d "$fig7" ]] || { echo "[final-eval] Fig.7 session not found: $fig7" >&2; exit 1; }
  [[ -d "$fig8" ]] || { echo "[final-eval] Fig.8 session not found: $fig8" >&2; exit 1; }
  echo "[final-eval] building separate Fig.8 package"
  postprocess_single fig8_combined "$fig8"
  echo "[final-eval] building separate Fig.7 package"
  postprocess_single fig7_bandwidth "$fig7"
}

print_summary() {
  local session_text="$1"
  local out_dir="$2"
  local missing_text="$3"
  echo ""
  echo "A. 使用的 session: $session_text"
  echo "B. baseline leg 是否完整: $([[ -z "$missing_text" || "$missing_text" != *baseline* ]] && echo yes || echo partial)"
  echo "C. Q-ACCeSS-T leg 是否完整: $([[ -z "$missing_text" || "$missing_text" != *qaccess* ]] && echo yes || echo partial)"
  echo "D. QoE summary 是否生成: $([[ -f "$out_dir/qoe_summary.csv" || -f "$out_dir/qoe_fig8/qoe_summary.csv" ]] && echo yes || echo partial)"
  echo "E. throughput summary 是否生成: $([[ -f "$out_dir/transport_summary.csv" ]] && echo yes || echo no)"
  echo "F. visual fidelity 是否生成: $([[ -f "$out_dir/visual_quality_summary.csv" ]] && echo "see visual_quality_summary.csv" || echo no)"
  echo "G. final table 路径: $out_dir/final_eval_combined_table.csv"
  echo "H. plots 路径: $out_dir/plots"
  echo "I. README 路径: $out_dir/README_evaluation_logic.md"
  echo "J. 缺失项和注意事项:"
  if [[ -n "$missing_text" ]]; then
    printf '%s\n' "$missing_text" | sed 's/^/   - /'
  else
    echo "   - none detected"
  fi
}

preflight_common

if [[ "$MODE" == "run" ]]; then
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "[final-eval] --run-experiment must be run with sudo because Mininet needs root" >&2
    exit 1
  fi
  preflight_run
  if [[ "$PROFILE" == "all" ]]; then
    run_one_profile fig8_combined
    FIG8_SESSION="$LAST_RUN_SESSION"
    run_one_profile fig7_bandwidth
    FIG7_SESSION="$LAST_RUN_SESSION"
    postprocess_all "$FIG7_SESSION" "$FIG8_SESSION"
  else
    run_one_profile "$PROFILE"
    SESSION="$LAST_RUN_SESSION"
    postprocess_single "$PROFILE" "$SESSION"
  fi
else
  if [[ "$PROFILE" == "all" ]]; then
    [[ -n "$FIG8_SESSION" ]] || FIG8_SESSION="$(latest_or_fail "$(profile_to_session_glob fig8_combined)")"
    [[ -n "$FIG7_SESSION" ]] || FIG7_SESSION="$(latest_or_fail "$(profile_to_session_glob fig7_bandwidth)")"
    postprocess_all "$FIG7_SESSION" "$FIG8_SESSION"
  else
    if [[ -z "$SESSION" ]]; then
      SESSION="$(latest_or_fail "$(profile_to_session_glob "$PROFILE")")"
      echo "[final-eval] --session omitted; using latest: $SESSION"
    fi
    postprocess_single "$PROFILE" "$SESSION"
  fi
fi
