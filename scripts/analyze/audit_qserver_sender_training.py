#!/usr/bin/env python3
"""Strict audit for authoritative qserver sender training data."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
import numpy as np
import pandas as pd


def audit(path: Path, allow_partial: bool = False) -> list[str]:
    df = pd.read_csv(path)
    failures: list[str] = []
    print(f"rows={len(df)} runs={df.run_id.nunique() if 'run_id' in df else 0}")
    required = {"endpoint_role", "producer_pid", "connection_id", "local_endpoint", "remote_endpoint",
                "delta_bw_1s", "future_bw_1s", "bw_bps", "phase_label",
                "physical_path_label", "sender_byte_delta", "alpha", "beta", "gamma"}
    missing = sorted(required - set(df.columns))
    if missing:
        return [f"missing required columns: {missing}"]
    tuples = df[["alpha", "beta", "gamma"]].drop_duplicates()
    if len(tuples) < 27 and not allow_partial:
        failures.append(f"coefficient coverage {len(tuples)}/27")
    if not (df.endpoint_role == "server_downlink_sender").any():
        failures.append("server_downlink_sender rows absent")
    for column in ("producer_pid", "connection_id", "local_endpoint", "remote_endpoint"):
        if df[column].isna().any() or (df[column].astype(str).str.strip() == "").any():
            failures.append(f"identity column {column} contains missing values")
    if "DURING" not in set(df.phase_label):
        failures.append("DURING samples absent")
    path_b = df[df.physical_path_label == "Path B"]
    if path_b.empty:
        failures.append("Path B samples absent")
    if path_b[path_b.phase_label == "DURING"].empty:
        failures.append("Path B DURING samples absent")
    if not (pd.to_numeric(path_b.get("sender_byte_delta", 0), errors="coerce").fillna(0) > 0).any():
        failures.append("Path B has no active sender bytes")
    expected = pd.to_numeric(df.future_bw_1s, errors="coerce") - pd.to_numeric(df.bw_bps, errors="coerce")
    error = (expected - pd.to_numeric(df.delta_bw_1s, errors="coerce")).abs()
    if not bool((error.fillna(np.inf) < 1e-6).all()):
        failures.append(f"delta_bw_1s inconsistent; max error={error.max()}")
    active = df[pd.to_numeric(df.sender_byte_delta, errors="coerce").fillna(0) > 0]
    if active.empty:
        failures.append("no active media path rows")
    during_path_b_tuples = path_b[path_b.phase_label == "DURING"][["alpha", "beta", "gamma"]].drop_duplicates()
    if len(during_path_b_tuples) < len(tuples):
        failures.append(f"not every tuple has Path B DURING samples ({len(during_path_b_tuples)}/{len(tuples)})")

    for cols, title in [
        (["alpha", "beta", "gamma"], "rows by tuple"), (["phase_label"], "rows by phase"),
        (["physical_path_label"], "rows by physical path"), (["path_id"], "rows by path_id"),
        (["endpoint_role"], "rows by endpoint role"),
    ]:
        print(f"\n=== {title} ===")
        print(df.groupby(cols, dropna=False).size().rename("rows").to_string())
    print(f"\npositive sender-byte rows={len(active)} idle/control rows={len(df)-len(active)}")
    print("\n=== variation by phase/path ===")
    features = [c for c in ["loss_rate", "owd_ms", "cwnd_bytes", "inflight_bytes"] if c in df]
    print(df.groupby(["phase_label", "physical_path_label"])[features].agg(["min", "max", "std"]).to_string())
    coverage = df.groupby(["alpha", "beta", "gamma", "phase_label", "physical_path_label"]).size()
    print("\n=== tuple coverage by phase/path ===")
    print(coverage.rename("rows").to_string())
    print(f"\nidentity_qserver_only={bool((df.endpoint_role == 'server_downlink_sender').all())}")
    print(f"tuple_coverage={len(tuples)}/27 allow_partial={allow_partial}")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    failures = audit(args.input, args.allow_partial)
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    print(f"SUMMARY {'PASS' if not failures else 'FAIL'} failures={len(failures)}")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
