#!/usr/bin/env python3
"""
Build windowed Q-ACCeSS-T training CSV with forward-looking bandwidth labels.

Aggregates dense per-tick samples into fixed time windows, then labels each window
with future bandwidth over a real horizon (not the next raw row).

Input:  derived/qaccess_training_samples_coeff_sweep.csv
Output: derived/qaccess_training_samples_coeff_sweep_windowed.csv

Usage:
  python3 scripts/analyze/build_qaccess_windowed_training.py --olia-only
  python3 scripts/analyze/build_qaccess_windowed_training.py \\
    --input derived/qaccess_training_samples_coeff_sweep.csv \\
    --output derived/qaccess_training_samples_coeff_sweep_olia_windowed.csv \\
    --olia-only --chunksize 200000

Low-memory VM fallback (process each sweep file separately, then concat windowed parts):
  python3 scripts/analyze/build_qaccess_windowed_training.py \\
    --olia-only --per-sweep \\
    --sweep-dir derived/coeff_sweep \\
    --output derived/qaccess_training_samples_coeff_sweep_olia_windowed.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = _REPO / "derived" / "qaccess_training_samples_coeff_sweep.csv"
DEFAULT_OUTPUT = _REPO / "derived" / "qaccess_training_samples_coeff_sweep_windowed.csv"
DEFAULT_OLIA_OUTPUT = _REPO / "derived" / "qaccess_training_samples_coeff_sweep_olia_windowed.csv"
DEFAULT_SWEEP_DIR = _REPO / "derived" / "coeff_sweep"
DEFAULT_STREAMING_THRESHOLD_MB = 512

REQUIRED_COLS = [
    "timestamp_ms",
    "run_id",
    "path_id",
    "bw_bps",
    "owd_ms",
    "delay_gradient_ms",
    "loss_rate",
    "lost_bytes_delta",
    "retrans_bytes_delta",
    "cwnd_bytes",
    "inflight_bytes",
    "cwnd_room",
    "alpha",
    "beta",
    "gamma",
    "utility",
    "gain",
    "backoff",
]

OPTIONAL_COLS = ["sweep_name"]

COEFF_COLS = ["alpha", "beta", "gamma"]

MEAN_COLS = [
    "bw_bps",
    "owd_ms",
    "delay_gradient_ms",
    "loss_rate",
    "cwnd_bytes",
    "inflight_bytes",
    "cwnd_room",
    "utility",
    "gain",
    "backoff",
]

SUM_COLS = ["lost_bytes_delta", "retrans_bytes_delta"]

PASS1_COLS = ["timestamp_ms", "run_id", "path_id", *COEFF_COLS, "sweep_name"]

NUMERIC_FEATURE_COLS = [
    "timestamp_ms",
    "path_id",
    "bw_bps",
    "owd_ms",
    "delay_gradient_ms",
    "loss_rate",
    "lost_bytes_delta",
    "retrans_bytes_delta",
    "cwnd_bytes",
    "inflight_bytes",
    "cwnd_room",
    *COEFF_COLS,
    "utility",
    "gain",
    "backoff",
]


def _resolve_group_cols(columns: Iterable[str]) -> list[str]:
    cols = list(columns)
    group_cols: list[str] = []
    if "sweep_name" in cols:
        group_cols.append("sweep_name")
    group_cols.extend(["run_id", "path_id", "alpha", "beta", "gamma"])
    return group_cols


def _sweep_name_from_path(path: Path) -> str:
    stem = path.stem
    prefix = "qaccess_samples_"
    if stem.startswith(prefix):
        return stem[len(prefix) :]
    return stem


def _to_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = df.copy()
    for col in columns:
        if col in out.columns and col not in {"run_id", "sweep_name"}:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _print_label_diagnostics(df: pd.DataFrame, *, label: str) -> None:
    if df.empty:
        print(f"[build_windowed] {label}: no rows")
        return

    work = df.dropna(subset=["bw_bps", "next_bw_bps"]).copy()
    n = len(work)
    if n == 0:
        print(f"[build_windowed] {label}: no valid label rows")
        return

    same = int((work["next_bw_bps"] == work["bw_bps"]).sum())
    frac_same = float(same) / float(n)
    corr = float(work[["bw_bps", "next_bw_bps"]].corr().iloc[0, 1])

    if "delta_bw_1s" in work.columns:
        delta = work["delta_bw_1s"]
    else:
        delta = work["next_bw_bps"] - work["bw_bps"]

    print(f"[build_windowed] {label} total rows: {n}")
    print(f"[build_windowed] {label} fraction next_bw_bps == bw_bps: {frac_same:.6f}")
    print(f"[build_windowed] {label} corr(bw_bps, next_bw_bps): {corr:.6f}")
    print(f"[build_windowed] {label} delta_bw_1s describe:")
    print(delta.describe().to_string())

    if "relative_delta_bw_1s" in work.columns:
        print(f"[build_windowed] {label} relative_delta_bw_1s describe:")
        print(work["relative_delta_bw_1s"].describe().to_string())


def _add_time_windows(df: pd.DataFrame, group_cols: list[str], window_ms: int) -> pd.DataFrame:
    work = df.copy()
    if "run_id" not in work.columns or work["run_id"].isna().all():
        work["run_id"] = "default"
    work["run_id"] = work["run_id"].astype(str)
    if "sweep_name" in work.columns:
        work["sweep_name"] = work["sweep_name"].astype(str)

    work["timestamp_ms"] = pd.to_numeric(work["timestamp_ms"], errors="coerce")
    work = work.dropna(subset=["timestamp_ms"])

    min_ts = work.groupby(group_cols, sort=False)["timestamp_ms"].transform("min")
    work["time_s"] = np.floor((work["timestamp_ms"] - min_ts) / float(window_ms)).astype(np.int64)
    return work


def _aggregate_windows(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    agg: dict[str, str] = {"timestamp_ms": "min"}
    for col in MEAN_COLS:
        agg[col] = "mean"
    for col in SUM_COLS:
        agg[col] = "sum"

    return (
        df.groupby(group_cols + ["time_s"], as_index=False, sort=False)
        .agg(agg)
        .sort_values(group_cols + ["time_s"], kind="mergesort")
        .reset_index(drop=True)
    )


def _add_future_labels(
    df: pd.DataFrame,
    group_cols: list[str],
    *,
    horizon_windows: int,
    future_avg_windows: int,
) -> pd.DataFrame:
    work = df.copy()
    grouped = work.groupby(group_cols, sort=False)

    work["future_bw_1s"] = grouped["bw_bps"].shift(-horizon_windows)

    fwd_parts = [grouped["bw_bps"].shift(-offset) for offset in range(1, future_avg_windows + 1)]
    work["future_bw_5s"] = pd.concat(fwd_parts, axis=1).mean(axis=1)

    work["delta_bw_1s"] = work["future_bw_1s"] - work["bw_bps"]
    work["relative_delta_bw_1s"] = work["delta_bw_1s"] / np.maximum(work["bw_bps"], 1.0)
    work["next_bw_bps"] = work["future_bw_1s"]
    return work


def build_windowed_dataset(
    df: pd.DataFrame,
    *,
    window_ms: int,
    horizon_windows: int,
    future_avg_windows: int,
    min_path_id: int = 0,
    min_bw_bps_relative: float = 0.0,
) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")

    keep_cols = [c for c in REQUIRED_COLS + OPTIONAL_COLS if c in df.columns]
    work = _to_numeric(df[keep_cols].copy(), REQUIRED_COLS)
    work = work.dropna(subset=["path_id", "bw_bps", *COEFF_COLS])
    work["path_id"] = work["path_id"].astype(int)
    if min_path_id > 0:
        work = work.loc[work["path_id"] >= min_path_id].copy()

    group_cols = _resolve_group_cols(work.columns)
    work = _add_time_windows(work, group_cols, window_ms)
    windowed = _aggregate_windows(work, group_cols)
    windowed = _add_future_labels(
        windowed,
        group_cols,
        horizon_windows=horizon_windows,
        future_avg_windows=future_avg_windows,
    )
    windowed = windowed.dropna(subset=["future_bw_1s"]).reset_index(drop=True)

    if min_bw_bps_relative > 0 and not windowed.empty:
        windowed = windowed.loc[windowed["bw_bps"] >= min_bw_bps_relative].copy()
        windowed = windowed.replace([np.inf, -np.inf], np.nan)
        windowed = windowed.dropna(subset=["relative_delta_bw_1s"]).reset_index(drop=True)

    return windowed


@dataclass(frozen=True)
class GroupKey:
    sweep_name: str | None
    run_id: str
    path_id: int
    alpha: float
    beta: float
    gamma: float

    def as_dict(self, group_cols: list[str]) -> dict[str, object]:
        out: dict[str, object] = {
            "run_id": self.run_id,
            "path_id": self.path_id,
            "alpha": self.alpha,
            "beta": self.beta,
            "gamma": self.gamma,
        }
        if "sweep_name" in group_cols and self.sweep_name is not None:
            out["sweep_name"] = self.sweep_name
        return out


@dataclass
class WindowBucket:
    timestamp_ms_min: int | None = None
    mean_sums: dict[str, float] = field(default_factory=dict)
    mean_counts: dict[str, int] = field(default_factory=dict)
    sum_totals: dict[str, float] = field(default_factory=dict)

    def add_row(self, row: dict[str, float]) -> None:
        ts = row.get("timestamp_ms")
        if ts is not None:
            ts_i = int(ts)
            if self.timestamp_ms_min is None or ts_i < self.timestamp_ms_min:
                self.timestamp_ms_min = ts_i

        for col in MEAN_COLS:
            val = row.get(col)
            if val is None or (isinstance(val, float) and np.isnan(val)):
                continue
            self.mean_sums[col] = self.mean_sums.get(col, 0.0) + float(val)
            self.mean_counts[col] = self.mean_counts.get(col, 0) + 1

        for col in SUM_COLS:
            val = row.get(col)
            if val is None or (isinstance(val, float) and np.isnan(val)):
                continue
            self.sum_totals[col] = self.sum_totals.get(col, 0.0) + float(val)

    def to_row(self, group_cols: list[str], key: GroupKey, time_s: int) -> dict[str, object]:
        out = key.as_dict(group_cols)
        out["time_s"] = time_s
        out["timestamp_ms"] = self.timestamp_ms_min
        for col in MEAN_COLS:
            count = self.mean_counts.get(col, 0)
            out[col] = self.mean_sums[col] / count if count else np.nan
        for col in SUM_COLS:
            out[col] = self.sum_totals.get(col, 0.0)
        return out


@dataclass
class StreamStats:
    chunks_processed: int = 0
    raw_rows_read: int = 0
    olia_rows_retained: int = 0
    windowed_rows_written: int = 0


def _read_usecols(all_names: list[str], requested: list[str]) -> list[str]:
    available = set(all_names)
    return [c for c in requested if c in available]


def _iter_csv_chunks(
    input_path: Path,
    *,
    chunksize: int,
    usecols: list[str] | None = None,
) -> Iterator[pd.DataFrame]:
    return pd.read_csv(input_path, chunksize=chunksize, usecols=usecols)


def _prepare_chunk(
    chunk: pd.DataFrame,
    *,
    min_path_id: int,
    default_sweep_name: str | None,
    numeric_cols: list[str],
    require_bw: bool = True,
) -> pd.DataFrame:
    work = chunk.copy()
    if default_sweep_name is not None and "sweep_name" not in work.columns:
        work["sweep_name"] = default_sweep_name

    for col in numeric_cols:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")

    drop_cols = ["path_id", *COEFF_COLS, "timestamp_ms"]
    if require_bw:
        drop_cols.insert(1, "bw_bps")
    present = [c for c in drop_cols if c in work.columns]
    work = work.dropna(subset=present)
    if work.empty:
        return work

    work["path_id"] = work["path_id"].astype(int)
    if min_path_id > 0:
        work = work.loc[work["path_id"] >= min_path_id]
    if work.empty:
        return work

    if "run_id" not in work.columns or work["run_id"].isna().all():
        work["run_id"] = "default"
    work["run_id"] = work["run_id"].astype(str)
    if "sweep_name" in work.columns:
        work["sweep_name"] = work["sweep_name"].astype(str)
    return work


def _row_group_key(row: pd.Series, *, include_sweep_name: bool) -> GroupKey | None:
    try:
        return GroupKey(
            sweep_name=str(row["sweep_name"]) if include_sweep_name else None,
            run_id=str(row["run_id"]),
            path_id=int(row["path_id"]),
            alpha=round(float(row["alpha"]), 4),
            beta=round(float(row["beta"]), 4),
            gamma=round(float(row["gamma"]), 4),
        )
    except (TypeError, ValueError, KeyError):
        return None


def _pass1_min_timestamps(
    input_path: Path,
    *,
    chunksize: int,
    min_path_id: int,
    default_sweep_name: str | None,
    stats: StreamStats,
) -> tuple[dict[GroupKey, int], bool]:
    header = pd.read_csv(input_path, nrows=0)
    include_sweep = "sweep_name" in header.columns or default_sweep_name is not None
    usecols = _read_usecols(list(header.columns), PASS1_COLS)

    min_ts: dict[GroupKey, int] = {}
    for chunk in _iter_csv_chunks(input_path, chunksize=chunksize, usecols=usecols):
        stats.chunks_processed += 1
        stats.raw_rows_read += len(chunk)
        work = _prepare_chunk(
            chunk,
            min_path_id=min_path_id,
            default_sweep_name=default_sweep_name,
            numeric_cols=["timestamp_ms", "path_id", *COEFF_COLS],
            require_bw=False,
        )
        pass1_olia = len(work)
        if work.empty:
            print(
                f"[build_windowed] pass1 chunk={stats.chunks_processed} "
                f"raw_rows={stats.raw_rows_read} olia_rows={pass1_olia} "
                f"groups={len(min_ts)}"
            )
            continue

        for _, row in work.iterrows():
            key = _row_group_key(row, include_sweep_name=include_sweep)
            if key is None:
                continue
            ts = int(row["timestamp_ms"])
            prev = min_ts.get(key)
            if prev is None or ts < prev:
                min_ts[key] = ts

        print(
            f"[build_windowed] pass1 chunk={stats.chunks_processed} "
            f"raw_rows={stats.raw_rows_read} olia_rows={pass1_olia} "
            f"groups={len(min_ts)}"
        )

    return min_ts, include_sweep


def _pass2_aggregate(
    input_path: Path,
    *,
    chunksize: int,
    min_path_id: int,
    default_sweep_name: str | None,
    include_sweep_name: bool,
    min_ts_by_group: dict[GroupKey, int],
    window_ms: int,
    stats: StreamStats,
) -> dict[GroupKey, dict[int, WindowBucket]]:
    header = pd.read_csv(input_path, nrows=0)
    read_cols = [c for c in REQUIRED_COLS + OPTIONAL_COLS if c in header.columns]
    numeric_cols = [c for c in NUMERIC_FEATURE_COLS if c in read_cols]

    pass2 = StreamStats()
    buckets_by_group: dict[GroupKey, dict[int, WindowBucket]] = defaultdict(dict)

    for chunk in _iter_csv_chunks(input_path, chunksize=chunksize, usecols=read_cols):
        pass2.chunks_processed += 1
        pass2.raw_rows_read += len(chunk)
        work = _prepare_chunk(
            chunk,
            min_path_id=min_path_id,
            default_sweep_name=default_sweep_name,
            numeric_cols=numeric_cols,
        )
        pass2.olia_rows_retained += len(work)
        if work.empty:
            continue

        for _, row in work.iterrows():
            key = _row_group_key(row, include_sweep_name=include_sweep_name)
            if key is None:
                continue
            group_min_ts = min_ts_by_group.get(key)
            if group_min_ts is None:
                continue

            ts = int(row["timestamp_ms"])
            time_s = int(np.floor((ts - group_min_ts) / float(window_ms)))
            bucket_map = buckets_by_group[key]
            bucket = bucket_map.get(time_s)
            if bucket is None:
                bucket = WindowBucket()
                bucket_map[time_s] = bucket

            row_dict = {col: float(row[col]) if col in row.index else np.nan for col in numeric_cols}
            row_dict["timestamp_ms"] = float(ts)
            bucket.add_row(row_dict)

        print(
            f"[build_windowed] pass2 chunk={pass2.chunks_processed} "
            f"raw_rows={pass2.raw_rows_read} olia_rows={pass2.olia_rows_retained} "
            f"active_groups={len(buckets_by_group)}"
        )

    stats.chunks_processed += pass2.chunks_processed
    stats.raw_rows_read += pass2.raw_rows_read
    stats.olia_rows_retained = pass2.olia_rows_retained
    return buckets_by_group


def _finalize_group_windows(
    key: GroupKey,
    bucket_map: dict[int, WindowBucket],
    *,
    group_cols: list[str],
    horizon_windows: int,
    future_avg_windows: int,
    min_bw_bps_relative: float,
) -> list[dict[str, object]]:
    if not bucket_map:
        return []

    ordered_time_s = sorted(bucket_map.keys())
    base_rows = [bucket_map[t].to_row(group_cols, key, t) for t in ordered_time_s]
    bw = [float(r["bw_bps"]) for r in base_rows]
    out_rows: list[dict[str, object]] = []

    for i, row in enumerate(base_rows):
        j = i + horizon_windows
        if j >= len(base_rows):
            continue

        future_vals = [bw[i + offset] for offset in range(1, future_avg_windows + 1) if i + offset < len(bw)]
        if not future_vals:
            continue

        future_bw_1s = bw[j]
        row = dict(row)
        row["future_bw_1s"] = future_bw_1s
        row["future_bw_5s"] = float(np.mean(future_vals))
        row["delta_bw_1s"] = future_bw_1s - float(row["bw_bps"])
        row["relative_delta_bw_1s"] = row["delta_bw_1s"] / max(float(row["bw_bps"]), 1.0)
        row["next_bw_bps"] = future_bw_1s

        if min_bw_bps_relative > 0:
            if float(row["bw_bps"]) < min_bw_bps_relative:
                continue
            rel = row["relative_delta_bw_1s"]
            if rel is None or (isinstance(rel, float) and (np.isnan(rel) or np.isinf(rel))):
                continue

        out_rows.append(row)

    return out_rows


def _windowed_fieldnames(group_cols: list[str]) -> list[str]:
    return group_cols + [
        "time_s",
        "timestamp_ms",
        *MEAN_COLS,
        *SUM_COLS,
        "future_bw_1s",
        "future_bw_5s",
        "delta_bw_1s",
        "relative_delta_bw_1s",
        "next_bw_bps",
    ]


def _write_windowed_rows(
    rows: list[dict[str, object]],
    *,
    output_path: Path,
    group_cols: list[str],
    write_header: bool,
) -> int:
    if not rows:
        return 0

    fieldnames = _windowed_fieldnames(group_cols)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if write_header else "a"
    with output_path.open(mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


def build_windowed_streaming(
    input_path: Path,
    output_path: Path,
    *,
    window_ms: int,
    horizon_windows: int,
    future_avg_windows: int,
    min_path_id: int,
    min_bw_bps_relative: float,
    chunksize: int,
    default_sweep_name: str | None = None,
    append_output: bool = False,
) -> StreamStats:
    stats = StreamStats()
    print(f"[build_windowed] streaming input: {input_path}")

    min_ts_by_group, include_sweep = _pass1_min_timestamps(
        input_path,
        chunksize=chunksize,
        min_path_id=min_path_id,
        default_sweep_name=default_sweep_name,
        stats=stats,
    )
    if not min_ts_by_group:
        print("[build_windowed] streaming: no OLIA rows found")
        return stats

    group_cols = _resolve_group_cols(
        ["sweep_name"] if include_sweep else [c for c in PASS1_COLS if c != "sweep_name"]
    )

    buckets_by_group = _pass2_aggregate(
        input_path,
        chunksize=chunksize,
        min_path_id=min_path_id,
        default_sweep_name=default_sweep_name,
        include_sweep_name=include_sweep,
        min_ts_by_group=min_ts_by_group,
        window_ms=window_ms,
        stats=stats,
    )

    write_header = not append_output or not output_path.is_file()
    for key in sorted(buckets_by_group.keys(), key=lambda k: (k.sweep_name or "", k.run_id, k.path_id, k.alpha, k.beta, k.gamma)):
        finalized = _finalize_group_windows(
            key,
            buckets_by_group[key],
            group_cols=group_cols,
            horizon_windows=horizon_windows,
            future_avg_windows=future_avg_windows,
            min_bw_bps_relative=min_bw_bps_relative,
        )
        written = _write_windowed_rows(
            finalized,
            output_path=output_path,
            group_cols=group_cols,
            write_header=write_header,
        )
        stats.windowed_rows_written += written
        write_header = False

    print(
        f"[build_windowed] streaming done input={input_path.name} "
        f"chunks={stats.chunks_processed} raw_rows={stats.raw_rows_read} "
        f"olia_rows={stats.olia_rows_retained} windowed_rows={stats.windowed_rows_written}"
    )
    return stats


def concat_windowed_csvs(part_paths: list[Path], output_path: Path) -> int:
    if not part_paths:
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    header_written = output_path.is_file() and output_path.stat().st_size > 0

    for part in part_paths:
        if not part.is_file():
            print(f"[build_windowed] warning: missing windowed part {part}", file=sys.stderr)
            continue
        with part.open(newline="", encoding="utf-8") as src:
            reader = csv.reader(src)
            try:
                header = next(reader)
            except StopIteration:
                continue

            mode = "a" if header_written else "w"
            with output_path.open(mode, newline="", encoding="utf-8") as dst:
                writer = csv.writer(dst)
                if not header_written:
                    writer.writerow(header)
                    header_written = True
                for row in reader:
                    writer.writerow(row)
                    total += 1

    return total


def _print_group_counts(df: pd.DataFrame) -> None:
    if df.empty:
        return

    if "sweep_name" in df.columns:
        print("\n[build_windowed] rows per sweep_name:")
        per_sweep = df.groupby("sweep_name", dropna=False).size().reset_index(name="rows")
        print(per_sweep.to_string(index=False))

    print("\n[build_windowed] rows per coefficient combination:")
    combo = (
        df.groupby(COEFF_COLS, dropna=False)
        .size()
        .reset_index(name="rows")
        .sort_values(COEFF_COLS)
    )
    print(combo.to_string(index=False))

    print("\n[build_windowed] rows per path_id:")
    per_path = df.groupby("path_id", dropna=False).size().reset_index(name="rows")
    print(per_path.to_string(index=False))

    if "run_id" in df.columns:
        print("\n[build_windowed] rows per run_id:")
        per_run = df.groupby("run_id", dropna=False).size().reset_index(name="rows")
        print(per_run.to_string(index=False))


def process_per_sweep(
    sweep_dir: Path,
    output_path: Path,
    *,
    window_ms: int,
    horizon_windows: int,
    future_avg_windows: int,
    min_path_id: int,
    min_bw_bps_relative: float,
    chunksize: int,
) -> StreamStats:
    files = sorted(sweep_dir.glob("qaccess_samples_*.csv"))
    if not files:
        print(f"[build_windowed] error: no qaccess_samples_*.csv under {sweep_dir}", file=sys.stderr)
        sys.exit(1)

    part_dir = sweep_dir / "windowed_parts"
    part_dir.mkdir(parents=True, exist_ok=True)
    if output_path.is_file():
        output_path.unlink()

    totals = StreamStats()
    part_paths: list[Path] = []

    for sweep_file in files:
        sweep_name = _sweep_name_from_path(sweep_file)
        part_path = part_dir / f"qaccess_samples_{sweep_name}_olia_windowed.csv"
        if part_path.is_file():
            part_path.unlink()

        print(f"[build_windowed] per-sweep processing {sweep_file.name}")
        part_stats = build_windowed_streaming(
            sweep_file,
            part_path,
            window_ms=window_ms,
            horizon_windows=horizon_windows,
            future_avg_windows=future_avg_windows,
            min_path_id=min_path_id,
            min_bw_bps_relative=min_bw_bps_relative,
            chunksize=chunksize,
            default_sweep_name=sweep_name,
            append_output=False,
        )
        totals.chunks_processed += part_stats.chunks_processed
        totals.raw_rows_read += part_stats.raw_rows_read
        totals.olia_rows_retained += part_stats.olia_rows_retained
        totals.windowed_rows_written += part_stats.windowed_rows_written
        part_paths.append(part_path)

    merged_rows = concat_windowed_csvs(part_paths, output_path)
    if merged_rows != totals.windowed_rows_written:
        print(
            f"[build_windowed] warning: concat row count {merged_rows} != "
            f"sum of parts {totals.windowed_rows_written}",
            file=sys.stderr,
        )
    totals.windowed_rows_written = merged_rows

    print(
        f"[build_windowed] per-sweep merged {len(part_paths)} parts -> {output_path} "
        f"({totals.windowed_rows_written} rows)"
    )
    return totals


def should_use_streaming(input_path: Path, *, force_chunked: bool, threshold_mb: int) -> bool:
    if force_chunked:
        return True
    size_mb = input_path.stat().st_size / (1024 * 1024)
    return size_mb >= threshold_mb


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Aggregate Q-ACCeSS samples into time windows with forward bandwidth labels",
    )
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--window-ms", type=int, default=1000, help="Aggregation window size in ms")
    ap.add_argument(
        "--horizon-windows",
        type=int,
        default=1,
        help="Forward label horizon in windows (default 1 = next 1s window)",
    )
    ap.add_argument(
        "--future-avg-windows",
        type=int,
        default=5,
        help="Number of forward windows averaged into future_bw_5s",
    )
    ap.add_argument(
        "--min-path-id",
        type=int,
        default=0,
        help="Drop rows with path_id < this value (use 1 to exclude Cubic path 0)",
    )
    ap.add_argument(
        "--min-bw-bps-relative",
        type=float,
        default=0.0,
        help="Drop rows with bw_bps below this before keeping relative_delta_bw_1s",
    )
    ap.add_argument(
        "--olia-only",
        action="store_true",
        help="Shortcut for --min-path-id 1 and default OLIA output path",
    )
    ap.add_argument(
        "--chunksize",
        type=int,
        default=200_000,
        help="Rows per pandas chunk for streaming mode (default 200000)",
    )
    ap.add_argument(
        "--force-chunked",
        action="store_true",
        help="Always use streaming two-pass mode even for small inputs",
    )
    ap.add_argument(
        "--streaming-threshold-mb",
        type=int,
        default=DEFAULT_STREAMING_THRESHOLD_MB,
        help="Auto-enable streaming when input size exceeds this many MB",
    )
    ap.add_argument(
        "--per-sweep",
        action="store_true",
        help="Low-memory fallback: window each derived/coeff_sweep file separately, then concat",
    )
    ap.add_argument(
        "--sweep-dir",
        type=Path,
        default=DEFAULT_SWEEP_DIR,
        help="Directory containing qaccess_samples_*.csv for --per-sweep",
    )
    args = ap.parse_args()

    if args.olia_only:
        args.min_path_id = max(args.min_path_id, 1)
        if args.output == DEFAULT_OUTPUT:
            args.output = DEFAULT_OLIA_OUTPUT
        if args.min_bw_bps_relative <= 0:
            args.min_bw_bps_relative = 100_000.0

    if args.window_ms <= 0:
        print("[build_windowed] error: --window-ms must be > 0", file=sys.stderr)
        sys.exit(1)
    if args.horizon_windows <= 0:
        print("[build_windowed] error: --horizon-windows must be > 0", file=sys.stderr)
        sys.exit(1)
    if args.future_avg_windows <= 0:
        print("[build_windowed] error: --future-avg-windows must be > 0", file=sys.stderr)
        sys.exit(1)
    if args.chunksize <= 0:
        print("[build_windowed] error: --chunksize must be > 0", file=sys.stderr)
        sys.exit(1)

    output_path = args.output.resolve()

    print(
        f"[build_windowed] window_ms={args.window_ms} "
        f"horizon_windows={args.horizon_windows} "
        f"future_avg_windows={args.future_avg_windows} "
        f"min_path_id={args.min_path_id} "
        f"min_bw_bps_relative={args.min_bw_bps_relative} "
        f"chunksize={args.chunksize}"
    )

    if args.per_sweep:
        sweep_dir = args.sweep_dir.resolve()
        if not sweep_dir.is_dir():
            print(f"[build_windowed] error: missing sweep dir: {sweep_dir}", file=sys.stderr)
            sys.exit(1)
        print(f"[build_windowed] mode=per-sweep sweep_dir={sweep_dir}")
        print(f"[build_windowed] output: {output_path}")
        totals = process_per_sweep(
            sweep_dir,
            output_path,
            window_ms=args.window_ms,
            horizon_windows=args.horizon_windows,
            future_avg_windows=args.future_avg_windows,
            min_path_id=args.min_path_id,
            min_bw_bps_relative=args.min_bw_bps_relative,
            chunksize=args.chunksize,
        )
        print(
            f"\n[build_windowed] totals chunks={totals.chunks_processed} "
            f"raw_rows={totals.raw_rows_read} olia_rows={totals.olia_rows_retained} "
            f"windowed_rows={totals.windowed_rows_written}"
        )
    else:
        input_path = args.input.resolve()
        if not input_path.is_file():
            print(f"[build_windowed] error: missing input CSV: {input_path}", file=sys.stderr)
            sys.exit(1)

        print(f"[build_windowed] input: {input_path}")
        print(f"[build_windowed] output: {output_path}")

        use_streaming = should_use_streaming(
            input_path,
            force_chunked=args.force_chunked,
            threshold_mb=args.streaming_threshold_mb,
        )

        if use_streaming:
            print("[build_windowed] mode=streaming (two-pass chunked)")
            if output_path.is_file():
                output_path.unlink()
            totals = build_windowed_streaming(
                input_path,
                output_path,
                window_ms=args.window_ms,
                horizon_windows=args.horizon_windows,
                future_avg_windows=args.future_avg_windows,
                min_path_id=args.min_path_id,
                min_bw_bps_relative=args.min_bw_bps_relative,
                chunksize=args.chunksize,
            )
            print(
                f"\n[build_windowed] totals chunks={totals.chunks_processed} "
                f"raw_rows={totals.raw_rows_read} olia_rows={totals.olia_rows_retained} "
                f"windowed_rows={totals.windowed_rows_written}"
            )
        else:
            print("[build_windowed] mode=in-memory (small input)")
            optional_read = set(OPTIONAL_COLS + ["next_bw_bps"])
            raw = pd.read_csv(input_path, usecols=lambda c: c in set(REQUIRED_COLS) | optional_read)
            print(f"[build_windowed] raw rows: {len(raw)}")

            if "next_bw_bps" in raw.columns:
                raw_diag = _to_numeric(raw, ["bw_bps", "next_bw_bps"]).dropna(subset=["bw_bps", "next_bw_bps"])
                if raw_diag.empty:
                    print("[build_windowed] raw (before): no valid next_bw_bps labels in input")
                else:
                    _print_label_diagnostics(raw_diag, label="raw (before)")
            else:
                print("[build_windowed] raw (before): input has no next_bw_bps column; skipping raw label stats")

            windowed = build_windowed_dataset(
                raw,
                window_ms=args.window_ms,
                horizon_windows=args.horizon_windows,
                future_avg_windows=args.future_avg_windows,
                min_path_id=args.min_path_id,
                min_bw_bps_relative=args.min_bw_bps_relative,
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            windowed.to_csv(output_path, index=False)
            print(f"\n[build_windowed] windowed rows: {len(windowed)}")
            _print_label_diagnostics(windowed, label="windowed (after)")

    if output_path.is_file():
        out_size = output_path.stat().st_size
        print(f"\n[build_windowed] wrote {output_path} ({out_size} bytes)")
        try:
            windowed_diag = pd.read_csv(output_path)
            print(f"[build_windowed] windowed rows: {len(windowed_diag)}")
            _print_label_diagnostics(windowed_diag, label="windowed (after)")
            _print_group_counts(windowed_diag)
        except pd.errors.EmptyDataError:
            print("[build_windowed] warning: output CSV is empty", file=sys.stderr)
    else:
        print("[build_windowed] warning: no output written", file=sys.stderr)

    print("[build_windowed] done.")


if __name__ == "__main__":
    main()
