#!/usr/bin/env python3
"""
Offline replay: parse [utility] log lines and simulate the same projected-gradient
weight update as in utility_controller.go (learn mode) for comparison with [learn] lines.

Usage:
  python3 verify_projected_gradient.py pull_2026.log
  python3 verify_projected_gradient.py pull_2026.log --eta 0.04 --eps 0.05 --min-interval 0.2
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from typing import List, Tuple


# Example: [utility] path=1 mode=learn G=0.1234 D=0.5678 L=0.1000 ...
UTILITY_RE = re.compile(
    r"\[utility\]\s+path=(\S+)\s+mode=(\S+)\s+G=([0-9.]+)\s+D=([0-9.]+)\s+L=([0-9.]+)\s+"
    r"bw=([0-9.]+)Mbps\s+loss=([0-9.]+)\s+owd=([0-9.]+)ms"
)


@dataclass
class Row:
    path: str
    mode: str
    g: float
    d: float
    ell: float


def _project_unit_simplex3(x0: float, x1: float, x2: float) -> Tuple[float, float, float]:
    x = [x0, x1, x2]
    u = sorted(x, reverse=True)
    n = 3
    cssv = []
    s = 0.0
    for j in range(n):
        s += u[j]
        cssv.append(s - 1.0)
    rho = 0
    for j in range(n):
        ind = float(j + 1)
        if u[j] - cssv[j] / ind > 0:
            rho = j + 1
    if rho < 1:
        rho = 1
    theta = cssv[rho - 1] / float(rho)
    return max(x0 - theta, 0.0), max(x1 - theta, 0.0), max(x2 - theta, 0.0)


def _project_to_bounded(v0: float, v1: float, v2: float, floor: float) -> Tuple[float, float, float]:
    ssum = 1.0 - 3.0 * floor
    if ssum <= 0:
        return 1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0
    p0, p1, p2 = _project_unit_simplex3(
        (v0 - floor) / ssum, (v1 - floor) / ssum, (v2 - floor) / ssum
    )
    return p0 * ssum + floor, p1 * ssum + floor, p2 * ssum + floor


def parse_utility(path: str) -> List[Row]:
    rows: List[Row] = []
    with open(path, "r", errors="replace") as f:
        for line in f:
            m = UTILITY_RE.search(line)
            if not m:
                continue
            rows.append(
                Row(
                    path=m.group(1),
                    mode=m.group(2),
                    g=float(m.group(3)),
                    d=float(m.group(4)),
                    ell=float(m.group(5)),
                )
            )
    return rows


def simulate(
    rows: List[Row],
    leader_path: str = "1",
    eta: float = 0.04,
    eps: float = 0.05,
    ema_alpha: float = 0.25,
    step_every: int = 1,
) -> None:
    """
    step_every: only perform a weight projection every Nth leader 'learn' row
    (offline proxy for 200ms min-interval; default 1 = step whenever EMA runs on a row).
    """
    wt = wd = wl = 1.0 / 3.0
    eg = ed = el = 0.0
    ema_inited = False
    k = 0
    for i, r in enumerate(rows):
        if r.path != leader_path or r.mode.lower() != "learn":
            continue
        g, d, l = r.g, r.d, r.ell
        if not ema_inited:
            eg, ed, el = g, d, l
            ema_inited = True
        else:
            a = ema_alpha
            eg = a * g + (1.0 - a) * eg
            ed = a * d + (1.0 - a) * ed
            el = a * l + (1.0 - a) * el

        k += 1
        if (k-1) % step_every != 0:
            continue
        g0, g1, g2 = eg, -ed, -el
        v0 = wt + eta * g0
        v1 = wd + eta * g1
        v2 = wl + eta * g2
        wt, wd, wl = _project_to_bounded(v0, v1, v2, eps)
        u = wt * g - wd * d - wl * l
        print(
            f"row={i:6d} k={k:5d} w=({wt:.4f},{wd:.4f},{wl:.4f}) U~{u:.4f} "
            f"ema=({eg:.4f},{ed:.4f},{el:.4f}) grad=({g0:.4f},{g1:.4f},{g2:.4f})"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log_path")
    ap.add_argument("--eta", type=float, default=0.04)
    ap.add_argument("--eps", type=float, default=0.05)
    ap.add_argument(
        "--step-every",
        type=int,
        default=1,
        help="perform projection every Nth learn sample on leader (default 1; use 5-20 to coarsen)",
    )
    args = ap.parse_args()
    rows = parse_utility(args.log_path)
    if not rows:
        print("No [utility] lines parsed; need mode=learn and matching regex.", file=sys.stderr)
        return 1
    simulate(rows, eta=args.eta, eps=args.eps, step_every=args.step_every)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
