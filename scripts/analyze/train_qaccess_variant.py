#!/usr/bin/env python3
"""Train a qserver-sender Q-ACCeSS-D or Q-ACCeSS-L model."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import train_qaccess_t_grouped as grouped
from build_qserver_sender_training import model_metadata
from train_qaccess_qserver_sender import filter_active_media_groups

REPO = Path(__file__).resolve().parents[2]
VARIANTS = {
    "qaccess_d": {
        "target": "delta_owd_1s",
        "out_dir": REPO / "derived/qaccess_d_qserver_sender",
        "report": "qaccess_d_qserver_sender_report.json",
    },
    "qaccess_l": {
        "target": "delta_loss_1s",
        "out_dir": REPO / "derived/qaccess_l_qserver_sender",
        "report": "qaccess_l_qserver_sender_report.json",
    },
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=sorted(VARIANTS), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--n-estimators", type=int, default=80)
    parser.add_argument("--max-depth", type=int, default=16)
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args()

    spec = VARIANTS[args.variant]
    target = spec["target"]
    out_dir = (args.out_dir or spec["out_dir"]).resolve()
    audit_cmd = [
        str(REPO / ".venv/bin/python3"),
        str(REPO / "scripts/analyze/audit_qserver_sender_training.py"),
        "--input",
        str(args.input),
        "--target",
        target,
    ]
    if args.allow_partial:
        audit_cmd.append("--allow-partial")
    subprocess.run(audit_cmd, cwd=REPO, check=True)

    df = grouped.load_training_frame(args.input.resolve(), min_path_id=0, min_bw_bps_relative=0)
    df = df[df.endpoint_role == "server_downlink_sender"].copy()
    df, media_filter = filter_active_media_groups(df)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = grouped.train_target(
        df,
        target,
        out_dir,
        n_estimators=args.n_estimators,
        max_depth=args.max_depth,
        random_state=args.random_state,
    )
    model_path = out_dir / grouped.MODEL_OUT[target]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    report = model_metadata(
        df,
        args.input,
        model_path,
        result,
        grouped.FEATURES,
        commit,
        args.allow_partial,
        controller_variant=args.variant,
        target=target,
    )
    report["media_path_filter"] = media_filter
    report_path = out_dir / spec["report"]
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"[train-variant] variant={args.variant} target={target} model={model_path} report={report_path}")


if __name__ == "__main__":
    main()
