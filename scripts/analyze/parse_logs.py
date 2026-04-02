"""
parse_logs.py — parse pull_*.log from qcurl-4dmap-experiment.

Log lines handled:
  [utility]  path=X mode=T G=.. D=.. L=.. bw=..Mbps loss=.. owd=..ms
             gain=.. backoff=.. U=.. trend_ms=..
  [m]monitor path=X rtt_smoothed=.. rtt_min=.. bw=..B/s inflight=..
             cwnd_full=.. cwnd_room=.. loss=.. lost_B=..
  tc_delay / tc_loss   (written by tc_delay_steps.sh / tc_loss_steps.sh)

Usage:
    from parse_logs import load_pull_log, load_tc_log, find_run_dirs

    df_util, df_mon = load_pull_log("logs_exp/vm_run_XXX/pull_XXX.log", label="baseline")
    tc_steps       = load_tc_log("logs_exp/vm_run_XXX/tc_delay_XXX.log")
"""

from __future__ import annotations

import re
import glob
import os
from pathlib import Path
from typing import Union

import pandas as pd

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

_RE_TC_STEP = re.compile(
    r"\[tc_(?P<mode>delay|loss)\] step \d+/\d+ at=(?P<at>\d+)s"
    r"(?: delay=(?P<delay_ms>\d+)ms)?"
    r"(?: loss=(?P<loss_pct>[^\s]+)%)?"
    r" dev=(?P<dev>[^\s]+)"
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
        columns: t, path, rtt_smoothed_ms, rtt_min_ms, bw_bytes,
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


def load_tc_log(path: Union[str, Path]) -> pd.DataFrame:
    """
    Parse a tc_delay_*.log or tc_loss_*.log file.

    Returns DataFrame with columns: at_sec, mode, delay_ms, loss_pct, dev
    """
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _RE_TC_STEP.search(line)
            if m:
                rows.append({
                    "at_sec": int(m["at"]),
                    "mode": m["mode"],
                    "delay_ms": float(m["delay_ms"]) if m["delay_ms"] else float("nan"),
                    "loss_pct": float(m["loss_pct"]) if m["loss_pct"] else float("nan"),
                    "dev": m["dev"],
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
        }
        if entry["tc_delay_log"]:
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
