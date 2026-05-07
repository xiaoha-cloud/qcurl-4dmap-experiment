#!/usr/bin/env python3

import argparse
import csv
import math
import os
import subprocess
from collections import defaultdict


DOWN_FILTERS = {
    "a": "udp && ip.src==10.0.1.2 && ip.dst==10.0.1.1",
    "b": "udp && ip.src==10.0.2.2 && ip.dst==10.0.2.1",
}

UP_FILTERS = {
    "a": "udp && ip.src==10.0.1.1 && ip.dst==10.0.1.2",
    "b": "udp && ip.src==10.0.2.1 && ip.dst==10.0.2.2",
}


def read_packets(pcap_path, display_filter):
    cmd = [
        "tshark",
        "-r", pcap_path,
        "-Y", display_filter,
        "-T", "fields",
        "-E", "separator=\t",
        "-E", "occurrence=f",
        "-e", "frame.time_epoch",
        "-e", "ip.len",
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"tshark failed for {pcap_path}: {result.stderr.strip()}")

    packets = []

    for line in result.stdout.splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 2:
            continue

        try:
            ts = float(parts[0])
            size_bytes = int(parts[1])
        except ValueError:
            continue

        packets.append((ts, size_bytes))

    return packets


def aggregate(packets, t0, interval):
    bins = defaultdict(int)

    for ts, size_bytes in packets:
        bin_id = int(math.floor((ts - t0) / interval))
        if bin_id >= 0:
            bins[bin_id] += size_bytes

    return bins


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pcap-a", required=True)
    parser.add_argument("--pcap-b", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--direction", choices=["down", "up"], default="down")
    args = parser.parse_args()

    filters = DOWN_FILTERS if args.direction == "down" else UP_FILTERS

    packets_a = read_packets(args.pcap_a, filters["a"])
    packets_b = read_packets(args.pcap_b, filters["b"])

    all_packets = packets_a + packets_b
    if not all_packets:
        raise SystemExit("No packets found. Check pcap files or display filters.")

    t0 = min(ts for ts, _ in all_packets)

    bins_a = aggregate(packets_a, t0, args.interval)
    bins_b = aggregate(packets_b, t0, args.interval)

    max_bin = 0
    if bins_a:
        max_bin = max(max_bin, max(bins_a.keys()))
    if bins_b:
        max_bin = max(max_bin, max(bins_b.keys()))

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time_s", "pathA_Mbps", "pathB_Mbps", "total_Mbps"])

        for i in range(max_bin + 1):
            time_s = i * args.interval
            bytes_a = bins_a.get(i, 0)
            bytes_b = bins_b.get(i, 0)

            path_a_mbps = bytes_a * 8 / 1_000_000 / args.interval
            path_b_mbps = bytes_b * 8 / 1_000_000 / args.interval
            total_mbps = path_a_mbps + path_b_mbps

            writer.writerow([
                f"{time_s:.3f}",
                f"{path_a_mbps:.6f}",
                f"{path_b_mbps:.6f}",
                f"{total_mbps:.6f}",
            ])

    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
