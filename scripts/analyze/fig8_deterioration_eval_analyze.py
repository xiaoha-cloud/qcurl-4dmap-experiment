#!/usr/bin/env python3
"""Backward-compatible wrapper for Fig.8 combined deterioration analysis."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "scripts" / "analyze") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

from qaccess_impairment_eval_analyze import PRESETS, analyze_session  # noqa: E402


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description="Analyze Fig.8-style sudden deterioration eval session"
    )
    ap.add_argument("--session", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--full-hi", type=float, default=220.0)
    args = ap.parse_args()

    session = args.session.resolve()
    if not session.is_dir():
        print(f"[error] session not found: {session}", file=sys.stderr)
        sys.exit(1)

    cfg = PRESETS["fig8"]
    out = args.out or (_REPO / "derived" / cfg["out_subdir"] / session.name)
    analyze_session(
        session,
        out.resolve(),
        title=cfg["title"],
        baseline_dir=cfg["baseline_dir"],
        dynamic_dir=cfg["dynamic_dir"],
        file_prefix=cfg["file_prefix"],
        full_hi=args.full_hi,
    )


if __name__ == "__main__":
    main()
