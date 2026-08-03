#!/usr/bin/env python3
"""
Analyze delay-only and loss-only Q-ACCeSS experiments.

Primary metrics differ from Fig.7 throughput eval:
  delay — QUIC Path-B latest RTT, smoothed RTT, configured tc delay evidence, path-B usage shift, recovery time
  loss  — loss rate, retrans/lost-byte proxy, path-B usage shift, recovery time

Throughput (total / path A / path B) is computed from every captured frame.
The evaluator is read-only: worker execution mode is recorded by the experiment runner.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "scripts" / "analyze") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

EVAL_WINDOWS = [
    ("0-50", 0.0, 50.0),
    ("50-90", 50.0, 90.0),
    ("90-110", 90.0, 110.0),
    ("110-150", 110.0, 150.0),
    ("150-200", 150.0, 200.0),
]

PRESETS: dict[str, dict[str, object]] = {
    "delay": {
        "title": "Delay-only (primary: Path B active-probe RTT/recovery)",
        "baseline_dir": "delay_baseline",
        "dynamic_dirs": ("delay_qaccess_d_dynamic", "delay_qaccess_dynamic"),
        "out_subdir": "delay_only_compare",
        "file_prefix": "delay",
        "recovery_start_s": 110.0,
    },
    "loss": {
        "title": "Loss-only (primary: loss/retrans/recovery)",
        "baseline_dir": "loss_baseline",
        "dynamic_dirs": ("loss_qaccess_l_dynamic", "loss_qaccess_dynamic"),
        "out_subdir": "loss_only_compare",
        "file_prefix": "loss",
        "recovery_start_s": 100.0,
    },
}


def _p95(s: pd.Series) -> float:
    if s.empty or s.isna().all():
        return float("nan")
    return float(s.quantile(0.95))


def _pcap_bytes_by_second(pcap: Path, global_t0: float) -> dict[int, int]:
    cmd = [
        "tshark", "-n", "-r", "-",
        "-T", "fields", "-E", "separator=,",
        "-e", "frame.time_epoch", "-e", "frame.len",
    ]
    bins: dict[int, int] = {}
    with pcap.open("rb") as capture:
        proc = subprocess.Popen(
            cmd,
            stdin=capture,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if not line.strip():
                continue
            parts = line.split(",", 1)
            if len(parts) < 2:
                continue
            try:
                t, n = float(parts[0]), int(float(parts[1]))
            except ValueError:
                continue
            si = int(math.floor(t - global_t0))
            if si >= 0:
                bins[si] = bins.get(si, 0) + n
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        returncode = proc.wait()
    if returncode != 0:
        raise RuntimeError(
            f"tshark failed for {pcap} (exit={returncode}): {stderr.strip()[:1000]}"
        )
    return bins


def _first_pcap_epoch(pcap: Path) -> float | None:
    with pcap.open("rb") as capture:
        proc = subprocess.run(
            [
                "tshark", "-n", "-r", "-", "-c", "1",
                "-T", "fields", "-e", "frame.time_epoch",
            ],
            stdin=capture,
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"tshark failed to read first packet from {pcap} "
            f"(exit={proc.returncode}): {proc.stderr.strip()[:1000]}"
        )
    for line in proc.stdout.splitlines():
        if line.strip():
            return float(line.strip())
    return None


def load_wire_timeseries(run_dir: Path) -> pd.DataFrame:
    pcap_dir = run_dir / "pcaps"
    pcaps = sorted(pcap_dir.glob("pathA_*.pcap")) + sorted(pcap_dir.glob("pathB_*.pcap"))
    if not pcaps:
        return pd.DataFrame()

    epochs = [epoch for p in pcaps if (epoch := _first_pcap_epoch(p)) is not None]
    if not epochs:
        return pd.DataFrame()

    global_t0 = min(epochs)
    path_bins: dict[str, dict[int, int]] = {}
    for p in pcaps:
        label = "path_a" if "pathA" in p.name else "path_b"
        path_bins[label] = _pcap_bytes_by_second(p, global_t0)

    last_s = max((max(b) for b in path_bins.values() if b), default=0)
    rows = []
    for s in range(last_s + 1):
        ba = path_bins.get("path_a", {}).get(s, 0)
        bb = path_bins.get("path_b", {}).get(s, 0)
        total = ba + bb
        rows.append({
            "time_s": float(s),
            "path_a_quic_wire_mbps": ba * 8 / 1e6,
            "path_b_quic_wire_mbps": bb * 8 / 1e6,
            "total_quic_wire_mbps": total * 8 / 1e6,
            "path_b_share_pct": (bb / total * 100.0) if total > 0 else float("nan"),
        })
    return pd.DataFrame(rows)


def _metric_log_candidates(run_dir: Path) -> list[Path]:
    logs = run_dir / "logs"
    patterns = ("pull_*.log", "server_*.log", "combined_*.log")
    hits: list[Path] = []
    for pattern in patterns:
        hits.extend(sorted(logs.glob(pattern)) if logs.is_dir() else [])
    if not hits:
        for pattern in patterns:
            hits.extend(sorted(run_dir.glob(f"**/{pattern}")))
    return list(dict.fromkeys(hits))


def load_pull_frames(run_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidates = _metric_log_candidates(run_dir)
    if not candidates:
        return pd.DataFrame(), pd.DataFrame()
    try:
        from parse_logs import load_pull_log  # type: ignore
    except ImportError:
        return pd.DataFrame(), pd.DataFrame()
    best_util, best_mon = pd.DataFrame(), pd.DataFrame()
    best_score = -1
    for candidate in candidates:
        df_util, df_mon = load_pull_log(candidate, label=run_dir.name)
        util = df_util if df_util is not None else pd.DataFrame()
        mon = df_mon if df_mon is not None else pd.DataFrame()
        score = len(util) + len(mon)
        if score > best_score:
            best_util, best_mon, best_score = util, mon, score
    return best_util, best_mon


def load_runtime_samples(run_dir: Path) -> pd.DataFrame:
    for p in (
        run_dir / "qaccess_runtime_samples.csv",
        run_dir / "qaccess_runtime_samples_full.csv.gz",
        run_dir / "derived_snapshots" / "qaccess_runtime_samples.csv",
        _REPO / "derived" / "qaccess_runtime_samples.csv",
    ):
        if p.is_file() and p.stat().st_size > 0:
            df = pd.read_csv(p)
            if "timestamp_ms" in df.columns and not df.empty:
                t0 = float(df["timestamp_ms"].min())
                df = df.copy()
                df["time_s"] = (df["timestamp_ms"] - t0) / 1000.0
            return df
    return pd.DataFrame()


def _path_b_rows(df: pd.DataFrame, path_col: str = "path") -> pd.DataFrame:
    if df.empty or path_col not in df.columns:
        return df

    for label_col in ("physical_path_label", "physical_path"):
        if label_col in df.columns:
            labels = df[label_col].astype(str).str.strip().str.lower()
            pb = df[labels.isin(("path b", "path_b", "b"))]
            if not pb.empty:
                return pb

    for endpoint_col in ("remote_endpoint", "endpoint"):
        if endpoint_col in df.columns:
            endpoints = df[endpoint_col].fillna("").astype(str)
            pb = df[endpoints.str.contains(r"10\.0\.2\.", regex=True)]
            if not pb.empty:
                return pb

    numeric_ids = pd.to_numeric(df[path_col], errors="coerce")
    # Pull utility logs use path=2 for Path B; qserver runtime samples use path_id=3.
    preferred_ids = (3, 2) if path_col == "path_id" else (2, 3)
    for path_id in preferred_ids:
        pb = df[numeric_ids == path_id]
        if not pb.empty:
            return pb

    unique_ids = sorted(numeric_ids.dropna().unique())
    if len(unique_ids) >= 2:
        return df[numeric_ids == unique_ids[-1]]
    return df.iloc[0:0]


def _window_mask(df: pd.DataFrame, lo: float, hi: float) -> pd.DataFrame:
    if df.empty or "time_s" not in df.columns and "t" not in df.columns:
        return df.iloc[0:0]
    tcol = "time_s" if "time_s" in df.columns else "t"
    return df[(df[tcol] >= lo) & (df[tcol] < hi)].copy()


def _recovery_time_s(
    series: pd.DataFrame,
    *,
    tcol: str,
    vcol: str,
    ref_lo: float,
    ref_hi: float,
    recover_after: float = 100.0,
    tol_frac: float = 0.15,
    stable_sec: int = 10,
) -> float:
    if series.empty or vcol not in series.columns:
        return float("nan")
    ref = _window_mask(series.rename(columns={tcol: "time_s"}), ref_lo, ref_hi)
    if ref.empty:
        return float("nan")
    target = float(ref[vcol].mean())
    if not math.isfinite(target) or target <= 0:
        return float("nan")
    post = series[series[tcol] >= recover_after].sort_values(tcol)
    if post.empty:
        return float("nan")
    thresh = target * (1.0 + tol_frac)
    streak = 0
    for _, row in post.iterrows():
        val = float(row[vcol])
        if math.isfinite(val) and val <= thresh:
            streak += 1
            if streak >= stable_sec:
                return float(row[tcol]) - recover_after
        else:
            streak = 0
    return float("nan")


def _per_second_delay(util: pd.DataFrame, mon: pd.DataFrame, method: str) -> pd.DataFrame:
    columns = [
        "method", "time_s", "owd_ms_mean", "owd_ms_p95",
        "rtt_ms_mean", "rtt_ms_p95", "rtt_latest_ms_mean", "rtt_latest_ms_p95", "jitter_ms_mean",
    ]
    pieces: list[pd.DataFrame] = []
    if not util.empty and "owd_ms" in util.columns:
        u = util.rename(columns={"t": "time_s"}) if "t" in util.columns else util.copy()
        u = _path_b_rows(u)
        if not u.empty:
            u = u.copy()
            u["time_s"] = pd.to_numeric(u["time_s"], errors="coerce").floordiv(1)
            pieces.append(
                u.groupby("time_s", as_index=False)["owd_ms"]
                .agg(owd_ms_mean="mean", owd_ms_p95=lambda s: s.quantile(0.95))
            )
    if not mon.empty and ("rtt_smoothed_ms" in mon.columns or "rtt_latest_ms" in mon.columns):
        m = mon.rename(columns={"t": "time_s"}) if "t" in mon.columns else mon.copy()
        m = _path_b_rows(m)
        if not m.empty:
            m = m.copy()
            m["time_s"] = pd.to_numeric(m["time_s"], errors="coerce").floordiv(1)
            agg: dict[str, object] = {}
            if "rtt_smoothed_ms" in m.columns:
                agg["rtt_ms_mean"] = ("rtt_smoothed_ms", "mean")
                agg["rtt_ms_p95"] = ("rtt_smoothed_ms", lambda s: s.quantile(0.95))
            if "rtt_latest_ms" in m.columns:
                agg["rtt_latest_ms_mean"] = ("rtt_latest_ms", "mean")
                agg["rtt_latest_ms_p95"] = ("rtt_latest_ms", lambda s: s.quantile(0.95))
            if "rtt_mean_dev_ms" in m.columns:
                agg["jitter_ms_mean"] = ("rtt_mean_dev_ms", "mean")
            pieces.append(m.groupby("time_s", as_index=False).agg(**agg))

    if not pieces:
        return pd.DataFrame(columns=columns)
    result = pieces[0]
    for piece in pieces[1:]:
        result = result.merge(piece, on="time_s", how="outer")
    result.insert(0, "method", method)
    for column in columns:
        if column not in result.columns:
            result[column] = float("nan")
    return result[columns].sort_values("time_s").reset_index(drop=True)


_RE_QDISC_TS = re.compile(r"^([0-9]+(?:\.[0-9]+)?)$")
_RE_QDISC_NETEM = re.compile(r"^qdisc netem\s+")
_RE_QDISC_SENT = re.compile(
    r"^Sent\s+(?P<bytes>\d+)\s+bytes\s+(?P<pkts>\d+)\s+pkt\s+"
    r"\(dropped\s+(?P<dropped>\d+),\s+overlimits\s+(?P<overlimits>\d+),"
)
_RE_QDISC_BACKLOG = re.compile(r"^backlog\s+(?P<bytes>[^\s]+)\s+(?P<pkts>\d+)p")


def _tc_duration_to_ms(token: str) -> float:
    token = str(token or "").strip()
    try:
        if token.endswith("ms"):
            return float(token[:-2])
        if token.endswith("us") or token.endswith("µs"):
            return float(token[:-2]) / 1000.0
        if token.endswith("s"):
            return float(token[:-1]) * 1000.0
        return float(token)
    except ValueError:
        return float("nan")


def _parse_tc_qdisc_log(path: Path, method: str) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    epoch0: float | None = None
    current_epoch: float | None = None
    in_netem = False
    current_iface = ""
    current_kind = ""
    netem_handle = ""
    netem_parent = ""
    configured_loss_pct = 0.0
    configured_delay_ms = float("nan")
    configured_jitter_ms = float("nan")

    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            ts_match = _RE_QDISC_TS.match(line)
            if ts_match:
                current_epoch = float(ts_match.group(1))
                if epoch0 is None:
                    epoch0 = current_epoch
                in_netem = False
                configured_loss_pct = 0.0
                configured_delay_ms = float("nan")
                configured_jitter_ms = float("nan")
                netem_handle = ""
                netem_parent = ""
                continue

            if line.startswith("iface="):
                meta = dict(part.split("=", 1) for part in line.split() if "=" in part)
                current_iface = meta.get("iface", current_iface)
                current_kind = meta.get("profile_kind", current_kind)
                continue

            netem_match = _RE_QDISC_NETEM.match(line)
            if netem_match:
                in_netem = True
                handle_match = re.search(r"^qdisc\s+netem\s+([^\s]+)", line)
                parent_match = re.search(r"\bparent\s+([^\s]+)", line)
                loss_match = re.search(r"loss\s+([0-9.]+)%", line)
                delay_match = re.search(r"delay\s+([^\s]+)(?:\s+([^\s]+))?", line)
                netem_handle = handle_match.group(1) if handle_match else ""
                netem_parent = parent_match.group(1) if parent_match else ""
                configured_loss_pct = float(loss_match.group(1)) if loss_match else 0.0
                configured_delay_ms = _tc_duration_to_ms(delay_match.group(1)) if delay_match else float("nan")
                configured_jitter_ms = float("nan")
                if delay_match and delay_match.group(2) and re.search(r"(ms|us|µs|s)$", delay_match.group(2)):
                    configured_jitter_ms = _tc_duration_to_ms(delay_match.group(2))
                continue

            if line.startswith("qdisc "):
                in_netem = False
                continue

            sent_match = _RE_QDISC_SENT.match(line)
            if in_netem and sent_match and current_epoch is not None and epoch0 is not None:
                rows.append({
                    "method": method,
                    "time_s": current_epoch - epoch0,
                    "tc_interface": current_iface,
                    "tc_profile_kind": current_kind,
                    "tc_qdisc_kind": "netem",
                    "tc_handle": netem_handle,
                    "tc_parent": netem_parent,
                    "tc_configured_delay_ms": configured_delay_ms,
                    "tc_configured_jitter_ms": configured_jitter_ms,
                    "tc_configured_loss_pct": configured_loss_pct,
                    "tc_sent_pkts": int(sent_match.group("pkts")),
                    "tc_dropped_pkts": int(sent_match.group("dropped")),
                    "tc_overlimits": int(sent_match.group("overlimits")),
                    "tc_backlog_bytes_raw": "",
                    "tc_backlog_pkts": float("nan"),
                })
                continue

            backlog_match = _RE_QDISC_BACKLOG.match(line)
            if in_netem and backlog_match and rows:
                rows[-1]["tc_backlog_bytes_raw"] = backlog_match.group("bytes")
                rows[-1]["tc_backlog_pkts"] = int(backlog_match.group("pkts"))
                in_netem = False

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values("time_s").reset_index(drop=True)
    df["tc_sent_pkts_delta"] = pd.to_numeric(df["tc_sent_pkts"], errors="coerce").diff().fillna(0)
    df["tc_dropped_delta"] = pd.to_numeric(df["tc_dropped_pkts"], errors="coerce").diff().fillna(0)
    df["tc_overlimits_delta"] = pd.to_numeric(df["tc_overlimits"], errors="coerce").diff().fillna(0)
    for column in ("tc_sent_pkts_delta", "tc_dropped_delta", "tc_overlimits_delta"):
        df.loc[df[column] < 0, column] = 0
    denom = df["tc_sent_pkts_delta"] + df["tc_dropped_delta"]
    df["tc_loss_rate"] = df["tc_dropped_delta"] / denom.where(denom > 0)
    return df


def load_tc_qdisc_timeseries(run_dir: Path, method: str) -> pd.DataFrame:
    pieces = [
        _parse_tc_qdisc_log(path, method)
        for path in sorted((run_dir / "logs").glob("tc_qdisc_stats_pathB_*.log"))
    ]
    pieces = [piece for piece in pieces if not piece.empty]
    if not pieces:
        return pd.DataFrame(columns=[
            "method", "time_s", "tc_interface", "tc_profile_kind", "tc_qdisc_kind",
            "tc_handle", "tc_parent", "tc_configured_delay_ms", "tc_configured_jitter_ms",
            "tc_configured_loss_pct", "tc_sent_pkts", "tc_dropped_pkts", "tc_overlimits",
            "tc_backlog_bytes_raw", "tc_backlog_pkts", "tc_sent_pkts_delta",
            "tc_dropped_delta", "tc_overlimits_delta", "tc_loss_rate",
        ])
    return pd.concat(pieces, ignore_index=True).sort_values(["method", "time_s"]).reset_index(drop=True)


_RE_PING_TS = re.compile(r"^(?P<ts>\d+\.\d+)$")
_RE_PING_RTT = re.compile(r"\btime=(?P<rtt>[0-9.]+)\s*ms\b")


def _parse_path_b_ping_log(path: Path, method: str) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    epoch0: float | None = None
    current: dict[str, object] | None = None

    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            ts_match = _RE_PING_TS.match(line)
            if ts_match:
                epoch = float(ts_match.group("ts"))
                if epoch0 is None:
                    epoch0 = epoch
                current = {
                    "method": method,
                    "time_s": epoch - epoch0,
                    "ping_src_iface": "",
                    "ping_src_ip": "",
                    "ping_dst_ip": "",
                    "active_probe_rtt_ms": float("nan"),
                    "active_probe_success": False,
                }
                rows.append(current)
                continue

            if current is None:
                continue

            if line.startswith("src_iface="):
                meta = dict(part.split("=", 1) for part in line.split() if "=" in part)
                current["ping_src_iface"] = meta.get("src_iface", "")
                current["ping_src_ip"] = meta.get("src_ip", "")
                current["ping_dst_ip"] = meta.get("dst_ip", "")
                continue

            rtt_match = _RE_PING_RTT.search(line)
            if rtt_match:
                current["active_probe_rtt_ms"] = float(rtt_match.group("rtt"))
                current["active_probe_success"] = True

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("time_s").reset_index(drop=True)


def load_path_b_ping_timeseries(run_dir: Path, method: str) -> pd.DataFrame:
    pieces = [
        _parse_path_b_ping_log(path, method)
        for path in sorted((run_dir / "logs").glob("ping_rtt_pathB_*.log"))
    ]
    pieces = [piece for piece in pieces if not piece.empty]
    if not pieces:
        return pd.DataFrame(columns=[
            "method", "time_s", "ping_src_iface", "ping_src_ip", "ping_dst_ip",
            "active_probe_rtt_ms", "active_probe_success",
        ])
    return pd.concat(pieces, ignore_index=True).sort_values(["method", "time_s"]).reset_index(drop=True)


_RE_RETRANS_BYTES = re.compile(
    r"(?P<date>\d{4}/\d{2}/\d{2}) (?P<time>\d{2}:\d{2}:\d{2}).*?\[m\]retransBytes:(?P<bytes>[^\s]+)"
)


def _hms_to_seconds(time_str: str) -> int:
    h, m, sec = map(int, time_str.split(":"))
    return h * 3600 + m * 60 + sec


def _bytes_value(value: str) -> float:
    value = value.rstrip("/s")
    if value.endswith("B"):
        value = value[:-1]
    try:
        return float(value)
    except ValueError:
        return float("nan")


def _parse_retrans_log(path: Path, method: str) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    t0: int | None = None
    last_path: int | None = None
    monitor_head = re.compile(
        r"(?P<date>\d{4}/\d{2}/\d{2}) (?P<time>\d{2}:\d{2}:\d{2}).*?\[m\]monitor path=(?P<path>\d+)"
    )
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            monitor_match = monitor_head.search(line)
            if monitor_match:
                ts = _hms_to_seconds(monitor_match["time"])
                if t0 is None:
                    t0 = ts
                last_path = int(monitor_match["path"])
                continue

            retrans_match = _RE_RETRANS_BYTES.search(line)
            if retrans_match and last_path is not None:
                ts = _hms_to_seconds(retrans_match["time"])
                if t0 is None:
                    t0 = ts
                rows.append({
                    "method": method,
                    "time_s": float(ts - t0),
                    "path": last_path,
                    "retrans_bytes_total": _bytes_value(retrans_match["bytes"]),
                })
    return pd.DataFrame(rows)


def _per_second_retrans_delta(run_dir: Path, method: str) -> pd.DataFrame:
    columns = ["method", "time_s", "path_b_retrans_bytes_delta_sum", "n_samples"]
    best = pd.DataFrame()
    best_score = -1
    for candidate in _metric_log_candidates(run_dir):
        parsed = _parse_retrans_log(candidate, method)
        score = len(parsed)
        if score > best_score:
            best = parsed
            best_score = score
    if best.empty:
        return pd.DataFrame(columns=columns)

    path_b = _path_b_rows(best)
    if path_b.empty:
        return pd.DataFrame(columns=columns)

    path_b = path_b.copy().sort_values("time_s")
    path_b["time_s"] = pd.to_numeric(path_b["time_s"], errors="coerce").floordiv(1)
    path_b["retrans_bytes_total"] = pd.to_numeric(path_b["retrans_bytes_total"], errors="coerce")
    path_b = path_b.dropna(subset=["time_s", "retrans_bytes_total"])
    if path_b.empty:
        return pd.DataFrame(columns=columns)

    # retransBytes is cumulative in the raw log. Convert it to positive per-sample deltas,
    # then sum deltas into one-second bins.
    path_b["retrans_delta"] = path_b["retrans_bytes_total"].diff()
    path_b.loc[path_b["retrans_delta"] < 0, "retrans_delta"] = 0
    path_b["retrans_delta"] = path_b["retrans_delta"].fillna(0)
    result = (
        path_b.groupby("time_s", as_index=False)["retrans_delta"]
        .agg(path_b_retrans_bytes_delta_sum="sum", n_samples="count")
        .sort_values("time_s")
        .reset_index(drop=True)
    )
    result.insert(0, "method", method)
    return result[columns]


def _per_second_loss_monitor(mon: pd.DataFrame, method: str) -> pd.DataFrame:
    columns = [
        "method", "time_s", "path_b_monitor_loss_mean",
        "path_b_monitor_loss_p95", "n_samples",
    ]
    if mon.empty or "loss" not in mon.columns:
        return pd.DataFrame(columns=columns)

    m = mon.rename(columns={"t": "time_s"}) if "t" in mon.columns else mon.copy()
    m = _path_b_rows(m)
    if m.empty or "time_s" not in m.columns:
        return pd.DataFrame(columns=columns)

    m = m.copy()
    m["time_s"] = pd.to_numeric(m["time_s"], errors="coerce").floordiv(1)
    m["loss"] = pd.to_numeric(m["loss"], errors="coerce")
    m = m.dropna(subset=["time_s", "loss"])
    if m.empty:
        return pd.DataFrame(columns=columns)

    result = (
        m.groupby("time_s", as_index=False)["loss"]
        .agg(
            path_b_monitor_loss_mean="mean",
            path_b_monitor_loss_p95=lambda series: series.quantile(0.95),
            n_samples="count",
        )
        .sort_values("time_s")
        .reset_index(drop=True)
    )
    result.insert(0, "method", method)
    return result[columns]


def _pct_change(baseline: float, dynamic: float, higher_is_better: bool) -> float:
    if not math.isfinite(baseline) or not math.isfinite(dynamic) or baseline == 0:
        return float("nan")
    delta = dynamic - baseline
    return float((delta if higher_is_better else -delta) / abs(baseline) * 100.0)


def build_improvement_table(df_win: pd.DataFrame, dynamic_method: str) -> pd.DataFrame:
    metric_directions = {
        "owd_ms_mean": False,
        "owd_ms_p95": False,
        "rtt_ms_mean": False,
        "rtt_ms_p95": False,
        "rtt_latest_ms_mean": False,
        "rtt_latest_ms_p95": False,
        "jitter_ms_mean": False,
        "secondary_total_quic_wire_mbps_mean": True,
        "secondary_path_a_quic_wire_mbps_mean": True,
        "secondary_path_b_quic_wire_mbps_mean": True,
    }
    rows: list[dict[str, object]] = []
    for window in df_win["window"].drop_duplicates():
        baseline_rows = df_win[(df_win["method"] == "baseline") & (df_win["window"] == window)]
        dynamic_rows = df_win[(df_win["method"] == dynamic_method) & (df_win["window"] == window)]
        if baseline_rows.empty or dynamic_rows.empty:
            continue
        baseline_row, dynamic_row = baseline_rows.iloc[0], dynamic_rows.iloc[0]
        for metric, higher_is_better in metric_directions.items():
            if metric not in df_win.columns:
                continue
            baseline = float(baseline_row[metric])
            dynamic = float(dynamic_row[metric])
            rows.append({
                "window": window,
                "metric": metric,
                "baseline": baseline,
                "qaccess": dynamic,
                "improvement_pct": _pct_change(baseline, dynamic, higher_is_better),
                "better_when": "higher" if higher_is_better else "lower",
            })
        if "path_b_share_pct_mean" in df_win.columns:
            baseline = float(baseline_row["path_b_share_pct_mean"])
            dynamic = float(dynamic_row["path_b_share_pct_mean"])
            rows.append({
                "window": window,
                "metric": "path_b_share_pct_mean",
                "baseline": baseline,
                "qaccess": dynamic,
                "improvement_pct": float("nan"),
                "change_percentage_points": dynamic - baseline,
                "better_when": "context-dependent",
            })
    return pd.DataFrame(rows)


def _loss_span_from_tc_logs(session: Path) -> tuple[float, float] | None:
    steps: list[tuple[float, float]] = []
    pattern = re.compile(r"profile_step\[\d+\] at=([0-9.]+)s loss=([0-9.]+)%")
    for log_path in sorted(session.glob("*/logs/tc_loss_*.log")):
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = pattern.search(line)
            if match:
                steps.append((float(match.group(1)), float(match.group(2))))
        if steps:
            break
    if len(steps) < 2:
        return None
    steps = sorted(steps)
    for idx, (start, loss_pct) in enumerate(steps):
        if loss_pct <= 0:
            continue
        end = steps[idx + 1][0] if idx + 1 < len(steps) else start
        if end > start:
            return start, end
    return None


def _delay_span_from_tc_logs(session: Path) -> tuple[float, float] | None:
    steps: list[tuple[float, float]] = []
    pattern = re.compile(r"profile_step\[\d+\] at=([0-9.]+)s delay=([0-9.]+)ms")
    for log_path in sorted(session.glob("*/logs/tc_delay_*.log")):
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = pattern.search(line)
            if match:
                steps.append((float(match.group(1)), float(match.group(2))))
        if steps:
            break
    if len(steps) < 2:
        return None
    steps = sorted(steps)
    initial_delay = steps[0][1]
    for idx, (start, delay_ms) in enumerate(steps[1:], start=1):
        if delay_ms == initial_delay:
            continue
        end = steps[idx + 1][0] if idx + 1 < len(steps) else start
        if end > start:
            return start, end
    return None


def _plot_timeseries(
    throughput: pd.DataFrame,
    delay: pd.DataFrame,
    active_probe_rtt: pd.DataFrame,
    loss_monitor: pd.DataFrame,
    loss_retrans: pd.DataFrame,
    loss_tc: pd.DataFrame,
    out: Path,
    prefix: str,
    impairment_span: tuple[float, float] | None = None,
) -> None:
    if throughput.empty and delay.empty and active_probe_rtt.empty and loss_monitor.empty and loss_retrans.empty and loss_tc.empty:
        return

    method_labels = {"baseline": "Baseline", "loss_qaccess_l": "Q-Access-L", "delay_qaccess_d": "Q-Access-D"}
    line_styles = {
        "baseline_total": {"color": "#1f77b4", "label": "Baseline total"},
        "loss_qaccess_l_total": {"color": "#ff7f0e", "label": "Q-Access-L total"},
        "delay_qaccess_d_total": {"color": "#ff7f0e", "label": "Q-Access-D total"},
        "baseline_path_a": {"color": "#2ca02c", "label": "Baseline Path A"},
        "baseline_path_b": {"color": "#d62728", "label": "Baseline Path B", "linestyle": "--"},
        "loss_qaccess_l_path_a": {"color": "#9467bd", "label": "Q-Access-L Path A"},
        "loss_qaccess_l_path_b": {"color": "#8c564b", "label": "Q-Access-L Path B", "linestyle": "--"},
        "delay_qaccess_d_path_a": {"color": "#9467bd", "label": "Q-Access-D Path A"},
        "delay_qaccess_d_path_b": {"color": "#8c564b", "label": "Q-Access-D Path B", "linestyle": "--"},
    }

    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    for method, group in throughput.groupby("method"):
        style = line_styles.get(f"{method}_total", {"label": method})
        axes[0].plot(group["time_s"], group["total_quic_wire_mbps"], linewidth=1.5, **style)
    axes[0].set_ylabel("Throughput (Mbps)")
    axes[0].set_title("Total throughput from all captured frames")
    axes[0].grid(alpha=0.25)
    axes[0].legend()

    for method, group in throughput.groupby("method"):
        style_a = line_styles.get(f"{method}_path_a", {"label": f"{method_labels.get(method, method)} Path A"})
        style_b = line_styles.get(f"{method}_path_b", {"label": f"{method_labels.get(method, method)} Path B", "linestyle": "--"})
        axes[1].plot(group["time_s"], group["path_a_quic_wire_mbps"], linewidth=1.1, **style_a)
        axes[1].plot(group["time_s"], group["path_b_quic_wire_mbps"], linewidth=1.1, **style_b)
    axes[1].set_ylabel("Per-path (Mbps)")
    axes[1].grid(alpha=0.25)
    axes[1].legend(ncol=2, fontsize=8)

    if prefix == "loss":
        if not loss_tc.empty and "tc_dropped_delta" in loss_tc.columns and loss_tc["tc_dropped_delta"].notna().any():
            for method, group in loss_tc.groupby("method"):
                style = line_styles.get(f"{method}_total", {"label": method})
                group = group.sort_values("time_s").copy()
                group["tc_dropped_cumulative"] = group["tc_dropped_delta"].fillna(0).cumsum()
                axes[2].plot(
                    group["time_s"], group["tc_dropped_cumulative"],
                    label=f"{method_labels.get(method, method)} Path B cumulative dropped packets",
                    linewidth=1.5, color=style.get("color"),
                )
            axes[2].set_ylabel("Path B dropped packets")
            axes[2].set_title("Path B cumulative tc-dropped packets over time")
            axes[2].legend()
        elif not loss_monitor.empty and loss_monitor["path_b_monitor_loss_mean"].notna().any():
            for method, group in loss_monitor.groupby("method"):
                style = line_styles.get(f"{method}_total", {"label": method})
                axes[2].plot(
                    group["time_s"], group["path_b_monitor_loss_mean"],
                    label=f"{method_labels.get(method, method)} Path B monitor loss",
                    linewidth=1.5, color=style.get("color"),
                )
            axes[2].set_ylabel("Path B monitor loss")
            axes[2].legend()
        else:
            axes[2].text(0.5, 0.5, "No Path B tc/monitor loss samples found",
                         ha="center", va="center", transform=axes[2].transAxes)
            axes[2].set_ylabel("Path B loss")
    elif not active_probe_rtt.empty and active_probe_rtt["active_probe_rtt_ms"].notna().any():
        for method, group in active_probe_rtt.groupby("method"):
            style = line_styles.get(f"{method}_total", {"label": method})
            axes[2].plot(
                group["time_s"], group["active_probe_rtt_ms"],
                label=f"{method_labels.get(method, method)} Path B active-probe RTT", linewidth=1.5,
                color=style.get("color"),
            )
        axes[2].set_ylabel("Path B active-probe RTT (ms)")
        axes[2].set_title("Path B active-probe RTT over time")
        axes[2].legend()
    elif not delay.empty and delay["rtt_latest_ms_mean"].notna().any():
        for method, group in delay.groupby("method"):
            style = line_styles.get(f"{method}_total", {"label": method})
            axes[2].plot(
                group["time_s"], group["rtt_latest_ms_mean"],
                label=f"{method_labels.get(method, method)} Path B latest RTT", linewidth=1.5,
                color=style.get("color"),
            )
        axes[2].set_ylabel("Path B latest RTT (ms)")
        axes[2].set_title("Path B QUIC latest RTT over time")
        axes[2].legend()
    elif not delay.empty and delay["rtt_ms_mean"].notna().any():
        for method, group in delay.groupby("method"):
            style = line_styles.get(f"{method}_total", {"label": method})
            axes[2].plot(
                group["time_s"], group["rtt_ms_mean"],
                label=f"{method_labels.get(method, method)} Path B smoothed RTT", linewidth=1.5,
                color=style.get("color"),
            )
        axes[2].set_ylabel("Path B smoothed RTT (ms)")
        axes[2].legend()
    else:
        axes[2].text(0.5, 0.5, "No OWD/RTT samples found", ha="center", va="center",
                     transform=axes[2].transAxes)
        axes[2].set_ylabel("Path B delay proxy (ms)")
    axes[2].set_xlabel("Time (s)")
    axes[2].grid(alpha=0.25)
    span = impairment_span or ((90.0, 110.0) if prefix == "delay" else (90.0, 100.0))
    for axis in axes:
        axis.axvspan(span[0], span[1], color="tab:red", alpha=0.08)
    fig.tight_layout()
    if prefix == "loss" and not loss_tc.empty and "tc_dropped_delta" in loss_tc.columns and loss_tc["tc_dropped_delta"].notna().any():
        plot_name = f"{prefix}_throughput_tc_dropped_packets_cumulative_over_time.png"
    elif prefix == "delay" and not active_probe_rtt.empty and active_probe_rtt["active_probe_rtt_ms"].notna().any():
        plot_name = f"{prefix}_throughput_active_probe_rtt_over_time.png"
    elif prefix == "delay":
        plot_name = f"{prefix}_throughput_quic_rtt_diagnostic_over_time.png"
    else:
        plot_name = f"{prefix}_throughput_loss_over_time.png"
    fig.savefig(out / plot_name, dpi=180)

    if prefix == "delay" and not active_probe_rtt.empty and not delay.empty and delay["rtt_latest_ms_mean"].notna().any():
        axes[2].clear()
        for method, group in delay.groupby("method"):
            style = line_styles.get(f"{method}_total", {"label": method})
            axes[2].plot(
                group["time_s"], group["rtt_latest_ms_mean"],
                label=f"{method_labels.get(method, method)} Path B QUIC latest RTT",
                linewidth=1.5, color=style.get("color"),
            )
        axes[2].set_ylabel("Path B QUIC latest RTT (ms)")
        axes[2].set_xlabel("Time (s)")
        axes[2].set_title("Path B QUIC latest RTT diagnostic")
        axes[2].grid(alpha=0.25)
        axes[2].legend()
        axes[2].axvspan(span[0], span[1], color="tab:red", alpha=0.08)
        fig.tight_layout()
        fig.savefig(out / f"{prefix}_throughput_quic_rtt_diagnostic_over_time.png", dpi=180)

    if prefix == "loss" and not loss_monitor.empty and loss_monitor["path_b_monitor_loss_mean"].notna().any():
        axes[2].clear()
        for method, group in loss_monitor.groupby("method"):
            style = line_styles.get(f"{method}_total", {"label": method})
            axes[2].plot(
                group["time_s"], group["path_b_monitor_loss_mean"],
                label=f"{method_labels.get(method, method)} Path B monitor loss",
                linewidth=1.5, color=style.get("color"),
            )
        axes[2].set_ylabel("Path B monitor loss")
        axes[2].set_xlabel("Time (s)")
        axes[2].grid(alpha=0.25)
        axes[2].legend()
        axes[2].axvspan(span[0], span[1], color="tab:red", alpha=0.08)
        fig.tight_layout()
        fig.savefig(out / f"{prefix}_throughput_monitor_loss_over_time.png", dpi=180)

    if prefix == "loss" and not loss_retrans.empty and loss_retrans["path_b_retrans_bytes_delta_sum"].notna().any():
        axes[2].clear()
        for method, group in loss_retrans.groupby("method"):
            style = line_styles.get(f"{method}_total", {"label": method})
            axes[2].plot(
                group["time_s"], group["path_b_retrans_bytes_delta_sum"],
                label=f"{method_labels.get(method, method)} Path B retransBytes delta",
                linewidth=1.5, color=style.get("color"),
            )
        axes[2].set_ylabel("Path B retransBytes delta")
        axes[2].set_xlabel("Time (s)")
        axes[2].grid(alpha=0.25)
        axes[2].legend()
        axes[2].axvspan(span[0], span[1], color="tab:red", alpha=0.08)
        fig.tight_layout()
        fig.savefig(out / f"{prefix}_throughput_retrans_over_time.png", dpi=180)

    plt.close(fig)


def _delay_window_metrics(
    util: pd.DataFrame,
    mon: pd.DataFrame,
    wire: pd.DataFrame,
    tc_qdisc: pd.DataFrame,
    active_probe_rtt: pd.DataFrame,
    lo: float,
    hi: float,
) -> dict:
    u = _window_mask(util, lo, hi)
    m = _window_mask(mon, lo, hi)
    w = _window_mask(wire, lo, hi)
    t = _window_mask(tc_qdisc, lo, hi)
    p = _window_mask(active_probe_rtt, lo, hi)
    ub = _path_b_rows(u)
    mb = _path_b_rows(m)

    out = {
        "owd_ms_mean": float(ub["owd_ms"].mean()) if "owd_ms" in ub.columns and len(ub) else float("nan"),
        "owd_ms_p95": _p95(ub["owd_ms"]) if "owd_ms" in ub.columns else float("nan"),
        "rtt_ms_mean": float(mb["rtt_smoothed_ms"].mean()) if "rtt_smoothed_ms" in mb.columns and len(mb) else float("nan"),
        "rtt_ms_p95": _p95(mb["rtt_smoothed_ms"]) if "rtt_smoothed_ms" in mb.columns else float("nan"),
        "rtt_latest_ms_mean": float(mb["rtt_latest_ms"].mean()) if "rtt_latest_ms" in mb.columns and len(mb) else float("nan"),
        "rtt_latest_ms_p95": _p95(mb["rtt_latest_ms"]) if "rtt_latest_ms" in mb.columns else float("nan"),
        "jitter_ms_mean": float(mb["rtt_mean_dev_ms"].mean()) if "rtt_mean_dev_ms" in mb.columns and len(mb) else float("nan"),
        "active_probe_rtt_ms_mean": float(p["active_probe_rtt_ms"].mean()) if "active_probe_rtt_ms" in p.columns and len(p) else float("nan"),
        "active_probe_rtt_ms_p95": _p95(p["active_probe_rtt_ms"]) if "active_probe_rtt_ms" in p.columns else float("nan"),
        "active_probe_success_fraction": float(p["active_probe_success"].mean()) if "active_probe_success" in p.columns and len(p) else float("nan"),
        "tc_configured_delay_ms_mean": float(t["tc_configured_delay_ms"].mean()) if "tc_configured_delay_ms" in t.columns and len(t) else float("nan"),
        "tc_configured_jitter_ms_mean": float(t["tc_configured_jitter_ms"].mean()) if "tc_configured_jitter_ms" in t.columns and len(t) else float("nan"),
        "tc_backlog_pkts_mean": float(t["tc_backlog_pkts"].mean()) if "tc_backlog_pkts" in t.columns and len(t) else float("nan"),
        "tc_dropped_delta_sum": float(t["tc_dropped_delta"].fillna(0).sum()) if "tc_dropped_delta" in t.columns and len(t) else float("nan"),
        "tc_overlimits_delta_sum": float(t["tc_overlimits_delta"].fillna(0).sum()) if "tc_overlimits_delta" in t.columns and len(t) else float("nan"),
        "path_b_share_pct_mean": float(w["path_b_share_pct"].mean()) if "path_b_share_pct" in w.columns and len(w) else float("nan"),
        "secondary_total_quic_wire_mbps_mean": float(w["total_quic_wire_mbps"].mean()) if len(w) else float("nan"),
        "secondary_path_a_quic_wire_mbps_mean": float(w["path_a_quic_wire_mbps"].mean()) if len(w) else float("nan"),
        "secondary_path_b_quic_wire_mbps_mean": float(w["path_b_quic_wire_mbps"].mean()) if len(w) else float("nan"),
    }
    return out


def _loss_window_metrics(
    util: pd.DataFrame,
    mon: pd.DataFrame,
    wire: pd.DataFrame,
    samples: pd.DataFrame,
    tc_qdisc: pd.DataFrame,
    lo: float,
    hi: float,
) -> dict:
    u = _window_mask(util, lo, hi)
    m = _window_mask(mon, lo, hi)
    w = _window_mask(wire, lo, hi)
    s = _window_mask(samples, lo, hi)
    t = _window_mask(tc_qdisc, lo, hi)
    ub = _path_b_rows(u)
    mb = _path_b_rows(m)
    sb = _path_b_rows(s, path_col="path_id")

    loss_event_frac = float("nan")
    if not sb.empty:
        flags = []
        if "loss_rate" in sb.columns:
            flags.append(sb["loss_rate"].fillna(0) > 0)
        if "retrans_bytes_delta" in sb.columns:
            flags.append(sb["retrans_bytes_delta"].fillna(0) > 0)
        if "lost_bytes_delta" in sb.columns:
            flags.append(sb["lost_bytes_delta"].fillna(0) > 0)
        if flags:
            any_event = flags[0]
            for f in flags[1:]:
                any_event = any_event | f
            loss_event_frac = float(any_event.mean())

    out = {
        "utility_loss_mean": float(ub["loss"].mean()) if "loss" in ub.columns and len(ub) else float("nan"),
        "monitor_loss_mean": float(mb["loss"].mean()) if "loss" in mb.columns and len(mb) else float("nan"),
        "sample_loss_rate_mean": float(sb["loss_rate"].mean()) if "loss_rate" in sb.columns and len(sb) else float("nan"),
        "retrans_bytes_delta_sum": float(sb["retrans_bytes_delta"].fillna(0).sum()) if "retrans_bytes_delta" in sb.columns and len(sb) else float("nan"),
        "lost_bytes_delta_sum": float(sb["lost_bytes_delta"].fillna(0).sum()) if "lost_bytes_delta" in sb.columns and len(sb) else float("nan"),
        "loss_event_fraction": loss_event_frac,
        "tc_loss_rate_mean": float(t["tc_loss_rate"].mean()) if "tc_loss_rate" in t.columns and len(t) else float("nan"),
        "tc_dropped_delta_sum": float(t["tc_dropped_delta"].fillna(0).sum()) if "tc_dropped_delta" in t.columns and len(t) else float("nan"),
        "tc_sent_pkts_delta_sum": float(t["tc_sent_pkts_delta"].fillna(0).sum()) if "tc_sent_pkts_delta" in t.columns and len(t) else float("nan"),
        "tc_configured_loss_pct_mean": float(t["tc_configured_loss_pct"].mean()) if "tc_configured_loss_pct" in t.columns and len(t) else float("nan"),
        "path_b_share_pct_mean": float(w["path_b_share_pct"].mean()) if "path_b_share_pct" in w.columns and len(w) else float("nan"),
        "secondary_total_quic_wire_mbps_mean": float(w["total_quic_wire_mbps"].mean()) if len(w) else float("nan"),
        "secondary_path_a_quic_wire_mbps_mean": float(w["path_a_quic_wire_mbps"].mean()) if len(w) else float("nan"),
        "secondary_path_b_quic_wire_mbps_mean": float(w["path_b_quic_wire_mbps"].mean()) if len(w) else float("nan"),
    }
    return out


def analyze_run(
    run_dir: Path,
    method: str,
    preset: str,
    full_hi: float,
    recovery_start_s: float,
) -> tuple[pd.DataFrame, dict, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    util, mon = load_pull_frames(run_dir)
    wire = load_wire_timeseries(run_dir)
    samples = load_runtime_samples(run_dir)

    if not wire.empty:
        wire = wire.copy()
        wire.insert(0, "method", method)
    delay_timeseries = _per_second_delay(util, mon, method) if preset == "delay" else pd.DataFrame()
    loss_monitor_timeseries = _per_second_loss_monitor(mon, method) if preset == "loss" else pd.DataFrame()
    loss_retrans_timeseries = _per_second_retrans_delta(run_dir, method) if preset == "loss" else pd.DataFrame()
    loss_tc_timeseries = load_tc_qdisc_timeseries(run_dir, method) if preset in ("loss", "delay") else pd.DataFrame()
    active_probe_rtt_timeseries = load_path_b_ping_timeseries(run_dir, method) if preset == "delay" else pd.DataFrame()

    recovery: dict[str, float] = {}
    if preset == "delay":
        ref_series = util.rename(columns={"t": "time_s"}) if not util.empty else pd.DataFrame()
        if not ref_series.empty and "owd_ms" in ref_series.columns:
            recovery["recovery_time_s_owd"] = _recovery_time_s(
                _path_b_rows(ref_series), tcol="time_s", vcol="owd_ms", ref_lo=50.0, ref_hi=90.0,
                recover_after=recovery_start_s,
            )
        share_series = wire if not wire.empty else pd.DataFrame()
        if not share_series.empty:
            recovery["recovery_time_s_path_b_share"] = _recovery_time_s(
                share_series, tcol="time_s", vcol="path_b_share_pct",
                ref_lo=50.0, ref_hi=90.0, recover_after=recovery_start_s, tol_frac=0.10,
            )
    else:
        if not samples.empty and "loss_rate" in samples.columns:
            sb = _path_b_rows(samples, path_col="path_id")
            recovery["recovery_time_s_loss_rate"] = _recovery_time_s(
                sb, tcol="time_s", vcol="loss_rate", ref_lo=50.0, ref_hi=90.0,
                recover_after=recovery_start_s,
            )
        share_series = wire if not wire.empty else pd.DataFrame()
        if not share_series.empty:
            recovery["recovery_time_s_path_b_share"] = _recovery_time_s(
                share_series, tcol="time_s", vcol="path_b_share_pct",
                ref_lo=50.0, ref_hi=90.0, recover_after=recovery_start_s, tol_frac=0.10,
            )

    rows: list[dict] = []
    windows = [*EVAL_WINDOWS, ("0-200", 0.0, full_hi)]
    for wname, lo, hi in windows:
        if preset == "delay":
            metrics = _delay_window_metrics(util.rename(columns={"t": "time_s"}) if "t" in util.columns else util,
                                            mon.rename(columns={"t": "time_s"}) if "t" in mon.columns else mon,
                                            wire, loss_tc_timeseries, active_probe_rtt_timeseries, lo, hi)
        else:
            metrics = _loss_window_metrics(
                util.rename(columns={"t": "time_s"}) if "t" in util.columns else util,
                mon.rename(columns={"t": "time_s"}) if "t" in mon.columns else mon,
                wire, samples, loss_tc_timeseries, lo, hi,
            )
        rows.append({
            "method": method,
            "window": wname,
            "t_lo": lo,
            "t_hi": hi,
            **metrics,
        })

    return pd.DataFrame(rows), recovery, wire, delay_timeseries, active_probe_rtt_timeseries, loss_monitor_timeseries, loss_retrans_timeseries, loss_tc_timeseries


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Delay/loss-primary analysis (throughput is secondary)",
    )
    ap.add_argument("--session", type=Path, required=True)
    ap.add_argument("--preset", choices=sorted(PRESETS), required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--full-hi", type=float, default=200.0)
    args = ap.parse_args()

    session = args.session.resolve()
    if not session.is_dir():
        print(f"[error] session not found: {session}", file=sys.stderr)
        sys.exit(1)

    cfg = PRESETS[args.preset]
    out = (args.out or (_REPO / "derived" / cfg["out_subdir"] / session.name)).resolve()
    out.mkdir(parents=True, exist_ok=True)

    dynamic_dir = next(
        (session / name for name in cfg["dynamic_dirs"] if (session / name).is_dir()),
        session / cfg["dynamic_dirs"][0],
    )
    runs = {
        "baseline": session / cfg["baseline_dir"],
        str(cfg["dynamic_dirs"][0]).removesuffix("_dynamic"): dynamic_dir,
    }

    all_windows: list[pd.DataFrame] = []
    all_wire: list[pd.DataFrame] = []
    all_delay: list[pd.DataFrame] = []
    all_active_probe_rtt: list[pd.DataFrame] = []
    all_loss_monitor: list[pd.DataFrame] = []
    all_loss_retrans: list[pd.DataFrame] = []
    all_loss_tc: list[pd.DataFrame] = []
    recovery_rows: list[dict] = []
    for method, rdir in runs.items():
        if not rdir.is_dir():
            print(f"[warn] missing run dir: {rdir}", file=sys.stderr)
            continue
        df, rec, wire, delay, active_probe_rtt, loss_monitor, loss_retrans, loss_tc = analyze_run(
            rdir, method, args.preset, args.full_hi, float(cfg["recovery_start_s"]),
        )
        if not df.empty:
            all_windows.append(df)
        if not wire.empty:
            all_wire.append(wire)
        if not delay.empty:
            all_delay.append(delay)
        if not active_probe_rtt.empty:
            all_active_probe_rtt.append(active_probe_rtt)
        if not loss_monitor.empty:
            all_loss_monitor.append(loss_monitor)
        if not loss_retrans.empty:
            all_loss_retrans.append(loss_retrans)
        if not loss_tc.empty:
            all_loss_tc.append(loss_tc)
        recovery_rows.append({"method": method, "run_dir": str(rdir), **rec})

    if not all_windows:
        print("[error] no window metrics produced (need pcaps and/or SAVE_LOGS=1 pull logs)", file=sys.stderr)
        sys.exit(2)

    prefix = cfg["file_prefix"]
    df_win = pd.concat(all_windows, ignore_index=True)
    df_wire = pd.concat(all_wire, ignore_index=True) if all_wire else pd.DataFrame()
    df_delay = pd.concat(all_delay, ignore_index=True) if all_delay else pd.DataFrame()
    df_active_probe_rtt = pd.concat(all_active_probe_rtt, ignore_index=True) if all_active_probe_rtt else pd.DataFrame()
    df_loss_monitor = pd.concat(all_loss_monitor, ignore_index=True) if all_loss_monitor else pd.DataFrame()
    df_loss_retrans = pd.concat(all_loss_retrans, ignore_index=True) if all_loss_retrans else pd.DataFrame()
    df_loss_tc = pd.concat(all_loss_tc, ignore_index=True) if all_loss_tc else pd.DataFrame()
    df_win.to_csv(out / f"{prefix}_primary_metrics_windows.csv", index=False)
    pd.DataFrame(recovery_rows).to_csv(out / f"{prefix}_recovery_times.csv", index=False)
    if not df_wire.empty:
        df_wire.to_csv(out / f"{prefix}_throughput_timeseries.csv", index=False)
    if not df_delay.empty:
        df_delay.to_csv(out / f"{prefix}_delay_timeseries.csv", index=False)
    if not df_active_probe_rtt.empty:
        df_active_probe_rtt.to_csv(out / f"{prefix}_active_probe_rtt_timeseries.csv", index=False)
    if not df_loss_monitor.empty:
        df_loss_monitor.to_csv(out / f"{prefix}_monitor_timeseries.csv", index=False)
    if not df_loss_retrans.empty:
        df_loss_retrans.to_csv(out / f"{prefix}_retrans_timeseries.csv", index=False)
    if not df_loss_tc.empty:
        df_loss_tc.to_csv(out / f"{prefix}_tc_qdisc_timeseries.csv", index=False)

    # Secondary throughput table (explicitly labeled).
    sec_cols = [
        "method", "window", "secondary_total_quic_wire_mbps_mean",
        "secondary_path_a_quic_wire_mbps_mean", "secondary_path_b_quic_wire_mbps_mean",
    ]
    df_win[sec_cols].to_csv(out / f"{prefix}_secondary_throughput_windows.csv", index=False)
    dynamic_method = str(cfg["dynamic_dirs"][0]).removesuffix("_dynamic")
    comparison = build_improvement_table(df_win, dynamic_method)
    comparison.to_csv(out / f"{prefix}_baseline_vs_qaccess_improvement.csv", index=False)
    if args.preset == "loss":
        impairment_span = _loss_span_from_tc_logs(session)
    elif args.preset == "delay":
        impairment_span = _delay_span_from_tc_logs(session)
    else:
        impairment_span = None
    _plot_timeseries(df_wire, df_delay, df_active_probe_rtt, df_loss_monitor, df_loss_retrans, df_loss_tc, out, prefix, impairment_span)

    metadata_path = session / "experiment_metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    execution_mode = metadata.get("execution_mode", "unknown")

    print(f"Experiment: {cfg['title']}")
    print(f"Session: {session}")
    print(f"Primary metrics: {out / f'{prefix}_primary_metrics_windows.csv'}")
    print(f"Recovery times:  {out / f'{prefix}_recovery_times.csv'}")
    print(f"Secondary TP:    {out / f'{prefix}_secondary_throughput_windows.csv'}")
    print(f"Comparison:      {out / f'{prefix}_baseline_vs_qaccess_improvement.csv'}")
    if prefix == "loss" and not df_loss_tc.empty and "tc_dropped_delta" in df_loss_tc.columns and df_loss_tc["tc_dropped_delta"].notna().any():
        plot_name = f"{prefix}_throughput_tc_dropped_packets_cumulative_over_time.png"
    elif prefix == "delay" and not df_active_probe_rtt.empty and df_active_probe_rtt["active_probe_rtt_ms"].notna().any():
        plot_name = f"{prefix}_throughput_active_probe_rtt_over_time.png"
    elif prefix == "delay":
        plot_name = f"{prefix}_throughput_quic_rtt_diagnostic_over_time.png"
    else:
        plot_name = f"{prefix}_throughput_loss_over_time.png"
    print(f"Time-series plot:{out / plot_name}")
    if prefix in ("loss", "delay") and not df_loss_tc.empty:
        print(f"TC qdisc CSV:    {out / f'{prefix}_tc_qdisc_timeseries.csv'}")
    if prefix == "delay" and not df_active_probe_rtt.empty:
        print(f"Active RTT CSV:  {out / f'{prefix}_active_probe_rtt_timeseries.csv'}")
        print(f"QUIC RTT diag:   {out / f'{prefix}_throughput_quic_rtt_diagnostic_over_time.png'}")
    if prefix == "loss":
        print(f"Monitor plot:   {out / f'{prefix}_throughput_monitor_loss_over_time.png'}")
        print(f"Retrans plot:   {out / f'{prefix}_throughput_retrans_over_time.png'}")
    print(f"\nWorker execution mode recorded by runner: {execution_mode}")
    print("Evaluation is read-only and does not start or stop the worker.")
    print(df_win.to_string(index=False, float_format="%.3f"))


if __name__ == "__main__":
    main()
