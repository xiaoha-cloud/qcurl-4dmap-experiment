#!/usr/bin/env python3
"""Generate a deterministic, randomized real-intervention manifest for clean D."""

from __future__ import annotations

import argparse
import csv
import itertools
import random
from pathlib import Path

GRID = tuple(itertools.product((0.6, 0.7, 0.8), (0.1, 0.2, 0.3), (0.1, 0.2, 0.3)))
DEFAULT_SEED = 20260730


def build_rows(seed: int, replicates: int) -> list[dict[str, object]]:
    rows = []
    for replicate in range(1, replicates + 1):
        for alpha, beta, gamma in GRID:
            rows.append({
                "candidate_id": f"a{alpha:.1f}_b{beta:.1f}_g{gamma:.1f}",
                "replicate": replicate,
                "alpha": alpha,
                "beta": beta,
                "gamma": gamma,
                "is_sham": int((alpha, beta, gamma) == (0.6, 0.3, 0.1)),
            })
    random.Random(seed).shuffle(rows)
    intervention_times = (65, 70, 75)
    for index, row in enumerate(rows, start=1):
        row["run_order"] = index
        row["intervention_s"] = intervention_times[(index - 1) % len(intervention_times)]
        row["seed"] = seed
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--replicates", type=int, default=5)
    args = parser.parse_args()
    if args.replicates < 1:
        parser.error("--replicates must be positive")
    rows = build_rows(args.seed, args.replicates)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = ("run_order", "candidate_id", "replicate", "alpha", "beta", "gamma", "intervention_s", "seed", "is_sham")
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} real-intervention assignments to {args.output}")


if __name__ == "__main__":
    main()
