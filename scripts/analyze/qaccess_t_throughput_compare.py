#!/usr/bin/env python3
"""
Q-ACCeSS-T final evaluation: baseline vs qaccess_t throughput windows.

Writes:
  derived/qaccess_t_compare/qaccess_t_throughput_windows.csv
  derived/qaccess_t_compare/qaccess_t_improvement_vs_baseline.csv

Usage
-----
    python3 scripts/analyze/qaccess_t_throughput_compare.py \\
        -r baseline:logs_exp/session_qaccess_t_X/fig7_baseline \\
        -r qaccess_t:logs_exp/session_qaccess_t_X/fig7_qaccess_t
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if str(_REPO / "scripts" / "analyze") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

from fig7_throughput_compare import WINDOWS, _find_pull, mean_tp_in_window  # noqa: E402
from throughput_timeline_eval import load_total_tp_mbps_timeseries  # noqa: E402

DEFAULT_OUT = _REPO / "derived" / "qaccess_t_compare"


def _parse_r(s: str) -> tuple[str, Path]:
    if ":" not in s:
        raise ValueError("expected LABEL:path")
    lab, p = s.split(":", 1)
    return lab.strip(), Path(p.strip()).resolve()


def main() -> None:
    ap = argparse.ArgumentParser(description="Q-ACCeSS-T throughput vs baseline")
    ap.add_argument("-r", "--run", action="append", required=True, help="LABEL:run_dir")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--baseline", default="baseline")
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
    for lab, rdir in runs.items():
        pull = _find_pull(rdir)
        if pull is None:
            print(f"[warn] no pull log under {rdir}", file=sys.stderr)
            continue
        ts = load_total_tp_mbps_timeseries(pull)
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
    windows_path = out / "qaccess_t_throughput_windows.csv"
    df.to_csv(windows_path, index=False)

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
    imp_path = out / "qaccess_t_improvement_vs_baseline.csv"
    imp.to_csv(imp_path, index=False)

    # Convenience copies at derived/ root (requested layout).
    derived = _REPO / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    shutil.copy2(windows_path, derived / "qaccess_t_throughput_windows.csv")
    shutil.copy2(imp_path, derived / "qaccess_t_improvement_vs_baseline.csv")

    print(f"Wrote {windows_path}")
    print(f"Wrote {imp_path}")
    if not imp.empty:
        print("\nImprovement vs baseline (%):")
        print(imp.pivot(index="window", columns="method", values="improvement_pct").to_string(float_format="%.2f"))


if __name__ == "__main__":
    main()
