#!/usr/bin/env python3
"""
Minimal 2-path Mininet topology for qcurl-4dmap-experiment.

Hosts:
  h1 (client): 10.0.1.1/24 on eth0, 10.0.2.1/24 on eth1
  h2 (server): 10.0.1.2/24 on eth0, 10.0.2.2/24 on eth1

Links:
  Path A: h1 <-> s1 <-> h2  (lower delay)
  Path B: h1 <-> s2 <-> h2  (higher delay, optional loss)
"""

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import OVSBridge
from mininet.topo import Topo


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


def main():
    topo = MPTopo()
    net = Mininet(topo=topo, link=TCLink, switch=OVSBridge, controller=None, autoSetMacs=True)
    net.start()
    setup_addresses_and_rules(net)

    print("\n[mp_topo] Topology is up.")
    print("[mp_topo] Quick checks:")
    print("  mininet> h1 ping -c 2 10.0.1.2")
    print("  mininet> h1 ping -c 2 10.0.2.2")
    print("  mininet> h1 ip addr")
    print("  mininet> h2 ip addr")
    print("")

    CLI(net)
    net.stop()


if __name__ == "__main__":
    main()
