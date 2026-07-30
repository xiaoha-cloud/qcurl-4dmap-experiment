#!/usr/bin/env python3
"""Wait for clean-delay profile start, then atomically intervene on one path."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "analyze"))
from qaccess_coefficients import update_path_coeffs_locked  # noqa: E402


def wait_for_profile_start(log_dir: Path, timeout_s: float) -> tuple[Path, float]:
    deadline = time.monotonic() + timeout_s
    marker = "step 1/3 at=0s delay=40ms"
    while time.monotonic() < deadline:
        for path in sorted(log_dir.glob("tc_*.log")):
            try:
                if marker in path.read_text(encoding="utf-8", errors="replace"):
                    return path, time.time()
            except OSError:
                pass
        time.sleep(0.2)
    raise TimeoutError(f"profile start marker not found under {log_dir} within {timeout_s}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--coefficients", type=Path, required=True)
    parser.add_argument("--metadata-out", type=Path, required=True)
    parser.add_argument("--path-id", type=int, default=3)
    parser.add_argument("--intervention-s", type=float, required=True)
    parser.add_argument("--alpha", type=float, required=True)
    parser.add_argument("--beta", type=float, required=True)
    parser.add_argument("--gamma", type=float, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--replicate", type=int, required=True)
    parser.add_argument("--run-order", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--start-timeout-s", type=float, default=45)
    args = parser.parse_args()

    tc_log, profile_start_wall_s = wait_for_profile_start(args.log_dir, args.start_timeout_s)
    target = time.monotonic() + args.intervention_s
    time.sleep(max(0.0, target - time.monotonic()))
    applied_wall_ms = int(time.time() * 1000)
    update_path_coeffs_locked(
        args.coefficients,
        args.path_id,
        alpha=args.alpha,
        beta=args.beta,
        gamma=args.gamma,
        metadata={
            "source": "clean_d_randomized_real_intervention",
            "candidate_id": args.candidate_id,
            "intervention_wall_timestamp_ms": applied_wall_ms,
        },
    )
    payload = {
        "candidate_id": args.candidate_id,
        "replicate": args.replicate,
        "run_order": args.run_order,
        "seed": args.seed,
        "path_id": args.path_id,
        "alpha": args.alpha,
        "beta": args.beta,
        "gamma": args.gamma,
        "intervention_s": args.intervention_s,
        "profile_start_wall_timestamp_ms": int(profile_start_wall_s * 1000),
        "intervention_wall_timestamp_ms": applied_wall_ms,
        "tc_log": str(tc_log.resolve()),
        "coefficients_path": str(args.coefficients.resolve()),
    }
    args.metadata_out.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
