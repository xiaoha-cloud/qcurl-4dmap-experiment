"""
Q-ACCeSS utility math aligned with quic-go43DMAP/utility_controller.go.
"""

from __future__ import annotations

import math

BW_REF_BPS = 30_000_000.0
DELAY_REF_MS = 100.0
DELAY_TREND_REF_MS = 50.0
LOSS_REF = 0.01
MIN_GAIN = 0.80
MAX_GAIN = 1.20
MIN_BACKOFF = 0.90
MAX_BACKOFF = 1.10

ALPHA_CANDIDATES = [0.60, 0.70, 0.80, 0.90]
BETA_CANDIDATES = [0.05, 0.10, 0.20, 0.30]
GAMMA_CANDIDATES = [0.05, 0.10, 0.20, 0.30]


def _sanitize(v: float) -> float:
    if math.isnan(v) or math.isinf(v) or v < 0:
        return 0.0
    return float(v)


def clamp(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def normalize_g(bw_bps: float) -> float:
    return clamp(_sanitize(bw_bps) / BW_REF_BPS, 0.0, 1.0)


def normalize_l(loss: float) -> float:
    return clamp(_sanitize(loss) / LOSS_REF, 0.0, 1.0)


def normalize_d(owd_ms: float, delay_gradient_ms: float) -> float:
    delay_level = clamp(_sanitize(owd_ms) / DELAY_REF_MS, 0.0, 1.0)
    trend = 0.0
    if delay_gradient_ms > 0:
        trend = clamp(_sanitize(delay_gradient_ms) / DELAY_TREND_REF_MS, 0.0, 1.0)
    return clamp(0.7 * delay_level + 0.3 * trend, 0.0, 1.0)


def qaccess_utility(g_total: float, norm_d: float, norm_l: float, alpha: float, beta: float, gamma: float) -> float:
    if g_total <= 0:
        g_total = 1e-9
    reward = g_total**alpha
    penalty = beta * g_total * norm_l + gamma * g_total * norm_d
    return reward - penalty


def qaccess_gain_backoff(
    g_total: float,
    norm_d: float,
    norm_l: float,
    alpha: float,
    beta: float,
    gamma: float,
) -> tuple[float, float]:
    gain = 1.0 + 0.20 * (g_total**alpha) - 0.10 * beta * 5 * norm_l - 0.05 * gamma * 5 * norm_d
    backoff = 1.0 - 0.08 * (g_total**alpha) + 0.05 * beta * 5 * norm_l + 0.03 * gamma * 5 * norm_d
    return clamp(gain, MIN_GAIN, MAX_GAIN), clamp(backoff, MIN_BACKOFF, MAX_BACKOFF)


def path_active(bw_bps: float, owd_ms: float, inflight_bytes: float, min_inflight: int = 1024) -> bool:
    if _sanitize(bw_bps) > 0 or _sanitize(owd_ms) > 0:
        return True
    return inflight_bytes > min_inflight


def candidate_triples() -> list[tuple[float, float, float]]:
    out: list[tuple[float, float, float]] = []
    for a in ALPHA_CANDIDATES:
        for b in BETA_CANDIDATES:
            for g in GAMMA_CANDIDATES:
                out.append((a, b, g))
    return out
