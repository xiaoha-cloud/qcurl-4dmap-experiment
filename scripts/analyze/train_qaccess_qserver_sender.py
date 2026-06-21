#!/usr/bin/env python3
"""Train a per-path qserver-sender delta model without replacing legacy models."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import train_qaccess_t_grouped as grouped
from build_qserver_sender_training import model_metadata

REPO = Path(__file__).resolve().parents[2]


def filter_active_media_groups(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    group_cols = [name for name in ("run_id", "connection_id", "path_id") if name in df.columns]
    if "path_id" not in group_cols or "sender_byte_delta" not in df.columns:
        raise ValueError("training data must contain path_id and sender_byte_delta")
    activity = (
        df.assign(sender_byte_delta=pd.to_numeric(df.sender_byte_delta, errors="coerce").fillna(0).clip(lower=0))
        .groupby(group_cols, dropna=False).sender_byte_delta.sum().rename("sender_byte_delta_total").reset_index()
    )
    active = activity[activity.sender_byte_delta_total > 0]
    filtered = df.merge(active[group_cols], on=group_cols, how="inner")
    summary = {
        "rows_before_media_filter": int(len(df)), "rows_after_media_filter": int(len(filtered)),
        "path_groups_total": int(len(activity)), "active_media_path_groups": int(len(active)),
        "excluded_idle_path_groups": int(len(activity) - len(active)),
        "active_path_ids": sorted(int(value) for value in active.path_id.unique()),
        "excluded_path_ids": sorted(int(value) for value in activity.loc[activity.sender_byte_delta_total <= 0, "path_id"].unique()),
    }
    if filtered.empty:
        raise ValueError("no active media path groups after sender-byte filtering")
    return filtered.reset_index(drop=True), summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=REPO / "derived/qaccess_t_qserver_sender")
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    audit = REPO / "scripts/analyze/audit_qserver_sender_training.py"
    audit_cmd = [str(REPO / ".venv/bin/python3"), str(audit), "--input", str(args.input)]
    if args.allow_partial:
        audit_cmd.append("--allow-partial")
    subprocess.run(audit_cmd, cwd=REPO, check=True)
    df = grouped.load_training_frame(args.input.resolve(), min_path_id=0, min_bw_bps_relative=0)
    df = df[df.endpoint_role == "server_downlink_sender"].copy()
    df, media_filter = filter_active_media_groups(df)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    result = grouped.train_target(df, "delta_bw_1s", args.out_dir, n_estimators=80, max_depth=16, random_state=42)
    model_path = args.out_dir / "qaccess_t_model_delta_bw_1s.pkl"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    report = model_metadata(df, args.input, model_path, result, grouped.FEATURES, commit, args.allow_partial)
    report["media_path_filter"] = media_filter
    report_path = args.out_dir / "qaccess_t_qserver_sender_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"[train-qserver] model={model_path} report={report_path} rows={len(df)}")


if __name__ == "__main__":
    main()
