#!/usr/bin/env python3
"""Build provenance-preserving one-second qserver sender training rows."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

MEAN = ["bw_bps", "owd_ms", "delay_gradient_ms", "loss_rate", "cwnd_bytes", "inflight_bytes", "cwnd_room", "utility", "gain", "backoff"]
SUM = ["lost_bytes_delta", "retrans_bytes_delta"]
IDENTITY = ["endpoint_role", "producer_pid", "connection_id", "local_endpoint", "remote_endpoint"]
TARGET_SPECS = {
    "delta_bw_1s": ("bw_bps", "future_bw_1s", "per_path_future_bw_1s_minus_current_bw"),
    "delta_owd_1s": ("owd_ms", "future_owd_1s", "per_path_future_owd_1s_minus_current_owd"),
    "delta_loss_1s": ("loss_rate", "future_loss_1s", "per_path_future_loss_1s_minus_current_loss"),
}


def physical_path(endpoint: str) -> str:
    if str(endpoint).startswith("10.0.1."):
        return "Path A"
    if str(endpoint).startswith("10.0.2."):
        return "Path B"
    return "unknown"


def model_metadata(
    df: pd.DataFrame,
    input_path: Path,
    model_path: Path,
    result: dict,
    feature_list: list[str],
    git_commit: str,
    partial: bool,
    *,
    controller_variant: str = "qaccess_t",
    target: str = "delta_bw_1s",
) -> dict:
    if target not in TARGET_SPECS:
        raise ValueError(f"unsupported target {target!r}")
    _, _, semantics = TARGET_SPECS[target]
    return {
        "schema_version": 1, "input_csv": str(input_path.resolve()), "rows": len(df), "n_samples": len(df),
        "endpoint_role_distribution": df.endpoint_role.value_counts().to_dict(),
        "coefficient_coverage": df.groupby(["alpha", "beta", "gamma"]).size().rename("rows").reset_index().to_dict("records"),
        "controller_variant": controller_variant,
        "worker_target_mode": target,
        "feature_columns": feature_list,
        "feature_list": feature_list,
        "target": target,
        "target_semantics": semantics,
        "training_sessions": sorted(df.run_id.astype(str).unique().tolist()) if "run_id" in df else [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_type": "RandomForestRegressor",
        "split_strategy": "GroupKFold by run_id; leave-one-coefficient-combination-out diagnostic",
        "model_path": str(model_path.resolve()), "model_out": str(model_path.resolve()), "metrics": result,
        "aggregate_active_ready": False, "aggregate_label_defined": False,
        "git_commit": git_commit, "partial_training_data": partial,
    }


def add_future_delta_targets(
    frame: pd.DataFrame,
    group_columns: list[str],
    *,
    horizon_ms: int = 1000,
    tolerance_ms: int = 500,
) -> pd.DataFrame:
    """Attach nearest per-path future values without crossing run/connection groups."""
    pieces: list[pd.DataFrame] = []
    for _, group in frame.groupby(group_columns, sort=False, dropna=False):
        work = group.sort_values("timestamp_ms").copy()
        timestamps = pd.to_numeric(work["timestamp_ms"], errors="coerce").to_numpy(dtype=float)
        for target, (source, future, _) in TARGET_SPECS.items():
            values = pd.to_numeric(work[source], errors="coerce").to_numpy(dtype=float)
            future_values = np.full(len(work), np.nan)
            for index, timestamp in enumerate(timestamps):
                if not np.isfinite(timestamp):
                    continue
                wanted = timestamp + horizon_ms
                pos = int(np.searchsorted(timestamps, wanted, side="left"))
                candidates = [candidate for candidate in (pos - 1, pos) if candidate > index and candidate < len(work)]
                if not candidates:
                    continue
                nearest = min(candidates, key=lambda candidate: abs(timestamps[candidate] - wanted))
                if abs(timestamps[nearest] - wanted) <= tolerance_ms:
                    future_values[index] = values[nearest]
            work[future] = future_values
            work[target] = work[future] - pd.to_numeric(work[source], errors="coerce")
        work["relative_delta_bw_1s"] = work["delta_bw_1s"] / work["bw_bps"].clip(lower=1)
        pieces.append(work)
    return pd.concat(pieces, ignore_index=True) if pieces else frame.copy()


def build_run(metadata_path: Path) -> pd.DataFrame:
    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    raw = pd.read_csv(meta["runtime_samples"])
    required = {"timestamp_ms", "path_id", "alpha", "beta", "gamma", "sender_bytes_total", *IDENTITY}
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f"{metadata_path}: missing columns {missing}")
    raw = raw.sort_values(["path_id", "timestamp_ms"]).copy()
    run_start = raw.timestamp_ms.min()
    raw["time_s"] = np.floor((raw.timestamp_ms - run_start) / 1000).astype(int)
    group = ["path_id", "time_s"]
    agg = {name: (name, "mean") for name in MEAN if name in raw}
    agg.update({name: (name, "sum") for name in SUM if name in raw})
    agg.update({name: (name, "first") for name in IDENTITY})
    agg.update({"timestamp_ms": ("timestamp_ms", "min"),
                "sender_bytes_first": ("sender_bytes_total", "first"),
                "sender_bytes_total": ("sender_bytes_total", "last")})
    out = raw.groupby(group, as_index=False).agg(**agg)
    for key in ["run_id", "sweep_name", "coefficient_tuple_id", "endpoint_role", "deterioration_start_s",
                "deterioration_end_s", "impaired_interface"]:
        if key in meta:
            out[key] = meta[key]
    out["alpha"], out["beta"], out["gamma"] = meta["alpha"], meta["beta"], meta["gamma"]
    out["physical_path_label"] = out.remote_endpoint.map(physical_path)
    out["phase_label"] = np.select(
        [out.time_s < meta["deterioration_start_s"], out.time_s < meta["deterioration_end_s"]],
        ["PRE", "DURING"], default="POST",
    )
    label_group = ["run_id", "connection_id", "endpoint_role", "path_id", "alpha", "beta", "gamma"]
    out = out.sort_values(label_group + ["time_s"])
    out["sender_byte_delta"] = out.groupby(label_group, sort=False).sender_bytes_total.diff().clip(lower=0).fillna(0)
    out["interval_sender_bytes"] = out.sender_byte_delta
    out = add_future_delta_targets(out, label_group)
    return out.dropna(subset=["future_bw_1s"]).reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sweep-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("derived/qaccess_t_qserver_sender/qaccess_qserver_sender_training.csv"))
    args = parser.parse_args()
    metadata = sorted(args.sweep_dir.rglob("sweep_metadata.json"))
    if not metadata:
        parser.error(f"no sweep_metadata.json under {args.sweep_dir}")
    result = pd.concat([build_run(path) for path in metadata], ignore_index=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False)
    print(f"[build-qserver] wrote {args.output.resolve()} rows={len(result)} runs={result.run_id.nunique()} tuples={result.coefficient_tuple_id.nunique()}")


if __name__ == "__main__":
    main()
