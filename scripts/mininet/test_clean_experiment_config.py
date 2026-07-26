#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MININET_DIR = Path(__file__).resolve().parent
ROOT = MININET_DIR.parents[1]
sys.path.insert(0, str(MININET_DIR))

from clean_experiment_config import (  # noqa: E402
    CLEAN_INTERFACE,
    CLEAN_PATH,
    ConfigurationError,
    build_configuration,
    format_loss_percent,
    parse_profile,
    validate_scenario,
)
from mp_topo import _experiment_exit_status, scenario_link_kwargs  # noqa: E402


PROFILES = {
    "bandwidth": MININET_DIR / "bw_profile.clean_20_30_10_200s.env",
    "delay": MININET_DIR / "delay_profile.clean_40_80_40_200s.env",
    "loss": MININET_DIR / "loss_profile.clean_0_1_0_200s.env",
}
HISTORICAL_SHA256 = {
    "bw_profile.fig7_200s.env": "3d569c4a7dd42818f8739a15cf30f4515e6116ffb075e474047f67fd4174727a",
    "delay_profile.pathB_200s.env": "0861df89ae5990be00a2e6255e18058f8c2e2944a88f755a8f54ea3066cb73c9",
    "loss_profile.pathB_200s.env": "774afdac0d6059edefe924900e725529a489b90773d1dba7967c15a263645f86",
    "run_qaccess_t_fig7_baseline_vs_dynamic_hybrid.sh": "96ecfa5191810e9e1c3507ab51b502c284f1f723829f18a4a6b539cc181ccffb",
    "run_qaccess_d_delay_deterioration_eval.sh": "7e4fd880f16a96a32298f9783ad7ec21c802fab97147c84cbaaa60e8f02c94b5",
    "run_qaccess_l_loss_deterioration_eval.sh": "18dd62b3372329e11435afa2ce2540cf9987ba6e137b545240257b62b5579cf2",
    "run_qaccess_t_no_deterioration_eval.sh": "ba220084803c0a90dce19e95d5dc961f76fe86cefada3a3d30d9bfbbc35068df",
}


class CleanExperimentConfigTests(unittest.TestCase):
    def test_clean_scenario_resolves_equal_paths(self) -> None:
        paths = validate_scenario("clean_equal_paths")
        self.assertEqual(paths, {"path_a": CLEAN_PATH, "path_b": CLEAN_PATH})

    def test_fig7_values_remain_unchanged(self) -> None:
        links = scenario_link_kwargs("fig7")
        self.assertEqual(links["path_a"], {"bw": 20, "delay": "40ms", "loss": 0})
        self.assertEqual(links["path_b"], {"bw": 20, "delay": "20ms", "loss": 0.001})

    def test_clean_profile_failure_is_fail_closed(self) -> None:
        self.assertEqual(_experiment_exit_status("clean_equal_paths", 1), 1)
        self.assertEqual(_experiment_exit_status("clean_equal_paths", 0), 0)
        self.assertEqual(_experiment_exit_status("clean_equal_paths", None), 0)
        self.assertEqual(_experiment_exit_status("fig7", 1), 0)

    def test_clean_profiles_parse_exact_transitions(self) -> None:
        expected = {
            "bandwidth": [20, 30, 10],
            "delay": [40, 80, 40],
            "loss": [0, 1, 0],
        }
        for kind, path in PROFILES.items():
            with self.subTest(kind=kind):
                parsed = parse_profile(path, kind)
                self.assertEqual(parsed["interface"], CLEAN_INTERFACE)
                self.assertEqual(parsed["transitions_sec"], [0, 50, 100])
                self.assertEqual(parsed["values"], expected[kind])

    def test_loss_values_are_tc_percentages(self) -> None:
        parsed = parse_profile(PROFILES["loss"], "loss")
        self.assertEqual(parsed["tc_values"], ["0%", "1%", "0%"])
        self.assertEqual(format_loss_percent(1), "1%")
        self.assertEqual(format_loss_percent(0.01), "0.01%")

    def test_incorrect_interface_is_rejected(self) -> None:
        self._assert_invalid_profile("IFACE=h1-eth1\n0 20\n50 30\n100 10\n", "bandwidth")

    def test_malformed_transition_order_is_rejected(self) -> None:
        self._assert_invalid_profile("IFACE=h2-eth1\n0 20\n100 30\n50 10\n", "bandwidth")

    def test_unsupported_extra_fields_are_rejected(self) -> None:
        self._assert_invalid_profile("IFACE=h2-eth1\n0 20 extra\n50 30\n100 10\n", "bandwidth")

    def test_stability_configuration_has_no_profile(self) -> None:
        config = build_configuration("clean_equal_paths", "none", None)
        self.assertEqual(config["dynamic_dimension"], "none")
        self.assertIsNone(config["profile_path"])
        self.assertEqual(config["transitions_sec"], [])
        self.assertEqual(config["profile_values"], [])

    def test_dynamic_metadata_is_auditable(self) -> None:
        config = build_configuration("clean_equal_paths", "delay", PROFILES["delay"])
        self.assertEqual(config["experiment_family"], "clean_controlled")
        self.assertEqual(config["gate_policy"], "legacy")
        self.assertEqual(config["trigger_mode"], "legacy_buffer_full")
        self.assertEqual(config["impairment_direction"], "server_to_client_path_b_egress")
        self.assertEqual(config["profile_sha256"], hashlib.sha256(PROFILES["delay"].read_bytes()).hexdigest())
        self.assertEqual(config["profile_text"], PROFILES["delay"].read_text(encoding="utf-8"))

    def test_historical_profiles_and_runner_defaults_are_byte_identical(self) -> None:
        for name, expected_digest in HISTORICAL_SHA256.items():
            with self.subTest(name=name):
                digest = hashlib.sha256((MININET_DIR / name).read_bytes()).hexdigest()
                self.assertEqual(digest, expected_digest)

    def test_clean_runner_defaults_and_check_only_forwarding(self) -> None:
        expected = {
            "run_qaccess_t_clean_bandwidth_eval.sh": (
                "QACCESS_PROFILE_KIND=bandwidth",
                "QACCESS_TARGET_MODE=delta_bw_1s",
                "QACCESS_SESSION_KIND=clean_bandwidth",
            ),
            "run_qaccess_d_clean_delay_eval.sh": (
                "QACCESS_PROFILE_KIND=delay",
                "QACCESS_TARGET_MODE=delta_owd_1s",
                "QACCESS_SESSION_KIND=clean_delay",
            ),
            "run_qaccess_l_clean_loss_eval.sh": (
                "QACCESS_PROFILE_KIND=loss",
                "QACCESS_TARGET_MODE=loss_risk_1s",
                "QACCESS_SESSION_KIND=clean_loss",
            ),
            "run_qaccess_t_clean_stability_eval.sh": (
                "QACCESS_PROFILE_KIND=none",
                "QACCESS_TARGET_MODE=delta_bw_1s",
                "QACCESS_SESSION_KIND=clean_stability",
            ),
        }
        for name, fragments in expected.items():
            with self.subTest(name=name):
                text = (MININET_DIR / name).read_text(encoding="utf-8")
                self.assertIn('SCENARIO="${SCENARIO:-clean_equal_paths}"', text)
                self.assertIn('"$@"', text)
                self.assertIn("QACCESS_GATE_POLICY", text)
                for fragment in fragments:
                    self.assertIn(fragment, text)
        bandwidth_runner = (MININET_DIR / "run_qaccess_t_clean_bandwidth_eval.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("TC_BW_FIXED_DELAY_MS=40", bandwidth_runner)
        self.assertIn("TC_BW_FIXED_LOSS_PERCENT=0", bandwidth_runner)

    def test_clean_runner_objective_trigger_mapping(self) -> None:
        expected = {
            "run_qaccess_t_clean_bandwidth_eval.sh": ("objective_t", "throughput"),
            "run_qaccess_d_clean_delay_eval.sh": ("objective_d", "delay"),
            "run_qaccess_l_clean_loss_eval.sh": ("objective_l", "loss"),
            "run_qaccess_t_clean_stability_eval.sh": ("objective_t", "throughput"),
        }
        for name, fragments in expected.items():
            text = (MININET_DIR / name).read_text(encoding="utf-8")
            for fragment in fragments:
                self.assertIn(fragment, text)

    def test_invalid_trigger_objective_combination_fails(self) -> None:
        runner = MININET_DIR / "run_qaccess_t_combined_deterioration_eval.sh"
        result = subprocess.run(
            [str(runner), "--configuration-only"],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "QACCESS_EXPERIMENT_FAMILY": "clean_controlled",
                "QACCESS_CONTROLLER_VARIANT": "qaccess_d",
                "QACCESS_TARGET_MODE": "delta_owd_1s",
                "QACCESS_WORKER_TARGET_MODE": "delta_owd_1s",
                "QACCESS_PROFILE_KIND": "none",
                "SCENARIO": "clean_equal_paths",
                "QACCESS_GATE_POLICY": "objective_aware",
                "QACCESS_TRIGGER_MODE": "objective_d",
                "QACCESS_GATE_OBJECTIVE": "throughput",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incompatible", result.stderr)

    def test_all_clean_runners_support_configuration_only_validation(self) -> None:
        runners = (
            "run_qaccess_t_clean_bandwidth_eval.sh",
            "run_qaccess_d_clean_delay_eval.sh",
            "run_qaccess_l_clean_loss_eval.sh",
            "run_qaccess_t_clean_stability_eval.sh",
        )
        for runner in runners:
            with self.subTest(runner=runner):
                result = subprocess.run(
                    [str(MININET_DIR / runner), "--configuration-only"],
                    cwd=ROOT,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("clean runner configuration is valid", result.stdout)

    def test_clean_runner_rejects_incorrect_interface(self) -> None:
        self._assert_runner_rejects_profile("IFACE=h1-eth1\n0 20\n50 30\n100 10\n")

    def test_clean_runner_rejects_malformed_ordering(self) -> None:
        self._assert_runner_rejects_profile("IFACE=h2-eth1\n0 20\n100 30\n50 10\n")

    def test_clean_runner_rejects_duration_shorter_than_profile(self) -> None:
        result = subprocess.run(
            [str(MININET_DIR / "run_qaccess_t_clean_bandwidth_eval.sh"), "--configuration-only"],
            cwd=ROOT,
            env={**os.environ, "TIMEOUT": "199", "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("at least 200 seconds", result.stderr)

    def test_clean_bandwidth_uses_tbf_with_fixed_netem_child(self) -> None:
        result, calls = self._run_bandwidth_script(composite=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "qdisc replace dev h2-eth1 root handle 1: tbf rate 20mbit burst 64kbit latency 400ms",
            calls,
        )
        self.assertIn(
            "qdisc replace dev h2-eth1 parent 1:1 handle 10: netem delay 40ms loss 0%",
            calls,
        )
        self.assertEqual(calls.count("root handle 1: tbf"), 3)
        self.assertEqual(calls.count("parent 1:1 handle 10: netem"), 3)
        self.assertIn("-s qdisc show dev h2-eth1", calls)
        self.assertIn("-s class show dev h2-eth1", calls)
        self.assertIn("qdisc_state stage=before_first_step", result.stderr)
        self.assertIn("qdisc_state stage=after_20mbit", result.stderr)
        self.assertIn("verification_ok: root_tbf=1: child_netem=10: parent=1:1", result.stderr)

    def test_legacy_bandwidth_command_remains_root_tbf_only(self) -> None:
        result, calls = self._run_bandwidth_script(composite=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "qdisc replace dev h2-eth1 root tbf rate 20mbit burst 64kbit latency 400ms",
            calls,
        )
        self.assertNotIn("parent 1:1 handle 10: netem", calls)
        self.assertIn("qdisc_show:", result.stderr)
        self.assertIn("qdisc_stats:", result.stderr)

    def test_shared_runner_uses_one_profile_for_both_legs(self) -> None:
        text = (MININET_DIR / "run_qaccess_t_combined_deterioration_eval.sh").read_text(encoding="utf-8")
        self.assertIn('run_one baseline "$BASELINE_LABEL" "$TIMEOUT"', text)
        self.assertIn('run_one "$CONTROLLER_VARIANT" "$DYNAMIC_LABEL" "$ACTIVE_DYNAMIC_TIMEOUT"', text)
        self.assertIn('--dynamic-bw-profile "$DETERIORATION_PROFILE"', text)
        self.assertIn('--dynamic-delay-profile "$DETERIORATION_PROFILE"', text)
        self.assertIn('--dynamic-loss-profile "$DETERIORATION_PROFILE"', text)
        self.assertIn('ACTIVE_DYNAMIC_TIMEOUT="$TIMEOUT"', text)

    def test_historical_runners_remain_legacy_by_default(self) -> None:
        shared = (MININET_DIR / "run_qaccess_t_combined_deterioration_eval.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('GATE_POLICY="${QACCESS_GATE_POLICY:-legacy}"', shared)
        self.assertIn('TRIGGER_MODE="${QACCESS_TRIGGER_MODE:-legacy_buffer_full}"', shared)
        historical = (
            "run_qaccess_d_delay_deterioration_eval.sh",
            "run_qaccess_l_loss_deterioration_eval.sh",
            "run_qaccess_t_baseline_vs_dynamic_hybrid.sh",
            "run_qaccess_t_combined_deterioration_active.sh",
            "run_qaccess_t_combined_deterioration_shadow.sh",
            "run_qaccess_t_fig7_baseline_vs_dynamic_hybrid.sh",
            "run_qaccess_t_no_deterioration_eval.sh",
        )
        for name in historical:
            text = (MININET_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("QACCESS_GATE_POLICY=objective_aware", text, name)
            self.assertNotIn("QACCESS_TRIGGER_MODE=objective_", text, name)

    def _assert_invalid_profile(self, text: str, kind: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.env"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                parse_profile(path, kind)

    def _assert_runner_rejects_profile(self, text: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.env"
            path.write_text(text, encoding="utf-8")
            result = subprocess.run(
                [str(MININET_DIR / "run_qaccess_t_clean_bandwidth_eval.sh"), "--configuration-only"],
                cwd=ROOT,
                env={**os.environ, "BW_PROFILE": str(path), "PYTHONDONTWRITEBYTECODE": "1"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def _run_bandwidth_script(self, *, composite: bool) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as directory:
            temp_dir = Path(directory)
            profile = temp_dir / "profile.env"
            profile.write_text("IFACE=h2-eth1\n0 20\n0 30\n0 10\n", encoding="utf-8")
            call_log = temp_dir / "tc_calls.log"
            state = temp_dir / "tc_state"
            fake_tc = temp_dir / "tc"
            fake_tc.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$TC_CALL_LOG"
if [[ "$*" == *"root handle 1: tbf"* ]]; then
  printf 'root\\n' > "$TC_STATE"
elif [[ "$*" == *"parent 1:1 handle 10: netem"* ]]; then
  printf 'composite\\n' > "$TC_STATE"
elif [[ "$*" == *"root tbf rate"* ]]; then
  printf 'legacy\\n' > "$TC_STATE"
fi
current="$(cat "$TC_STATE" 2>/dev/null || true)"
if [[ "$*" == *"qdisc show dev h2-eth1"* ]]; then
  case "$current" in
    composite)
      printf 'qdisc netem 10: parent 1:1 limit 1000 delay 40ms loss 0%%\\n'
      printf 'qdisc tbf 1: root rate 20Mbit burst 8Kb lat 400ms\\n'
      ;;
    root)
      printf 'qdisc tbf 1: root rate 20Mbit burst 8Kb lat 400ms\\n'
      ;;
    legacy)
      printf 'qdisc tbf 8001: root rate 20Mbit burst 8Kb lat 400ms\\n'
      ;;
    *)
      printf 'qdisc netem 5: root limit 1000 delay 40ms loss 0%%\\n'
      ;;
  esac
elif [[ "$*" == *"class show dev h2-eth1"* ]]; then
  printf 'class tbf 1:1 parent 1: leaf 10: rate 20Mbit burst 8Kb\\n'
fi
""",
                encoding="utf-8",
            )
            fake_tc.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{temp_dir}:{os.environ['PATH']}",
                "TC_CALL_LOG": str(call_log),
                "TC_STATE": str(state),
            }
            if composite:
                environment.update(
                    {"TC_BW_FIXED_DELAY_MS": "40", "TC_BW_FIXED_LOSS_PERCENT": "0"}
                )
            else:
                environment.pop("TC_BW_FIXED_DELAY_MS", None)
                environment.pop("TC_BW_FIXED_LOSS_PERCENT", None)
            result = subprocess.run(
                ["bash", str(MININET_DIR / "tc_bw_steps.sh"), str(profile)],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            calls = call_log.read_text(encoding="utf-8") if call_log.is_file() else ""
            return result, calls


if __name__ == "__main__":
    unittest.main()
