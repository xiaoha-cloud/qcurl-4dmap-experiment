#!/usr/bin/env python3
import argparse
import csv
import math
import re
from pathlib import Path


VIDEO_RECEIVE_EVENTS = {"puller_first_video", "receiver_frame", "server_video_receive"}


def parse_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except ValueError:
        return None


def percentile(values, pct):
    values = sorted(v for v in values if v is not None and not math.isnan(v))
    if not values:
        return ""
    if len(values) == 1:
        return round(values[0], 3)
    rank = (len(values) - 1) * pct
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return round(values[int(rank)], 3)
    return round(values[lo] + (values[hi] - values[lo]) * (rank - lo), 3)


def mean(values):
    values = [v for v in values if v is not None and not math.isnan(v)]
    if not values:
        return ""
    return round(sum(values) / len(values), 3)


def event_time(row):
    return parse_float(row.get("physical_time_ms")) or parse_float(row.get("timestamp_ms"))


def event_flv_time(row):
    return parse_float(row.get("flv_timestamp_ms"))


def is_video_event(row):
    return row.get("tag_type") == "video" and row.get("event") in VIDEO_RECEIVE_EVENTS


def explicit_gap_ms(row):
    note = row.get("note") or ""
    match = re.search(r"(?:excess_ms|duration_ms)=(-?\d+(?:\.\d+)?)", note)
    if match:
        return max(0.0, float(match.group(1)))
    return 0.0


def input_files(path):
    p = Path(path)
    if p.is_dir():
        return sorted(p.rglob("qoe_events_*.csv"))
    return [p]


def read_rows(paths):
    rows = []
    for path in paths:
        try:
            with path.open(newline="") as f:
                for row in csv.DictReader(f):
                    row["_input_file"] = str(path)
                    rows.append(row)
        except OSError as exc:
            print(f"[qoe] warning: failed to read {path}: {exc}")
    return rows


def group_rows(rows):
    groups = {}
    for row in rows:
        session = row.get("session_id") or "unknown"
        groups.setdefault(session, []).append(row)
    return groups


def first_event(rows, name):
    matches = [r for r in rows if r.get("event") == name and event_time(r) is not None]
    if not matches:
        return None
    return min(matches, key=event_time)


def first_any_event(rows, names):
    matches = [r for r in rows if r.get("event") in names and event_time(r) is not None]
    if not matches:
        return None
    return min(matches, key=event_time)


def video_receive_rows(rows):
    out = [r for r in rows if is_video_event(r) and event_time(r) is not None]
    return sorted(out, key=event_time)


def sender_first_tag(rows):
    candidates = [r for r in rows if r.get("event") == "first_tag_send" and event_time(r) is not None and event_flv_time(r) is not None]
    if not candidates:
        return None
    return min(candidates, key=event_time)


def infer_gaps(video_rows, threshold_ms):
    count = 0
    total = 0.0
    gaps = []
    late = 0
    for prev, cur in zip(video_rows, video_rows[1:]):
        prev_wall = event_time(prev)
        cur_wall = event_time(cur)
        prev_media = event_flv_time(prev)
        cur_media = event_flv_time(cur)
        if prev_wall is None or cur_wall is None or prev_media is None or cur_media is None:
            continue
        wall_gap = cur_wall - prev_wall
        media_gap = cur_media - prev_media
        if media_gap < 0:
            continue
        excess = wall_gap - media_gap
        gaps.append(wall_gap)
        if excess > threshold_ms:
            count += 1
            total += excess
            late += 1
    return count, total, gaps, late


def summarize_session(session_id, rows, threshold_ms):
    rows = sorted(rows, key=lambda r: event_time(r) or 0)
    inputs = sorted({r.get("_input_file", "") for r in rows if r.get("_input_file")})
    notes = []

    pusher_start = first_event(rows, "pusher_start")
    first_receiver_video = first_any_event(rows, {"puller_first_video", "server_video_receive"})
    first_tag = first_event(rows, "first_tag_send")

    startup = ""
    if pusher_start and first_receiver_video:
        startup = round(event_time(first_receiver_video) - event_time(pusher_start), 3)
    elif pusher_start and first_tag:
        startup = round(event_time(first_tag) - event_time(pusher_start), 3)
        notes.append("startup_fallback_first_tag_send")
    else:
        notes.append("startup_unavailable")

    stream_delays = []
    origin_row = sender_first_tag(rows)
    if origin_row:
        sender_origin = event_time(origin_row) - event_flv_time(origin_row)
        for row in video_receive_rows(rows):
            flv_ts = event_flv_time(row)
            recv_time = event_time(row)
            if flv_ts is not None and recv_time is not None:
                stream_delays.append(recv_time - (sender_origin + flv_ts))
    else:
        notes.append("stream_delay_unavailable_no_sender_alignment")

    explicit_gaps = [r for r in rows if r.get("event") in {"receiver_gap", "playback_gap"}]
    if explicit_gaps:
        rebuffer_count = len(explicit_gaps)
        rebuffer_total = sum(explicit_gap_ms(r) for r in explicit_gaps)
        video_rows = video_receive_rows(rows)
        _, _, frame_gaps, late_count = infer_gaps(video_rows, threshold_ms)
    else:
        video_rows = video_receive_rows(rows)
        rebuffer_count, rebuffer_total, frame_gaps, late_count = infer_gaps(video_rows, threshold_ms)
        if rebuffer_count:
            notes.append("rebuffering_inferred_from_video_gaps")

    video_rows = video_receive_rows(rows)
    video_count = len(video_rows)
    avg_fps = ""
    duration_s = ""
    if video_count >= 2:
        duration_ms = event_time(video_rows[-1]) - event_time(video_rows[0])
        if duration_ms > 0:
            duration_s = duration_ms / 1000.0
            avg_fps = round(video_count / duration_s, 3)

    rebuffer_ratio = ""
    if duration_s:
        rebuffer_ratio = round((rebuffer_total / 1000.0) / duration_s, 6)

    return {
        "session_id": session_id,
        "input_file": ";".join(inputs),
        "startup_latency_ms": startup,
        "avg_stream_delay_ms": mean(stream_delays),
        "p95_stream_delay_ms": percentile(stream_delays, 0.95),
        "rebuffering_count": rebuffer_count,
        "total_rebuffering_duration_ms": round(rebuffer_total, 3),
        "rebuffering_ratio": rebuffer_ratio,
        "avg_fps": avg_fps,
        "p95_frame_gap_ms": percentile(frame_gaps, 0.95),
        "max_frame_gap_ms": round(max(frame_gaps), 3) if frame_gaps else "",
        "video_event_count": video_count,
        "notes": ";".join(notes),
        "_late_frame_count": late_count,
    }


def main():
    ap = argparse.ArgumentParser(description="Compute live-streaming QoE metrics from qoe_events_*.csv files.")
    ap.add_argument("--input", required=True, help="qoe_events CSV file or directory containing qoe_events_*.csv")
    ap.add_argument("--output", default="qoe_summary.csv", help="summary CSV path")
    ap.add_argument("--gap-threshold-ms", type=float, default=500.0, help="gap excess threshold for inferred rebuffering")
    args = ap.parse_args()

    paths = input_files(args.input)
    rows = read_rows(paths)
    summaries = [summarize_session(session, group, args.gap_threshold_ms) for session, group in group_rows(rows).items()]

    fields = [
        "session_id",
        "input_file",
        "startup_latency_ms",
        "avg_stream_delay_ms",
        "p95_stream_delay_ms",
        "rebuffering_count",
        "total_rebuffering_duration_ms",
        "rebuffering_ratio",
        "avg_fps",
        "p95_frame_gap_ms",
        "max_frame_gap_ms",
        "video_event_count",
        "notes",
    ]
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for summary in summaries:
            writer.writerow({k: summary.get(k, "") for k in fields})

    print(f"[qoe] input_files={len(paths)} rows={len(rows)} sessions={len(summaries)}")
    for summary in summaries:
        print(
            "[qoe] session={session_id} startup_ms={startup_latency_ms} "
            "avg_delay_ms={avg_stream_delay_ms} rebuffer_count={rebuffering_count} "
            "rebuffer_ms={total_rebuffering_duration_ms} fps={avg_fps} "
            "video_events={video_event_count} notes={notes}".format(**summary)
        )
    print(f"[qoe] wrote {out}")


if __name__ == "__main__":
    main()
