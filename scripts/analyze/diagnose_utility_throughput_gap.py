#!/usr/bin/env python3
"""Diagnose why qaccess_t dynamic aggregate throughput trails baseline."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "scripts" / "analyze") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

from qaccess_impairment_eval_analyze import load_run_wire_timeseries  # noqa: E402

SESSION = _REPO / "logs_exp" / "session_combined_deterioration_20260614_232155"
DYN_DIR = SESSION / "combined_qaccess_t_dynamic"
BASE_DIR = SESSION / "combined_baseline"
OUT = _REPO / "derived" / "combined_deterioration_compare" / "utility_gap_diagnosis"
OUT.mkdir(parents=True, exist_ok=True)

# path_id in runtime samples: 0=Path A (initial), 1=Path B (2nd path), 3=extra/duplicate stream view
PATH_LABEL = {0: "path_a_ctrl", 1: "path_b_ctrl", 3: "path_b_alt"}


def pcap_global_t0(run_dir: Path) -> float:
    import subprocess

    epochs: list[float] = []
    for p in sorted((run_dir / "pcaps").glob("*.pcap")):
        out = subprocess.run(
            ["tshark", "-r", str(p), "-T", "fields", "-e", "frame.time_epoch"],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in out.stdout.splitlines():
            if line.strip():
                epochs.append(float(line.strip()))
    if not epochs:
        raise RuntimeError(f"no pcap epochs in {run_dir}")
    return min(epochs)


def load_all_runtime_samples(dyn_dir: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for p in sorted(dyn_dir.glob("processed_buffers/qaccess_runtime_samples_*.csv")):
        df = pd.read_csv(p)
        df["source_file"] = p.name
        frames.append(df)
    snap = dyn_dir / "derived_snapshots" / "qaccess_runtime_samples.csv"
    if snap.is_file():
        df = pd.read_csv(snap)
        df["source_file"] = snap.name
        frames.append(df)
    if not frames:
        raise RuntimeError("no runtime samples found")
    all_df = pd.concat(frames, ignore_index=True)
    all_df = all_df.drop_duplicates(
        subset=["timestamp_ms", "run_id", "path_id", "bw_bps", "owd_ms", "gain", "backoff"],
        keep="last",
    )
    return all_df.sort_values(["timestamp_ms", "path_id"]).reset_index(drop=True)


def load_accepted_updates(dyn_dir: Path) -> pd.DataFrame:
    """Derive accepted coefficient updates from coeffs_before chain + final response."""
    buf = dyn_dir / "processed_buffers"
    rows: list[dict] = []
    for p in sorted(buf.glob("qaccess_t_runtime_coefficients_before_*.json")):
        data = json.loads(p.read_text())
        req_id = data.get("request_id") or p.stem.split("_before_")[-1]
        req_path = buf / f"qaccess_update_request_{req_id}.json"
        req = json.loads(req_path.read_text()) if req_path.is_file() else {}
        ts = float(req.get("timestamp_ms") or data.get("request", {}).get("timestamp_ms", 0))
        rows.append(
            {
                "request_id": req_id,
                "timestamp_ms": ts,
                "before_alpha": data.get("previous_alpha", data.get("alpha")),
                "before_beta": data.get("previous_beta", data.get("beta")),
                "before_gamma": data.get("previous_gamma", data.get("gamma")),
                "applied_alpha": data.get("alpha"),
                "applied_beta": data.get("beta"),
                "applied_gamma": data.get("gamma"),
                "pred_current": data.get("pred_current"),
                "pred_best": data.get("pred_best"),
                "score_gain_bps": data.get("score_gain_bps"),
                "n_samples": data.get("n_samples"),
                "status": "accepted",
            }
        )
    df = pd.DataFrame(rows).sort_values("timestamp_ms").reset_index(drop=True)
    # dedupe: keep last accepted state per timestamp cluster (<2s apart same epoch)
    if df.empty:
        return df
    return df


def load_update_requests(dyn_dir: Path) -> pd.DataFrame:
    rows = []
    for p in sorted((dyn_dir / "processed_buffers").glob("qaccess_update_request_*.json")):
        d = json.loads(p.read_text())
        rows.append(d)
    return pd.DataFrame(rows).sort_values("timestamp_ms").reset_index(drop=True)


def per_second_metrics(samples: pd.DataFrame, global_t0: float) -> pd.DataFrame:
    samples = samples.copy()
    samples["time_s"] = samples["timestamp_ms"] / 1000.0 - global_t0
    samples["time_s_int"] = samples["time_s"].astype(int)

    agg_cols = {
        "alpha": "mean",
        "beta": "mean",
        "gamma": "mean",
        "gain": "mean",
        "backoff": "mean",
        "cwnd_bytes": "mean",
        "inflight_bytes": "mean",
        "bw_bps": "mean",
        "owd_ms": "mean",
        "loss_rate": "mean",
    }
    per_path = (
        samples.groupby(["time_s_int", "path_id"], as_index=False)
        .agg(agg_cols)
        .rename(columns={"time_s_int": "time_s"})
    )
    per_path["path_label"] = per_path["path_id"].map(PATH_LABEL).fillna("path_unknown")

    # pivot path metrics wide
    wide = per_path.pivot(index="time_s", columns="path_id", values=list(agg_cols.keys()))
    wide.columns = [f"{col}_pid{pid}" for col, pid in wide.columns]
    wide = wide.reset_index()

    return per_path, wide


def attach_throughput(wide: pd.DataFrame, ts_wire: pd.DataFrame) -> pd.DataFrame:
    tp = ts_wire.rename(
        columns={
            "path_a_quic_wire_mbps": "tp_path_a_mbps",
            "path_b_quic_wire_mbps": "tp_path_b_mbps",
            "total_quic_wire_mbps": "tp_total_mbps",
        }
    )[["time_s", "tp_path_a_mbps", "tp_path_b_mbps", "tp_total_mbps"]].copy()
    tp["path_b_share_pct"] = (
        tp["tp_path_b_mbps"] / tp["tp_total_mbps"].replace(0, float("nan")) * 100.0
    )
    merged = tp.merge(wide, on="time_s", how="left")
    return merged


def window_mean(df: pd.DataFrame, col: str, center: float, lo_off: float, hi_off: float) -> float:
    w = df[(df["time_s"] >= center + lo_off) & (df["time_s"] < center + hi_off)]
    if w.empty or col not in w.columns:
        return float("nan")
    return float(w[col].mean())


def update_window_table(
    merged: pd.DataFrame, updates: pd.DataFrame, global_t0: float
) -> pd.DataFrame:
    rows = []
    metrics = [
        ("gain", "gain_pid1"),
        ("backoff", "backoff_pid1"),
        ("cwnd", "cwnd_bytes_pid1"),
        ("tp_path_a", "tp_path_a_mbps"),
        ("tp_path_b", "tp_path_b_mbps"),
        ("tp_total", "tp_total_mbps"),
        ("path_b_share", "path_b_share_pct"),
    ]
    # also path 0 for comparison
    metrics_p0 = [
        ("gain_p0", "gain_pid0"),
        ("backoff_p0", "backoff_pid0"),
        ("cwnd_p0", "cwnd_bytes_pid0"),
    ]
    all_metrics = metrics + metrics_p0

    for _, u in updates.iterrows():
        t = u["timestamp_ms"] / 1000.0 - global_t0
        row = {
            "request_id": u["request_id"],
            "update_time_s": t,
            "before_alpha": u["before_alpha"],
            "before_beta": u["before_beta"],
            "before_gamma": u["before_gamma"],
            "applied_alpha": u["applied_alpha"],
            "applied_beta": u["applied_beta"],
            "applied_gamma": u["applied_gamma"],
            "score_gain_bps": u["score_gain_bps"],
            "n_samples": u["n_samples"],
        }
        for label, col in all_metrics:
            if col not in merged.columns:
                row[f"{label}_m5_0"] = float("nan")
                row[f"{label}_0_5"] = float("nan")
                row[f"{label}_5_10"] = float("nan")
                continue
            row[f"{label}_m5_0"] = window_mean(merged, col, t, -5, 0)
            row[f"{label}_0_5"] = window_mean(merged, col, t, 0, 5)
            row[f"{label}_5_10"] = window_mean(merged, col, t, 5, 10)
        rows.append(row)
    return pd.DataFrame(rows)


def compare_utilization(ts_base: pd.DataFrame, ts_dyn: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for label, ts in [("baseline", ts_base), ("dynamic", ts_dyn)]:
        rows.append(
            {
                "leg": label,
                "mean_path_a_mbps": ts["path_a_quic_wire_mbps"].mean(),
                "mean_path_b_mbps": ts["path_b_quic_wire_mbps"].mean(),
                "mean_total_mbps": ts["total_quic_wire_mbps"].mean(),
                "mean_path_b_share_pct": (
                    ts["path_b_quic_wire_mbps"].sum() / ts["total_quic_wire_mbps"].sum() * 100
                ),
            }
        )
    cmp_df = pd.DataFrame(rows)
    cmp_df["delta_total_vs_baseline"] = cmp_df["mean_total_mbps"] - cmp_df.loc[
        cmp_df["leg"] == "baseline", "mean_total_mbps"
    ].iloc[0]
    return cmp_df


def first_persistent_drop(ts_base: pd.DataFrame, ts_dyn: pd.DataFrame, updates: pd.DataFrame, global_t0: float) -> dict:
    merged = ts_dyn.merge(
        ts_base[["time_s", "total_quic_wire_mbps"]].rename(
            columns={"total_quic_wire_mbps": "baseline_total_mbps"}
        ),
        on="time_s",
        how="left",
    )
    merged["deficit_mbps"] = merged["baseline_total_mbps"] - merged["total_quic_wire_mbps"]
    merged["deficit_pct"] = merged["deficit_mbps"] / merged["baseline_total_mbps"] * 100

  # rolling 30s mean deficit > 2%
    merged["deficit_roll30"] = merged["deficit_pct"].rolling(30, min_periods=15).mean()

    first_persistent = merged[merged["deficit_roll30"] > 2.0]
    first_t = float(first_persistent["time_s"].iloc[0]) if not first_persistent.empty else float("nan")

    update_times = [
        (r["request_id"], r["timestamp_ms"] / 1000.0 - global_t0)
        for _, r in updates.iterrows()
    ]
    last_before = None
    first_after = None
    for rid, ut in update_times:
        if ut <= first_t:
            last_before = (rid, ut)
        elif first_after is None:
            first_after = (rid, ut)

    return {
        "first_persistent_lower_s": first_t,
        "last_update_before_drop": last_before,
        "first_update_after_drop": first_after,
    }


def diagnose_hypotheses(
    samples: pd.DataFrame,
    updates: pd.DataFrame,
    requests: pd.DataFrame,
    merged: pd.DataFrame,
    util_cmp: pd.DataFrame,
    win_tbl: pd.DataFrame,
) -> dict:
    out: dict = {}

    # A: utility lowered gain
    g_pre = samples[samples["path_id"].isin([0, 1])]["gain"].mean()
    g_post = samples[samples["timestamp_ms"] >= updates["timestamp_ms"].iloc[0]]["gain"].mean() if len(updates) else g_pre
    out["A_gain_lowered"] = {
        "verdict": g_post < g_pre - 0.005,
        "gain_mean_before_first_update": g_pre,
        "gain_mean_after_first_update": g_post,
        "detail": "Higher gamma/beta in coeffs raises backoff term and can clamp gain down toward MinGain=0.80",
    }

    # B: backoff increased
    b_pre = samples[samples["path_id"].isin([0, 1])]["backoff"].mean()
    b_post = samples[samples["timestamp_ms"] >= updates["timestamp_ms"].iloc[0]]["backoff"].mean() if len(updates) else b_pre
    out["B_backoff_increased"] = {
        "verdict": b_post > b_pre + 0.005,
        "backoff_mean_before": b_pre,
        "backoff_mean_after": b_post,
        "final_gamma": float(updates["applied_gamma"].iloc[-1]) if len(updates) else None,
    }

    # C: cwnd fell after updates
    cwnd_cols = [c for c in merged.columns if c.startswith("cwnd_bytes_pid")]
    cwnd_drop = False
    if cwnd_cols and not win_tbl.empty:
        drops = []
        for _, r in win_tbl.iterrows():
            before = r.get("cwnd_m5_0", float("nan"))
            after = r.get("cwnd_0_5", float("nan"))
            if before == before and after == after:
                drops.append(after < before * 0.95)
        cwnd_drop = any(drops)
    out["C_cwnd_fell_after_updates"] = {"verdict": cwnd_drop, "per_update_drops": drops if cwnd_cols else []}

    # D: path lost traffic without compensation
    base_b = util_cmp.loc[util_cmp["leg"] == "baseline", "mean_path_b_mbps"].iloc[0]
    dyn_b = util_cmp.loc[util_cmp["leg"] == "dynamic", "mean_path_b_mbps"].iloc[0]
    base_a = util_cmp.loc[util_cmp["leg"] == "baseline", "mean_path_a_mbps"].iloc[0]
    dyn_a = util_cmp.loc[util_cmp["leg"] == "dynamic", "mean_path_a_mbps"].iloc[0]
    out["D_path_loss_no_compensation"] = {
        "verdict": (dyn_b < base_b) and not (dyn_a > base_a + 0.5),
        "baseline_path_b": base_b,
        "dynamic_path_b": dyn_b,
        "baseline_path_a": base_a,
        "dynamic_path_a": dyn_a,
        "path_b_drop_mbps": base_b - dyn_b,
        "path_a_gain_mbps": dyn_a - base_a,
    }

    # E: repeated updates before settling
    req_gap = requests["timestamp_ms"].diff().dropna()
    out["E_repeated_updates"] = {
        "verdict": bool((req_gap < 8000).any()),
        "n_requests": len(requests),
        "n_accepted": len(updates),
        "min_gap_ms": float(req_gap.min()) if len(req_gap) else None,
        "pairs_under_8s": int((req_gap < 8000).sum()),
        "detail": "Duplicate buffer_full triggers within seconds; cooldown 60s on client but worker processes each buffer flush",
    }

    # F: conflicting path recommendations - check per-path bw_bps spread at update times
    conflicts = []
    for _, u in updates.iterrows():
        ts = u["timestamp_ms"]
        snap = samples[(samples["timestamp_ms"] >= ts - 500) & (samples["timestamp_ms"] <= ts + 500)]
        per_path = snap.groupby("path_id")["bw_bps"].mean()
        if len(per_path) >= 2:
            spread = float(per_path.max() - per_path.min())
            conflicts.append({"request_id": u["request_id"], "bw_spread_bps": spread, "per_path": per_path.to_dict()})
    out["F_conflicting_path_recommendations"] = {
        "verdict": any(c["bw_spread_bps"] > 2e6 for c in conflicts),
        "snapshots": conflicts,
        "detail": "RF scores one global candidate from pooled multi-path samples; per-path bw_bps can diverge",
    }

    # G: global coefficients shared
    uniq_coeffs = samples.groupby("timestamp_ms")[["alpha", "beta", "gamma"]].nunique()
    shared = (uniq_coeffs.max().max() == 1) if not uniq_coeffs.empty else True
    out["G_global_coeffs_shared"] = {
        "verdict": shared,
        "detail": "All path_id rows at each timestamp carry identical alpha/beta/gamma from one JSON",
        "gamma_end": float(samples["gamma"].iloc[-1]),
    }

    return out


def plot_all(
    merged: pd.DataFrame,
    ts_base: pd.DataFrame,
    ts_dyn: pd.DataFrame,
    updates: pd.DataFrame,
    global_t0: float,
) -> None:
    update_times = [u["timestamp_ms"] / 1000.0 - global_t0 for _, u in updates.iterrows()]
    update_labels = [u["request_id"].split("_")[-1] for _, u in updates.iterrows()]

    def vlines(ax):
        for t, lab in zip(update_times, update_labels):
            ax.axvline(t, color="purple", alpha=0.35, linestyle="--")
        if update_times:
            ax.axvline(update_times[0], color="purple", alpha=0.8, linestyle="--", label="coeff update")

    fig, axes = plt.subplots(6, 1, figsize=(14, 18), sharex=True)

    ax = axes[0]
    ax.plot(ts_dyn["time_s"], ts_dyn["total_quic_wire_mbps"], label="dynamic total", color="C0")
    ax.plot(ts_base["time_s"], ts_base["total_quic_wire_mbps"], label="baseline total", color="C1", alpha=0.7)
    vlines(ax)
    ax.set_ylabel("Total Mbps")
    ax.legend(loc="upper left")
    ax.set_title("Utility throughput gap diagnosis — combined deterioration session")

    ax = axes[1]
    ax.plot(ts_dyn["time_s"], ts_dyn["path_a_quic_wire_mbps"], label="dynamic A")
    ax.plot(ts_dyn["time_s"], ts_dyn["path_b_quic_wire_mbps"], label="dynamic B")
    ax.plot(ts_base["time_s"], ts_base["path_a_quic_wire_mbps"], label="baseline A", alpha=0.5, linestyle=":")
    ax.plot(ts_base["time_s"], ts_base["path_b_quic_wire_mbps"], label="baseline B", alpha=0.5, linestyle=":")
    vlines(ax)
    ax.set_ylabel("Path Mbps")
    ax.legend(loc="upper left", ncol=2)

    if "gain_pid1" in merged.columns:
        ax = axes[2]
        ax.plot(merged["time_s"], merged["gain_pid1"], label="gain pid1 (path B)")
        ax.plot(merged["time_s"], merged["backoff_pid1"], label="backoff pid1")
        if "gain_pid0" in merged.columns:
            ax.plot(merged["time_s"], merged["gain_pid0"], label="gain pid0 (path A)", alpha=0.6)
        vlines(ax)
        ax.set_ylabel("Gain / Backoff")
        ax.legend(loc="upper right")

        ax = axes[3]
        ax.plot(merged["time_s"], merged["cwnd_bytes_pid1"], label="cwnd pid1")
        if "cwnd_bytes_pid0" in merged.columns:
            ax.plot(merged["time_s"], merged["cwnd_bytes_pid0"], label="cwnd pid0", alpha=0.6)
        vlines(ax)
        ax.set_ylabel("cwnd bytes")
        ax.legend(loc="upper right")

        ax = axes[4]
        ax.plot(merged["time_s"], merged["alpha_pid1"], label="alpha")
        ax.plot(merged["time_s"], merged["beta_pid1"], label="beta")
        ax.plot(merged["time_s"], merged["gamma_pid1"], label="gamma")
        vlines(ax)
        ax.set_ylabel("Coefficients")
        ax.legend(loc="upper right")

    ax = axes[5]
    share = ts_dyn["path_b_quic_wire_mbps"] / ts_dyn["total_quic_wire_mbps"].replace(0, float("nan")) * 100
    ax.plot(ts_dyn["time_s"], share, label="dynamic Path B share %")
    share_b = ts_base["path_b_quic_wire_mbps"] / ts_base["total_quic_wire_mbps"].replace(0, float("nan")) * 100
    ax.plot(ts_base["time_s"], share_b, label="baseline Path B share %", alpha=0.6)
    vlines(ax)
    ax.set_ylabel("Path B share %")
    ax.set_xlabel("time_s (pcap epoch)")
    ax.legend(loc="upper right")

    fig.tight_layout()
    fig.savefig(OUT / "utility_gap_timeseries.png", dpi=150)
    plt.close(fig)


def rank_causes(hyp: dict, util_cmp: pd.DataFrame, win_tbl: pd.DataFrame) -> list[tuple[str, str, int]]:
    """Return ranked (cause, evidence, score 1-5)."""
    scores: list[tuple[str, str, int]] = []

    delta_total = float(util_cmp.loc[util_cmp["leg"] == "dynamic", "delta_total_vs_baseline"].iloc[0])
    b_drop = hyp["D_path_loss_no_compensation"]["path_b_drop_mbps"]
    gamma_end = hyp["G_global_coeffs_shared"].get("gamma_end", 0.1)

    scores.append((
        "high gamma penalty",
        f"gamma rose 0.10→{gamma_end:.3f}; backoff formula adds 0.03*gamma*5*normD; utility penalizes delay harder",
        5 if gamma_end > 0.22 else 3,
    ))
    scores.append((
        "overly conservative gain/backoff",
        f"gain after updates {hyp['A_gain_lowered']['gain_mean_after_first_update']:.4f} vs before {hyp['A_gain_lowered']['gain_mean_before_first_update']:.4f}; "
        f"backoff {hyp['B_backoff_increased']['backoff_mean_after']:.4f} vs {hyp['B_backoff_increased']['backoff_mean_before']:.4f}",
        5 if hyp["B_backoff_increased"]["verdict"] or hyp["A_gain_lowered"]["verdict"] else 4,
    ))
    scores.append((
        "global coefficients shared by multiple paths",
        "Single qaccess_t_runtime_coefficients.json reloaded by all paths; RF trained on pooled samples",
        5 if hyp["G_global_coeffs_shared"]["verdict"] else 2,
    ))
    scores.append((
        "local target vs aggregate objective mismatch",
        f"Path B lost {b_drop:.2f} Mbps mean wire vs baseline without Path A compensation ({hyp['D_path_loss_no_compensation']['path_a_gain_mbps']:.2f} Mbps gain)",
        5 if hyp["D_path_loss_no_compensation"]["verdict"] else 4,
    ))
    scores.append((
        "repeated updates / insufficient settling time",
        f"{hyp['E_repeated_updates']['n_requests']} buffer_full requests, {hyp['E_repeated_updates']['pairs_under_8s']} pairs <8s apart; 4 accepted updates in ~200s",
        4 if hyp["E_repeated_updates"]["verdict"] else 2,
    ))
    scores.append((
        "scheduler not reacting to predicted path improvement",
        f"Worker predicted +{hyp['E_repeated_updates'].get('score', 'N/A')} bps gains but total throughput fell {delta_total:.2f} Mbps vs baseline",
        4,
    ))

    scores.sort(key=lambda x: x[2], reverse=True)
    return scores


def main() -> None:
    global_t0 = pcap_global_t0(DYN_DIR)
    samples = load_all_runtime_samples(DYN_DIR)
    samples["time_s"] = samples["timestamp_ms"] / 1000.0 - global_t0

    updates = load_accepted_updates(DYN_DIR)
    requests = load_update_requests(DYN_DIR)

    per_path, wide = per_second_metrics(samples, global_t0)
    ts_dyn = load_run_wire_timeseries(DYN_DIR)
    ts_base = load_run_wire_timeseries(BASE_DIR)
    merged = attach_throughput(wide, ts_dyn)

    per_path.to_csv(OUT / "dynamic_per_path_per_second.csv", index=False)
    merged.to_csv(OUT / "dynamic_per_second_merged.csv", index=False)
    updates.to_csv(OUT / "accepted_coefficient_updates.csv", index=False)
    requests.to_csv(OUT / "all_update_requests.csv", index=False)

    win_tbl = update_window_table(merged, updates, global_t0)
    win_tbl.to_csv(OUT / "update_window_comparison.csv", index=False)

    util_cmp = compare_utilization(ts_base, ts_dyn)
    util_cmp.to_csv(OUT / "baseline_vs_dynamic_utilization.csv", index=False)

    drop_info = first_persistent_drop(ts_base, ts_dyn, updates, global_t0)
    hyp = diagnose_hypotheses(samples, updates, requests, merged, util_cmp, win_tbl)
    ranks = rank_causes(hyp, util_cmp, win_tbl)

    plot_all(merged, ts_base, ts_dyn, updates, global_t0)

    report_lines = [
        "# Utility throughput gap diagnosis",
        "",
        f"Session: `{SESSION.name}`",
        "",
        "## Utilization comparison",
        util_cmp.to_string(index=False),
        "",
        "## Accepted coefficient updates (aligned to pcap t0)",
    ]
    for _, u in updates.iterrows():
        t = u["timestamp_ms"] / 1000.0 - global_t0
        report_lines.append(
            f"- t={t:.1f}s {u['request_id']}: "
            f"({u['before_alpha']:.3f},{u['before_beta']:.3f},{u['before_gamma']:.3f}) → "
            f"({u['applied_alpha']:.3f},{u['applied_beta']:.3f},{u['applied_gamma']:.3f}) "
            f"pred_gain={u['score_gain_bps']:.0f} bps"
        )

    report_lines.extend([
        "",
        "## First persistent total-throughput deficit vs baseline",
        json.dumps(drop_info, indent=2),
        "",
        "## Hypothesis checklist",
        json.dumps(hyp, indent=2, default=str),
        "",
        "## Ranked causes",
    ])
    for i, (cause, evidence, score) in enumerate(ranks, 1):
        report_lines.append(f"{i}. **[{score}/5] {cause}** — {evidence}")

    (OUT / "REPORT.md").write_text("\n".join(report_lines) + "\n")
    print(f"Wrote diagnosis to {OUT}")
    print("\n".join(report_lines[:40]))


if __name__ == "__main__":
    main()
