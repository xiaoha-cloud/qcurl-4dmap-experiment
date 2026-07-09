#!/usr/bin/env python3
"""Evaluate Q-ACCeSS-T no-deterioration experiment.

The evaluator does not use qoe_from_events.py. It reports throughput from path
pcaps and computes video/QoE metrics only when the required raw evidence exists:
output FLV files, optional raw QoE event timestamps, ffprobe, and ffmpeg SSIM.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

WINDOWS = [(0.0, 50.0), (50.0, 100.0), (100.0, 150.0), (150.0, 200.0)]
RUNS = {
    "baseline": "no_deterioration_baseline",
    "qaccess_t": "no_deterioration_qaccess_t",
}


def parse_windows(spec: str) -> list[tuple[float, float]]:
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        lo_s, hi_s = item.split(":", 1)
        lo, hi = float(lo_s), float(hi_s)
        if hi <= lo:
            raise ValueError(f"invalid window {item}")
        out.append((lo, hi))
    return out or WINDOWS


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = list(rows[0].keys())
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def find_one(directory: Path, pattern: str) -> Path | None:
    hits = sorted(directory.glob(pattern))
    return hits[-1] if hits else None


def run_tshark_all_frames(pcap: Path, tshark_bin: str) -> list[tuple[float, int]]:
    cmd = [tshark_bin, "-r", "-", "-T", "fields", "-e", "frame.time_epoch", "-e", "frame.len"]
    with pcap.open("rb") as f:
        proc = subprocess.run(cmd, stdin=f, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"tshark failed for {pcap}: {proc.stderr[:2000]}")
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        try:
            rows.append((float(parts[0]), int(float(parts[1]))))
        except ValueError:
            continue
    return rows


def pcap_series(run_dir: Path, tshark_bin: str, bin_seconds: float) -> list[dict[str, float]]:
    pcap_dir = run_dir / "pcaps"
    pcap_a = find_one(pcap_dir, "pathA_h1_*.pcap*")
    pcap_b = find_one(pcap_dir, "pathB_h1_*.pcap*")
    if not pcap_a or not pcap_b:
        raise FileNotFoundError(f"missing path pcaps under {pcap_dir}")
    packets_a = run_tshark_all_frames(pcap_a, tshark_bin)
    packets_b = run_tshark_all_frames(pcap_b, tshark_bin)
    all_times = [t for t, _ in packets_a + packets_b]
    if not all_times:
        return []
    t0 = min(all_times)
    bins: dict[str, dict[int, int]] = {"pathA_mbps": {}, "pathB_mbps": {}}
    for key, packets in (("pathA_mbps", packets_a), ("pathB_mbps", packets_b)):
        for ts, size in packets:
            idx = int(math.floor((ts - t0) / bin_seconds))
            if idx >= 0:
                bins[key][idx] = bins[key].get(idx, 0) + size
    max_idx = max([0] + list(bins["pathA_mbps"].keys()) + list(bins["pathB_mbps"].keys()))
    rows = []
    for idx in range(max_idx + 1):
        a = bins["pathA_mbps"].get(idx, 0) * 8.0 / 1_000_000.0 / bin_seconds
        b = bins["pathB_mbps"].get(idx, 0) * 8.0 / 1_000_000.0 / bin_seconds
        rows.append({"time_s": idx * bin_seconds, "pathA_mbps": a, "pathB_mbps": b, "total_mbps": a + b})
    return rows


def mean(values: list[float]) -> float | str:
    vals = [v for v in values if v is not None and not math.isnan(v)]
    return sum(vals) / len(vals) if vals else ""


def mean_window(rows: list[dict[str, float]], key: str, lo: float, hi: float) -> float | str:
    return mean([r[key] for r in rows if lo <= r["time_s"] < hi])


def pct(new: Any, old: Any) -> float | str:
    try:
        if old == 0 or old == "" or new == "":
            return ""
        return (float(new) - float(old)) / float(old) * 100.0
    except Exception:
        return ""


def load_metadata(session: Path) -> dict[str, Any]:
    path = session / "experiment_metadata.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def ffprobe_json(path: Path, ffprobe_bin: str) -> dict[str, Any]:
    cmd = [ffprobe_bin, "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=duration,nb_frames,avg_frame_rate,r_frame_rate,width,height", "-of", "json", str(path)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()[:500]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": "invalid ffprobe json"}


def parse_fraction(raw: str | None) -> float | str:
    if not raw:
        return ""
    if "/" in raw:
        a, b = raw.split("/", 1)
        try:
            b_f = float(b)
            return float(a) / b_f if b_f else ""
        except ValueError:
            return ""
    try:
        return float(raw)
    except ValueError:
        return ""


def video_summary(path: Path | None, ffprobe_bin: str) -> dict[str, Any]:
    row = {
        "output_flv": str(path) if path else "",
        "output_exists": bool(path and path.exists()),
        "duration_s": "",
        "frame_count": "",
        "frame_rate": "",
        "width": "",
        "height": "",
        "video_note": "",
    }
    if not path or not path.exists():
        row["video_note"] = "output_flv_missing"
        return row
    if shutil.which(ffprobe_bin) is None:
        row["video_note"] = "ffprobe_missing"
        return row
    info = ffprobe_json(path, ffprobe_bin)
    if "error" in info:
        row["video_note"] = info["error"]
        return row
    streams = info.get("streams") or []
    if not streams:
        row["video_note"] = "video_stream_missing"
        return row
    s = streams[0]
    row["duration_s"] = float(s["duration"]) if s.get("duration") else ""
    if s.get("nb_frames") not in (None, "N/A", ""):
        try:
            row["frame_count"] = int(s["nb_frames"])
        except ValueError:
            pass
    row["frame_rate"] = parse_fraction(s.get("avg_frame_rate") or s.get("r_frame_rate"))
    row["width"] = s.get("width", "")
    row["height"] = s.get("height", "")
    return row


def read_qoe_rows(run_dir: Path) -> list[dict[str, str]]:
    rows = []
    for path in sorted((run_dir / "qoe").rglob("qoe_events_*.csv")):
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                row["_file"] = str(path)
                rows.append(row)
    return rows


def to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except ValueError:
        return None


def event_time(row: dict[str, str]) -> float | None:
    return to_float(row.get("physical_time_ms")) or to_float(row.get("timestamp_ms"))


def media_time(row: dict[str, str]) -> float | None:
    return to_float(row.get("flv_timestamp_ms"))


def role_text(row: dict[str, str]) -> str:
    return " ".join(str(row.get(k, "")) for k in ("role", "qoe_role", "endpoint_role", "QACCESS_QOE_ROLE", "_file")).lower()


def is_video(row: dict[str, str]) -> bool:
    return str(row.get("tag_type", "")).lower() == "video" or "video" in str(row.get("event", "")).lower()


def strict_qoe_summary(run_dir: Path) -> dict[str, Any]:
    rows = read_qoe_rows(run_dir)
    out = {
        "qoe_events_available": "yes" if rows else "no",
        "startup_latency_ms": "",
        "avg_stream_delay_ms": "",
        "stream_delay_scope": "",
        "matched_stream_delay_frames": "",
        "p50_stream_delay_ms": "",
        "p95_stream_delay_ms": "",
        "max_stream_delay_ms": "",
        "startup_pusher_server_ms": "",
        "avg_pusher_server_delay_ms": "",
        "p50_pusher_server_delay_ms": "",
        "p95_pusher_server_delay_ms": "",
        "max_pusher_server_delay_ms": "",
        "matched_pusher_server_frames": "",
        "rebuffering_time_s": "",
        "rebuffering_ratio": "",
        "qoe_note": "",
    }
    if not rows:
        out["qoe_note"] = "raw_qoe_events_missing"
        return out

    def is_pusher_send(row: dict[str, str]) -> bool:
        ev = str(row.get("event", "")).lower()
        role = role_text(row)
        return is_video(row) and ("pusher" in role or "publisher" in role) and ("send" in ev or ev == "first_tag_send")

    def is_puller_receive(row: dict[str, str]) -> bool:
        ev = str(row.get("event", "")).lower()
        role = role_text(row)
        return is_video(row) and ("puller" in role or "receiver" in role) and (
            "receive" in ev or "receiver" in ev or ev in {"receiver_frame", "puller_first_video"}
        )

    def is_server_receive(row: dict[str, str]) -> bool:
        ev = str(row.get("event", "")).lower()
        role = role_text(row)
        return is_video(row) and "server" in role and ("receive" in ev or ev == "server_video_receive")

    def matched_delay_stats(send_events: list[dict[str, str]], recv_events: list[dict[str, str]]) -> dict[str, Any]:
        sends_by_media: dict[float, float] = {}
        for row in sorted(send_events, key=lambda r: event_time(r) or 0):
            mt, et = media_time(row), event_time(row)
            if mt is not None and et is not None:
                sends_by_media.setdefault(round(mt, 3), et)
        delays = []
        for row in sorted(recv_events, key=lambda r: event_time(r) or 0):
            mt, rt = media_time(row), event_time(row)
            if mt is None or rt is None:
                continue
            st = sends_by_media.get(round(mt, 3))
            if st is not None and rt >= st:
                delays.append(rt - st)
        if not delays:
            return {}
        delays_sorted = sorted(delays)

        def percentile(values: list[float], q: float) -> float:
            if len(values) == 1:
                return values[0]
            pos = (len(values) - 1) * q
            lo = int(math.floor(pos))
            hi = int(math.ceil(pos))
            if lo == hi:
                return values[lo]
            return values[lo] + (values[hi] - values[lo]) * (pos - lo)

        return {
            "matched": len(delays_sorted),
            "startup": round(delays_sorted[0], 3),
            "avg": round(sum(delays_sorted) / len(delays_sorted), 3),
            "p50": round(percentile(delays_sorted, 0.50), 3),
            "p95": round(percentile(delays_sorted, 0.95), 3),
            "max": round(delays_sorted[-1], 3),
        }

    send_events = [r for r in rows if is_pusher_send(r)]
    puller_recv_events = [r for r in rows if is_puller_receive(r)]
    server_recv_events = [r for r in rows if is_server_receive(r)]

    notes = []
    puller_stats = matched_delay_stats(send_events, puller_recv_events)
    server_stats = matched_delay_stats(send_events, server_recv_events)

    if server_stats:
        out["startup_pusher_server_ms"] = server_stats["startup"]
        out["avg_pusher_server_delay_ms"] = server_stats["avg"]
        out["p50_pusher_server_delay_ms"] = server_stats["p50"]
        out["p95_pusher_server_delay_ms"] = server_stats["p95"]
        out["max_pusher_server_delay_ms"] = server_stats["max"]
        out["matched_pusher_server_frames"] = server_stats["matched"]

    if puller_stats:
        out["startup_latency_ms"] = puller_stats["startup"]
        out["avg_stream_delay_ms"] = puller_stats["avg"]
        out["p50_stream_delay_ms"] = puller_stats["p50"]
        out["p95_stream_delay_ms"] = puller_stats["p95"]
        out["max_stream_delay_ms"] = puller_stats["max"]
        out["matched_stream_delay_frames"] = puller_stats["matched"]
        out["stream_delay_scope"] = "pusher_to_puller"
    elif server_stats:
        out["startup_latency_ms"] = server_stats["startup"]
        out["avg_stream_delay_ms"] = server_stats["avg"]
        out["p50_stream_delay_ms"] = server_stats["p50"]
        out["p95_stream_delay_ms"] = server_stats["p95"]
        out["max_stream_delay_ms"] = server_stats["max"]
        out["matched_stream_delay_frames"] = server_stats["matched"]
        out["stream_delay_scope"] = "pusher_to_server_fallback"
        notes.append("stream_delay_uses_pusher_to_server_fallback_no_puller_frames")
    else:
        notes.append("startup_unavailable_no_matched_first_video_send_receive")
        notes.append("stream_delay_unavailable_no_matched_frame_timestamps")

    gap_events = [r for r in rows if str(r.get("event", "")).lower() in {"receiver_gap", "playback_gap", "rebuffer", "rebuffering"}]
    if gap_events:
        total_ms = 0.0
        for row in gap_events:
            note = row.get("note", "")
            match = re.search(r"(?:duration_ms|excess_ms)=(-?\d+(?:\.\d+)?)", note)
            if match:
                total_ms += max(0.0, float(match.group(1)))
            else:
                val = to_float(row.get("duration_ms")) or to_float(row.get("excess_ms")) or 0.0
                total_ms += max(0.0, val)
        out["rebuffering_time_s"] = round(total_ms / 1000.0, 6)
    else:
        out["rebuffering_time_s"] = 0.0
        notes.append("no_explicit_rebuffer_events")

    ratio_reference_events = puller_recv_events if puller_recv_events else server_recv_events
    video_rows = sorted(ratio_reference_events, key=lambda r: event_time(r) or 0)
    if video_rows and out["rebuffering_time_s"] != "":
        start = event_time(video_rows[0])
        end = event_time(video_rows[-1])
        if start is not None and end is not None and end > start:
            out["rebuffering_ratio"] = round(float(out["rebuffering_time_s"]) / ((end - start) / 1000.0), 6)
    out["qoe_note"] = ";".join(notes)
    return out


def compute_mean_ssim(input_flv: Path | None, output_flv: Path | None, ffmpeg_bin: str) -> tuple[Any, str]:
    if not input_flv or not input_flv.exists():
        return "", "input_flv_missing"
    if not output_flv or not output_flv.exists():
        return "", "output_flv_missing"
    if shutil.which(ffmpeg_bin) is None:
        return "", "ffmpeg_missing"
    filt = "[0:v]setpts=PTS-STARTPTS[ref];[1:v]setpts=PTS-STARTPTS[dist];[ref][dist]ssim"
    cmd = [ffmpeg_bin, "-hide_banner", "-nostats", "-i", str(input_flv), "-i", str(output_flv), "-lavfi", filt, "-f", "null", "-"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    text = proc.stderr + "\n" + proc.stdout
    matches = re.findall(r"All:([0-9.]+)", text)
    if proc.returncode != 0 or not matches:
        return "", "ffmpeg_ssim_failed"
    return float(matches[-1]), ""


def evaluate_video_qoe(session: Path, input_flv: Path | None, ffprobe_bin: str, ffmpeg_bin: str) -> list[dict[str, Any]]:
    rows = []
    for method, label in RUNS.items():
        run_dir = session / label
        output = find_one(run_dir, "output_*.flv")
        video = video_summary(output, ffprobe_bin)
        qoe = strict_qoe_summary(run_dir)
        mean_ssim, ssim_note = compute_mean_ssim(input_flv, output, ffmpeg_bin)
        rebuffer_s = qoe.get("rebuffering_time_s", "")
        frame_count = video.get("frame_count", "")
        frame_rate = video.get("frame_rate", "")
        assim = ""
        if mean_ssim != "" and rebuffer_s != "" and frame_count != "" and frame_rate != "":
            denom = float(frame_count) + float(frame_rate) * float(rebuffer_s)
            if denom > 0:
                assim = float(mean_ssim) * float(frame_count) / denom
        row = {"method": method}
        row.update(video)
        row.update(qoe)
        row["mean_ssim"] = mean_ssim
        row["aSSIM"] = assim
        row["ssim_note"] = ssim_note
        rows.append(row)
    return rows


def plot_throughput(out: Path, timeseries: list[dict[str, Any]]) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    for method in ("baseline", "qaccess_t"):
        rows = [r for r in timeseries if r["method"] == method]
        axes[0].plot([r["time_s"] for r in rows], [r["total_mbps"] for r in rows], label=f"{method} total", linewidth=1.5)
        axes[1].plot([r["time_s"] for r in rows], [r["pathA_mbps"] for r in rows], label=f"{method} Path A", linewidth=1.1)
        axes[1].plot([r["time_s"] for r in rows], [r["pathB_mbps"] for r in rows], label=f"{method} Path B", linewidth=1.1, linestyle="--")
    axes[0].set_ylabel("Total throughput (Mbps)")
    axes[1].set_ylabel("Path throughput (Mbps)")
    axes[1].set_xlabel("Time (s)")
    for ax in axes:
        ax.grid(alpha=0.25)
        ax.legend()
    fig.tight_layout()
    fig.savefig(out / "no_deterioration_throughput_over_time.png", dpi=180)
    plt.close(fig)


def plot_qoe(out: Path, qoe_rows: list[dict[str, Any]]) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    metrics = [
        ("rebuffering_time_s", "Re-buffering time (s)"),
        ("startup_latency_ms", "Start-up latency (ms)"),
        ("avg_stream_delay_ms", "Stream delay (ms)"),
        ("aSSIM", "aSSIM"),
    ]
    methods = [r["method"] for r in qoe_rows]
    fig, axes = plt.subplots(2, 2, figsize=(9, 7))
    for ax, (key, label) in zip(axes.ravel(), metrics):
        vals = []
        for row in qoe_rows:
            value = row.get(key, "")
            vals.append(float(value) if value not in ("", None) else math.nan)
        ax.bar(methods, vals)
        ax.set_ylabel(label)
        ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(out / "no_deterioration_qoe_summary.png", dpi=180)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description="Evaluate Q-ACCeSS-T no-deterioration experiment")
    ap.add_argument("--session", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--input-flv", type=Path, default=None)
    ap.add_argument("--windows", default="0:50,50:100,100:150,150:200")
    ap.add_argument("--bin-seconds", type=float, default=1.0)
    ap.add_argument("--tshark-bin", default="tshark")
    ap.add_argument("--ffprobe-bin", default="ffprobe")
    ap.add_argument("--ffmpeg-bin", default="ffmpeg")
    args = ap.parse_args()

    session = args.session.resolve()
    out = args.out.resolve() if args.out else session / "evaluation_no_deterioration"
    out.mkdir(parents=True, exist_ok=True)
    windows = parse_windows(args.windows)
    metadata = load_metadata(session)
    input_flv = args.input_flv or (Path(metadata.get("input_flv")) if metadata.get("input_flv") else None)

    if shutil.which(args.tshark_bin) is None:
        raise SystemExit(f"tshark not found: {args.tshark_bin}")

    by_method: dict[str, list[dict[str, float]]] = {}
    timeseries: list[dict[str, Any]] = []
    for method, label in RUNS.items():
        rows = pcap_series(session / label, args.tshark_bin, args.bin_seconds)
        by_method[method] = rows
        for row in rows:
            timeseries.append({"method": method, **row})

    window_rows = []
    for method, rows in by_method.items():
        for lo, hi in windows:
            window_rows.append({
                "method": method,
                "window": f"{lo:g}-{hi:g}",
                "t_lo": lo,
                "t_hi": hi,
                "total_mbps_mean": mean_window(rows, "total_mbps", lo, hi),
                "pathA_mbps_mean": mean_window(rows, "pathA_mbps", lo, hi),
                "pathB_mbps_mean": mean_window(rows, "pathB_mbps", lo, hi),
            })

    compare_rows = []
    for lo, hi in windows:
        b_total = mean_window(by_method["baseline"], "total_mbps", lo, hi)
        q_total = mean_window(by_method["qaccess_t"], "total_mbps", lo, hi)
        b_a = mean_window(by_method["baseline"], "pathA_mbps", lo, hi)
        q_a = mean_window(by_method["qaccess_t"], "pathA_mbps", lo, hi)
        b_b = mean_window(by_method["baseline"], "pathB_mbps", lo, hi)
        q_b = mean_window(by_method["qaccess_t"], "pathB_mbps", lo, hi)
        compare_rows.append({
            "window": f"{lo:g}-{hi:g}",
            "baseline_total_mbps": b_total,
            "qaccess_t_total_mbps": q_total,
            "total_delta_mbps": float(q_total) - float(b_total) if b_total != "" and q_total != "" else "",
            "total_change_pct": pct(q_total, b_total),
            "baseline_pathA_mbps": b_a,
            "qaccess_t_pathA_mbps": q_a,
            "pathA_change_pct": pct(q_a, b_a),
            "baseline_pathB_mbps": b_b,
            "qaccess_t_pathB_mbps": q_b,
            "pathB_change_pct": pct(q_b, b_b),
        })

    qoe_rows = evaluate_video_qoe(session, input_flv, args.ffprobe_bin, args.ffmpeg_bin)

    write_csv(out / "throughput_timeseries.csv", timeseries)
    write_csv(out / "throughput_windows.csv", window_rows)
    write_csv(out / "throughput_comparison.csv", compare_rows)
    write_csv(out / "qoe_summary.csv", qoe_rows)
    plot_throughput(out, timeseries)
    plot_qoe(out, qoe_rows)

    eval_meta = {
        "session": str(session),
        "input_flv": str(input_flv) if input_flv else "",
        "throughput_source": "path pcaps, all frames, no direction filter",
        "qoe_source": "output FLV + strict raw QoE event matching when available; qoe_from_events.py is not used",
        "windows": [{"start_s": lo, "end_s": hi} for lo, hi in windows],
    }
    (out / "evaluation_metadata.json").write_text(json.dumps(eval_meta, indent=2), encoding="utf-8")

    print(f"Session: {session}")
    print(f"Throughput windows: {out / 'throughput_windows.csv'}")
    print(f"Throughput comparison: {out / 'throughput_comparison.csv'}")
    print(f"QoE summary: {out / 'qoe_summary.csv'}")
    print(f"Throughput plot: {out / 'no_deterioration_throughput_over_time.png'}")
    print(f"QoE plot: {out / 'no_deterioration_qoe_summary.png'}")


if __name__ == "__main__":
    main()
