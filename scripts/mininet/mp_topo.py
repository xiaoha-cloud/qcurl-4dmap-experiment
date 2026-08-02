#!/usr/bin/env python3
"""
Fig.7-style 2-path Mininet topology for Q ACCeSS evaluation (qcurl-4dmap-experiment).

Hosts:
  h1 (client): 10.0.1.1/24 on eth0, 10.0.2.1/24 on eth1
  h2 (server): 10.0.1.2/24 on eth0, 10.0.2.2/24 on eth1

Links (scenario fig7):
  Path A: h1 <-> s1 <-> h2  (10.0.1.0/24)
  Path B: h1 <-> s2 <-> h2  (10.0.2.0/24)

Each --run-exp captures pcaps on h1-eth0/h1-eth1, derives throughput CSVs, and writes
role logs under --log-parent / --run-label. Optional --dynamic-bw-profile applies TBF
steps on server egress (h2-eth1) so pull goodput follows the Fig.7 capacity trace.

Usage:
  Interactive:
    sudo python3 scripts/mininet/mp_topo.py

  List scenarios:
    sudo python3 scripts/mininet/mp_topo.py --list-scenarios

  Baseline:
    sudo python3 scripts/mininet/mp_topo.py --run-exp \\
      --scenario fig7 \\
      --utility-mode baseline \\
      --timeout 420 \\
      --log-parent logs_exp/session7 \\
      --run-label baseline

  Q ACCeSS data collection:
    sudo python3 scripts/mininet/mp_topo.py --run-exp \\
      --scenario fig7 \\
      --utility-mode qaccess_collect \\
      --timeout 420 \\
      --dynamic-bw-profile scripts/mininet/bw_profile.fig7_200s.env \\
      --log-parent logs_exp/session7 \\
      --run-label qaccess_collect

  Q ACCeSS T mode:
    sudo python3 scripts/mininet/mp_topo.py --run-exp \\
      --scenario fig7 \\
      --utility-mode qaccess_t \\
      --timeout 420 \\
      --dynamic-bw-profile scripts/mininet/bw_profile.fig7_200s.env \\
      --log-parent logs_exp/session7 \\
      --run-label qaccess_t

  # Under sudo, ~/Videos resolves to SUDO_USER's home, not /root.
"""


import argparse
import csv
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import time
from datetime import datetime, timezone

# Mininet is imported lazily in main() so `python3 mp_topo.py --list-scenarios`
# works on machines without Mininet installed.

# Project root is two directories above this script (scripts/mininet/mp_topo.py -> root)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MININET_DIR = os.path.join(ROOT, "scripts", "mininet")
TC_DELAY_SCRIPT = os.path.join(MININET_DIR, "tc_delay_steps.sh")
TC_LOSS_SCRIPT = os.path.join(MININET_DIR, "tc_loss_steps.sh")
TC_DETERIORATION_SCRIPT = os.path.join(MININET_DIR, "tc_deterioration_steps.sh")
TC_BW_SCRIPT = os.path.join(MININET_DIR, "tc_bw_steps.sh")

# Static TCLink presets: path A = h1–s1–h2 (10.0.1.0/24), path B = h1–s2–h2 (10.0.2.0/24).
# Each path: (bw Mbps, delay string, loss %). Independent of 4D-MAP utility-mode selection.
SCENARIOS = {
    # Clean loss validation baseline:
    #   Link1(path_a): 20Mbps, 0ms, 0%
    #   Link2(path_b): 20Mbps, 0ms, 0%
    # Combine with dynamic TBF on h2-eth1 (server egress) + bw_profile.fig7_200s.env so
    # pull (h2→h1) media is shaped; h1-eth1 egress would only limit ACKs, not goodput.
    "fig7": {
        "path_a": (20, "0ms", 0),
        "path_b": (20, "0ms", 0),
    },
    # Fig.8-style combined deterioration (heterogeneous paths):
    #   Path A: 20 Mbps, 40 ms, 0%
    #   Path B: 30 Mbps, 20 ms, 0% static; combined delay+loss steps on h2-eth1 via
    #   combined_deterioration_profile.env (90–100s: 80 ms + 0.05% loss).
    "fig8": {
        "path_a": (20, "40ms", 0),
        "path_b": (30, "20ms", 0),
    },
    # Path B stress validation: lower Path A static cap so multipath uses Path B more.
    # Same loss/delay as fig7; combine with bw_profile.fig7_200s.env on h2-eth1 (Path B egress).
    "pathB_stress": {
        "path_a": (10, "40ms", 0),
        "path_b": (20, "20ms", 0.001),
    },
    # Stronger variant: Path A 5 Mbps so Path B should carry more before 100s TBF drop (30→10 Mbps).
    "pathB_stress_strong": {
        "path_a": (5, "40ms", 0),
        "path_b": (20, "20ms", 0.001),
    },
}


def scenario_link_kwargs(name):
    """Return dict of TCLink kwargs for path_a and path_b from SCENARIOS[name]."""
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario: {name!r} (valid: {', '.join(sorted(SCENARIOS))})")
    cfg = SCENARIOS[name]
    pa, pb = cfg["path_a"], cfg["path_b"]
    ka = {"bw": pa[0], "delay": pa[1], "loss": pa[2]}
    kb = {"bw": pb[0], "delay": pb[1], "loss": pb[2]}
    ka.update(cfg.get("path_a_extra", {}))
    kb.update(cfg.get("path_b_extra", {}))
    return {"path_a": ka, "path_b": kb}


def print_scenarios():
    for key in sorted(SCENARIOS):
        cfg = SCENARIOS[key]
        pa, pb = cfg["path_a"], cfg["path_b"]
        xa = cfg.get("path_a_extra") or {}
        xb = cfg.get("path_b_extra") or {}
        extra = ""
        if xa or xb:
            extra = f"  extras: path_a={xa!r} path_b={xb!r}"
        print(
            f"  {key:8}  path_a: bw={pa[0]}Mbps delay={pa[1]} loss={pa[2]}%  |  "
            f"path_b: bw={pb[0]}Mbps delay={pb[1]} loss={pb[2]}%{extra}"
        )


def effective_home():
    """Home directory for ~/ paths when this script is run as root via sudo."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            return pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def expand_user_path(path):
    """Expand ~ using the invoking user's home (not /root) under sudo."""
    if not path:
        return path
    if path.startswith("~/"):
        return os.path.join(effective_home(), path[2:])
    if path == "~":
        return effective_home()
    return os.path.expanduser(path)


def resolve_repo_path(path):
    """Resolve a path: absolute paths unchanged; relative paths are under ROOT."""
    path = expand_user_path(path)
    if os.path.isabs(path):
        return path
    return os.path.join(ROOT, path)


def _iface_for_profile(prof_path: str) -> str:
    """Return IFACE= from a tc profile, or an empty string if unavailable."""
    try:
        with open(prof_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line.startswith("IFACE="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _tc_bw_host_for_profile(prof_path: str) -> str:
    """
    Mininet host on which to run ``tc_bw_steps.sh``: ``IFACE=`` in the profile must
    exist in that node’s network namespace. For **pull (download)** experiments,
    path-b caps on **server egress** (e.g. h2-eth1) actually limit h2→h1 throughput;
    the same TBF on h1-eth1 would only limit client egress (ACKs), not the media stream.
    """
    try:
        with open(prof_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                if line.startswith("IFACE="):
                    iface = line.split("=", 1)[1].strip()
                    if iface.startswith("h2-"):
                        return "h2"
                    break
    except OSError:
        pass
    return "h1"


def sanitize_run_label(name):
    """Allow only safe folder-name characters; empty → 'run'."""
    name = (name or "").strip()
    if not name:
        return "run"
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)
    name = name.strip("._-") or "run"
    return name


def _scenario_tag_for_dir(name):
    """Compact scenario tag used in default run directory names."""
    if name == "fig7":
        return "figure7"
    if name == "fig8":
        return "fig8"
    return sanitize_run_label(name)


def _write_combined_log(output_path, inputs):
    """Concatenate multiple logs into one file with section headers."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8", errors="replace") as out_f:
        for label, in_path in inputs:
            out_f.write(f"===== BEGIN {label}: {in_path} =====\n")
            if os.path.isfile(in_path):
                with open(in_path, "r", encoding="utf-8", errors="replace") as in_f:
                    out_f.write(in_f.read())
            else:
                out_f.write("(missing log file)\n")
            out_f.write(f"\n===== END {label} =====\n\n")


def _env_flag(name, default=False):
    v = os.environ.get(name, "")
    if v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name, default):
    v = os.environ.get(name, "")
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _open_log_file(path, save_logs):
    """Open a log destination; /dev/null keeps runs quiet when logs are disabled."""
    if save_logs:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    return open(path if save_logs else os.devnull, "w")


def _append_timeline(timeline_path, event, **fields):
    """Append one JSONL timeline row (mandatory experiment evidence)."""
    if not timeline_path:
        return
    os.makedirs(os.path.dirname(timeline_path), exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        **fields,
    }
    with open(timeline_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _split_throughput_csv(src_csv, out_a_csv, out_b_csv, out_total_csv):
    """Split merged throughput CSV into pathA/pathB/total single-column CSV files."""
    rows = []
    with open(src_csv, "r", newline="", encoding="utf-8", errors="replace") as in_f:
        reader = csv.DictReader(in_f)
        for row in reader:
            rows.append(row)

    def write_single(out_path, key, header):
        with open(out_path, "w", newline="", encoding="utf-8") as out_f:
            writer = csv.writer(out_f)
            writer.writerow(["time_s", header])
            for row in rows:
                writer.writerow([row.get("time_s", ""), row.get(key, "")])

    write_single(out_a_csv, "pathA_Mbps", "pathA_down_Mbps")
    write_single(out_b_csv, "pathB_Mbps", "pathB_down_Mbps")
    write_single(out_total_csv, "total_Mbps", "total_down_Mbps")


def _mp_topo_class():
    """Build MPTopo after Mininet imports (keeps --list-scenarios usable without Mininet)."""
    from mininet.link import TCLink
    from mininet.topo import Topo

    class MPTopo(Topo):
        def __init__(self, scenario="fig7", **params):
            self.scenario = scenario
            super().__init__(**params)

        def build(self):
            h1 = self.addHost("h1")
            h2 = self.addHost("h2")
            s1 = self.addSwitch("s1")
            s2 = self.addSwitch("s2")

            kw = scenario_link_kwargs(self.scenario)
            ka, kb = kw["path_a"], kw["path_b"]

            self.addLink(h1, s1, cls=TCLink, **ka)
            self.addLink(s1, h2, cls=TCLink, **ka)

            self.addLink(h1, s2, cls=TCLink, **kb)
            self.addLink(s2, h2, cls=TCLink, **kb)

    return MPTopo


def setup_addresses_and_rules(net):
    h1 = net.get("h1")
    h2 = net.get("h2")

    # Clear defaults set by Mininet and configure explicit addresses.
    h1.cmd("ip addr flush dev h1-eth0")
    h1.cmd("ip addr flush dev h1-eth1")
    h2.cmd("ip addr flush dev h2-eth0")
    h2.cmd("ip addr flush dev h2-eth1")

    h1.cmd("ip addr add 10.0.1.1/24 dev h1-eth0")
    h1.cmd("ip addr add 10.0.2.1/24 dev h1-eth1")
    h2.cmd("ip addr add 10.0.1.2/24 dev h2-eth0")
    h2.cmd("ip addr add 10.0.2.2/24 dev h2-eth1")

    h1.cmd("ip link set h1-eth0 up")
    h1.cmd("ip link set h1-eth1 up")
    h2.cmd("ip link set h2-eth0 up")
    h2.cmd("ip link set h2-eth1 up")

    # Main routing table: direct routes for both subnets (ip addr flush removes
    # auto-generated connected routes, so we must re-add them explicitly).
    h1.cmd("ip route add 10.0.1.0/24 dev h1-eth0 scope link")
    h1.cmd("ip route add 10.0.2.0/24 dev h1-eth1 scope link")
    h2.cmd("ip route add 10.0.1.0/24 dev h2-eth0 scope link")
    h2.cmd("ip route add 10.0.2.0/24 dev h2-eth1 scope link")

    # Source-based routing (minimal rules to keep both paths usable).
    h1.cmd("ip rule add from 10.0.1.1 table 101")
    h1.cmd("ip rule add from 10.0.2.1 table 102")
    h1.cmd("ip route add 10.0.1.0/24 dev h1-eth0 scope link table 101")
    h1.cmd("ip route add 10.0.2.0/24 dev h1-eth1 scope link table 102")
    h1.cmd("ip route add default scope global nexthop via 10.0.1.2 dev h1-eth0")

    h2.cmd("ip rule add from 10.0.1.2 table 201")
    h2.cmd("ip rule add from 10.0.2.2 table 202")
    h2.cmd("ip route add 10.0.1.0/24 dev h2-eth0 scope link table 201")
    h2.cmd("ip route add 10.0.2.0/24 dev h2-eth1 scope link table 202")
    h2.cmd("ip route add default scope global nexthop via 10.0.1.1 dev h2-eth0")


def _log(tag, msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}][{tag}] {msg}", flush=True)


def run_experiment(net, args):
    h1 = net.get("h1")
    h2 = net.get("h2")
    save_logs = not getattr(args, "disable_logs", False)
    tc_proc = None
    tc_log_path = None
    tc_log_f = None
    tcpdump_a = None
    tcpdump_b = None
    tcpdump_a_log = None
    tcpdump_b_log = None
    tc_qdisc_proc = None
    tc_qdisc_log = None
    tc_qdisc_log_path = None
    tc_qdisc_iface = ""
    tc_qdisc_node = ""

    run_id = time.strftime("%Y%m%d_%H%M%S")

    log_parent = getattr(args, "log_parent", None)
    run_label = getattr(args, "run_label", None)
    if log_parent:
        parent = resolve_repo_path(log_parent)
    else:
        parent = os.path.join(ROOT, "logs_exp")

    scen = getattr(args, "scenario", "fig7")
    um = getattr(args, "utility_mode", "qaccess_t")
    if run_label:
        subdir = sanitize_run_label(run_label)
    else:
        subdir = sanitize_run_label(f"{_scenario_tag_for_dir(scen)}_um_{um}")

    logdir = os.path.join(parent, subdir)
    os.makedirs(logdir, exist_ok=True)

    logs_dir = os.path.join(logdir, "logs")
    pcap_dir = os.path.join(logdir, "pcaps")
    throughput_dir = os.path.join(logdir, "csv")
    qoe_dir = os.path.join(logdir, "qoe")
    os.makedirs(logs_dir, exist_ok=True)
    os.makedirs(pcap_dir, exist_ok=True)
    os.makedirs(throughput_dir, exist_ok=True)

    timeline_path = os.path.join(logdir, f"experiment_timeline_{run_id}.jsonl")

    pcap_a = os.path.join(pcap_dir, f"pathA_h1_{run_id}.pcap")
    pcap_b = os.path.join(pcap_dir, f"pathB_h1_{run_id}.pcap")

    videos_dir = os.path.join(effective_home(), "Videos")
    os.makedirs(videos_dir, exist_ok=True)
    save_output_flv = _env_flag("SAVE_OUTPUT_FLV", False)
    keep_pcap = _env_flag("KEEP_PCAP", False)
    qoe_enabled = _env_flag("QACCESS_ENABLE_QOE_LOG", False)
    throughput_interval = _env_float("THROUGHPUT_INTERVAL", 1.0)
    if qoe_enabled:
        os.makedirs(qoe_dir, exist_ok=True)
    if save_output_flv:
        outfile = os.path.join(logdir, f"output_{run_id}.flv")
    else:
        outfile = os.devnull
        _log("exp", "SAVE_OUTPUT_FLV=0: pull output discarded to /dev/null")
    input_flv = (
        expand_user_path(args.input_flv)
        if args.input_flv
        else os.path.join(videos_dir, "push_input.flv")
    )

    server_bin = os.path.join(ROOT, "qserver")
    client_bin = os.path.join(ROOT, "4dmap")
    server_dir = os.path.join(ROOT, "server")

    # Pre-flight checks
    if not os.path.isfile(input_flv):
        _log("error", f"input media file not found: {input_flv}")
        _log("error", "place file at ~/Videos/push_input.flv (or e.g. new_video_200s.mp4) or pass --input-flv <path>")
        return
    if not os.path.isfile(server_bin):
        _log("error", f"qserver binary not found: {server_bin}")
        _log("error", "build with: GO111MODULE=on go build -o qserver ./server")
        return
    if not os.path.isfile(client_bin):
        _log("error", f"4dmap binary not found: {client_bin}")
        _log("error", "build with: GO111MODULE=on go build -o 4dmap .")
        return

    tcpdump_a_log_path = os.path.join(logs_dir, f"tcpdump_pathA_{run_id}.log")
    tcpdump_b_log_path = os.path.join(logs_dir, f"tcpdump_pathB_{run_id}.log")
    tcpdump_a_log = _open_log_file(tcpdump_a_log_path, save_logs)
    tcpdump_b_log = _open_log_file(tcpdump_b_log_path, save_logs)

    _log("pcap", f"starting tcpdump path A h1-eth0 -> {pcap_a}")
    tcpdump_a = h1.popen(
        f"tcpdump -U -n -i h1-eth0 -s 0 -w {shlex.quote(pcap_a)} udp",
        stdout=tcpdump_a_log,
        stderr=tcpdump_a_log,
        shell=True,
    )

    _log("pcap", f"starting tcpdump path B h1-eth1 -> {pcap_b}")
    tcpdump_b = h1.popen(
        f"tcpdump -U -n -i h1-eth1 -s 0 -w {shlex.quote(pcap_b)} udp",
        stdout=tcpdump_b_log,
        stderr=tcpdump_b_log,
        shell=True,
    )
    _append_timeline(
        timeline_path,
        "tcpdump_start",
        run_id=run_id,
        run_label=run_label or "",
        pcap_a=pcap_a,
        pcap_b=pcap_b,
    )

    time.sleep(1)

    _log("exp", f"RUN_ID  = {run_id}")
    _log("exp", f"LOGDIR  = {logdir}")
    if not save_logs:
        _log("exp", "runtime logs disabled (--disable-logs); role/tc/tcpdump/tshark logs go to /dev/null")
    if log_parent or run_label:
        _log("exp", f"log_parent = {log_parent!r}  run_label = {run_label!r}")
    cfg = SCENARIOS[scen]
    pa, pb = cfg["path_a"], cfg["path_b"]
    _log(
        "exp",
        f"scenario = {scen}  path_a bw={pa[0]}Mbps delay={pa[1]} loss={pa[2]}%  "
        f"path_b bw={pb[0]}Mbps delay={pb[1]} loss={pb[2]}%",
    )
    _log("exp", f"HOME    = {effective_home()} (for ~/Videos when using sudo)")
    _log("exp", f"outfile = {outfile}")
    _log("exp", f"input   = {input_flv}")
    _log("exp", f"timeout = {args.timeout}s")

    bw_prof = getattr(args, "dynamic_bw_profile", None)
    delay_prof = getattr(args, "dynamic_delay_profile", None)
    loss_prof = getattr(args, "dynamic_loss_profile", None)
    deterioration_prof = getattr(args, "dynamic_deterioration_profile", None)
    if bw_prof:
        prof_path = expand_user_path(bw_prof)
        if not os.path.isfile(prof_path):
            _log("error", f"bw profile not found: {prof_path}")
            return
        if not os.path.isfile(TC_BW_SCRIPT):
            _log("error", f"tc_bw_steps.sh not found: {TC_BW_SCRIPT}")
            return
        tc_log_path = os.path.join(logs_dir, f"tc_bw_{run_id}.log")
        tc_log_f = _open_log_file(tc_log_path, save_logs)
        cmd = f"bash {shlex.quote(TC_BW_SCRIPT)} {shlex.quote(prof_path)}"
        tc_node = _tc_bw_host_for_profile(prof_path)
        tc_h = net.get(tc_node)
        _log("tc", f"starting bandwidth steps on {tc_node} → {tc_log_path}")
        _log("tc", f"profile = {prof_path}")
        tc_proc = tc_h.popen(cmd, shell=True, stdout=tc_log_f, stderr=tc_log_f)
    elif delay_prof:
        prof_path = expand_user_path(delay_prof)
        if not os.path.isfile(prof_path):
            _log("error", f"delay profile not found: {prof_path}")
            return
        if not os.path.isfile(TC_DELAY_SCRIPT):
            _log("error", f"tc_delay_steps.sh not found: {TC_DELAY_SCRIPT}")
            return
        tc_log_path = os.path.join(logs_dir, f"tc_delay_{run_id}.log")
        tc_log_f = _open_log_file(tc_log_path, save_logs)
        cmd = f"bash {shlex.quote(TC_DELAY_SCRIPT)} {shlex.quote(prof_path)}"
        tc_node = _tc_bw_host_for_profile(prof_path)
        tc_h = net.get(tc_node)
        _log("tc", f"starting delay steps on {tc_node} → {tc_log_path}")
        _log("tc", f"profile = {prof_path}")
        tc_proc = tc_h.popen(cmd, shell=True, stdout=tc_log_f, stderr=tc_log_f)
    elif loss_prof:
        prof_path = expand_user_path(loss_prof)
        if not os.path.isfile(prof_path):
            _log("error", f"loss profile not found: {prof_path}")
            return
        if not os.path.isfile(TC_LOSS_SCRIPT):
            _log("error", f"tc_loss_steps.sh not found: {TC_LOSS_SCRIPT}")
            return
        tc_log_path = os.path.join(logs_dir, f"tc_loss_{run_id}.log")
        tc_log_f = _open_log_file(tc_log_path, save_logs)
        cmd = f"bash {shlex.quote(TC_LOSS_SCRIPT)} {shlex.quote(prof_path)}"
        tc_node = _tc_bw_host_for_profile(prof_path)
        tc_h = net.get(tc_node)
        _log("tc", f"starting loss steps on {tc_node} → {tc_log_path}")
        _log("tc", f"profile = {prof_path}")
        tc_proc = tc_h.popen(cmd, shell=True, stdout=tc_log_f, stderr=tc_log_f)
        tc_qdisc_iface = _iface_for_profile(prof_path)
        tc_qdisc_node = tc_node
    elif deterioration_prof:
        prof_path = expand_user_path(deterioration_prof)
        if not os.path.isfile(prof_path):
            _log("error", f"deterioration profile not found: {prof_path}")
            return
        if not os.path.isfile(TC_DETERIORATION_SCRIPT):
            _log("error", f"tc_deterioration_steps.sh not found: {TC_DETERIORATION_SCRIPT}")
            return
        tc_log_path = os.path.join(logs_dir, f"tc_deterioration_{run_id}.log")
        os.makedirs(logs_dir, exist_ok=True)
        tc_log_f = open(tc_log_path, "w", encoding="utf-8")
        timeline_q = shlex.quote(timeline_path)
        cmd = (
            f"TIMELINE_JSONL={timeline_q} bash {shlex.quote(TC_DETERIORATION_SCRIPT)} "
            f"{shlex.quote(prof_path)}"
        )
        tc_node = _tc_bw_host_for_profile(prof_path)
        tc_h = net.get(tc_node)
        _log("tc", f"starting combined deterioration steps on {tc_node} → {tc_log_path}")
        _log("tc", f"profile = {prof_path}")
        _append_timeline(
            timeline_path,
            "tc_script_start",
            run_id=run_id,
            run_label=run_label or "",
            tc_node=tc_node,
            tc_log=tc_log_path,
            profile=prof_path,
        )
        tc_proc = tc_h.popen(cmd, shell=True, stdout=tc_log_f, stderr=tc_log_f)

    if loss_prof and tc_qdisc_iface and tc_qdisc_node:
        tc_qdisc_log_path = os.path.join(logs_dir, f"tc_qdisc_stats_pathB_{run_id}.log")
        tc_qdisc_log = _open_log_file(tc_qdisc_log_path, save_logs)
        qdisc_cmd = (
            "while true; do "
            "date +%s.%N; "
            f"tc -s -d qdisc show dev {shlex.quote(tc_qdisc_iface)}; "
            "sleep 1; "
            "done"
        )
        tc_qdisc_host = net.get(tc_qdisc_node)
        _log("tc", f"starting qdisc sampler on {tc_qdisc_node}:{tc_qdisc_iface} -> {tc_qdisc_log_path}")
        _append_timeline(
            timeline_path,
            "tc_qdisc_sampler_start",
            run_id=run_id,
            run_label=run_label or "",
            tc_node=tc_qdisc_node,
            iface=tc_qdisc_iface,
            tc_qdisc_log=tc_qdisc_log_path,
        )
        tc_qdisc_proc = tc_qdisc_host.popen(qdisc_cmd, shell=True, stdout=tc_qdisc_log, stderr=tc_qdisc_log)

    iperf_procs = []
    iperf_aux_files = []
    if getattr(args, "bg_iperf", False):
        if not shutil.which("iperf3"):
            _log("error", "iperf3 not found; install iperf3 or omit --bg-iperf")
            if tc_log_f is not None:
                tc_log_f.close()
            return
        port = int(getattr(args, "bg_iperf_port", 55201))
        bw = getattr(args, "bg_iperf_bw", "10M")
        path_key = getattr(args, "bg_iperf_path", "b")
        dst = "10.0.1.2" if path_key == "a" else "10.0.2.2"
        srv_log = os.path.join(logs_dir, f"iperf_server_{run_id}.log")
        cli_log = os.path.join(logs_dir, f"iperf_client_{run_id}.log")
        srv_f = _open_log_file(srv_log, save_logs)
        cli_f = _open_log_file(cli_log, save_logs)
        iperf_aux_files.extend([srv_f, cli_f])
        _log("exp", f"bg-iperf3 server on h2 port {port} → {srv_log}")
        iperf_procs.append(
            h2.popen(
                f"iperf3 -s -p {port}",
                stdout=srv_f,
                stderr=srv_f,
                shell=True,
            )
        )
        time.sleep(1)
        _log("exp", f"bg-iperf3 client h1 → {dst}:{port} -b {bw} (600s) → {cli_log}")
        iperf_procs.append(
            h1.popen(
                f"iperf3 -c {dst} -p {port} -b {shlex.quote(bw)} -t 600",
                stdout=cli_f,
                stderr=cli_f,
                shell=True,
            )
        )

    # Write run_id for later reference
    with open(os.path.join(ROOT, ".last_run_id"), "w") as f:
        f.write(run_id + "\n")

    env_prefix = "QUIC_GO_LOG_LEVEL=info"
    if getattr(args, "log_control", False):
        env_prefix += " QUIC_GO_LOG_CONTROL=1"

    qoe_env_base = ""
    if qoe_enabled:
        qoe_experiment_name = os.environ.get("QACCESS_QOE_EXPERIMENT_NAME", _scenario_tag_for_dir(scen))
        qoe_video_every_n = os.environ.get("QACCESS_QOE_LOG_VIDEO_EVERY_N", "1")
        qoe_log_audio = os.environ.get("QACCESS_QOE_LOG_AUDIO", "0")
        qoe_env_base = (
            f"QACCESS_ENABLE_QOE_LOG=1 "
            f"QACCESS_QOE_LOG_DIR={shlex.quote(os.path.abspath(qoe_dir))} "
            f"QACCESS_QOE_SESSION_ID={shlex.quote(run_id)} "
            f"QACCESS_QOE_EXPERIMENT_NAME={shlex.quote(qoe_experiment_name)} "
            f"QACCESS_QOE_VARIANT={shlex.quote(run_label or subdir)} "
            f"QACCESS_QOE_LOG_VIDEO_EVERY_N={shlex.quote(qoe_video_every_n)} "
            f"QACCESS_QOE_LOG_AUDIO={shlex.quote(qoe_log_audio)} "
        )
        _log("qoe", f"enabled dir={qoe_dir} video_every_n={qoe_video_every_n} audio={qoe_log_audio}")

    def qoe_env_for(role):
        if not qoe_env_base:
            return ""
        return qoe_env_base + f"QACCESS_QOE_ROLE={shlex.quote(role)} "

    # ---- Start server on h2 ------------------------------------------------
    server_log_path = os.path.join(logs_dir, f"server_{run_id}.log")
    server_log = _open_log_file(server_log_path, save_logs)
    phase2_state_dir = os.path.abspath(os.environ.get("QACCESS_PHASE2_STATE_DIR", os.path.join(ROOT, "derived")))
    server_phase2_enabled = um in ("qaccess_t", "qaccess_d", "qaccess_l")
    server_cmd = (
        f"QACCESS_PHASE2_ENABLED={int(server_phase2_enabled)} "
        f"QACCESS_PHASE2_OWNER=0 QACCESS_ENDPOINT_ROLE=server_listener "
        f"QACCESS_UTILITY_MODE={shlex.quote(um)} "
        f"QACCESS_PHASE2_STATE_DIR={shlex.quote(phase2_state_dir)} "
        f"QACCESS_EXPERIMENT_RUN_ID={shlex.quote(run_id)} {qoe_env_for('server')} {env_prefix} "
        f"{server_bin} -protocol=quic -au=false"
    )
    _log("server", f"starting on h2 → {server_log_path}")
    server_proc = h2.popen(
        f"cd {server_dir} && {server_cmd}",
        stdout=server_log, stderr=server_log, shell=True,
    )
    _append_timeline(
        timeline_path, "phase2_identity", endpoint_role="server_listener",
        phase2_enabled=server_phase2_enabled, phase2_owner=False,
        controller_created=False, phase2_state_dir=phase2_state_dir,
    )
    time.sleep(3)

    # ---- Start pull on h1 --------------------------------------------------
    pull_log_path = os.path.join(logs_dir, f"pull_{run_id}.log")
    pull_log = _open_log_file(pull_log_path, save_logs)
    if save_output_flv:
        open(outfile, "w").close()  # touch
    # 4dmap (main.go) takes the rtmp URL as the last os.Args element; all flags (including
    # -log-control) must come before the URL, or the client prints "unsupport" and exits.
    lc = " -log-control" if getattr(args, "log_control", False) else ""
    pull_cmd = (
        f"export RUN_ID={shlex.quote(run_id)} && cd {ROOT} && "
        f"QACCESS_PHASE2_ENABLED=0 QACCESS_PHASE2_OWNER=0 QACCESS_ENDPOINT_ROLE=client_pull_receiver {qoe_env_for('puller')} {env_prefix} {client_bin}"
        f" -type=true -protocol=quic -multi=true -sch=rr"
        f" -run-id={shlex.quote(run_id)} -utility-mode={shlex.quote(um)}"
        f" -experiment-input={shlex.quote(outfile)}"
        f"{lc}"
        f" -file={shlex.quote(outfile)} rtmp://10.0.1.2/live/test"
    )
    _log("phase2", "endpoint_role=client_pull_receiver phase2_enabled=0 phase2_owner=0 mutation_allowed=0")
    _log("pull", f"starting on h1 → {pull_log_path}")
    pull_proc = h1.popen(
        pull_cmd,
        stdout=pull_log, stderr=pull_log, shell=True,
    )
    _append_timeline(
        timeline_path, "phase2_identity", endpoint_role="client_pull_receiver",
        phase2_enabled=False, phase2_owner=False, controller_created=False,
        phase2_state_dir=phase2_state_dir,
    )
    time.sleep(3)

    # ---- Start push on h1 --------------------------------------------------
    push_log_path = os.path.join(logs_dir, f"push_{run_id}.log")
    push_log = _open_log_file(push_log_path, save_logs)
    push_cmd = (
        f"export RUN_ID={shlex.quote(run_id)} && cd {ROOT} && "
        f"QACCESS_PHASE2_ENABLED=0 QACCESS_PHASE2_OWNER=0 QACCESS_ENDPOINT_ROLE=client_push_publisher {qoe_env_for('pusher')} {env_prefix} {client_bin}"
        f" -type=false -protocol=quic -multi=true -sch=rr"
        f" -run-id={shlex.quote(run_id)} -utility-mode={shlex.quote(um)}"
        f" -experiment-input={shlex.quote(input_flv)}"
        f"{lc}"
        f" -file={shlex.quote(input_flv)} rtmp://10.0.1.2/live/test"
    )
    _log("phase2", "endpoint_role=client_push_publisher phase2_enabled=0 phase2_owner=0 mutation_allowed=0")
    _log("push", f"starting on h1 → {push_log_path}")
    push_proc = h1.popen(
        push_cmd,
        stdout=push_log, stderr=push_log, shell=True,
    )
    _append_timeline(
        timeline_path, "phase2_identity", endpoint_role="client_push_publisher",
        phase2_enabled=False, phase2_owner=False, controller_created=False,
        phase2_state_dir=phase2_state_dir,
    )
    _append_timeline(
        timeline_path,
        "push_start",
        run_id=run_id,
        run_label=run_label or "",
        input_flv=input_flv,
    )

    # ---- Watchdog loop -----------------------------------------------------
    timeout = args.timeout
    grace_sec = 20
    poll_sec = 2
    max_stable_rounds = 15

    _log("watchdog", f"grace period {grace_sec}s ...")
    time.sleep(grace_sec)

    start_time = time.time()
    last_size = 0
    stable_rounds = 0

    while True:
        elapsed = time.time() - start_time

        if elapsed >= (timeout - grace_sec):
            _log("watchdog", f"timeout {timeout}s reached, stopping")
            break

        if push_proc.poll() is not None:
            _log("watchdog", f"push exited naturally at {elapsed:.0f}s")
            break

        if save_output_flv:
            try:
                size = os.path.getsize(outfile)
            except OSError:
                size = 0

            if size > last_size:
                _log("watchdog", f"outfile growing: {size} B (+{size - last_size} B), elapsed {elapsed:.0f}s")
                last_size = size
                stable_rounds = 0
            else:
                stable_rounds += 1
                if stable_rounds >= max_stable_rounds:
                    _log("watchdog", f"output stalled for {stable_rounds * poll_sec}s, stopping")
                    break
        else:
            # SAVE_OUTPUT_FLV=0: rely on timeout / push process exit only.
            pass

        time.sleep(poll_sec)

    # ---- Teardown ----------------------------------------------------------
    _log("exp", "stopping all processes...")
    tc_was_running_at_cleanup = False
    if tc_proc is not None:
        tc_status = tc_proc.poll()
        if tc_status is None:
            tc_was_running_at_cleanup = True
            _log("tc", "deterioration process was still running; terminating during cleanup")
        elif tc_status == 0:
            _log("tc", "deterioration process completed normally; exit_status=0")
        else:
            _log("tc", f"deterioration process exited early; exit_status={tc_status}")
        _append_timeline(
            timeline_path,
            "tc_process_status",
            run_id=run_id,
            run_label=run_label or "",
            state=("running_at_cleanup" if tc_status is None else "exited"),
            exit_status=("" if tc_status is None else str(tc_status)),
        )
    _log("pcap", "stopping tcpdump...")
    for p in [tcpdump_a, tcpdump_b]:
        try:
            p.send_signal(signal.SIGINT)
        except Exception:
            pass
    time.sleep(1)
    for p in [tcpdump_a, tcpdump_b]:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass

    procs = list(iperf_procs) + [push_proc, pull_proc, server_proc]
    if tc_proc is not None:
        procs.append(tc_proc)
    if tc_qdisc_proc is not None:
        procs.append(tc_qdisc_proc)
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
    time.sleep(2)
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.send_signal(signal.SIGKILL)
        except Exception:
            pass
    if tc_proc is not None and tc_was_running_at_cleanup:
        try:
            tc_status = tc_proc.wait(timeout=1)
        except Exception:
            tc_status = tc_proc.poll()
        _log("tc", f"deterioration process terminated by cleanup; exit_status={tc_status}")

    for f in [server_log, pull_log, push_log]:
        f.flush()
        f.close()
    if tc_log_f is not None:
        tc_log_f.flush()
        tc_log_f.close()
    if tc_qdisc_log is not None:
        tc_qdisc_log.flush()
        tc_qdisc_log.close()
    for f in [tcpdump_a_log, tcpdump_b_log]:
        if f is not None:
            try:
                f.flush()
                f.close()
            except Exception:
                pass
    for f in iperf_aux_files:
        try:
            f.flush()
            f.close()
        except Exception:
            pass

    throughput_ok = False
    analyzer = os.path.join(ROOT, "scripts", "analyze", "pcap_throughput.py")
    throughput_csv = os.path.join(throughput_dir, f"throughput_all_down_{run_id}.csv")
    throughput_a_csv = os.path.join(throughput_dir, f"throughput_pathA_down_{run_id}.csv")
    throughput_b_csv = os.path.join(throughput_dir, f"throughput_pathB_down_{run_id}.csv")
    throughput_total_csv = os.path.join(throughput_dir, f"throughput_total_down_{run_id}.csv")
    tshark_summary = os.path.join(logs_dir, f"tshark_summary_down_10s_{run_id}.log")
    tshark_err = os.path.join(logs_dir, f"tshark_summary_down_10s_{run_id}.err")
    if shutil.which("tshark") and os.path.isfile(analyzer):
        _log("tshark", f"generating per-second throughput csvs in {logdir}")
        try:
            import subprocess
            for pcap_path in (pcap_a, pcap_b):
                try:
                    os.chmod(pcap_path, 0o644)
                except OSError:
                    pass
            with _open_log_file(tshark_summary, save_logs) as out_f, _open_log_file(tshark_err, save_logs) as err_f:
                subprocess.run(
                    [
                        "python3", analyzer,
                        "--pcap-a", pcap_a,
                        "--pcap-b", pcap_b,
                        "--per-path-dir", logdir,
                        "--interval", str(throughput_interval),
                        "--direction", "down",
                    ],
                    stdout=out_f,
                    stderr=err_f,
                    check=False,
                )
            required = (
                "throughput_all_down.csv",
                "throughput_pathA_down.csv",
                "throughput_pathB_down.csv",
            )
            throughput_ok = all(
                os.path.isfile(os.path.join(logdir, name)) and os.path.getsize(os.path.join(logdir, name)) > 0
                for name in required
            )
            if throughput_ok:
                _log("tshark", f"throughput csvs -> {logdir}/throughput_*_down.csv")
            else:
                with _open_log_file(tshark_summary, save_logs) as out_f, _open_log_file(tshark_err, save_logs) as err_f:
                    subprocess.run(
                        [
                            "python3", analyzer,
                            "--pcap-a", pcap_a,
                            "--pcap-b", pcap_b,
                            "--out", throughput_csv,
                            "--interval", str(max(throughput_interval, 1.0)),
                            "--direction", "down",
                        ],
                        stdout=out_f,
                        stderr=err_f,
                        check=False,
                    )
                if os.path.isfile(throughput_csv):
                    _split_throughput_csv(
                        throughput_csv,
                        throughput_a_csv,
                        throughput_b_csv,
                        throughput_total_csv,
                    )
                    throughput_ok = True
                    _log("tshark", f"legacy split csv -> {throughput_a_csv}, {throughput_b_csv}")
        except Exception as e:
            _log("tshark", f"failed to generate throughput csv: {e}")
    else:
        _log("tshark", "skip throughput csv: tshark or analyzer script not found")

    if throughput_ok and not keep_pcap:
        for pcap_path in (pcap_a, pcap_b):
            try:
                if os.path.isfile(pcap_path):
                    os.remove(pcap_path)
            except OSError as e:
                _log("pcap", f"failed to delete {pcap_path}: {e}")
        _log("pcap", "KEEP_PCAP=0: deleted pcaps after throughput CSV generation")
    elif keep_pcap:
        _log("pcap", "KEEP_PCAP=1: retaining pcaps")

    if save_logs:
        _log("exp", "combined log disabled; role logs are retained separately")
    elif tc_log_path and os.path.isfile(tc_log_path):
        _log("exp", f"tc deterioration log -> {tc_log_path}")
    if timeline_path and os.path.isfile(timeline_path):
        _log("exp", f"experiment timeline -> {timeline_path}")

    _log("exp", f"done! outputs saved to {logdir}")
    if save_logs:
        _log("exp", "--- quick check commands ---")
        _log("exp", f"grep '[m]monitor path=' {pull_log_path} | head -30")
        _log("exp", f"grep '[utility]'        {pull_log_path} | head -30")
        if tc_log_path:
            _log("exp", f"tc timeline log: {tc_log_path}")
        if tc_qdisc_log_path:
            _log("exp", f"tc qdisc sampler log: {tc_qdisc_log_path}")


def main():
    parser = argparse.ArgumentParser(
        description="2-path Mininet topology for qcurl-4dmap-experiment"
    )
    parser.add_argument(
        "--scenario",
        default="fig7",
        choices=sorted(SCENARIOS.keys()),
        help="static link preset (TCLink bw/delay/loss per path); default is Paper Fig.7-like baseline",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="print SCENARIOS presets and exit (no Mininet)",
    )
    parser.add_argument(
        "--run-exp", action="store_true",
        help="run one-shot experiment (server on h2, pull+push on h1) instead of interactive CLI",
    )
    parser.add_argument(
        "--timeout", type=int, default=90,
        help="experiment hard timeout in seconds (default: 90)",
    )
    parser.add_argument(
        "--input-flv", default=None,
        help="path to input media for push, e.g. .flv or .mp4 (default: ~/Videos/push_input.flv)",
    )
    parser.add_argument(
        "--utility-mode", default="qaccess_t",
        help="Q-ACCeSS -utility-mode: baseline/off, qaccess_collect, qaccess_t, qaccess_d, or qaccess_l",
    )
    parser.add_argument(
        "--log-control", action="store_true",
        help="enable [control] ACK/LOSS cwnd logs (sets -log-control and QUIC_GO_LOG_CONTROL=1; very verbose)",
    )
    parser.add_argument(
        "--disable-logs", action="store_true",
        help="discard server/pull/push/tc/tcpdump/tshark logs and skip combined_*.log (pcaps/csv/output FLV are still produced)",
    )
    parser.add_argument(
        "--bg-iperf", action="store_true",
        help=(
            "Start iperf3 server on h2 and client on h1 (UDP/TCP per iperf3 default: TCP) for the run "
            "duration, to add cross-traffic on path A or B (--bg-iperf-path). Requires iperf3 in PATH."
        ),
    )
    parser.add_argument(
        "--bg-iperf-bw", default="10M",
        help="iperf3 -b limit for background client (default: 10M)",
    )
    parser.add_argument(
        "--bg-iperf-path", choices=("a", "b"), default="b",
        help="h2 destination IP: a=10.0.1.2 b=10.0.2.2 (default: b)",
    )
    parser.add_argument(
        "--bg-iperf-port", type=int, default=55201,
        help="iperf3 listening port on h2 (default: 55201)",
    )
    dyn = parser.add_mutually_exclusive_group()
    dyn.add_argument(
        "--dynamic-bw-profile",
        metavar="PATH",
        default=None,
        help="Phase 2: bandwidth steps on one interface (tc_bw_steps.sh); requires --run-exp; mutually exclusive with --dynamic-delay-profile / --dynamic-loss-profile",
    )
    dyn.add_argument(
        "--dynamic-delay-profile",
        metavar="PATH",
        default=None,
        help="Phase 2: path-B delay steps only (tc_delay_steps.sh); requires --run-exp; mutually exclusive with --dynamic-loss-profile",
    )
    dyn.add_argument(
        "--dynamic-loss-profile",
        metavar="PATH",
        default=None,
        help="Phase 2: path-B loss steps only (tc_loss_steps.sh); requires --run-exp; mutually exclusive with other dynamic profiles",
    )
    dyn.add_argument(
        "--dynamic-deterioration-profile",
        metavar="PATH",
        default=None,
        help="Fig.8: combined delay+loss steps on one interface (tc_deterioration_steps.sh); requires --run-exp",
    )
    parser.add_argument(
        "--log-parent",
        metavar="DIR",
        default=None,
        help=(
            "Place this run under ROOT/DIR (or absolute DIR). "
            "Default parent is logs_exp. Subdir defaults to <scenario>_um_<utility-mode> unless --run-label is set "
            "(e.g. fig7_baseline, fig7_qaccess_t)."
        ),
    )
    parser.add_argument(
        "--run-label",
        metavar="NAME",
        default=None,
        help=(
            "Folder name under --log-parent (sanitized) instead of the default <scenario>_um_<utility-mode>. "
            "Log files still use RUN_ID in their names. Example: fig7_baseline, fig7_qaccess_t."
        ),
    )
    args = parser.parse_args()

    if (
        args.dynamic_bw_profile
        or args.dynamic_delay_profile
        or args.dynamic_loss_profile
        or args.dynamic_deterioration_profile
    ) and not args.run_exp:
        parser.error(
            "--dynamic-bw-profile / --dynamic-delay-profile / --dynamic-loss-profile / "
            "--dynamic-deterioration-profile require --run-exp"
        )

    if args.list_scenarios:
        print("SCENARIOS (path_a = 10.0.1.x path, path_b = 10.0.2.x path):")
        print_scenarios()
        return

    from mininet.cli import CLI
    from mininet.link import TCLink
    from mininet.net import Mininet
    from mininet.node import OVSBridge

    MPTopo = _mp_topo_class()
    topo = MPTopo(scenario=args.scenario)
    net = Mininet(topo=topo, link=TCLink, switch=OVSBridge, controller=None, autoSetMacs=True)
    net.start()
    setup_addresses_and_rules(net)

    print("\n[mp_topo] Topology is up.")
    print(f"[mp_topo] scenario={args.scenario} (see SCENARIOS in mp_topo.py or --list-scenarios)")

    if args.run_exp:
        try:
            run_experiment(net, args)
        finally:
            net.stop()
    else:
        print("[mp_topo] Quick checks:")
        print("  mininet> h1 /bin/ping -c 2 10.0.1.2")
        print("  mininet> h1 /bin/ping -c 2 10.0.2.2")
        print("  mininet> h1 ip addr show")
        print("  mininet> h2 ip addr show")
        print("")
        CLI(net)
        net.stop()


if __name__ == "__main__":
    main()
