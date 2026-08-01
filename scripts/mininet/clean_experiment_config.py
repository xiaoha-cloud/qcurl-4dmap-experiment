#!/usr/bin/env python3
"""Strict validation and metadata for controlled experiment configurations."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from mp_topo import scenario_link_kwargs


CLEAN_SCENARIO = "clean_equal_paths"
CLEAN_INTERFACE = "h2-eth1"
CLEAN_PATH = {"bandwidth_mbps": 20, "delay_ms": 40, "loss_percent": 0}
EXPECTED_PROFILES: dict[str, tuple[list[int], list[int | float]]] = {
    "bandwidth": ([0, 50, 100], [20, 30, 10]),
    "delay": ([0, 50, 100], [40, 80, 40]),
    "loss": ([0, 50, 100], [0, 0.5, 0]),
}


class ConfigurationError(ValueError):
    """Raised when a controlled experiment configuration is invalid."""


def format_loss_percent(value: int | float) -> str:
    """Format a parsed profile value for tc netem's percentage argument."""
    return f"{value:g}%"


def _number(text: str, *, integer: bool) -> int | float:
    if integer:
        if not re.fullmatch(r"[0-9]+", text):
            raise ConfigurationError(f"expected a non-negative integer, got {text!r}")
        return int(text)
    if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", text):
        raise ConfigurationError(f"expected a non-negative number, got {text!r}")
    value = float(text)
    return int(value) if value.is_integer() else value


def validate_scenario(name: str) -> dict[str, dict[str, int]]:
    if name != CLEAN_SCENARIO:
        raise ConfigurationError(f"clean experiments require scenario {CLEAN_SCENARIO!r}, got {name!r}")
    links = scenario_link_kwargs(name)
    effective: dict[str, dict[str, int]] = {}
    for path_name in ("path_a", "path_b"):
        link = links[path_name]
        delay = str(link["delay"])
        if not delay.endswith("ms"):
            raise ConfigurationError(f"{path_name} delay must use milliseconds: {delay!r}")
        loss = float(link["loss"])
        effective[path_name] = {
            "bandwidth_mbps": int(link["bw"]),
            "delay_ms": int(delay[:-2]),
            "loss_percent": int(loss) if loss.is_integer() else loss,
        }
        if effective[path_name] != CLEAN_PATH:
            raise ConfigurationError(
                f"{path_name} must resolve to {CLEAN_PATH}, got {effective[path_name]}"
            )
    return effective


def parse_profile(path: Path, kind: str) -> dict[str, Any]:
    if kind not in EXPECTED_PROFILES:
        raise ConfigurationError(f"unsupported profile kind: {kind!r}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"cannot read profile {path}: {exc}") from exc
    if b"\r" in raw:
        raise ConfigurationError(f"profile must use Unix line endings: {path}")

    interface: str | None = None
    transitions: list[int] = []
    values: list[int | float] = []
    for line_number, raw_line in enumerate(raw.decode("utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("IFACE="):
            if interface is not None:
                raise ConfigurationError(f"duplicate IFACE at line {line_number}")
            interface = line.split("=", 1)[1]
            if not interface:
                raise ConfigurationError(f"empty IFACE at line {line_number}")
            continue
        fields = line.split()
        if len(fields) != 2:
            raise ConfigurationError(f"unsupported profile line {line_number}: {raw_line!r}")
        transition = _number(fields[0], integer=True)
        value_text = fields[1]
        if kind == "delay" and value_text.endswith("ms"):
            value_text = value_text[:-2]
        elif kind == "loss" and value_text.endswith("%"):
            value_text = value_text[:-1]
        value = _number(value_text, integer=kind != "loss")
        transitions.append(int(transition))
        values.append(value)

    if interface != CLEAN_INTERFACE:
        raise ConfigurationError(
            f"profile interface must be {CLEAN_INTERFACE!r}, got {interface!r}"
        )
    if not transitions or transitions[0] != 0:
        raise ConfigurationError("first profile transition must occur at 0 seconds")
    if any(current <= previous for previous, current in zip(transitions, transitions[1:])):
        raise ConfigurationError("profile transition times must be strictly increasing")
    expected_transitions, expected_values = EXPECTED_PROFILES[kind]
    if transitions != expected_transitions:
        raise ConfigurationError(
            f"{kind} transitions must be {expected_transitions}, got {transitions}"
        )
    if values != expected_values:
        raise ConfigurationError(f"{kind} values must be {expected_values}, got {values}")
    return {
        "kind": kind,
        "interface": interface,
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "transitions_sec": transitions,
        "values": values,
        "value_unit": "percent" if kind == "loss" else ("ms" if kind == "delay" else "Mbps"),
        "tc_values": [format_loss_percent(value) for value in values] if kind == "loss" else values,
        "text": raw.decode("utf-8"),
    }


def build_configuration(scenario: str, kind: str, profile: Path | None) -> dict[str, Any]:
    paths = validate_scenario(scenario)
    if kind == "none":
        if profile is not None:
            raise ConfigurationError("stability configuration must not select a dynamic profile")
        profile_data = {
            "kind": "none",
            "interface": CLEAN_INTERFACE,
            "path": None,
            "sha256": None,
            "transitions_sec": [],
            "values": [],
            "value_unit": None,
            "tc_values": [],
            "text": None,
        }
    else:
        if profile is None:
            raise ConfigurationError(f"{kind} configuration requires a profile")
        profile_data = parse_profile(profile, kind)
    return {
        "experiment_family": "clean_controlled",
        "scenario": scenario,
        "path_a_initial": paths["path_a"],
        "path_b_initial": paths["path_b"],
        "dynamic_dimension": kind,
        "dynamic_interface": CLEAN_INTERFACE,
        "profile_path": profile_data["path"],
        "profile_sha256": profile_data["sha256"],
        "transitions_sec": profile_data["transitions_sec"],
        "profile_values": profile_data["values"],
        "profile_value_unit": profile_data["value_unit"],
        "profile_text": profile_data["text"],
        "impairment_direction": "server_to_client_path_b_egress",
        "gate_policy": "legacy",
        "trigger_mode": "legacy_buffer_full",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--profile-kind", choices=("bandwidth", "delay", "loss", "none"), required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--json", action="store_true", help="emit resolved configuration as JSON")
    args = parser.parse_args()
    try:
        config = build_configuration(args.scenario, args.profile_kind, args.profile)
    except ConfigurationError as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(config, indent=2, sort_keys=True))
    else:
        print(f"scenario={config['scenario']}")
        print(f"path_a_initial={config['path_a_initial']}")
        print(f"path_b_initial={config['path_b_initial']}")
        print(f"dynamic_dimension={config['dynamic_dimension']}")
        print(f"dynamic_interface={config['dynamic_interface']}")
        print(f"profile_path={config['profile_path'] or 'none'}")
        print(f"transitions_sec={config['transitions_sec']}")
        print(f"profile_values={config['profile_values']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
