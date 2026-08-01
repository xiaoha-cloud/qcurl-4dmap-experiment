#!/usr/bin/env python3
"""Collect fixed-coefficient qserver sender samples for offline RF training."""

from __future__ import annotations

import argparse
import csv
import gzip
import itertools
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = REPO / "derived/qaccess_t_qserver_sender/sweeps"
PROFILE = REPO / "scripts/mininet/combined_deterioration_profile_90_150.env"
PROFILE_OPTIONS = {
    "combined": ("--dynamic-deterioration-profile", PROFILE),
    "delay": ("--dynamic-delay-profile", REPO / "scripts/mininet/delay_profile.pathB_200s.env"),
    "delay_clean": ("--dynamic-delay-profile", REPO / "scripts/mininet/delay_profile.clean_40_80_40_200s.env"),
    "loss": ("--dynamic-loss-profile", REPO / "scripts/mininet/loss_profile.pathB_200s.env"),
}
GRID = tuple(itertools.product((0.6, 0.7, 0.8), (0.1, 0.2, 0.3), (0.1, 0.2, 0.3)))
SMOKE = ((0.6, 0.3, 0.1), (0.7, 0.3, 0.2))


def tuple_id(values: tuple[float, float, float]) -> str:
    return "a%s_b%s_g%s" % tuple(f"{value:.1f}".replace(".", "p") for value in values)


def parse_tuples_file(path: Path) -> list[tuple[float, float, float]]:
    rows: list[tuple[float, float, float]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append(tuple(float(row[name]) for name in ("alpha", "beta", "gamma")))
    if not rows:
        raise ValueError(f"no coefficient tuples in {path}")
    if len(set(rows)) != len(rows):
        raise ValueError(f"duplicate coefficient tuple in {path}")
    return rows


def select_tuples(
    *,
    smoke: bool,
    tuples_file: Path | None,
    seed: int,
) -> list[tuple[float, float, float]]:
    """Select run-level fixed tuples; randomize only the complete built-in grid order."""
    if smoke:
        return list(SMOKE)
    if tuples_file is not None:
        return parse_tuples_file(tuples_file)
    rows = list(GRID)
    random.Random(seed).shuffle(rows)
    return rows


def write_coefficients(path: Path, values: tuple[float, float, float], name: str) -> None:
    alpha, beta, gamma = values
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1,
        "default": {"alpha": alpha, "beta": beta, "gamma": gamma},
        "paths": {},
        "source": f"qserver_sender_sweep:{name}",
        "metric": "fixed_collection_coefficients",
    }, indent=2) + "\n", encoding="utf-8")


def runtime_samples_path(state_dir: Path) -> Path:
    """Mirror the explicit Phase2StateDir contract used by qaccess_phase2.go."""
    return state_dir / "qaccess_runtime_samples.csv"


def validate_fixed_samples(
    samples: Path,
    expected: tuple[float, float, float],
) -> dict[str, object]:
    """Fail closed if a supposedly fixed run exported any other coefficients."""
    rows = 0
    path_ids: set[int] = set()
    with samples.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"path_id", "alpha", "beta", "gamma"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise RuntimeError(f"{samples}: missing fixed-run fields {sorted(missing)}")
        for row in reader:
            rows += 1
            observed = tuple(float(row[name]) for name in ("alpha", "beta", "gamma"))
            if any(abs(actual - wanted) > 1e-6 for actual, wanted in zip(observed, expected)):
                raise RuntimeError(
                    f"{samples}: coefficient changed in fixed run; expected={expected} observed={observed}"
                )
            path_ids.add(int(row["path_id"]))
    if rows == 0:
        raise RuntimeError(f"{samples}: no runtime samples")
    return {"runtime_sample_rows": rows, "runtime_sample_path_ids": sorted(path_ids)}


def validate_clean_delay_tc_log(run_dir: Path) -> dict[str, object]:
    """Verify every clean-D step preserved fixed TBF bandwidth and zero loss."""
    logs = sorted((run_dir / "logs").glob("tc_delay_*.log"))
    if len(logs) != 1:
        raise RuntimeError(f"{run_dir}: expected one retained tc_delay log, found {logs}")
    path = logs[0]
    content = path.read_text(encoding="utf-8", errors="replace")
    expected = [
        "profile_step[0] at=0s delay=40ms",
        "profile_step[1] at=50s delay=80ms",
        "profile_step[2] at=100s delay=40ms",
        "composite_qdisc=1 fixed_bw_mbit=20 fixed_loss_percent=0",
        "root_tbf=1: fixed_bw=20mbit",
        "fixed_loss=0%",
        "finished all steps",
    ]
    missing = [item for item in expected if item not in content]
    if content.count("verification_ok:") < 3:
        missing.append("three hierarchy verifications")
    if "verification_failed:" in content:
        missing.append("verification_failed present")
    if missing:
        raise RuntimeError(f"{path}: clean delay qdisc validation failed: {missing}")
    return {"qdisc_profile_valid": True, "tc_log": str(path)}


def profile_transition_times(path: Path) -> tuple[int, int]:
    """Return the first two non-zero profile step times."""
    steps: list[int] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" in line:
            continue
        try:
            at = int(line.split()[0])
        except (IndexError, ValueError) as exc:
            raise ValueError(f"unsupported profile row in {path}: {raw!r}") from exc
        if at > 0:
            steps.append(at)
    if len(steps) < 2:
        raise ValueError(f"profile requires at least two non-zero transitions: {path}")
    return steps[0], steps[1]


def restore_sudo_ownership(path: Path) -> None:
    uid_text, gid_text = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not uid_text or not gid_text:
        return
    uid, gid = int(uid_text), int(gid_text)
    for root, directories, files in os.walk(path):
        os.chown(root, uid, gid)
        for name in directories:
            os.chown(Path(root) / name, uid, gid)
        for name in files:
            os.chown(Path(root) / name, uid, gid)


def owner_identity(audit_path: Path) -> dict:
    owners = []
    if audit_path.is_file():
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("phase2_owner") is True and row.get("controller_created") is True:
                owners.append(row)
    if len(owners) != 1 or owners[0].get("endpoint_role") != "server_downlink_sender":
        raise RuntimeError(f"expected one server_downlink_sender owner, found {owners}")
    return owners[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tuples-file", type=Path)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--timeout", type=int, default=220)
    parser.add_argument("--input-flv", type=Path, default=Path("/home/mininet/Videos/push_input.flv"))
    parser.add_argument("--profile-kind", choices=sorted(PROFILE_OPTIONS), default="combined")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--scenario", choices=("fig7", "fig8", "clean_equal_paths"))
    parser.add_argument("--controller-variant", choices=("qaccess_t", "qaccess_d"), default="qaccess_t")
    parser.add_argument("--sample-interval-ms", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260801,
                        help="reproducible run-order seed for the built-in 27-tuple grid")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 and not args.check_only:
        parser.error("run collection with sudo; Mininet requires root")
    if args.smoke and args.tuples_file:
        parser.error("choose either --smoke or --tuples-file")
    if args.sample_interval_ms < 50:
        parser.error("--sample-interval-ms must be at least 50")
    if args.profile_kind == "delay_clean":
        required_clean_env = {
            "TC_DELAY_FIXED_BW_MBIT": "20",
            "TC_DELAY_FIXED_LOSS_PERCENT": "0",
        }
        conflicts = {
            key: os.environ[key]
            for key, expected in required_clean_env.items()
            if key in os.environ and os.environ[key] != expected
        }
        if conflicts:
            parser.error(f"clean delay requires fixed bandwidth/loss {required_clean_env}; conflicting env={conflicts}")
    tuples = select_tuples(smoke=args.smoke, tuples_file=args.tuples_file, seed=args.seed)
    if not args.input_flv.is_file() and not args.check_only:
        parser.error(f"missing input media: {args.input_flv}")
    profile_flag, default_profile = PROFILE_OPTIONS[args.profile_kind]
    profile = (args.profile or default_profile).resolve()
    scenario = args.scenario or ("clean_equal_paths" if args.profile_kind == "delay_clean" else ("fig8" if args.profile_kind == "combined" else "fig7"))
    output_root = (args.output_root or (
        REPO / "derived/qaccess_d_rtt_fixed_sweep/sweeps"
        if args.controller_variant == "qaccess_d" else DEFAULT_ROOT
    )).resolve()
    if not profile.is_file():
        parser.error(f"missing deterioration profile: {profile}")
    transition_start_s, transition_end_s = profile_transition_times(profile)
    print(
        f"[qserver-sweep] variant={args.controller_variant} tuples={len(tuples)} "
        f"complete_grid={set(tuples) == set(GRID)} fixed_coefficients=true"
    )
    print(
        f"[qserver-sweep] scenario={scenario} profile_kind={args.profile_kind} "
        f"transitions={transition_start_s},{transition_end_s} sample_interval_ms={args.sample_interval_ms} "
        f"run_order_seed={args.seed if not args.smoke and args.tuples_file is None else 'not_applied'}"
    )
    if args.check_only:
        input_state = "present" if args.input_flv.is_file() else "EXTERNAL_VM_INPUT"
        print(f"[qserver-sweep] input_media={args.input_flv} state={input_state}")
        for values in tuples:
            print(tuple_id(values), *values)
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    session = output_root / f"sweep_{stamp}"
    session.mkdir(parents=True)
    git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    manifest = {"session": str(session), "partial": set(tuples) != set(GRID), "expected_tuple_count": 27,
                "tuple_count": len(tuples), "git_commit": git_commit,
                "fixed_coefficient_per_run": True,
                "run_order_randomized": not args.smoke and args.tuples_file is None,
                "run_order_seed": args.seed if not args.smoke and args.tuples_file is None else None,
                "runs": []}
    for index, values in enumerate(tuples, 1):
        name = tuple_id(values)
        run_dir = session / name
        state_dir = run_dir / "phase2_state"
        run_dir.mkdir()
        samples = runtime_samples_path(state_dir)
        write_coefficients(state_dir / "qaccess_t_runtime_coefficients.json", values, name)
        env = os.environ.copy()
        env.update({
            "QACCESS_PHASE2_STATE_DIR": str(state_dir),
            "QACCESS_COEFFS_JSON": str(state_dir / "qaccess_t_runtime_coefficients.json"),
            "QACCESS_COEFF_RELOAD": "0", "QACCESS_TRIGGER_UPDATE": "0",
            "QACCESS_RUNTIME_SAMPLE_EXPORT": "1",
            "QACCESS_RUNTIME_BUFFER_SIZE": "0", "QACCESS_LABEL_INTERVAL_MS": "100",
            "QACCESS_RUNTIME_SAMPLE_INTERVAL_MS": str(args.sample_interval_ms),
            "QACCESS_RETAIN_TC_LOG": "1",
            "KEEP_PCAP": "0", "SAVE_OUTPUT_FLV": "0",
        })
        if args.profile_kind == "delay_clean":
            env.update({
                "TC_DELAY_FIXED_BW_MBIT": "20",
                "TC_DELAY_FIXED_LOSS_PERCENT": "0",
            })
        cmd = [sys.executable, str(REPO / "scripts/mininet/mp_topo.py"), "--run-exp",
               "--scenario", scenario, "--utility-mode", args.controller_variant, "--timeout", str(args.timeout),
               "--log-parent", str(session), "--run-label", name,
               profile_flag, str(profile), "--input-flv", str(args.input_flv),
               "--disable-logs"]
        print(f"[qserver-sweep] {index}/{len(tuples)} {name}", flush=True)
        subprocess.run(cmd, cwd=REPO, env=env, check=True)
        if not samples.is_file():
            raise RuntimeError(
                f"missing qserver runtime samples under explicit Phase2StateDir: {samples}"
            )
        fixed_sample_validation = validate_fixed_samples(samples, values)
        qdisc_validation = (
            validate_clean_delay_tc_log(run_dir)
            if args.profile_kind == "delay_clean" else {}
        )
        with samples.open(encoding="utf-8") as source:
            header = source.readline()
            tail = deque(source, maxlen=10_000)
        with gzip.open(run_dir / "qaccess_runtime_samples_tail.csv.gz", "wt", encoding="utf-8") as target:
            target.write(header)
            target.writelines(tail)
        timelines = sorted(run_dir.glob("experiment_timeline_*.jsonl"))
        if not timelines:
            raise RuntimeError(f"missing experiment timeline under {run_dir}")
        owner_audit = state_dir / "qaccess_owner_audit.jsonl"
        owner = owner_identity(owner_audit)
        (run_dir / "qaccess_owner_audit.jsonl").write_bytes(owner_audit.read_bytes())
        run_id = timelines[-1].stem.removeprefix("experiment_timeline_")
        metadata = {
            "schema_version": 1, "sweep_name": name, "coefficient_tuple_id": name,
            "alpha": values[0], "beta": values[1], "gamma": values[2], "run_id": run_id,
            "endpoint_role": "server_downlink_sender", "owner_pid": owner.get("pid"),
            "phase2_owner": True, "phase2_state_dir": str(state_dir), "execution_mode": "fixed_offline_collection",
            "active_updates_enabled": False, "worker_used": False, "deterioration_profile": str(profile),
            "profile_kind": args.profile_kind,
            "controller_variant": args.controller_variant,
            "scenario": scenario,
            "deterioration_start_s": transition_start_s, "deterioration_end_s": transition_end_s,
            "runtime_sample_interval_ms": args.sample_interval_ms,
            "run_order": index,
            "run_order_seed": manifest["run_order_seed"],
            **fixed_sample_validation,
            **qdisc_validation,
            "impaired_interface": "h2-eth1", "intended_physical_path": "Path B / 10.0.2.x",
            "physical_path_mapping": {"10.0.1.x": "Path A", "10.0.2.x": "Path B"},
            "runtime_samples": str(samples), "timeline": str(timelines[-1]), "owner_audit": str(owner_audit),
            "command_line": cmd, "git_commit": git_commit,
        }
        (run_dir / "sweep_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        manifest["runs"].append(metadata)
        (session / "sweep_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    ownership_root = output_root.parent if output_root in (DEFAULT_ROOT, REPO / "derived/qaccess_d_rtt_fixed_sweep/sweeps") else session
    restore_sudo_ownership(ownership_root)
    print(f"[qserver-sweep] complete: {session}")


if __name__ == "__main__":
    main()
