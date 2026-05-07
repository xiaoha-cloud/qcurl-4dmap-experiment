#!/usr/bin/env python3
"""
Minimal 2-path Mininet topology for qcurl-4dmap-experiment.

Hosts:
  h1 (client): 10.0.1.1/24 on eth0, 10.0.2.1/24 on eth1
  h2 (server): 10.0.1.2/24 on eth0, 10.0.2.2/24 on eth1

Links:
  Path A: h1 <-> s1 <-> h2  (lower delay)
  Path B: h1 <-> s2 <-> h2  (higher delay, optional loss)

Usage:
  Interactive (default):
    sudo python3 scripts/mininet/mp_topo.py

  One-shot experiment (server on h2, pull+push on h1, logs saved):
    sudo python3 scripts/mininet/mp_topo.py --run-exp
    sudo python3 scripts/mininet/mp_topo.py --run-exp --timeout 90 --input-flv ~/Videos/push_input.flv
    # Under sudo, ~/Videos resolves to the invoking user's home (SUDO_USER), not /root.

  Paper Fig.7-like baseline scenario:
    sudo python3 scripts/mininet/mp_topo.py --scenario fig7 --run-exp --utility-mode baseline
    sudo python3 scripts/mininet/mp_topo.py --list-scenarios

  Phase 2 (dynamic perturbation; use one mode per run):
    sudo python3 scripts/mininet/mp_topo.py --run-exp --timeout 300 \\
      --utility-mode T --dynamic-bw-profile scripts/mininet/bw_profile.example.env
    # Route A: online (wT,wD,wL) on simplex, ~200s media, capacity steps at 0/50/100s (see bw_profile.route_a_200s.env).
    #   Copy new_video_200s.mp4 to ~/Videos/ and run:
    #   If the publisher rejects MP4, remux once: ffmpeg -i new_video_200s.mp4 -c copy ~/Videos/new_video_200s.flv
    sudo python3 scripts/mininet/mp_topo.py --run-exp --timeout 220 \\
      --utility-mode learn --log-control --dynamic-bw-profile scripts/mininet/bw_profile.route_a_200s.env \\
      --input-flv ~/Videos/new_video_200s.mp4
    sudo python3 scripts/mininet/mp_topo.py --run-exp --timeout 120 \\
      --dynamic-delay-profile scripts/mininet/delay_profile.example.env
    sudo python3 scripts/mininet/mp_topo.py --run-exp --timeout 120 \\
      --dynamic-loss-profile scripts/mininet/loss_profile.example.env

  Group runs under one session directory (see run_experiment_matrix.sh):
    sudo python3 scripts/mininet/mp_topo.py --run-exp --log-parent logs_exp/session_20260402_001 \\
      --run-label phase1_default_T
    # → logs_exp/session_20260402_001/phase1_default_T/  (files still named *_<RUN_ID>.log)
"""


import argparse
import os
import pwd
import re
import shlex
import shutil
import signal
import time

# Mininet is imported lazily in main() so `python3 mp_topo.py --list-scenarios`
# works on machines without Mininet installed.

# Project root is two directories above this script (scripts/mininet/mp_topo.py -> root)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MININET_DIR = os.path.join(ROOT, "scripts", "mininet")
TC_DELAY_SCRIPT = os.path.join(MININET_DIR, "tc_delay_steps.sh")
TC_LOSS_SCRIPT = os.path.join(MININET_DIR, "tc_loss_steps.sh")
TC_BW_SCRIPT = os.path.join(MININET_DIR, "tc_bw_steps.sh")

# Static TCLink presets: path A = h1–s1–h2 (10.0.1.0/24), path B = h1–s2–h2 (10.0.2.0/24).
# Each path: (bw Mbps, delay string, loss %). Independent of 4D-MAP utility-mode selection.
SCENARIOS = {
    # Paper Fig.7-like baseline:
    #   Link1(path_a): 20Mbps, 40ms, 0%
    #   Link2(path_b): 20Mbps, 20ms, 0.001%
    # Combine with dynamic TBF on h2-eth1 (server egress) + bw_profile.fig7_200s.env so
    # pull (h2→h1) media is shaped; h1-eth1 egress would only limit ACKs, not goodput.
    "fig7": {
        "path_a": (20, "40ms", 0),
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
    tc_proc = None
    tc_log_path = None
    tc_log_f = None
    tcpdump_a = None
    tcpdump_b = None
    tcpdump_a_log = None
    tcpdump_b_log = None

    run_id = time.strftime("%Y%m%d_%H%M%S")

    log_parent = getattr(args, "log_parent", None)
    run_label = getattr(args, "run_label", None)
    if log_parent:
        parent = resolve_repo_path(log_parent)
    else:
        parent = os.path.join(ROOT, "logs_exp")

    if run_label:
        subdir = sanitize_run_label(run_label)
    else:
        subdir = f"vm_run_{run_id}"

    logdir = os.path.join(parent, subdir)
    os.makedirs(logdir, exist_ok=True)

    pcap_dir = os.path.join(logdir, "pcap")
    throughput_dir = os.path.join(logdir, "throughput")
    os.makedirs(pcap_dir, exist_ok=True)
    os.makedirs(throughput_dir, exist_ok=True)

    pcap_a = os.path.join(pcap_dir, f"pathA_h1_{run_id}.pcap")
    pcap_b = os.path.join(pcap_dir, f"pathB_h1_{run_id}.pcap")

    videos_dir = os.path.join(effective_home(), "Videos")
    os.makedirs(videos_dir, exist_ok=True)

    outfile = os.path.join(videos_dir, f"pulled_{run_id}.flv")
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

    tcpdump_a_log = open(os.path.join(pcap_dir, f"tcpdump_pathA_{run_id}.log"), "w")
    tcpdump_b_log = open(os.path.join(pcap_dir, f"tcpdump_pathB_{run_id}.log"), "w")

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

    time.sleep(1)

    _log("exp", f"RUN_ID  = {run_id}")
    _log("exp", f"LOGDIR  = {logdir}")
    if log_parent or run_label:
        _log("exp", f"log_parent = {log_parent!r}  run_label = {run_label!r}")
    scen = getattr(args, "scenario", "fig7")
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
    if bw_prof:
        prof_path = expand_user_path(bw_prof)
        if not os.path.isfile(prof_path):
            _log("error", f"bw profile not found: {prof_path}")
            return
        if not os.path.isfile(TC_BW_SCRIPT):
            _log("error", f"tc_bw_steps.sh not found: {TC_BW_SCRIPT}")
            return
        tc_log_path = os.path.join(logdir, f"tc_bw_{run_id}.log")
        tc_log_f = open(tc_log_path, "w")
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
        tc_log_path = os.path.join(logdir, f"tc_delay_{run_id}.log")
        tc_log_f = open(tc_log_path, "w")
        cmd = f"bash {shlex.quote(TC_DELAY_SCRIPT)} {shlex.quote(prof_path)}"
        _log("tc", f"starting delay steps on h1 → {tc_log_path}")
        _log("tc", f"profile = {prof_path}")
        tc_proc = h1.popen(cmd, shell=True, stdout=tc_log_f, stderr=tc_log_f)
    elif loss_prof:
        prof_path = expand_user_path(loss_prof)
        if not os.path.isfile(prof_path):
            _log("error", f"loss profile not found: {prof_path}")
            return
        if not os.path.isfile(TC_LOSS_SCRIPT):
            _log("error", f"tc_loss_steps.sh not found: {TC_LOSS_SCRIPT}")
            return
        tc_log_path = os.path.join(logdir, f"tc_loss_{run_id}.log")
        tc_log_f = open(tc_log_path, "w")
        cmd = f"bash {shlex.quote(TC_LOSS_SCRIPT)} {shlex.quote(prof_path)}"
        _log("tc", f"starting loss steps on h1 → {tc_log_path}")
        _log("tc", f"profile = {prof_path}")
        tc_proc = h1.popen(cmd, shell=True, stdout=tc_log_f, stderr=tc_log_f)

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
        srv_log = os.path.join(logdir, f"iperf_server_{run_id}.log")
        cli_log = os.path.join(logdir, f"iperf_client_{run_id}.log")
        srv_f = open(srv_log, "w")
        cli_f = open(cli_log, "w")
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
    um = getattr(args, "utility_mode", "T")

    # ---- Start server on h2 ------------------------------------------------
    server_log_path = os.path.join(logdir, f"server_{run_id}.log")
    server_log = open(server_log_path, "w")
    server_cmd = f"{env_prefix} {server_bin} -protocol=quic -au=false"
    _log("server", f"starting on h2 → {server_log_path}")
    server_proc = h2.popen(
        f"cd {server_dir} && {server_cmd}",
        stdout=server_log, stderr=server_log, shell=True,
    )
    time.sleep(3)

    # ---- Start pull on h1 --------------------------------------------------
    pull_log_path = os.path.join(logdir, f"pull_{run_id}.log")
    pull_log = open(pull_log_path, "w")
    open(outfile, "w").close()  # touch
    # 4dmap (main.go) takes the rtmp URL as the last os.Args element; all flags (including
    # -log-control) must come before the URL, or the client prints "unsupport" and exits.
    lc = " -log-control" if getattr(args, "log_control", False) else ""
    pull_cmd = (
        f"export RUN_ID={shlex.quote(run_id)} && cd {ROOT} && {env_prefix} {client_bin}"
        f" -type=true -protocol=quic -multi=true -sch=rr"
        f" -run-id={shlex.quote(run_id)} -utility-mode={shlex.quote(um)}"
        f" -experiment-input={shlex.quote(outfile)}"
        f"{lc}"
        f" -file={shlex.quote(outfile)} rtmp://10.0.1.2/live/test"
    )
    _log("pull", f"starting on h1 → {pull_log_path}")
    pull_proc = h1.popen(
        pull_cmd,
        stdout=pull_log, stderr=pull_log, shell=True,
    )
    time.sleep(3)

    # ---- Start push on h1 --------------------------------------------------
    push_log_path = os.path.join(logdir, f"push_{run_id}.log")
    push_log = open(push_log_path, "w")
    push_cmd = (
        f"export RUN_ID={shlex.quote(run_id)} && cd {ROOT} && {env_prefix} {client_bin}"
        f" -type=false -protocol=quic -multi=true -sch=rr"
        f" -run-id={shlex.quote(run_id)} -utility-mode={shlex.quote(um)}"
        f" -experiment-input={shlex.quote(input_flv)}"
        f"{lc}"
        f" -file={shlex.quote(input_flv)} rtmp://10.0.1.2/live/test"
    )
    _log("push", f"starting on h1 → {push_log_path}")
    push_proc = h1.popen(
        push_cmd,
        stdout=push_log, stderr=push_log, shell=True,
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

        time.sleep(poll_sec)

    # ---- Teardown ----------------------------------------------------------
    _log("exp", "stopping all processes...")
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
    for proc in procs:
        try:
            proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
    time.sleep(2)
    for proc in procs:
        try:
            proc.send_signal(signal.SIGKILL)
        except Exception:
            pass

    for f in [server_log, pull_log, push_log]:
        f.flush()
        f.close()
    if tc_log_f is not None:
        tc_log_f.flush()
        tc_log_f.close()
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

    analyzer = os.path.join(ROOT, "scripts", "analyze", "pcap_throughput.py")
    throughput_csv = os.path.join(throughput_dir, "throughput_down_10s.csv")
    tshark_summary = os.path.join(throughput_dir, "tshark_summary_down_10s.log")
    tshark_err = os.path.join(throughput_dir, "tshark_summary_down_10s.err")
    if shutil.which("tshark") and os.path.isfile(analyzer):
        _log("tshark", f"generating throughput csv -> {throughput_csv}")
        try:
            import subprocess
            with open(tshark_summary, "w") as out_f, open(tshark_err, "w") as err_f:
                subprocess.run(
                    [
                        "python3", analyzer,
                        "--pcap-a", pcap_a,
                        "--pcap-b", pcap_b,
                        "--out", throughput_csv,
                        "--interval", "10",
                        "--direction", "down",
                    ],
                    stdout=out_f,
                    stderr=err_f,
                    check=False,
                )
        except Exception as e:
            _log("tshark", f"failed to generate throughput csv: {e}")
    else:
        _log("tshark", "skip throughput csv: tshark or analyzer script not found")

    _log("exp", f"done! logs saved to {logdir}")
    _log("exp", "--- quick check commands ---")
    _log("exp", f"grep '[m]monitor path=' {pull_log_path} | head -30")
    _log("exp", f"grep '[utility]'        {pull_log_path} | head -30")
    if tc_log_path:
        _log("exp", f"tc timeline log: {tc_log_path}")


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
        "--utility-mode", default="T",
        help="4D-MAP -utility-mode: T, D, L, auto (adaptive), learn (pg weights), or baseline (disables utility controller)",
    )
    parser.add_argument(
        "--log-control", action="store_true",
        help="enable [control] ACK/LOSS cwnd logs (sets -log-control and QUIC_GO_LOG_CONTROL=1; very verbose)",
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
        help="Phase 2: path-B loss steps only (tc_loss_steps.sh); requires --run-exp; mutually exclusive with --dynamic-delay-profile",
    )
    parser.add_argument(
        "--log-parent",
        metavar="DIR",
        default=None,
        help=(
            "Place this run under ROOT/DIR (or absolute DIR). "
            "Default parent is logs_exp. Subdir is vm_run_<RUN_ID> unless --run-label is set."
        ),
    )
    parser.add_argument(
        "--run-label",
        metavar="NAME",
        default=None,
        help=(
            "Folder name under --log-parent (sanitized) instead of vm_run_<RUN_ID>. "
            "Log files still use RUN_ID in their names. Example: phase1_default_T."
        ),
    )
    args = parser.parse_args()

    if (args.dynamic_bw_profile or args.dynamic_delay_profile or args.dynamic_loss_profile) and not args.run_exp:
        parser.error("--dynamic-bw-profile / --dynamic-delay-profile / --dynamic-loss-profile require --run-exp")

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
