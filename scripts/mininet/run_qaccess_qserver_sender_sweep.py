#!/usr/bin/env python3
"""Collect fixed-coefficient qserver sender samples under the 90-150s profile."""

from __future__ import annotations

import argparse
import csv
import gzip
import itertools
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = REPO / "derived/qaccess_t_qserver_sender/sweeps"
PROFILE = REPO / "scripts/mininet/combined_deterioration_profile_90_150.env"
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


def write_coefficients(path: Path, values: tuple[float, float, float], name: str) -> None:
    alpha, beta, gamma = values
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "alpha": alpha, "beta": beta, "gamma": gamma,
        "source": f"qserver_sender_sweep:{name}",
        "metric": "fixed_collection_coefficients",
    }, indent=2) + "\n", encoding="utf-8")


def runtime_samples_path(state_dir: Path) -> Path:
    """Mirror the explicit Phase2StateDir contract used by qaccess_phase2.go."""
    return state_dir / "qaccess_runtime_samples.csv"


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
    parser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--timeout", type=int, default=220)
    parser.add_argument("--input-flv", type=Path, default=Path("/home/mininet/Videos/push_input.flv"))
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 and not args.check_only:
        parser.error("run collection with sudo; Mininet requires root")
    if args.smoke and args.tuples_file:
        parser.error("choose either --smoke or --tuples-file")
    tuples = list(SMOKE if args.smoke else (parse_tuples_file(args.tuples_file) if args.tuples_file else GRID))
    if not args.input_flv.is_file():
        parser.error(f"missing input media: {args.input_flv}")
    if not PROFILE.is_file():
        parser.error(f"missing deterioration profile: {PROFILE}")
    print(f"[qserver-sweep] tuples={len(tuples)} complete_grid={set(tuples) == set(GRID)} shadow_only=true")
    if args.check_only:
        for values in tuples:
            print(tuple_id(values), *values)
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    session = args.output_root.resolve() / f"sweep_{stamp}"
    session.mkdir(parents=True)
    git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    manifest = {"session": str(session), "partial": set(tuples) != set(GRID), "expected_tuple_count": 27,
                "tuple_count": len(tuples), "git_commit": git_commit, "runs": []}
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
            "KEEP_PCAP": "0", "SAVE_OUTPUT_FLV": "0",
        })
        cmd = [sys.executable, str(REPO / "scripts/mininet/mp_topo.py"), "--run-exp",
               "--scenario", "fig8", "--utility-mode", "qaccess_t", "--timeout", str(args.timeout),
               "--log-parent", str(session), "--run-label", name,
               "--dynamic-deterioration-profile", str(PROFILE), "--input-flv", str(args.input_flv),
               "--disable-logs"]
        print(f"[qserver-sweep] {index}/{len(tuples)} {name}", flush=True)
        subprocess.run(cmd, cwd=REPO, env=env, check=True)
        if not samples.is_file():
            raise RuntimeError(
                f"missing qserver runtime samples under explicit Phase2StateDir: {samples}"
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
            "phase2_owner": True, "phase2_state_dir": str(state_dir), "execution_mode": "fixed_shadow_collection",
            "active_updates_enabled": False, "worker_used": False, "deterioration_profile": str(PROFILE),
            "deterioration_start_s": 90, "deterioration_end_s": 150,
            "impaired_interface": "h2-eth1", "intended_physical_path": "Path B / 10.0.2.x",
            "physical_path_mapping": {"10.0.1.x": "Path A", "10.0.2.x": "Path B"},
            "runtime_samples": str(samples), "timeline": str(timelines[-1]), "owner_audit": str(owner_audit),
            "command_line": cmd, "git_commit": git_commit,
        }
        (run_dir / "sweep_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        manifest["runs"].append(metadata)
        (session / "sweep_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    ownership_root = DEFAULT_ROOT.parent if args.output_root.resolve() == DEFAULT_ROOT.resolve() else session
    restore_sudo_ownership(ownership_root)
    print(f"[qserver-sweep] complete: {session}")


if __name__ == "__main__":
    main()
