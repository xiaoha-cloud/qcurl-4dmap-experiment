#!/usr/bin/env python3
"""Offline replay of legacy and candidate Q-ACCeSS-T control laws from runtime CSV."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

BW_REF_BPS = 30_000_000
DELAY_REF_MS = 100.0
DELAY_TREND_REF_MS = 50.0
LOSS_REF = 0.01
INFLIGHT_ACTIVE_MIN = 1024

LEGACY_GAIN_MIN, LEGACY_GAIN_MAX = 0.80, 1.20
LEGACY_RET_MIN, LEGACY_RET_MAX = 0.90, 1.10
CAND_A_GAIN = (0.95, 1.10)
CAND_A_RET = (0.95, 1.05)
SAFE_DELAY_CAP = 0.02

# Candidate C (throughput-oriented separated law)
PROP_K_ACK_REWARD = 0.06
PROP_K_ACK_LOSS = 0.08
PROP_K_ACK_DELAY = 0.02
PROP_K_RET_REWARD = 0.04
PROP_K_RET_LOSS = 0.06
PROP_K_RET_DELAY = 0.02
PROP_DELAY_TREND_CAP = 0.40
PROP_REWARD_CENTER = 0.35
CAND_C_ACK = (0.95, 1.10)
CAND_C_RET = (0.90, 1.05)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def sanitize(v: float) -> float:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v) or v < 0)):
        return 0.0
    return float(v)


def path_active(row: pd.Series) -> bool:
    return (
        sanitize(row.get("bw_bps", 0)) > 0
        or sanitize(row.get("owd_ms", 0)) > 0
        or int(row.get("inflight_bytes", 0) or 0) > INFLIGHT_ACTIVE_MIN
    )


def norm_g(bw_bps: float) -> float:
    return clamp(sanitize(bw_bps) / BW_REF_BPS, 0, 1)


def norm_l(loss_rate: float) -> float:
    return clamp(sanitize(loss_rate) / LOSS_REF, 0, 1)


def norm_d_current(owd_ms: float, delay_gradient_ms: float) -> float:
    delay_level = clamp(sanitize(owd_ms) / DELAY_REF_MS, 0, 1)
    trend = 0.0
    if delay_gradient_ms > 0:
        trend = clamp(sanitize(delay_gradient_ms) / DELAY_TREND_REF_MS, 0, 1)
    return clamp(0.7 * delay_level + 0.3 * trend, 0, 1)


def norm_d_proposed(delay_gradient_ms: float) -> float:
    if delay_gradient_ms <= 0:
        return 0.0
    return min(clamp(sanitize(delay_gradient_ms) / DELAY_TREND_REF_MS, 0, 1), PROP_DELAY_TREND_CAP)


def g_pow(g_total: float, alpha: float) -> float:
    return (g_total**alpha) if g_total > 0 else 0.0


def legacy_terms(g_total: float, norm_d: float, norm_l_val: float, alpha: float, beta: float, gamma: float):
    gp = g_pow(g_total, alpha)
    tp = 0.20 * gp
    loss_pen = -0.10 * beta * 5.0 * norm_l_val
    delay_pen = -0.05 * gamma * 5.0 * norm_d
    gain_raw = 1.0 + tp + loss_pen + delay_pen
    gain = clamp(gain_raw, LEGACY_GAIN_MIN, LEGACY_GAIN_MAX)

    ret_tp = -0.08 * gp
    ret_loss = 0.05 * beta * 5.0 * norm_l_val
    ret_delay = 0.03 * gamma * 5.0 * norm_d
    ret_raw = 1.0 + ret_tp + ret_loss + ret_delay
    retention = clamp(ret_raw, LEGACY_RET_MIN, LEGACY_RET_MAX)
    return tp, loss_pen, delay_pen, gain_raw, gain, ret_raw, retention


def candidate_a(gain_raw: float, ret_raw: float) -> tuple[float, float]:
    return clamp(gain_raw, *CAND_A_GAIN), clamp(ret_raw, *CAND_A_RET)


def candidate_b(gain_raw: float, tp: float, loss_pen: float, delay_pen: float, ret_raw: float) -> tuple[float, float]:
    delay_bounded = max(delay_pen, -SAFE_DELAY_CAP)
    gain_raw_b = 1.0 + tp + loss_pen + delay_bounded
    gain = clamp(gain_raw_b, LEGACY_GAIN_MIN, LEGACY_GAIN_MAX)
    return gain, clamp(ret_raw, LEGACY_RET_MIN, LEGACY_RET_MAX)


def candidate_c(
    g_total: float, norm_l_val: float, delay_trend: float, alpha: float, beta: float, gamma: float
) -> tuple[float, float]:
    g = g_total if g_total > 0 else 1e-9
    reward = clamp(g**alpha, 0, 1)
    loss_pen = beta * norm_l_val
    delay_pen = gamma * delay_trend
    ack_raw = 1.0 + PROP_K_ACK_REWARD * (reward - PROP_REWARD_CENTER) - PROP_K_ACK_LOSS * loss_pen - PROP_K_ACK_DELAY * delay_pen
    ret_raw = 1.0 - PROP_K_RET_REWARD * (reward - PROP_REWARD_CENTER) + PROP_K_RET_LOSS * loss_pen + PROP_K_RET_DELAY * delay_pen
    return clamp(ack_raw, *CAND_C_ACK), clamp(ret_raw, *CAND_C_RET)


def compute_g_total(df: pd.DataFrame) -> pd.Series:
    active = df.apply(path_active, axis=1)
    tmp = df[active].copy()
    tmp["ng"] = tmp["bw_bps"].map(norm_g)
    by_ts = tmp.groupby("timestamp_ms")["ng"].sum().clip(0, 1)
    out = df["timestamp_ms"].map(by_ts).fillna(0)
    return out


def load_samples(path: Path) -> pd.DataFrame:
    if path.is_file():
        files = [path]
    else:
        files = sorted(path.glob("**/qaccess_runtime_samples*.csv"))
        if not files:
            raise FileNotFoundError(f"no runtime CSV under {path}")
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    df = df.drop_duplicates(
        subset=["timestamp_ms", "path_id", "bw_bps", "owd_ms", "gain", "backoff"], keep="last"
    )
    return df.sort_values(["timestamp_ms", "path_id"]).reset_index(drop=True)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["active"] = df.apply(path_active, axis=1)
    df["g_total"] = compute_g_total(df)

    rows = []
    for _, r in df.iterrows():
        alpha, beta, gamma = float(r["alpha"]), float(r["beta"]), float(r["gamma"])
        owd = float(r["owd_ms"])
        dg = float(r.get("delay_gradient_ms", 0) or 0)
        loss = float(r.get("loss_rate", 0) or 0)
        nl = norm_l(loss)
        nd = norm_d_current(owd, dg)
        nd_prop = norm_d_proposed(dg)
        g_total = float(r["g_total"])
        if g_total <= 0 and r["active"]:
            g_total = norm_g(r["bw_bps"])

        if not r["active"]:
            rows.append({**r.to_dict()})
            continue

        tp, loss_pen, delay_pen, gain_raw, legacy_gain, ret_raw, legacy_ret = legacy_terms(
            g_total, nd, nl, alpha, beta, gamma
        )
        cand_a_gain, cand_a_ret = candidate_a(gain_raw, ret_raw)
        cand_b_gain, cand_b_ret = candidate_b(gain_raw, tp, loss_pen, delay_pen, ret_raw)
        cand_c_gain, cand_c_ret = candidate_c(g_total, nl, nd_prop, alpha, beta, gamma)

        rows.append(
            {
                **r.to_dict(),
                "g_total_used": g_total,
                "throughput_reward_term": tp,
                "loss_penalty_term": loss_pen,
                "delay_penalty_term": delay_pen,
                "legacy_gain_raw": gain_raw,
                "legacy_gain": legacy_gain,
                "legacy_retention": legacy_ret,
                "candidate_a_gain": cand_a_gain,
                "candidate_a_retention": cand_a_ret,
                "candidate_b_gain": cand_b_gain,
                "candidate_b_retention": cand_b_ret,
                "candidate_c_gain": cand_c_gain,
                "candidate_c_retention": cand_c_ret,
            }
        )
    return pd.DataFrame(rows)


def dist_stats(s: pd.Series) -> dict:
    s = s.dropna()
    if s.empty:
        return {}
    return {
        "mean": float(s.mean()),
        "std": float(s.std()),
        "p5": float(s.quantile(0.05)),
        "p50": float(s.quantile(0.50)),
        "p95": float(s.quantile(0.95)),
    }


def clamp_hit_rate(s: pd.Series, lo: float, hi: float) -> dict:
    s = s.dropna()
    if s.empty:
        return {"hit_min": 0.0, "hit_max": 0.0}
    return {
        "hit_min": float((s <= lo + 1e-9).mean()),
        "hit_max": float((s >= hi - 1e-9).mean()),
    }


def gamma_sensitivity(sub: pd.DataFrame, gain_col: str) -> float:
    """Mean gain(gamma+0.1) - gain(gamma) via paired recompute."""
    if sub.empty:
        return float("nan")
    deltas = []
    for _, r in sub.iterrows():
        g0 = float(r["gamma"])
        g1 = g0 + 0.1
        g_total = float(r["g_total_used"])
        nd = norm_d_current(float(r["owd_ms"]), float(r.get("delay_gradient_ms", 0) or 0))
        nl = norm_l(float(r.get("loss_rate", 0) or 0))
        alpha, beta = float(r["alpha"]), float(r["beta"])
        if gain_col == "candidate_c_gain":
            d0, _ = candidate_c(g_total, nl, norm_d_proposed(float(r.get("delay_gradient_ms", 0) or 0)), alpha, beta, g0)
            d1, _ = candidate_c(g_total, nl, norm_d_proposed(float(r.get("delay_gradient_ms", 0) or 0)), alpha, beta, g1)
        else:
            _, _, _, _, d0, _, _ = legacy_terms(g_total, nd, nl, alpha, beta, g0)
            _, _, _, _, d1, _, _ = legacy_terms(g_total, nd, nl, alpha, beta, g1)
            if gain_col == "candidate_b_gain":
                tp, lp, dp, _, _, _, _ = legacy_terms(g_total, nd, nl, alpha, beta, g1)
                d1, _ = candidate_b(0, tp, lp, dp, 0)
            if gain_col == "candidate_a_gain":
                _, _, _, gr, _, _, _ = legacy_terms(g_total, nd, nl, alpha, beta, g1)
                d1, _ = candidate_a(gr, 0)
        deltas.append(d1 - d0)
    return float(np.mean(deltas))


def path_summary(sub: pd.DataFrame, path_id: int, gain_cols: list[str]) -> dict:
    s = sub[sub["path_id"] == path_id]
    s = s[s["active"]] if "active" in s.columns else s
    out: dict = {"path_id": int(path_id), "n": int(len(s))}
    if s.empty:
        return out
    s = s.copy()
    s["next_bw_bps_num"] = pd.to_numeric(s.get("next_bw_bps", 0), errors="coerce")
    for col in gain_cols:
        out[col] = {
            **dist_stats(s[col]),
            **clamp_hit_rate(
                s[col],
                LEGACY_GAIN_MIN if "legacy" in col or "candidate_b" in col else CAND_A_GAIN[0],
                LEGACY_GAIN_MAX if "legacy" in col or "candidate_b" in col else CAND_A_GAIN[1],
            ),
            "gamma_sensitivity_plus_0.1": gamma_sensitivity(s, col),
            "corr_cwnd": float(s[[col, "cwnd_bytes"]].corr().iloc[0, 1]) if len(s) > 2 else float("nan"),
            "corr_next_bw": float(s[[col, "next_bw_bps_num"]].corr().iloc[0, 1]) if len(s) > 2 else float("nan"),
        }
    out["corr_gamma_legacy_gain"] = float(s[["gamma", "legacy_gain"]].corr().iloc[0, 1]) if len(s) > 2 else float("nan")
    return out


def make_plots(df: pd.DataFrame, out_dir: Path) -> None:
    active = df[df["active"]].copy()
    gain_cols = ["legacy_gain", "candidate_a_gain", "candidate_b_gain", "candidate_c_gain"]

    fig, axes = plt.subplots(2, 2, figsize=(12, 9))
    for ax, col in zip(axes.ravel(), gain_cols):
        for pid, color in [(1, "C0"), (3, "C1")]:
            s = active[active["path_id"] == pid][col].dropna()
            if len(s):
                ax.hist(s, bins=40, alpha=0.5, label=f"path {pid}", density=True, color=color)
        ax.set_title(col)
        ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / "gain_distributions.png", dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 5))
    for pid, color in [(1, "C0"), (3, "C1")]:
        s = active[active["path_id"] == pid]
        ax.scatter(s["gamma"], s["legacy_gain"], s=4, alpha=0.25, color=color, label=f"path {pid} legacy")
        ax.scatter(s["gamma"], s["candidate_b_gain"], s=4, alpha=0.25, color=color, marker="x", label=f"path {pid} cand B")
    ax.set_xlabel("gamma")
    ax.set_ylabel("ack gain")
    ax.set_title("gamma sensitivity: legacy vs candidate B")
    ax.legend(fontsize=7, ncol=2)
    fig.tight_layout()
    fig.savefig(out_dir / "gamma_sensitivity.png", dpi=150)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    for ax, pid in zip(axes, [1, 3]):
        s = active[active["path_id"] == pid]
        data = [s["legacy_gain"], s["candidate_a_gain"], s["candidate_b_gain"], s["candidate_c_gain"]]
        ax.boxplot(data, labels=["legacy", "A", "B", "C"])
        ax.set_title(f"path_id={pid} gain comparison")
        ax.set_ylabel("ack gain")
    fig.tight_layout()
    fig.savefig(out_dir / "path1_vs_path3_comparison.png", dpi=150)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("logs_exp/session_combined_deterioration_20260614_232155/combined_qaccess_t_dynamic"),
    )
    parser.add_argument("--out", type=Path, default=Path("derived/combined_deterioration_compare/control_law_replay_v2"))
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[2]
    inp = args.input if args.input.is_absolute() else repo / args.input
    out_dir = args.out if args.out.is_absolute() else repo / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    df = load_samples(inp)
    enriched = enrich(df)
    enriched.to_csv(out_dir / "per_row_comparison.csv", index=False)

    gain_cols = ["legacy_gain", "candidate_a_gain", "candidate_b_gain", "candidate_c_gain"]
    summary = {
        "paths": [path_summary(enriched, pid, gain_cols) for pid in sorted(enriched["path_id"].unique())],
        "global": {
            "legacy_gain_clamp": clamp_hit_rate(enriched["legacy_gain"], LEGACY_GAIN_MIN, LEGACY_GAIN_MAX),
            "candidate_a_gain_clamp": clamp_hit_rate(enriched["candidate_a_gain"], *CAND_A_GAIN),
            "candidate_b_gain_clamp": clamp_hit_rate(enriched["candidate_b_gain"], LEGACY_GAIN_MIN, LEGACY_GAIN_MAX),
            "candidate_c_gain_clamp": clamp_hit_rate(enriched["candidate_c_gain"], *CAND_C_ACK),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    make_plots(enriched, out_dir)
    print(f"Wrote replay to {out_dir}")
    for p in summary["paths"]:
        if p.get("n", 0):
            print(
                f"path {p['path_id']}: legacy_gain mean={p['legacy_gain']['mean']:.4f} "
                f"cand_B mean={p['candidate_b_gain']['mean']:.4f} "
                f"gamma_sens_B={p['candidate_b_gain']['gamma_sensitivity_plus_0.1']:.4f}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
