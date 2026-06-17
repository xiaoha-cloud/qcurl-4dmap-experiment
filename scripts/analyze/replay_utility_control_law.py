#!/usr/bin/env python3
"""Offline replay of current vs proposed Q-ACCeSS-T utility control law.

Reads existing qaccess_runtime_samples CSV(s); does not alter runtime behavior.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# --- constants aligned with quic-go43DMAP/utility_controller.go ----------------
BW_REF_BPS = 30_000_000
DELAY_REF_MS = 100.0
DELAY_TREND_REF_MS = 50.0
LOSS_REF = 0.01
INFLIGHT_ACTIVE_MIN = 1024

CUR_GAIN_MIN, CUR_GAIN_MAX = 0.80, 1.20
CUR_RET_MIN, CUR_RET_MAX = 0.90, 1.10  # field name "backoff" in CSV

# Proposed safe ranges (Part 3)
PROP_ACK_GAIN_MIN, PROP_ACK_GAIN_MAX = 0.95, 1.10
PROP_RET_MIN, PROP_RET_MAX = 0.90, 1.05

# Proposed coefficients
PROP_K_ACK_REWARD = 0.06
PROP_K_ACK_LOSS = 0.08
PROP_K_ACK_DELAY = 0.02
PROP_K_RET_REWARD = 0.04
PROP_K_RET_LOSS = 0.06
PROP_K_RET_DELAY = 0.02
PROP_DELAY_TREND_CAP = 0.40
PROP_REWARD_CENTER = 0.35
SMOOTH_PREV_WEIGHT = 0.8
SMOOTH_NEW_WEIGHT = 0.2
MAX_ACK_GAIN_STEP = 0.02
MAX_RET_STEP = 0.01

RENO_BETA = 0.7  # typical single-connection RenoBeta() ≈ 0.7


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def sanitize(v: float) -> float:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v) or v < 0)):
        return 0.0
    return float(v)


def path_active(row: pd.Series) -> bool:
    bw = sanitize(row.get("bw_bps", 0))
    owd = sanitize(row.get("owd_ms", 0))
    inflight = int(row.get("inflight_bytes", 0) or 0)
    return bw > 0 or owd > 0 or inflight > INFLIGHT_ACTIVE_MIN


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
    """Positive delay gradient only, bounded."""
    if delay_gradient_ms <= 0:
        return 0.0
    raw = clamp(sanitize(delay_gradient_ms) / DELAY_TREND_REF_MS, 0, 1)
    return min(raw, PROP_DELAY_TREND_CAP)


def current_utility(g_total: float, norm_d: float, norm_l_val: float, alpha: float, beta: float, gamma: float) -> float:
    g = g_total if g_total > 0 else 1e-9
    reward = g**alpha
    penalty = beta * g * norm_l_val + gamma * g * norm_d
    return reward - penalty


def current_gain_backoff(
    g_total: float, norm_d: float, norm_l_val: float, alpha: float, beta: float, gamma: float
) -> tuple[float, ...]:
    """Return utility, raw/clamped gain, raw/clamped retention, and term contributions."""
    g = g_total if g_total > 0 else 1e-9
    g_pow = g**alpha

    u_reward = g_pow
    u_loss_pen = beta * g * norm_l_val
    u_delay_pen = gamma * g * norm_d
    utility = u_reward - u_loss_pen - u_delay_pen

    gain_tp = 0.20 * g_pow
    gain_loss = -0.10 * beta * 5.0 * norm_l_val
    gain_delay = -0.05 * gamma * 5.0 * norm_d
    gain_raw = 1.0 + gain_tp + gain_loss + gain_delay
    gain_clamped = clamp(gain_raw, CUR_GAIN_MIN, CUR_GAIN_MAX)

    # "backoff" in code = retention multiplier on RenoBeta (higher → weaker reduction)
    ret_tp = -0.08 * g_pow
    ret_loss = 0.05 * beta * 5.0 * norm_l_val
    ret_delay = 0.03 * gamma * 5.0 * norm_d
    ret_raw = 1.0 + ret_tp + ret_loss + ret_delay
    ret_clamped = clamp(ret_raw, CUR_RET_MIN, CUR_RET_MAX)

    return (
        utility,
        gain_raw,
        gain_clamped,
        ret_raw,
        ret_clamped,
        gain_tp,
        gain_loss,
        gain_delay,
        ret_tp,
        ret_loss,
        ret_delay,
        u_reward,
        u_loss_pen,
        u_delay_pen,
    )


def proposed_ack_retention_raw(
    g_total: float,
    norm_l_val: float,
    delay_trend: float,
    alpha: float,
    beta: float,
    gamma: float,
) -> tuple[float, float]:
    g = g_total if g_total > 0 else 1e-9
    reward_signal = clamp(g**alpha, 0, 1)
    loss_pen = beta * norm_l_val
    delay_pen = gamma * delay_trend

    ack_raw = (
        1.0
        + PROP_K_ACK_REWARD * (reward_signal - PROP_REWARD_CENTER)
        - PROP_K_ACK_LOSS * loss_pen
        - PROP_K_ACK_DELAY * delay_pen
    )
    ret_raw = (
        1.0
        - PROP_K_RET_REWARD * (reward_signal - PROP_REWARD_CENTER)
        + PROP_K_RET_LOSS * loss_pen
        + PROP_K_RET_DELAY * delay_pen
    )
    return ack_raw, ret_raw


def apply_proposed_smoothing(df: pd.DataFrame) -> pd.DataFrame:
    """Stateful per-path smoothing + rate limits for proposed law."""
    ack_applied = []
    ret_applied = []
    prev_ack: dict[int, float] = {}
    prev_ret: dict[int, float] = {}

    for _, row in df.iterrows():
        pid = int(row["path_id"])
        ack_c = row["prop_ack_gain_clamped"]
        ret_c = row["prop_retention_clamped"]

        pa = prev_ack.get(pid, 1.0)
        pr = prev_ret.get(pid, 1.0)

        smoothed_ack = SMOOTH_PREV_WEIGHT * pa + SMOOTH_NEW_WEIGHT * ack_c
        smoothed_ret = SMOOTH_PREV_WEIGHT * pr + SMOOTH_NEW_WEIGHT * ret_c

        delta_ack = clamp(smoothed_ack - pa, -MAX_ACK_GAIN_STEP, MAX_ACK_GAIN_STEP)
        delta_ret = clamp(smoothed_ret - pr, -MAX_RET_STEP, MAX_RET_STEP)

        final_ack = pa + delta_ack
        final_ret = pr + delta_ret

        prev_ack[pid] = final_ack
        prev_ret[pid] = final_ret
        ack_applied.append(final_ack)
        ret_applied.append(final_ret)

    df = df.copy()
    df["prop_ack_gain_applied"] = ack_applied
    df["prop_retention_applied"] = ret_applied
    return df


def predicted_ack_growth_factor(gain: float) -> float:
    """Unitless multiplier on OLIA cwnd delta (CA mode)."""
    return gain


def predicted_loss_retention_fraction(retention: float) -> float:
    """Fraction of cwnd retained after loss: effectiveBeta = RenoBeta * retention."""
    return RENO_BETA * retention


def load_runtime_samples(session_dynamic_dir: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    buf = session_dynamic_dir / "processed_buffers"
    for p in sorted(buf.glob("qaccess_runtime_samples_*.csv")):
        frames.append(pd.read_csv(p))
    snap = session_dynamic_dir / "derived_snapshots" / "qaccess_runtime_samples.csv"
    if snap.is_file():
        frames.append(pd.read_csv(snap))
    if not frames:
        raise FileNotFoundError(f"no runtime samples under {session_dynamic_dir}")
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(
        subset=["timestamp_ms", "path_id", "bw_bps", "owd_ms", "gain", "backoff"],
        keep="last",
    )
    return df.sort_values(["timestamp_ms", "path_id"]).reset_index(drop=True)


def compute_g_total_per_round(df: pd.DataFrame) -> pd.Series:
    """Reconstruct round GTotal = sum normG over active paths at each timestamp_ms."""
    active = df.apply(path_active, axis=1)
    tmp = df[active].copy()
    tmp["norm_g_path"] = tmp["bw_bps"].map(norm_g)
    g_by_ts = tmp.groupby("timestamp_ms")["norm_g_path"].sum().clip(0, 1)
    return df["timestamp_ms"].map(g_by_ts).fillna(0)


def enrich_rows(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["active"] = df.apply(path_active, axis=1)
    df["g_total"] = compute_g_total_per_round(df)

    rows = []
    for _, r in df.iterrows():
        alpha = float(r["alpha"])
        beta = float(r["beta"])
        gamma = float(r["gamma"])
        owd = float(r["owd_ms"])
        dg = float(r.get("delay_gradient_ms", 0) or 0)
        loss = float(r.get("loss_rate", 0) or 0)

        ng = norm_g(r["bw_bps"])
        nl = norm_l(loss)
        nd_cur = norm_d_current(owd, dg)
        nd_prop = norm_d_proposed(dg)

        g_total = float(r["g_total"])
        if g_total <= 0 and r["active"]:
            g_total = ng

        if not r["active"]:
            rows.append(
                {
                    **r.to_dict(),
                    "norm_g": ng,
                    "norm_l": nl,
                    "norm_d_current": nd_cur,
                    "norm_d_proposed": nd_prop,
                    "g_total_used": g_total,
                }
            )
            continue

        (
            utility,
            gain_raw,
            gain_clamped,
            ret_raw,
            ret_clamped,
            gain_tp,
            gain_loss,
            gain_delay,
            ret_tp,
            ret_loss,
            ret_delay,
            u_reward,
            u_loss_pen,
            u_delay_pen,
        ) = current_gain_backoff(g_total, nd_cur, nl, alpha, beta, gamma)

        ack_prop_raw, ret_prop_raw = proposed_ack_retention_raw(
            g_total, nl, nd_prop, alpha, beta, gamma
        )
        ack_prop_clamped = clamp(ack_prop_raw, PROP_ACK_GAIN_MIN, PROP_ACK_GAIN_MAX)
        ret_prop_clamped = clamp(ret_prop_raw, PROP_RET_MIN, PROP_RET_MAX)

        rows.append(
            {
                **r.to_dict(),
                "norm_g": ng,
                "norm_l": nl,
                "norm_d_current": nd_cur,
                "norm_d_proposed": nd_prop,
                "g_total_used": g_total,
                "utility_recomputed": utility,
                "u_reward": u_reward,
                "u_loss_penalty": u_loss_pen,
                "u_delay_penalty": u_delay_pen,
                "gain_throughput_term": gain_tp,
                "gain_loss_term": gain_loss,
                "gain_delay_term": gain_delay,
                "gain_raw": gain_raw,
                "gain_clamped": gain_clamped,
                "retention_raw": ret_raw,
                "retention_clamped": ret_clamped,
                "retention_throughput_term": ret_tp,
                "retention_loss_term": ret_loss,
                "retention_delay_term": ret_delay,
                "gain_at_min": gain_clamped <= CUR_GAIN_MIN + 1e-9,
                "gain_at_max": gain_clamped >= CUR_GAIN_MAX - 1e-9,
                "ret_at_min": ret_clamped <= CUR_RET_MIN + 1e-9,
                "ret_at_max": ret_clamped >= CUR_RET_MAX - 1e-9,
                "prop_ack_gain_raw": ack_prop_raw,
                "prop_ack_gain_clamped": ack_prop_clamped,
                "prop_retention_raw": ret_prop_raw,
                "prop_retention_clamped": ret_prop_clamped,
                "cur_ack_growth_factor": predicted_ack_growth_factor(gain_clamped),
                "prop_ack_growth_factor": np.nan,
                "cur_loss_retain_frac": predicted_loss_retention_fraction(ret_clamped),
                "prop_loss_retain_frac": np.nan,
            }
        )

    out = pd.DataFrame(rows)
    out = apply_proposed_smoothing(out)
    out["prop_ack_growth_factor"] = out["prop_ack_gain_applied"]
    out["prop_loss_retain_frac"] = out["prop_retention_applied"].map(
        predicted_loss_retention_fraction
    )
    return out


def path_summary(df: pd.DataFrame, path_id: int) -> dict:
    sub = df[(df["path_id"] == path_id) & df["active"]].copy()
    if sub.empty:
        return {"path_id": path_id, "n": 0}

    def stats(s: pd.Series) -> dict:
        return {
            "mean": float(s.mean()),
            "std": float(s.std()),
            "p5": float(s.quantile(0.05)),
            "p50": float(s.quantile(0.50)),
            "p95": float(s.quantile(0.95)),
        }

    def corr(a: str, b: str) -> float:
        if len(sub) < 3:
            return float("nan")
        return float(sub[[a, b]].corr().iloc[0, 1])

    sub = sub.copy()
    sub["next_bw_bps_num"] = pd.to_numeric(sub.get("next_bw_bps", 0), errors="coerce")

    return {
        "path_id": path_id,
        "n": len(sub),
        "gain_clamped": stats(sub["gain_clamped"]),
        "retention_clamped": stats(sub["retention_clamped"]),
        "gain_at_min_frac": float(sub["gain_at_min"].mean()),
        "gain_at_max_frac": float(sub["gain_at_max"].mean()),
        "ret_at_min_frac": float(sub["ret_at_min"].mean()),
        "ret_at_max_frac": float(sub["ret_at_max"].mean()),
        "corr_gamma_gain": corr("gamma", "gain_clamped"),
        "corr_gamma_retention": corr("gamma", "retention_clamped"),
        "corr_gamma_ack_growth": corr("gamma", "cur_ack_growth_factor"),
        "corr_gain_next_bw": corr("gain_clamped", "next_bw_bps_num"),
        "corr_retention_next_bw": corr("retention_clamped", "next_bw_bps_num"),
        "corr_cwnd_gain": corr("cwnd_bytes", "gain_clamped"),
        "prop_ack_gain_applied": stats(sub["prop_ack_gain_applied"]),
        "prop_retention_applied": stats(sub["prop_retention_applied"]),
    }


def collapse_analysis(df: pd.DataFrame, path_id: int = 3) -> dict:
    """Identify pre-collapse control term shifts for Path B (path_id=3)."""
    sub = df[(df["path_id"] == path_id) & df["active"]].copy()
    if sub.empty or "next_bw_bps_num" not in sub.columns:
        sub["next_bw_bps_num"] = pd.to_numeric(sub.get("next_bw_bps", 0), errors="coerce")
    sub = sub.sort_values("timestamp_ms")
    sub["t_rel_s"] = (sub["timestamp_ms"] - sub["timestamp_ms"].min()) / 1000.0

    # Collapse: rolling median bw drops >40% vs early baseline
    early_med = sub.loc[sub["t_rel_s"] < 30, "bw_bps"].median()
    sub["bw_roll"] = sub["bw_bps"].rolling(20, min_periods=5).median()
    collapse_idx = sub.index[sub["bw_roll"] < 0.6 * early_med]
    if len(collapse_idx) == 0:
        return {"path_id": path_id, "collapse_detected": False}

    t_collapse = float(sub.loc[collapse_idx[0], "t_rel_s"])
    pre = sub[(sub["t_rel_s"] >= t_collapse - 20) & (sub["t_rel_s"] < t_collapse)]
    post = sub[(sub["t_rel_s"] >= t_collapse) & (sub["t_rel_s"] < t_collapse + 20)]

    def mean_cols(frame: pd.DataFrame, cols: list[str]) -> dict:
        return {c: float(frame[c].mean()) if len(frame) else float("nan") for c in cols}

    cols = [
        "gain_clamped",
        "retention_clamped",
        "gain_delay_term",
        "gain_throughput_term",
        "retention_delay_term",
        "gamma",
        "loss_rate",
        "cwnd_bytes",
    ]
    return {
        "path_id": path_id,
        "collapse_detected": True,
        "collapse_t_rel_s": t_collapse,
        "pre_window_mean": mean_cols(pre, cols),
        "post_window_mean": mean_cols(post, cols),
        "gain_drop": float(pre["gain_clamped"].mean() - post["gain_clamped"].mean()),
        "retention_change": float(post["retention_clamped"].mean() - pre["retention_clamped"].mean()),
    }


def make_plots(df: pd.DataFrame, out_dir: Path) -> None:
    active = df[df["active"]].copy()

    fig, axes = plt.subplots(2, 2, figsize=(12, 9))

    for label, col, ax in [
        ("current gain", "gain_clamped", axes[0, 0]),
        ("proposed ack_gain", "prop_ack_gain_applied", axes[0, 1]),
        ("current retention", "retention_clamped", axes[1, 0]),
        ("proposed retention", "prop_retention_applied", axes[1, 1]),
    ]:
        for pid, color in [(1, "C0"), (3, "C1")]:
            s = active[active["path_id"] == pid][col].dropna()
            if len(s):
                ax.hist(s, bins=40, alpha=0.5, label=f"path {pid}", color=color, density=True)
        ax.set_title(label)
        ax.legend(fontsize=8)

    fig.tight_layout()
    fig.savefig(out_dir / "current_vs_proposed_distributions.png", dpi=150)
    plt.close(fig)

    # gamma vs gain
    fig, ax = plt.subplots(figsize=(8, 5))
    for pid, color in [(1, "C0"), (3, "C1")]:
        s = active[active["path_id"] == pid]
        ax.scatter(s["gamma"], s["gain_clamped"], s=4, alpha=0.3, label=f"path {pid} current", color=color)
        ax.scatter(
            s["gamma"],
            s["prop_ack_gain_applied"],
            s=4,
            alpha=0.3,
            label=f"path {pid} proposed",
            color=color,
            marker="x",
        )
    ax.set_xlabel("gamma")
    ax.set_ylabel("ack gain")
    ax.set_title("gamma vs ACK gain (current vs proposed)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / "gamma_vs_gain.png", dpi=150)
    plt.close(fig)

    # predicted ACK growth over time path 3
    fig, ax = plt.subplots(figsize=(10, 4))
    s3 = active[active["path_id"] == 3].sort_values("timestamp_ms")
    if not s3.empty:
        t0 = s3["timestamp_ms"].iloc[0]
        t = (s3["timestamp_ms"] - t0) / 1000.0
        ax.plot(t, s3["cur_ack_growth_factor"], label="current gain", alpha=0.8)
        ax.plot(t, s3["prop_ack_growth_factor"], label="proposed ack_gain", alpha=0.8)
        ax2 = ax.twinx()
        ax2.plot(t, s3["bw_bps"] / 1e6, color="gray", alpha=0.4, label="bw Mbps")
        ax.set_xlabel("time (s)")
        ax.set_ylabel("ACK growth factor")
        ax2.set_ylabel("bw (Mbps)", color="gray")
        ax.set_title("path_id=3: ACK growth factor vs throughput")
        ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(out_dir / "path3_ack_growth_vs_time.png", dpi=150)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay utility control law offline")
    parser.add_argument(
        "--session-dynamic",
        type=Path,
        default=Path("logs_exp/session_combined_deterioration_20260614_232155/combined_qaccess_t_dynamic"),
        help="Path to combined_qaccess_t_dynamic run directory",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("derived/combined_deterioration_compare/control_law_replay"),
    )
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[2]
    dyn_dir = args.session_dynamic if args.session_dynamic.is_absolute() else repo / args.session_dynamic
    out_dir = args.out if args.out.is_absolute() else repo / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    df = load_runtime_samples(dyn_dir)
    enriched = enrich_rows(df)

    # Validation: recomputed vs logged
    active = enriched[enriched["active"]].copy()
    if "gain" in active.columns:
        active["gain_logged"] = pd.to_numeric(active["gain"], errors="coerce")
        active["gain_err"] = (active["gain_clamped"] - active["gain_logged"]).abs()
        match_rate = float((active["gain_err"] < 0.002).mean())
    else:
        match_rate = float("nan")

    enriched.to_csv(out_dir / "per_row_control_law_comparison.csv", index=False)

    # Summaries
    summaries = []
    for pid in sorted(enriched["path_id"].unique()):
        summaries.append(path_summary(enriched, int(pid)))
    pd.DataFrame(summaries).to_csv(out_dir / "path_summary.csv", index=False)

    clamp_rates = {
        "gain_at_min_frac_all_active": float(active["gain_at_min"].mean()),
        "gain_at_max_frac_all_active": float(active["gain_at_max"].mean()),
        "ret_at_min_frac_all_active": float(active["ret_at_min"].mean()),
        "ret_at_max_frac_all_active": float(active["ret_at_max"].mean()),
        "gain_recompute_match_rate": match_rate,
    }
    pd.DataFrame([clamp_rates]).to_csv(out_dir / "clamp_hit_rates.csv", index=False)

    collapse = collapse_analysis(enriched, path_id=3)
    pd.DataFrame([collapse]).to_csv(out_dir / "path3_collapse_analysis.csv", index=False)

    # Gamma tests
    gamma_tests = []
    for pid in [1, 3]:
        sub = active[active["path_id"] == pid]
        if len(sub) < 3:
            continue
        gamma_tests.append(
            {
                "path_id": pid,
                "corr_gamma_gain": float(sub[["gamma", "gain_clamped"]].corr().iloc[0, 1]),
                "corr_gamma_retention": float(sub[["gamma", "retention_clamped"]].corr().iloc[0, 1]),
                "corr_gamma_gain_delay_term": float(sub[["gamma", "gain_delay_term"]].corr().iloc[0, 1]),
                "mean_gain_when_gamma_high": float(
                    sub.loc[sub["gamma"] >= sub["gamma"].median(), "gain_clamped"].mean()
                ),
                "mean_gain_when_gamma_low": float(
                    sub.loc[sub["gamma"] < sub["gamma"].median(), "gain_clamped"].mean()
                ),
                "mean_retention_when_gamma_high": float(
                    sub.loc[sub["gamma"] >= sub["gamma"].median(), "retention_clamped"].mean()
                ),
                "mean_retention_when_gamma_low": float(
                    sub.loc[sub["gamma"] < sub["gamma"].median(), "retention_clamped"].mean()
                ),
            }
        )
    pd.DataFrame(gamma_tests).to_csv(out_dir / "gamma_effect_tests.csv", index=False)

    make_plots(enriched, out_dir)

    print(f"Wrote replay outputs to {out_dir}")
    print(f"Gain recompute match rate (±0.002): {match_rate:.3f}")
    for s in summaries:
        if s.get("n", 0) > 0:
            print(
                f"path {s['path_id']}: gain mean={s['gain_clamped']['mean']:.4f} "
                f"clamp_min={s['gain_at_min_frac']:.2%} clamp_max={s['gain_at_max_frac']:.2%} "
                f"corr(gamma,gain)={s['corr_gamma_gain']:.3f}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
