#!/usr/bin/env python3

import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

import pandas as pd

import qaccess_delay_loss_eval_analyze as evaluation


class DelayLossEvaluationTest(unittest.TestCase):
    def test_runtime_path_b_uses_endpoint_mapping(self):
        frame = pd.DataFrame([
            {"path_id": 1, "remote_endpoint": "10.0.1.1:5000"},
            {"path_id": 3, "remote_endpoint": "10.0.2.1:5001"},
        ])
        selected = evaluation._path_b_rows(frame, path_col="path_id")
        self.assertEqual(selected["path_id"].tolist(), [3])

    def test_pull_log_path_b_uses_path_two(self):
        frame = pd.DataFrame([{"path": 1}, {"path": 2}])
        selected = evaluation._path_b_rows(frame)
        self.assertEqual(selected["path"].tolist(), [2])

    def test_improvement_sign_depends_on_metric_direction(self):
        frame = pd.DataFrame([
            {
                "method": "baseline",
                "window": "90-150",
                "owd_ms_mean": 80.0,
                "secondary_total_quic_wire_mbps_mean": 10.0,
                "path_b_share_pct_mean": 60.0,
            },
            {
                "method": "delay_qaccess_d",
                "window": "90-150",
                "owd_ms_mean": 60.0,
                "secondary_total_quic_wire_mbps_mean": 12.0,
                "path_b_share_pct_mean": 50.0,
            },
        ])
        result = evaluation.build_improvement_table(frame, "delay_qaccess_d")
        improvements = result.set_index("metric")["improvement_pct"]
        self.assertAlmostEqual(improvements["owd_ms_mean"], 25.0)
        self.assertAlmostEqual(
            improvements["secondary_total_quic_wire_mbps_mean"], 20.0,
        )
        share = result[result.metric == "path_b_share_pct_mean"].iloc[0]
        self.assertAlmostEqual(share["change_percentage_points"], -10.0)

    def test_parse_tc_qdisc_log_accepts_overlimits_without_comma(self):
        text = """1785740134.270442528
iface=h2-eth1 profile_kind=delay
qdisc htb 5: root refcnt 5 r2q 10 default 0x1 direct_packets_stat 0 ver 3.17 direct_qlen 1000
 Sent 320 bytes 4 pkt (dropped 0, overlimits 0 requeues 0)
 backlog 0b 0p requeues 0
qdisc netem 10: parent 5:1 limit 1000 delay 20ms seed 947112343233105958
 Sent 320 bytes 4 pkt (dropped 0, overlimits 0 requeues 0)
 backlog 0b 0p requeues 0
1785740224.270442528
iface=h2-eth1 profile_kind=delay
qdisc htb 5: root refcnt 5 r2q 10 default 0x1 direct_packets_stat 0 ver 3.17 direct_qlen 1000
 Sent 640 bytes 8 pkt (dropped 0, overlimits 1 requeues 0)
 backlog 0b 0p requeues 0
qdisc netem 10: parent 5:1 limit 1000 delay 80ms seed 947112343233105958
 Sent 640 bytes 8 pkt (dropped 0, overlimits 0 requeues 0)
 backlog 400b 3p requeues 0
"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "tc_qdisc_stats_pathB.log"
            path.write_text(text)
            parsed = evaluation._parse_tc_qdisc_log(path, "baseline")
        self.assertEqual(parsed["tc_configured_delay_ms"].tolist(), [20.0, 80.0])
        self.assertEqual(parsed["tc_backlog_pkts"].tolist(), [0, 3])
        self.assertEqual(parsed["tc_interface"].tolist(), ["h2-eth1", "h2-eth1"])


if __name__ == "__main__":
    unittest.main()
