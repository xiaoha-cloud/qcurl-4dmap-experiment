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
"""

import argparse
import os
import pwd
import signal
import time

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import OVSBridge
from mininet.topo import Topo

# Project root is two directories above this script (scripts/mininet/mp_topo.py -> root)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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


class MPTopo(Topo):
    def build(self):
        h1 = self.addHost("h1")
        h2 = self.addHost("h2")
        s1 = self.addSwitch("s1")
        s2 = self.addSwitch("s2")

        # Path A: faster
        self.addLink(h1, s1, cls=TCLink, bw=20, delay="10ms", loss=0)
        self.addLink(s1, h2, cls=TCLink, bw=20, delay="10ms", loss=0)

        # Path B: slower + slight loss to make path characteristics different
        self.addLink(h1, s2, cls=TCLink, bw=10, delay="30ms", loss=0.5)
        self.addLink(s2, h2, cls=TCLink, bw=10, delay="30ms", loss=0.5)


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

    run_id = time.strftime("%Y%m%d_%H%M%S")
    logdir = os.path.join(ROOT, "logs_exp", f"vm_run_{run_id}")
    os.makedirs(logdir, exist_ok=True)

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
        _log("error", f"input FLV not found: {input_flv}")
        _log("error", "place file at ~/Videos/push_input.flv or pass --input-flv <path>")
        return
    if not os.path.isfile(server_bin):
        _log("error", f"qserver binary not found: {server_bin}")
        _log("error", "build with: GO111MODULE=on go build -o qserver ./server")
        return
    if not os.path.isfile(client_bin):
        _log("error", f"4dmap binary not found: {client_bin}")
        _log("error", "build with: GO111MODULE=on go build -o 4dmap .")
        return

    _log("exp", f"RUN_ID  = {run_id}")
    _log("exp", f"LOGDIR  = {logdir}")
    _log("exp", f"HOME    = {effective_home()} (for ~/Videos when using sudo)")
    _log("exp", f"outfile = {outfile}")
    _log("exp", f"input   = {input_flv}")
    _log("exp", f"timeout = {args.timeout}s")

    # Write run_id for later reference
    with open(os.path.join(ROOT, ".last_run_id"), "w") as f:
        f.write(run_id + "\n")

    env_prefix = "QUIC_GO_LOG_LEVEL=info"

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
    pull_cmd = (
        f"{env_prefix} {client_bin} -type=true -protocol=quic -multi=true"
        f" -file={outfile} rtmp://10.0.1.2/live/test"
    )
    _log("pull", f"starting on h1 → {pull_log_path}")
    pull_proc = h1.popen(
        f"cd {ROOT} && {pull_cmd}",
        stdout=pull_log, stderr=pull_log, shell=True,
    )
    time.sleep(3)

    # ---- Start push on h1 --------------------------------------------------
    push_log_path = os.path.join(logdir, f"push_{run_id}.log")
    push_log = open(push_log_path, "w")
    push_cmd = (
        f"{env_prefix} {client_bin} -type=false -protocol=quic -multi=true -sch=rr"
        f" -file={input_flv} rtmp://10.0.1.2/live/test"
    )
    _log("push", f"starting on h1 → {push_log_path}")
    push_proc = h1.popen(
        f"cd {ROOT} && {push_cmd}",
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
    for proc in [push_proc, pull_proc, server_proc]:
        try:
            proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
    time.sleep(2)
    for proc in [push_proc, pull_proc, server_proc]:
        try:
            proc.send_signal(signal.SIGKILL)
        except Exception:
            pass

    for f in [server_log, pull_log, push_log]:
        f.flush()
        f.close()

    _log("exp", f"done! logs saved to {logdir}")
    _log("exp", "--- quick check commands ---")
    _log("exp", f"grep '[m]monitor path=' {pull_log_path} | head -30")
    _log("exp", f"grep '[utility]'        {pull_log_path} | head -30")


def main():
    parser = argparse.ArgumentParser(
        description="2-path Mininet topology for qcurl-4dmap-experiment"
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
        help="path to input FLV file for push (default: ~/Videos/push_input.flv)",
    )
    args = parser.parse_args()

    topo = MPTopo()
    net = Mininet(topo=topo, link=TCLink, switch=OVSBridge, controller=None, autoSetMacs=True)
    net.start()
    setup_addresses_and_rules(net)

    print("\n[mp_topo] Topology is up.")

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
