"""Read/write Q-ACCeSS-T runtime coefficient JSON (version-1 per-path + legacy flat)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from qaccess_io import atomic_write_json

QACCESS_COEFFS_VERSION = 1
DEFAULT_ENTRY = {"alpha": 0.6, "beta": 0.3, "gamma": 0.1}


def _finite_positive(x: float, hi: float) -> bool:
    return isinstance(x, (int, float)) and 0 < float(x) <= hi


def _finite_non_neg(x: float, hi: float) -> bool:
    return isinstance(x, (int, float)) and 0 <= float(x) <= hi


def _valid_entry(alpha: float, beta: float, gamma: float) -> bool:
    return _finite_positive(alpha, 2.0) and _finite_non_neg(beta, 1.0) and _finite_non_neg(gamma, 1.0)


def _default_doc() -> dict[str, Any]:
    return {
        "version": QACCESS_COEFFS_VERSION,
        "default": dict(DEFAULT_ENTRY),
        "paths": {},
    }


def normalize_coeffs_doc(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc or {})
    if doc.get("version") == QACCESS_COEFFS_VERSION:
        default = doc.get("default") or {}
        if not _valid_entry(default.get("alpha", 0), default.get("beta", 0), default.get("gamma", 0)):
            default = dict(DEFAULT_ENTRY)
        paths = doc.get("paths") or {}
        clean: dict[str, dict[str, float]] = {}
        for key, entry in paths.items():
            if not isinstance(entry, dict):
                continue
            a, b, g = entry.get("alpha"), entry.get("beta"), entry.get("gamma")
            if _valid_entry(a, b, g):
                clean[str(key)] = {"alpha": float(a), "beta": float(b), "gamma": float(g)}
        doc["default"] = default
        doc["paths"] = clean
        return doc

    # Legacy flat JSON → version-1 document.
    a, b, g = doc.get("alpha"), doc.get("beta"), doc.get("gamma")
    if _valid_entry(a, b, g):
        out = _default_doc()
        out["default"] = {"alpha": float(a), "beta": float(b), "gamma": float(g)}
        if doc.get("source"):
            out["source"] = doc["source"]
        if doc.get("metric"):
            out["metric"] = doc["metric"]
        return out
    return _default_doc()


def load_coeffs_doc(path: Path) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file():
        return _default_doc()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_doc()
    return normalize_coeffs_doc(doc)


def resolve_path_coeffs(doc: dict[str, Any], path_id: int) -> tuple[float, float, float, str]:
    doc = normalize_coeffs_doc(doc)
    key = str(int(path_id))
    entry = (doc.get("paths") or {}).get(key)
    if entry and _valid_entry(entry["alpha"], entry["beta"], entry["gamma"]):
        return float(entry["alpha"]), float(entry["beta"]), float(entry["gamma"]), "per_path"

    default = doc.get("default") or DEFAULT_ENTRY
    if _valid_entry(default.get("alpha", 0), default.get("beta", 0), default.get("gamma", 0)):
        return float(default["alpha"]), float(default["beta"]), float(default["gamma"]), "default"

    return 0.7, 0.1, 0.1, "builtin"


def update_path_coeffs_locked(
    path: Path,
    path_id: int,
    *,
    alpha: float,
    beta: float,
    gamma: float,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Update one path entry in the runtime coefficient JSON (atomic write)."""
    if not _valid_entry(alpha, beta, gamma):
        raise ValueError(f"invalid coefficients: alpha={alpha} beta={beta} gamma={gamma}")

    path = path.resolve()
    doc = load_coeffs_doc(path)
    doc = normalize_coeffs_doc(doc)
    doc.setdefault("paths", {})
    doc["paths"][str(int(path_id))] = {
        "alpha": float(alpha),
        "beta": float(beta),
        "gamma": float(gamma),
    }
    if metadata:
        doc.update({k: v for k, v in metadata.items() if k not in ("paths", "default", "version")})
    doc["source"] = "qaccess_update_worker.py"
    atomic_write_json(path, doc)
