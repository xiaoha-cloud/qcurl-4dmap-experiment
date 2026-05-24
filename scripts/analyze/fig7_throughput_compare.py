#!/usr/bin/env python3
"""
Fig.7-style system evaluation: throughput windows + % improvement vs baseline.

Generic label-based comparison (any method names). For Q-ACCeSS-T final eval, prefer
``qaccess_t_throughput_compare.py`` (writes ``derived/qaccess_t_compare/``).

Usage
-----
    python3 scripts/analyze/fig7_throughput_compare.py --out derived/fig7_compare \\
        -r baseline:logs_exp/session_qaccess_t_X/fig7_baseline \\
        -r qaccess_t:logs_exp/session_qaccess_t_X/fig7_qaccess_t
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

from throughput_timeline_eval import load_total_tp_mbps_timeseries  # noqa: E402

WINDOWS = [
    ("0-50", 0.0, 50.0),
    ("50-60", 50.0, 60.0),
    ("50-100", 50.0, 100.0),
    ("100-110", 100.0, 110.0),
    ("100-150", 100.0, 150.0),
]


def _find_pull(run_dir: Path) -> Path | None:
    logs = run_dir / "logs"
    if logs.is_dir():
        hits = sorted(logs.glob("pull_*.log"))
        if hits:
            return hits[-1]
    hits = sorted(run_dir.glob("**/pull_*.log"))
    return hits[-1] if hits else None


def _parse_r(s: str) -> tuple[str, Path]:
    if ":" not in s:
        raise ValueError("expected LABEL:path")
    lab, p = s.split(":", 1)
    return lab.strip(), Path(p.strip()).resolve()


def mean_tp_in_window(ts: pd.DataFrame, lo: float, hi: float) -> float:
    if ts.empty:
        return float("nan")
    w = ts[(ts["t_sec"] >= lo) & (ts["t_sec"] < hi)]
    if w.empty:
        return float("nan")
    return float(w["tp_mbps_total"].mean())


def main() -> None:
    ap = argparse.ArgumentParser(description="Fig.7 throughput windows vs baseline")
    ap.add_argument("-r", "--run", action="append", required=True, help="LABEL:run_dir")
    ap.add_argument("--out", type=Path, required=True, help="output directory for CSVs")
    ap.add_argument("--baseline", default="baseline", help="label treated as baseline")
    args = ap.parse_args()

    runs: dict[str, Path] = {}
    for item in args.run:
        lab, p = _parse_r(item)
        runs[lab] = p

    if args.baseline not in runs:
        print(f"[error] baseline label {args.baseline!r} not in runs", file=sys.stderr)
        sys.exit(1)

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    series: dict[str, pd.DataFrame] = {}

    for lab, rdir in runs.items():
        pull = _find_pull(rdir)
        if pull is None:
            print(f"[warn] no pull log under {rdir}", file=sys.stderr)
            continue
        ts = load_total_tp_mbps_timeseries(pull)
        series[lab] = ts
        if not ts.empty:
            ts.to_csv(out / f"throughput_timeseries_{lab}.csv", index=False)
        for wname, lo, hi in WINDOWS:
            rows.append({
                "method": lab,
                "window": wname,
                "t_lo": lo,
                "t_hi": hi,
                "tp_mbps_mean": mean_tp_in_window(ts, lo, hi),
                "pull_log": str(pull),
            })

    df = pd.DataFrame(rows)
    df.to_csv(out / "throughput_windows.csv", index=False)

    base = df[df["method"] == args.baseline].set_index("window")["tp_mbps_mean"]
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
            })
    imp = pd.DataFrame(imp_rows)
    imp.to_csv(out / "improvement_vs_baseline.csv", index=False)

    print(f"Wrote {out}/throughput_windows.csv")
    print(f"Wrote {out}/improvement_vs_baseline.csv")
    if not imp.empty:
        print("\nImprovement vs baseline (%):")
        print(imp.pivot(index="window", columns="method", values="improvement_pct").to_string(float_format="%.2f"))


if __name__ == "__main__":
    main()
