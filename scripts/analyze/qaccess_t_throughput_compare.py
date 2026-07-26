#!/usr/bin/env python3
"""
Q-ACCeSS-T final evaluation: baseline vs qaccess_t throughput windows.

Supports:
  1. pcap-based throughput when SAVE_LOGS=0
  2. old pull-log based throughput as fallback

Writes:
  derived/qaccess_t_compare/qaccess_t_throughput_windows.csv
  derived/qaccess_t_compare/qaccess_t_improvement_vs_baseline.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

from clean_evaluator_presets import clean_windows, clip_to_clean_run, preset_from_metadata

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if str(_REPO / "scripts" / "analyze") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

try:
    from fig7_throughput_compare import WINDOWS, _find_pull, mean_tp_in_window  # type: ignore
    from throughput_timeline_eval import load_total_tp_mbps_timeseries  # type: ignore
except Exception:
    WINDOWS = [
        ("0-50", 0.0, 50.0),
        ("50-60", 50.0, 60.0),
        ("50-100", 50.0, 100.0),
        ("100-110", 100.0, 110.0),
        ("100-150", 100.0, 150.0),
        ("150-420", 150.0, 420.0),
    ]

    def mean_tp_in_window(ts: pd.DataFrame, lo: float, hi: float) -> float:
        if ts.empty:
            return float("nan")
        time_col = "time_s" if "time_s" in ts.columns else "t"
        val_col = "tp_mbps" if "tp_mbps" in ts.columns else "mbps"
        sub = ts[(ts[time_col] >= lo) & (ts[time_col] < hi)]
        if sub.empty:
            return float("nan")
        return float(sub[val_col].mean())

    def _find_pull(_rdir: Path):
        return None

    def load_total_tp_mbps_timeseries(_pull: Path):
        return pd.DataFrame()

DEFAULT_OUT = _REPO / "derived" / "qaccess_t_compare"


def _parse_r(s: str) -> tuple[str, Path]:
    if ":" not in s:
        raise ValueError("expected LABEL:path")
    lab, p = s.split(":", 1)
    return lab.strip(), Path(p.strip()).resolve()


def _find_pcaps(run_dir: Path) -> list[Path]:
    pcap_dir = run_dir / "pcaps"
    if not pcap_dir.exists():
        return []
    files = sorted(pcap_dir.glob("*.pcap")) + sorted(pcap_dir.glob("*.pcapng"))
    return [p for p in files if p.is_file() and p.stat().st_size > 0]


def _pcap_to_bytes_by_bin(pcap: Path, tshark_bin: str, bin_seconds: float) -> dict[float, int]:
    """
    Return {relative_bin_start_seconds: total_frame_bytes} for one pcap.

    Uses tshark fields:
      frame.time_epoch
      frame.len
    """
    cmd = [
        tshark_bin,
        "-n",
        "-r",
        "-",
        "-T",
        "fields",
        "-E",
        "separator=,",
        "-e",
        "frame.time_epoch",
        "-e",
        "frame.len",
    ]

    try:
        # Open the capture in Python and stream it to tshark. Some VM images
        # confine tshark with AppArmor so it cannot open captures under the
        # experiment repository even though the invoking user can read them.
        with pcap.open("rb") as capture:
            proc = subprocess.run(
                cmd,
                stdin=capture,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"tshark not found. Install it with: sudo apt install -y tshark"
        ) from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"tshark failed for {pcap}\nSTDERR:\n{e.stderr[:2000]}"
        ) from e

    rows = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            t = float(parts[0])
            n = int(float(parts[1]))
        except ValueError:
            continue
        if n <= 0:
            continue
        rows.append((t, n))

    if not rows:
        return {}

    t0 = min(t for t, _ in rows)
    bins: dict[float, int] = defaultdict(int)

    for t, n in rows:
        rel = t - t0
        idx = math.floor(rel / bin_seconds)
        bstart = idx * bin_seconds
        bins[bstart] += n

    return dict(bins)


def load_total_tp_mbps_from_pcaps(
    run_dir: Path,
    tshark_bin: str = "tshark",
    bin_seconds: float = 1.0,
) -> tuple[pd.DataFrame, list[Path]]:
    pcaps = _find_pcaps(run_dir)
    if not pcaps:
        return pd.DataFrame(), []

    total_bytes: dict[float, int] = defaultdict(int)

    for pcap in pcaps:
        one = _pcap_to_bytes_by_bin(pcap, tshark_bin=tshark_bin, bin_seconds=bin_seconds)
        for t, n in one.items():
            total_bytes[t] += n

    if not total_bytes:
        return pd.DataFrame(), pcaps

    rows = []
    for t in sorted(total_bytes):
        mbps = total_bytes[t] * 8.0 / bin_seconds / 1_000_000.0
        rows.append({"time_s": t, "tp_mbps": mbps})

    return pd.DataFrame(rows), pcaps


def load_clean_path_tp_mbps_from_pcaps(
    run_dir: Path,
    tshark_bin: str = "tshark",
    bin_seconds: float = 1.0,
) -> tuple[pd.DataFrame, list[Path]]:
    pcaps = _find_pcaps(run_dir)
    if not pcaps:
        return pd.DataFrame(), []
    path_bytes: dict[str, dict[float, int]] = {
        "path_a_mbps": defaultdict(int),
        "path_b_mbps": defaultdict(int),
        "unclassified_mbps": defaultdict(int),
    }
    for pcap in pcaps:
        lowered = pcap.name.lower()
        key = (
            "path_a_mbps" if "patha" in lowered or "path_a" in lowered
            else "path_b_mbps" if "pathb" in lowered or "path_b" in lowered
            else "unclassified_mbps"
        )
        for timestamp, byte_count in _pcap_to_bytes_by_bin(
            pcap, tshark_bin=tshark_bin, bin_seconds=bin_seconds,
        ).items():
            path_bytes[key][timestamp] += byte_count
    timestamps = sorted({timestamp for values in path_bytes.values() for timestamp in values})
    rows = []
    for timestamp in timestamps:
        a = path_bytes["path_a_mbps"].get(timestamp, 0) * 8.0 / bin_seconds / 1_000_000.0
        b = path_bytes["path_b_mbps"].get(timestamp, 0) * 8.0 / bin_seconds / 1_000_000.0
        unknown = path_bytes["unclassified_mbps"].get(timestamp, 0) * 8.0 / bin_seconds / 1_000_000.0
        rows.append({
            "time_s": timestamp,
            "path_a_mbps": a,
            "path_b_mbps": b,
            "tp_mbps": a + b + unknown,
        })
    return pd.DataFrame(rows), pcaps


def load_run_timeseries(
    run_dir: Path,
    source: str,
    tshark_bin: str,
    bin_seconds: float,
    clean_per_path: bool = False,
) -> tuple[pd.DataFrame, str, str]:
    """
    Returns:
      dataframe with time_s,tp_mbps
      source_used: pcap/log/none
      source_files string
    """
    if source in ("auto", "pcap"):
        loader = load_clean_path_tp_mbps_from_pcaps if clean_per_path else load_total_tp_mbps_from_pcaps
        ts, pcaps = loader(
            run_dir,
            tshark_bin=tshark_bin,
            bin_seconds=bin_seconds,
        )
        if not ts.empty:
            return ts, "pcap", ";".join(str(p) for p in pcaps)
        if source == "pcap":
            return pd.DataFrame(), "none", ""

    if source in ("auto", "log"):
        pull = _find_pull(run_dir)
        if pull is not None:
            ts = load_total_tp_mbps_timeseries(pull)
            if not ts.empty:
                # Normalize columns if the old parser uses different names.
                if "time_s" not in ts.columns:
                    if "t" in ts.columns:
                        ts = ts.rename(columns={"t": "time_s"})
                if "tp_mbps" not in ts.columns:
                    for c in ("mbps", "total_tp_mbps"):
                        if c in ts.columns:
                            ts = ts.rename(columns={c: "tp_mbps"})
                            break
                return ts, "log", str(pull)
        return pd.DataFrame(), "none", ""

    raise ValueError(f"unsupported source: {source}")


def _mean_tp(ts: pd.DataFrame, lo: float, hi: float) -> float:
    if ts.empty or "time_s" not in ts.columns or "tp_mbps" not in ts.columns:
        return float("nan")
    sub = ts[(ts["time_s"] >= lo) & (ts["time_s"] < hi)]
    if sub.empty:
        return float("nan")
    return float(sub["tp_mbps"].mean())


def _mean_column(ts: pd.DataFrame, column: str, lo: float, hi: float) -> float:
    if ts.empty or column not in ts.columns:
        return float("nan")
    sub = ts[(ts["time_s"] >= lo) & (ts["time_s"] < hi)]
    return float(sub[column].mean()) if not sub.empty else float("nan")


def _load_evaluator_metadata(run_dirs: list[Path]) -> dict:
    candidates: list[Path] = []
    for run_dir in run_dirs:
        candidates.extend((run_dir / "experiment_metadata.json", run_dir.parent / "experiment_metadata.json"))
    for path in candidates:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
    return {}


def main() -> None:
    ap = argparse.ArgumentParser(description="Q-ACCeSS-T throughput vs baseline")
    ap.add_argument("-r", "--run", action="append", required=True, help="LABEL:run_dir")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--baseline", default="baseline")
    ap.add_argument("--source", choices=["auto", "pcap", "log"], default="auto")
    ap.add_argument("--bin-seconds", type=float, default=1.0)
    ap.add_argument("--tshark-bin", default="tshark")
    ap.add_argument("--preset", choices=("auto", "historical", "bandwidth_clean"), default="auto")
    args = ap.parse_args()

    runs: dict[str, Path] = {}
    for item in args.run:
        lab, p = _parse_r(item)
        runs[lab] = p

    if args.baseline not in runs:
        print(f"[error] baseline label {args.baseline!r} not in runs", file=sys.stderr)
        sys.exit(1)

    metadata = _load_evaluator_metadata(list(runs.values()))
    selected_preset = args.preset
    if selected_preset == "auto":
        selected_preset = "bandwidth_clean" if preset_from_metadata(metadata) == "bandwidth_clean" else "historical"
    clean = selected_preset == "bandwidth_clean"
    windows = clean_windows("bandwidth_clean", metadata) if clean else WINDOWS

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []

    for lab, rdir in runs.items():
        ts, source_used, source_files = load_run_timeseries(
            rdir,
            source=args.source,
            tshark_bin=args.tshark_bin,
            bin_seconds=args.bin_seconds,
            clean_per_path=clean,
        )

        if ts.empty:
            print(
                f"[warn] no throughput data under {rdir}; "
                f"expected pcaps under {rdir / 'pcaps'} or pull logs",
                file=sys.stderr,
            )
            continue

        ts = ts.sort_values("time_s")
        if clean:
            ts = clip_to_clean_run(ts)
        ts.to_csv(out / f"throughput_timeseries_{lab}.csv", index=False)

        for window in windows:
            if clean:
                wname, lo, hi = window.name, window.start_s, window.end_s
            else:
                wname, lo, hi = window
            row = {
                "method": lab,
                "window": wname,
                "t_lo": lo,
                "t_hi": hi,
                "tp_mbps_mean": _mean_tp(ts, lo, hi),
                "source": source_used,
                "source_files": source_files,
            }
            if clean:
                row.update({
                    "window_role": window.role,
                    "condition": window.condition,
                    "primary_metric": "throughput",
                    "total_mbps_mean": _mean_column(ts, "tp_mbps", lo, hi),
                    "path_a_mbps_mean": _mean_column(ts, "path_a_mbps", lo, hi),
                    "path_b_mbps_mean": _mean_column(ts, "path_b_mbps", lo, hi),
                })
            rows.append(row)

    if not rows:
        print(
            "[error] No throughput data found. Expected pcaps under run_dir/pcaps or pull logs.",
            file=sys.stderr,
        )
        sys.exit(2)

    df = pd.DataFrame(rows)
    windows_path = out / "qaccess_t_throughput_windows.csv"
    df.to_csv(windows_path, index=False)

    base_df = df[df["method"] == args.baseline]
    if base_df.empty:
        print(f"[error] no baseline rows for label {args.baseline!r}", file=sys.stderr)
        sys.exit(3)

    base = base_df.set_index("window")["tp_mbps_mean"]

    imp_rows: list[dict] = []
    for lab in runs:
        if lab == args.baseline:
            continue
        sub = df[df["method"] == lab].set_index("window")
        for wname in sub.index:
            b = base.get(wname, float("nan"))
            e = float(sub.loc[wname, "tp_mbps_mean"])
            pct = float("nan")
            if b == b and b > 0 and e == e:
                pct = (e - b) / b * 100.0
            imp_rows.append({
                "method": lab,
                "window": wname,
                "baseline_tp_mbps": b,
                "enhanced_tp_mbps": e,
                "improvement_pct": pct,
                **({
                    "window_role": str(sub.loc[wname, "window_role"]),
                    "condition": str(sub.loc[wname, "condition"]),
                    "baseline_total_mbps": b,
                    "qaccess_total_mbps": e,
                    "total_delta_mbps": e - b if math.isfinite(b) and math.isfinite(e) else float("nan"),
                    "baseline_path_a_mbps": float(base_df.set_index("window").loc[wname, "path_a_mbps_mean"]),
                    "qaccess_path_a_mbps": float(sub.loc[wname, "path_a_mbps_mean"]),
                    "baseline_path_b_mbps": float(base_df.set_index("window").loc[wname, "path_b_mbps_mean"]),
                    "qaccess_path_b_mbps": float(sub.loc[wname, "path_b_mbps_mean"]),
                    "result_type": str(sub.loc[wname, "window_role"]),
                } if clean else {}),
            })

    imp = pd.DataFrame(imp_rows)
    imp_path = out / "qaccess_t_improvement_vs_baseline.csv"
    imp.to_csv(imp_path, index=False)

    derived = _REPO / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    shutil.copy2(windows_path, derived / "qaccess_t_throughput_windows.csv")
    shutil.copy2(imp_path, derived / "qaccess_t_improvement_vs_baseline.csv")
    (out / "evaluation_metadata.json").write_text(json.dumps({
        "preset": selected_preset,
        "source_metadata": metadata,
        "windows": [
            {"name": window.name, "start_s": window.start_s, "end_s": window.end_s,
             "role": window.role, "condition": window.condition}
            for window in windows
        ] if clean else [
            {"name": name, "start_s": lo, "end_s": hi} for name, lo, hi in windows
        ],
    }, indent=2), encoding="utf-8")

    print(f"Wrote {windows_path}")
    print(f"Wrote {imp_path}")
    print(f"Evaluator preset: {selected_preset}")

    if not imp.empty:
        print("\nImprovement vs baseline (%):")
        print(
            imp.pivot(index="window", columns="method", values="improvement_pct")
            .to_string(float_format="%.2f")
        )


if __name__ == "__main__":
    main()
