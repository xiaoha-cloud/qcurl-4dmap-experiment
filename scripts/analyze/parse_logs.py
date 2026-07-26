"""
parse_logs.py — parse pull_*.log from qcurl-4dmap-experiment.

Log lines handled:
  [utility]  path=X mode=T G=.. D=.. L=.. bw=..Mbps loss=.. owd=..ms
             gain=.. backoff=.. U=.. trend_ms=..
  [m]monitor path=X rtt_smoothed=.. rtt_min=.. bw=..B/s inflight=..
             cwnd_full=.. cwnd_room=.. loss=.. lost_B=..
  tc_delay / tc_loss   (written by tc_delay_steps.sh / tc_loss_steps.sh)
  [learn]   path=X wT=.. wD=.. wL=.. grad=(g0,g1,g2) eta=.. floor=..  (ModeLearn on leader path)
  tc_bw               (written by tc_bw_steps.sh — capacity steps on one interface)

Usage:
    from parse_logs import (
        load_pull_log,
        load_utility_gains,
        load_learn_from_pull,
        load_tc_log,
        load_tc_bw_log,
        phase_steady_windows_from_tc_bw,
        find_run_dirs,
        load_labeled_vm_runs,
    )

    df_util, df_mon = load_pull_log("logs_exp/vm_run_XXX/pull_XXX.log", label="baseline")
    tc_steps       = load_tc_log("logs_exp/vm_run_XXX/tc_delay_XXX.log")
    tc_bw          = load_tc_bw_log("logs_exp/vm_run_XXX/tc_bw_XXX.log")
    ph = phase_steady_windows_from_tc_bw(".../tc_bw_*.log", ".../pull_*.log", experiment_end_sec=200)
    ph4 = route_a_four_steady_windows(".../tc_bw_*.log", ".../pull_*.log")  # 50/100/150s design, 4×40s steady
    # If pull t=0 matches tc t=0: steady_windows_from_phase_edges(ROUTE_A_PHASE_EDGES_SEC) → 10–50, 60–100, …

    # ACCeSS-style T/D/L comparison (three vm_run dirs, same scenario):
    df_util, df_mon, tc_steps = load_labeled_vm_runs({"T": t_dir, "D": d_dir, "L": l_dir})
"""

from __future__ import annotations

import math
import re
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import pandas as pd

# Route A default designed phase edges (seconds from tc start, 200s run, steps at 0/50/100 in profile).
ROUTE_A_PHASE_EDGES_SEC = (0, 50, 100, 150, 200)

# ── regex patterns ──────────────────────────────────────────────────────────

_RE_UTILITY = re.compile(
    r"(?P<date>\d{4}/\d{2}/\d{2}) (?P<time>\d{2}:\d{2}:\d{2})"
    r" \[utility\] path=(?P<path>\d+) mode=(?P<mode>\w+)"
    r" G=(?P<G>[^\s]+) D=(?P<D>[^\s]+) L=(?P<L>[^\s]+)"
    r" bw=(?P<bw>[^\s]+)Mbps loss=(?P<loss>[^\s]+)"
    r" owd=(?P<owd>[^\s]+)ms gain=(?P<gain>[^\s]+)"
    r" backoff=(?P<backoff>[^\s]+) U=(?P<U>[^\s]+)"
    r" trend_ms=(?P<trend>[^\s]+)"
)

_RE_MONITOR = re.compile(
    r"(?P<date>\d{4}/\d{2}/\d{2}) (?P<time>\d{2}:\d{2}:\d{2})"
    r" \[m\]monitor path=(?P<path>\d+)"
    r" rtt_smoothed=(?P<rtt_smoothed>[^\s]+)"
    r" rtt_min=(?P<rtt_min>[^\s]+)"
    r" rtt_latest=(?P<rtt_latest>[^\s]+)"
    r" rtt_mean_dev=(?P<rtt_mean_dev>[^\s]+)"
    r" owd=(?P<owd>[^\s]+)"
    r" bw=(?P<bw>[^\s]+)"
    r" inflight=(?P<inflight>[^\s]+)"
    r" cwnd_full=(?P<cwnd_full>[^\s]+)"
    r" cwnd_room=(?P<cwnd_room>[^\s]+)"
    r" loss=(?P<loss>[^\s]+)"
    r" lost_B=(?P<lost_B>[^\s]+)"
)

_RE_TC_HEAD = re.compile(
    r"\[tc_(?P<mode>delay|loss)\] step \d+/\d+ at=(?P<at>\d+)s"
)
_RE_DELAY_MS = re.compile(r"delay=(?P<delay_ms>\d+)ms")
_RE_LOSS_PCT = re.compile(r"loss=(?P<loss_pct>[^\s]+)%")
_RE_TC_DEV = re.compile(r"dev=(?P<dev>[^\s]+)")

# [2026-04-24T03:25:15+00:00] [tc_bw] step 1/3 at=0s bw=20mbit dev=h1-eth0
_RE_TC_BW = re.compile(
    r"\[tc_bw\]\s+step\s+(?P<i>\d+)/(?P<n>\d+)\s+at=(?P<at>\d+)s\s+bw=(?P<bw>\d+)mbit\s+dev=(?P<dev>\S+)"
)

# 2026/04/26 09:58:14 [learn] path=1 wT=0.3333 wD=0.3333 wL=0.3333 grad=(0.0,-0.0,-0.0) eta=0.0400 floor=0.0500
_RE_LEARN = re.compile(
    r"(?P<date>\d{4}/\d{2}/\d{2}) (?P<time>\d{2}:\d{2}:\d{2})"
    r".*?\[learn\] path=(?P<path>\d+)"
    r" wT=(?P<wT>[\d.]+) wD=(?P<wD>[\d.]+) wL=(?P<wL>[\d.]+)"
    r" grad=\((?P<g0>[-\d.]+),(?P<g1>[-\d.]+),(?P<g2>[-\d.]+)\)"
    r" eta=(?P<eta>[\d.]+) floor=(?P<floor>[\d.]+)"
)

# ── helpers ─────────────────────────────────────────────────────────────────

def _parse_go_duration(s: str) -> float:
    """Convert Go duration string to milliseconds. Returns NaN on failure."""
    if not s or s in ("0s", "0"):
        return 0.0
    try:
        if s.endswith("ms"):
            return float(s[:-2])
        if s.endswith("µs") or s.endswith("us"):
            return float(s[:-2]) / 1000.0
        if s.endswith("ns"):
            return float(s[:-2]) / 1_000_000.0
        if s.endswith("s"):
            return float(s[:-1]) * 1000.0
        return float(s)
    except ValueError:
        return float("nan")


def _parse_bytes(s: str) -> float:
    """Convert Go byte string (e.g. '1783392B/s', '0B', '46720B') to float bytes."""
    s = s.rstrip("/s")
    if s.endswith("B"):
        try:
            return float(s[:-1])
        except ValueError:
            pass
    try:
        return float(s)
    except ValueError:
        return float("nan")


def _hms_to_sec(date: str, time_str: str) -> int:
    h, m, s = map(int, time_str.split(":"))
    return h * 3600 + m * 60 + s


# ── public API ───────────────────────────────────────────────────────────────

def load_pull_log(path: Union[str, Path], label: str = "") -> tuple:
    """
    Parse a pull_*.log file.

    Returns
    -------
    df_util : DataFrame  — one row per [utility] line (NaN rows dropped)
        columns: t, path, mode, G, D, L, bw_mbps, loss, owd_ms,
                 gain, backoff, U, trend_ms, label
    df_mon  : DataFrame  — one row per [m]monitor line (zero-RTT rows kept)
        columns: t, path, rtt_smoothed_ms, rtt_min_ms, rtt_latest_ms,
                 rtt_mean_dev_ms, bw_bytes,
                 cwnd_full, cwnd_room, inflight, loss, lost_B, label
    """
    util_rows = []
    mon_rows = []
    t0 = None

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            mu = _RE_UTILITY.search(line)
            if mu:
                ts = _hms_to_sec(mu["date"], mu["time"])
                if t0 is None:
                    t0 = ts
                try:
                    row = {
                        "t": ts - t0,
                        "path": int(mu["path"]),
                        "mode": mu["mode"],
                        "G": float(mu["G"]),
                        "D": float(mu["D"]),
                        "L": float(mu["L"]),
                        "bw_mbps": float(mu["bw"]),
                        "loss": float(mu["loss"]),
                        "owd_ms": float(mu["owd"]),
                        "gain": float(mu["gain"]),
                        "backoff": float(mu["backoff"]),
                        "U": float(mu["U"]),
                        "trend_ms": float(mu["trend"]),
                        "label": label,
                    }
                    util_rows.append(row)
                except (ValueError, TypeError):
                    pass
                continue

            mm = _RE_MONITOR.search(line)
            if mm:
                ts = _hms_to_sec(mm["date"], mm["time"])
                if t0 is None:
                    t0 = ts
                try:
                    row = {
                        "t": ts - t0,
                        "path": int(mm["path"]),
                        "rtt_smoothed_ms": _parse_go_duration(mm["rtt_smoothed"]),
                        "rtt_min_ms": _parse_go_duration(mm["rtt_min"]),
                        "rtt_latest_ms": _parse_go_duration(mm["rtt_latest"]),
                        "rtt_mean_dev_ms": _parse_go_duration(mm["rtt_mean_dev"]),
                        "bw_bytes": _parse_bytes(mm["bw"]),
                        "cwnd_full": _parse_bytes(mm["cwnd_full"]),
                        "cwnd_room": _parse_bytes(mm["cwnd_room"]),
                        "inflight": _parse_bytes(mm["inflight"]),
                        "loss": float(mm["loss"]) if mm["loss"] not in ("NaN", "nan") else float("nan"),
                        "lost_B": float(mm["lost_B"]),
                        "label": label,
                    }
                    mon_rows.append(row)
                except (ValueError, TypeError):
                    pass

    df_util = pd.DataFrame(util_rows)
    df_mon = pd.DataFrame(mon_rows)

    if not df_util.empty:
        df_util = df_util[df_util["U"].notna() & df_util["bw_mbps"].notna()]

    return df_util, df_mon


def _first_metric_tod_sec(path: Union[str, Path]) -> int:
    """Time-of-day (sec) of first ``[utility]`` or ``[m]monitor`` line — same origin as load_pull_log ``t``."""
    p = Path(path)
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _RE_UTILITY.search(line) or _RE_MONITOR.search(line)
            if m:
                return _hms_to_sec(m["date"], m["time"])
    return 0


def load_utility_gains(path: Union[str, Path], label: str = "") -> pd.DataFrame:
    """
    All ``[utility]`` lines with ``t`` = seconds since first [utility] or [m]monitor (like ``load_pull_log``),
    but **no** filter on U/bw. Use for time series of **gain** / **backoff** (early NaN values kept).
    """
    util_rows: list[dict] = []
    t0: Optional[int] = None
    p = Path(path)
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            mu = _RE_UTILITY.search(line)
            if mu:
                ts = _hms_to_sec(mu["date"], mu["time"])
                if t0 is None:
                    t0 = ts
                try:
                    row = {
                        "t": ts - t0,
                        "path": int(mu["path"]),
                        "mode": mu["mode"],
                        "G": float(mu["G"]) if mu["G"] not in ("NaN", "nan") else float("nan"),
                        "D": float(mu["D"]) if mu["D"] not in ("NaN", "nan") else float("nan"),
                        "L": float(mu["L"]) if mu["L"] not in ("NaN", "nan") else float("nan"),
                        "bw_mbps": float(mu["bw"]),
                        "loss": float(mu["loss"]) if mu["loss"] not in ("NaN", "nan") else float("nan"),
                        "owd_ms": float(mu["owd"]) if mu["owd"] not in ("NaN", "nan") else float("nan"),
                        "gain": float(mu["gain"]) if mu["gain"] not in ("NaN", "nan") else float("nan"),
                        "backoff": float(mu["backoff"]) if mu["backoff"] not in ("NaN", "nan") else float("nan"),
                        "U": float(mu["U"]) if mu["U"] not in ("NaN", "nan") else float("nan"),
                        "trend_ms": float(mu["trend"]) if mu["trend"] not in ("NaN", "nan") else float("nan"),
                        "label": label,
                    }
                    util_rows.append(row)
                except (ValueError, TypeError):
                    pass
                continue
            mm = _RE_MONITOR.search(line)
            if mm and t0 is None:
                t0 = _hms_to_sec(mm["date"], mm["time"])
    if not util_rows or t0 is None:
        return pd.DataFrame(
            columns=[
                "t",
                "path",
                "mode",
                "G",
                "D",
                "L",
                "bw_mbps",
                "loss",
                "owd_ms",
                "gain",
                "backoff",
                "U",
                "trend_ms",
                "label",
            ]
        )
    return pd.DataFrame(util_rows)


def load_learn_from_pull(path: Union[str, Path], label: str = "") -> pd.DataFrame:
    """
    Parse ``[learn]`` lines (ModeLearn: online ``wT,wD,wL`` and gradient on leader path).
    ``t`` uses the same second-resolution origin as :func:`load_pull_log` (first [utility] or [m]monitor).
    """
    t0 = _first_metric_tod_sec(path)
    rows: list[dict] = []
    p = Path(path)
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            ml = _RE_LEARN.search(line)
            if not ml:
                continue
            try:
                ts = _hms_to_sec(ml["date"], ml["time"])
                rows.append(
                    {
                        "t": ts - t0,
                        "path": int(ml["path"]),
                        "wT": float(ml["wT"]),
                        "wD": float(ml["wD"]),
                        "wL": float(ml["wL"]),
                        "g0": float(ml["g0"]),
                        "g1": float(ml["g1"]),
                        "g2": float(ml["g2"]),
                        "eta": float(ml["eta"]),
                        "floor": float(ml["floor"]),
                        "label": label,
                    }
                )
            except (ValueError, TypeError):
                pass
    if not rows:
        return pd.DataFrame(
            columns=["t", "path", "wT", "wD", "wL", "g0", "g1", "g2", "eta", "floor", "label"]
        )
    return pd.DataFrame(rows)


def _parse_leading_iso_timestamp(line: str) -> Optional[float]:
    """Parse leading ``[2026-04-02T16:56:38+00:00]`` from a tc log line → Unix timestamp."""
    if not line.startswith("["):
        return None
    end = line.find("]")
    if end <= 1:
        return None
    raw = line[1:end]
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return None


def first_utility_wall_unix(pull_path: Union[str, Path]) -> Optional[float]:
    """Unix timestamp of the first [utility] line in a pull log (UTC-naive parsed as UTC)."""
    pull_path = Path(pull_path)
    with open(pull_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            mu = _RE_UTILITY.search(line)
            if mu:
                try:
                    dt = datetime.strptime(
                        mu["date"] + " " + mu["time"], "%Y/%m/%d %H:%M:%S"
                    )
                    return dt.replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
    return None


def estimate_tc_pull_offset_seconds(pull_path: Union[str, Path], tc_path: Union[str, Path]) -> float:
    """
    Seconds to add to tc ``at_sec`` so it aligns with pull log ``t``.

    Pull ``t`` is (wall time of row − wall time of first ``[utility]`` line), using second
    resolution. TC ``at_sec`` is relative to tc script start. We approximate script start as
    the wall time of the first ``[tc_delay]`` / ``[tc_loss]`` step line in the tc log.

    plot_t ≈ at_sec + offset,  where  offset ≈ t_tc_first_step_wall − t_pull_first_utility_wall.

    Returns 0.0 if timestamps cannot be read.
    """
    pull_path, tc_path = Path(pull_path), Path(tc_path)
    t_util: Optional[float] = None
    with open(pull_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            mu = _RE_UTILITY.search(line)
            if mu:
                try:
                    dt = datetime.strptime(
                        mu["date"] + " " + mu["time"], "%Y/%m/%d %H:%M:%S"
                    )
                    t_util = dt.replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    pass
                break

    t_tc: Optional[float] = None
    with open(tc_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if _RE_TC_HEAD.search(line):
                ts = _parse_leading_iso_timestamp(line)
                if ts is not None:
                    t_tc = ts
                    break

    if t_util is None or t_tc is None:
        return 0.0
    return float(t_tc - t_util)


def estimate_tc_bw_pull_offset_seconds(pull_path: Union[str, Path], tc_bw_path: Union[str, Path]) -> float:
    """
    Same idea as ``estimate_tc_pull_offset_seconds``, but the first tc event is the first
    ``[tc_bw] step`` line in ``tc_bw_path``.
    """
    pull_path, tc_bw_path = Path(pull_path), Path(tc_bw_path)
    t_util = first_utility_wall_unix(pull_path)
    t_tc: Optional[float] = None
    with open(tc_bw_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if _RE_TC_BW.search(line):
                ts = _parse_leading_iso_timestamp(line)
                if ts is not None:
                    t_tc = ts
                    break
    if t_util is None or t_tc is None:
        return 0.0
    return float(t_tc - t_util)


def load_tc_bw_log(path: Union[str, Path]) -> pd.DataFrame:
    """
    Parse a ``tc_bw_*.log`` file (``tc_bw_steps.sh``).

    Each row is one applied step. Columns:

    - ``at_sec`` — time from tc script start (matches profile)
    - ``step_i``, ``step_n`` — 1-based index and total steps
    - ``bw_mbit`` — shaped rate in Mbit/s
    - ``dev`` — interface (e.g. h1-eth0)
    - ``wall_unix`` — Unix time from the leading ``[ISO8601]`` prefix on that line (NaN if missing)
    """
    rows: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _RE_TC_BW.search(line)
            if not m:
                continue
            wu = _parse_leading_iso_timestamp(line)
            rows.append({
                "at_sec": int(m["at"]),
                "step_i": int(m["i"]),
                "step_n": int(m["n"]),
                "bw_mbit": float(m["bw"]),
                "dev": m["dev"],
                "wall_unix": float(wu) if wu is not None else float("nan"),
            })
    return pd.DataFrame(rows)


def tc_bw_with_pull_t(tc_bw_path: Union[str, Path], pull_path: Union[str, Path]) -> pd.DataFrame:
    """
    Join ``load_tc_bw_log`` with pull-timeline coordinates: ``t_pull`` = seconds from the first
    ``[utility]`` line to the wall time of each tc step (uses ISO timestamps on tc lines).

    This is more accurate than ``at_sec + offset`` when tc starts before the first utility log line.
    """
    df = load_tc_bw_log(tc_bw_path)
    if df.empty:
        return df.assign(t_pull=pd.Series(dtype=float))

    t0 = first_utility_wall_unix(pull_path)
    if t0 is None:
        return df.assign(t_pull=float("nan"))

    out = []
    for _, row in df.iterrows():
        wu = row["wall_unix"]
        if wu is None or (isinstance(wu, float) and math.isnan(wu)):
            out.append(float("nan"))
        else:
            out.append(float(wu) - t0)
    df = df.copy()
    df["t_pull"] = out
    return df


def steady_windows_from_phase_edges(
    phase_edges_sec: tuple[float, ...],
    *,
    transition_sec: float = 10.0,
) -> list[tuple[int, float, float]]:
    """
    Build steady analysis windows from **designed** phase edges (experiment time, same origin as pull ``t``).

    For each segment ``[edges[i], edges[i+1])``, the steady window is
    ``[edges[i] + transition_sec, edges[i+1])`` (no clipping). With 50s-long segments and
    ``transition_sec=10``, each steady window is 40s.

    Example: ``phase_edges_sec=(0, 50, 100, 150, 200)`` → four phases aligned to 50/100/150s boundaries.

    Returns
    -------
    list of (phase_index_1based, t_steady_start, t_steady_end)
    """
    edges = tuple(phase_edges_sec)
    if len(edges) < 2:
        return []
    win: list[tuple[int, float, float]] = []
    for i in range(len(edges) - 1):
        a, b = float(edges[i]), float(edges[i + 1])
        ts, te = a + transition_sec, b
        if te > ts:
            win.append((i + 1, ts, te))
    return win


def phase_steady_windows_from_tc_bw(
    tc_bw_path: Union[str, Path],
    pull_path: Union[str, Path],
    *,
    transition_sec: float = 10.0,
    experiment_end_sec: float = 200.0,
) -> pd.DataFrame:
    """
    Steady windows derived from **actual** ``tc_bw`` step timestamps (ISO) vs first pull ``[utility]``.

    Each row is one capacity segment after a step: from that step's ``t_pull`` until the next step
    (or ``experiment_end_sec``). Steady sub-window skips the first ``transition_sec`` seconds in
    each segment. ``bw_mbit`` is the cap in effect for that segment (value applied at the segment's
    step line).

    For a profile with steps at 0, 50, 100s and 200s end, you get three segments; add a design-time
    fourth phase (e.g. 150–200s) by using :func:`steady_windows_from_phase_edges` with
    ``(0, 50, 100, 150, 200)`` in pull time after you confirm alignment.
    """
    df = tc_bw_with_pull_t(tc_bw_path, pull_path)
    if df.empty:
        return pd.DataFrame(
            columns=[
                "phase",
                "at_sec",
                "bw_mbit",
                "dev",
                "t_step_pull",
                "t_steady_start",
                "t_steady_end",
            ]
        )
    df = df.sort_values("at_sec").reset_index(drop=True)
    rows: list[dict] = []
    n = len(df)
    for i in range(n):
        seg_start = float(df.loc[i, "t_pull"])
        if seg_start != seg_start:  # NaN
            continue
        seg_end = float(df.loc[i + 1, "t_pull"]) if i + 1 < n else float(experiment_end_sec)
        ts = seg_start + transition_sec
        te = seg_end
        if te <= ts:
            continue
        rows.append({
            "phase": i + 1,
            "at_sec": int(df.loc[i, "at_sec"]),
            "bw_mbit": float(df.loc[i, "bw_mbit"]),
            "dev": str(df.loc[i, "dev"]),
            "t_step_pull": seg_start,
            "t_steady_start": ts,
            "t_steady_end": te,
        })
    return pd.DataFrame(rows)


def route_a_four_steady_windows(
    tc_bw_path: Union[str, Path],
    pull_path: Union[str, Path],
    *,
    transition_sec: float = 10.0,
    experiment_end_sec: float = 200.0,
) -> pd.DataFrame:
    """
    Four design phases in **pull** time: tc-design 0-50, 50-100, 100-150, 150-200 (s from tc start).

    Uses observed ``t_pull`` for the steps at 0, 50, 100s. The 150s boundary is
    ``t100 + (t50 - t0)`` (one 50s wall-clock span). Steady = skip ``transition_sec`` after each
    edge; last segment ends at ``min(t150 + 50, experiment_end_sec)`` (i.e. up to 200s).

    Returns columns: ``phase, tc_design_lo, tc_design_hi, t_steady_start, t_steady_end``.
    """
    dfp = tc_bw_with_pull_t(tc_bw_path, pull_path)
    if dfp.empty:
        return pd.DataFrame(
            columns=[
                "phase",
                "tc_design_lo",
                "tc_design_hi",
                "t_steady_start",
                "t_steady_end",
            ]
        )
    need = {0, 50, 100}
    have = set(dfp["at_sec"].astype(int).unique().tolist())
    if not need <= have:
        return pd.DataFrame(
            columns=[
                "phase",
                "tc_design_lo",
                "tc_design_hi",
                "t_steady_start",
                "t_steady_end",
            ]
        )

    def _tp(sec: int) -> float:
        r = dfp[dfp["at_sec"] == sec]
        if r.empty:
            return float("nan")
        v = float(r.iloc[0]["t_pull"])
        return v

    t0, t50, t100 = _tp(0), _tp(50), _tp(100)
    if any(math.isnan(x) for x in (t0, t50, t100)):
        return pd.DataFrame(
            columns=[
                "phase",
                "tc_design_lo",
                "tc_design_hi",
                "t_steady_start",
                "t_steady_end",
            ]
        )
    span = t50 - t0
    t150 = t100 + span
    t200 = min(t100 + 2.0 * span, float(experiment_end_sec))
    edges = [(0, 50, t0, t50), (50, 100, t50, t100), (100, 150, t100, t150), (150, 200, t150, t200)]
    rows_out: list[dict] = []
    for i, (lo, hi, ea, eb) in enumerate(edges, start=1):
        ts, te = ea + transition_sec, eb
        if te > ts:
            rows_out.append({
                "phase": i,
                "tc_design_lo": float(lo),
                "tc_design_hi": float(hi),
                "t_steady_start": ts,
                "t_steady_end": te,
            })
    return pd.DataFrame(rows_out)


def load_tc_log(path: Union[str, Path]) -> pd.DataFrame:
    """
    Parse a tc_delay_*.log or tc_loss_*.log file.

    Returns DataFrame with columns: at_sec, mode, delay_ms, loss_pct, dev
    """
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _RE_TC_HEAD.search(line)
            if not m:
                continue
            dm = _RE_DELAY_MS.search(line)
            lm = _RE_LOSS_PCT.search(line)
            dev_m = _RE_TC_DEV.search(line)
            rows.append({
                "at_sec": int(m["at"]),
                "mode": m["mode"],
                "delay_ms": float(dm["delay_ms"]) if dm else float("nan"),
                "loss_pct": float(lm["loss_pct"]) if lm else float("nan"),
                "dev": dev_m["dev"] if dev_m else "",
            })
    return pd.DataFrame(rows)


def find_run_dirs(logs_root: Union[str, Path] = "logs_exp") -> list:
    """
    Scan logs_root for vm_run_* directories.

    Each entry contains:
      run_id, dir, pull_log, push_log, server_log,
      tc_delay_log (or None), tc_loss_log (or None),
      phase2_type: 'baseline' | 'delay' | 'loss' | 'unknown'
    """
    root = Path(logs_root)
    runs = []
    for d in sorted(root.glob("vm_run_*")):
        if not d.is_dir():
            continue
        run_id = d.name.replace("vm_run_", "")
        entry = {
            "run_id": run_id,
            "dir": d,
            "pull_log": next(d.glob("pull_*.log"), None),
            "push_log": next(d.glob("push_*.log"), None),
            "server_log": next(d.glob("server_*.log"), None),
            "tc_delay_log": next(d.glob("tc_delay_*.log"), None),
            "tc_loss_log": next(d.glob("tc_loss_*.log"), None),
            "tc_bw_log": next(d.glob("tc_bw_*.log"), None),
        }
        if entry["tc_bw_log"]:
            entry["phase2_type"] = "bw"
        elif entry["tc_delay_log"]:
            entry["phase2_type"] = "delay"
        elif entry["tc_loss_log"]:
            entry["phase2_type"] = "loss"
        elif entry["pull_log"]:
            entry["phase2_type"] = "baseline"
        else:
            entry["phase2_type"] = "unknown"
        runs.append(entry)
    return runs


def load_phase2_triple(
    baseline_dir: Union[str, Path],
    delay_dir: Union[str, Path],
    loss_dir: Union[str, Path],
) -> tuple:
    """
    Load all three Phase 2 runs and return combined DataFrames.

    Returns
    -------
    df_util : combined utility DataFrame with 'label' in {baseline, delay, loss}
    df_mon  : combined monitor DataFrame
    tc_steps: {'delay': DataFrame, 'loss': DataFrame}
    """
    dirs = {
        "baseline": Path(baseline_dir),
        "delay": Path(delay_dir),
        "loss": Path(loss_dir),
    }

    util_dfs, mon_dfs = [], []
    tc_steps = {}

    for label, d in dirs.items():
        pull = next(d.glob("pull_*.log"), None)
        if pull is None:
            raise FileNotFoundError(f"No pull_*.log in {d}")
        u, m = load_pull_log(pull, label=label)
        util_dfs.append(u)
        mon_dfs.append(m)

        tc_delay = next(d.glob("tc_delay_*.log"), None)
        tc_loss = next(d.glob("tc_loss_*.log"), None)
        if tc_delay:
            tc_steps["delay"] = load_tc_log(tc_delay)
        if tc_loss:
            tc_steps["loss"] = load_tc_log(tc_loss)

    return (
        pd.concat(util_dfs, ignore_index=True),
        pd.concat(mon_dfs, ignore_index=True),
        tc_steps,
    )


def load_labeled_vm_runs(
    label_to_dir: dict[str, Union[str, Path]],
    tc_from_label: Optional[str] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, pd.DataFrame]]:
    """
    Load several ``vm_run_*`` folders (e.g. utility modes T / D / L) for ACCeSS-style comparison.

    Parameters
    ----------
    label_to_dir :
        Maps experiment label to directory, e.g. ``{"T": Path(".../vm_run_t"), "D": ...}``.
        Each directory must contain ``pull_*.log``.
    tc_from_label :
        If set, use this key's directory to load ``tc_delay_*.log`` / ``tc_loss_*.log``.
        If ``None``, use the first key in iteration order (sorted by key for stability).

    Returns
    -------
    df_util, df_mon : concatenated DataFrames with column ``label`` set to each key.
    tc_steps : same shape as ``load_phase2_triple`` (``delay`` / ``loss`` keys if present).
    """
    if not label_to_dir:
        raise ValueError("label_to_dir is empty")

    util_dfs: list[pd.DataFrame] = []
    mon_dfs: list[pd.DataFrame] = []

    for lab in sorted(label_to_dir.keys()):
        d = Path(label_to_dir[lab])
        pull = next(d.glob("pull_*.log"), None)
        if pull is None:
            raise FileNotFoundError(f"No pull_*.log in {d} (label={lab})")
        u, m = load_pull_log(pull, label=lab)
        util_dfs.append(u)
        mon_dfs.append(m)

    tc_steps: dict[str, pd.DataFrame] = {}
    src_key = tc_from_label
    if src_key is not None and src_key not in label_to_dir:
        src_key = None
    if src_key is None:
        src_key = sorted(label_to_dir.keys())[0]
    src_dir = Path(label_to_dir[src_key])
    tc_delay = next(src_dir.glob("tc_delay_*.log"), None)
    tc_loss = next(src_dir.glob("tc_loss_*.log"), None)
    if tc_delay:
        tc_steps["delay"] = load_tc_log(tc_delay)
    if tc_loss:
        tc_steps["loss"] = load_tc_log(tc_loss)

    return (
        pd.concat(util_dfs, ignore_index=True),
        pd.concat(mon_dfs, ignore_index=True),
        tc_steps,
    )
