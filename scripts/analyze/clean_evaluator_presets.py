#!/usr/bin/env python3
"""Clean-experiment evaluator presets; historical evaluator windows live elsewhere."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WindowSpec:
    name: str
    start_s: float
    end_s: float
    role: str
    condition: str


CLEAN_PRESETS: dict[str, dict[str, Any]] = {
    "bandwidth_clean": {
        "profile_kind": "bandwidth",
        "primary_metric": "throughput",
        "conditions": (
            "stable 20 Mbps",
            "response to 20 -> 30 Mbps",
            "stable 30 Mbps",
            "response to 30 -> 10 Mbps",
            "stable low bandwidth 10 Mbps",
            "full run",
        ),
    },
    "delay_clean": {
        "profile_kind": "delay",
        "primary_metric": "delay_proxy_ms",
        "conditions": (
            "stable 40 ms",
            "response to 40 -> 80 ms",
            "stable 80 ms",
            "response to 80 -> 40 ms",
            "recovery 40 ms",
            "full run",
        ),
    },
    "loss_clean": {
        "profile_kind": "loss",
        "primary_metric": "loss_risk_or_loss",
        "conditions": (
            "stable 0% configured loss",
            "response to 0 -> 0.5% configured loss",
            "stable 0.5% configured loss",
            "response to 0.5 -> 0% configured loss",
            "recovery 0% configured loss",
            "full run",
        ),
    },
    "stability_clean": {
        "profile_kind": "none",
        "primary_metric": "throughput_and_update_count",
        "conditions": (
            "stable equal paths",
            "stability observation",
            "stable equal paths",
            "stability observation",
            "stable equal paths",
            "full run",
        ),
    },
}


def preset_from_metadata(metadata: dict[str, Any]) -> str | None:
    family = str(metadata.get("experiment_family") or "")
    scenario = str(metadata.get("scenario") or "")
    if family != "clean_controlled" and scenario != "clean_equal_paths":
        return None
    kind = str(metadata.get("profile_kind") or metadata.get("dynamic_dimension") or "none")
    return {
        "bandwidth": "bandwidth_clean",
        "delay": "delay_clean",
        "loss": "loss_clean",
        "none": "stability_clean",
    }.get(kind)


def _transition_times(metadata: dict[str, Any]) -> tuple[float, float]:
    raw = metadata.get("transitions_sec")
    if not isinstance(raw, (list, tuple)):
        raw = []
    positive_values: set[float] = set()
    for value in raw:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            positive_values.add(parsed)
    positive = sorted(positive_values)
    if len(positive) >= 2:
        return positive[0], positive[1]
    return 50.0, 100.0


def clean_windows(preset: str, metadata: dict[str, Any] | None = None) -> tuple[WindowSpec, ...]:
    if preset not in CLEAN_PRESETS:
        raise ValueError(f"unknown clean evaluator preset: {preset}")
    first, second = _transition_times(metadata or {})
    if not (0 < first < second < 200):
        raise ValueError(f"invalid clean transition times: {first}, {second}")
    conditions = CLEAN_PRESETS[preset]["conditions"]
    raw_boundaries = (
        (0.0, first, "stable"),
        (first, first + 10.0, "response"),
        (first + 10.0, second, "stable"),
        (second, second + 10.0, "response"),
        (second + 10.0, 200.0, "stable" if preset in {"bandwidth_clean", "stability_clean"} else "recovery"),
        (0.0, 200.0, "full_run"),
    )
    boundaries = tuple(
        (f"{start:g}-{end:g}", start, end, role)
        for start, end, role in raw_boundaries
    )
    return tuple(
        WindowSpec(name, start, end, role, str(condition))
        for (name, start, end, role), condition in zip(boundaries, conditions)
    )


def clip_to_clean_run(frame, time_column: str = "time_s"):
    """Return only [0, 200) rows without importing pandas in this small preset module."""
    if frame.empty or time_column not in frame.columns:
        return frame
    return frame[(frame[time_column] >= 0.0) & (frame[time_column] < 200.0)].copy()
