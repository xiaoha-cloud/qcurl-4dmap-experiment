# Utility throughput gap diagnosis

Session: `session_combined_deterioration_20260614_232155`

## Utilization comparison
     leg  mean_path_a_mbps  mean_path_b_mbps  mean_total_mbps  mean_path_b_share_pct  delta_total_vs_baseline
baseline         21.889012          0.643142        22.532154               2.854328                 0.000000
 dynamic         21.378966          0.644173        22.023139               2.924983                -0.509015

## Accepted coefficient updates (aligned to pcap t0)
- t=8.4s 20260614_232546_1781479556319_1: (0.600,0.300,0.100) → (0.600,0.300,0.100) pred_gain=nan bps
- t=8.4s 20260614_232546_1781479556319_1: (0.600,0.300,0.100) → (0.700,0.200,0.100) pred_gain=1284846 bps
- t=16.3s 20260614_232546_1781479564123_1: (0.600,0.300,0.100) → (0.700,0.200,0.200) pred_gain=851336 bps
- t=76.2s 20260614_232546_1781479624113_2: (0.636,0.264,0.120) → (0.700,0.300,0.200) pred_gain=1811079 bps
- t=83.1s 20260614_232546_1781479630936_2: (0.636,0.264,0.120) → (0.700,0.200,0.220) pred_gain=815771 bps

## First persistent total-throughput deficit vs baseline
{
  "first_persistent_lower_s": 41.0,
  "last_update_before_drop": [
    "20260614_232546_1781479564123_1",
    16.25090193748474
  ],
  "first_update_after_drop": [
    "20260614_232546_1781479624113_2",
    76.24090194702148
  ]
}

## Hypothesis checklist
{
  "A_gain_lowered": {
    "verdict": "True",
    "gain_mean_before_first_update": 1.154166395821242,
    "gain_mean_after_first_update": 1.1451681054667617,
    "detail": "Higher gamma/beta in coeffs raises backoff term and can clamp gain down toward MinGain=0.80"
  },
  "B_backoff_increased": {
    "verdict": "False",
    "backoff_mean_before": 0.9431004788160187,
    "backoff_mean_after": 0.9476554209508299,
    "final_gamma": 0.22000000000000003
  },
  "C_cwnd_fell_after_updates": {
    "verdict": true,
    "per_update_drops": [
      true,
      true,
      true
    ]
  },
  "D_path_loss_no_compensation": {
    "verdict": "False",
    "baseline_path_b": 0.643141592920354,
    "dynamic_path_b": 0.6441730619469026,
    "baseline_path_a": 21.88901207079646,
    "dynamic_path_a": 21.37896605309734,
    "path_b_drop_mbps": -0.0010314690265486037,
    "path_a_gain_mbps": -0.5100460176991213
  },
  "E_repeated_updates": {
    "verdict": true,
    "n_requests": 8,
    "n_accepted": 5,
    "min_gap_ms": 6823.0,
    "pairs_under_8s": 4,
    "detail": "Duplicate buffer_full triggers within seconds; cooldown 60s on client but worker processes each buffer flush"
  },
  "F_conflicting_path_recommendations": {
    "verdict": true,
    "snapshots": [
      {
        "request_id": "20260614_232546_1781479556319_1",
        "bw_spread_bps": 99486557.82045099,
        "per_path": {
          "0": 15879442.128755365,
          "1": 38574037.56776557,
          "3": 115365999.94920635
        }
      },
      {
        "request_id": "20260614_232546_1781479556319_1",
        "bw_spread_bps": 99486557.82045099,
        "per_path": {
          "0": 15879442.128755365,
          "1": 38574037.56776557,
          "3": 115365999.94920635
        }
      },
      {
        "request_id": "20260614_232546_1781479564123_1",
        "bw_spread_bps": 123139071.6734694,
        "per_path": {
          "0": 4579776.0,
          "1": 27901386.753387533,
          "3": 127718847.6734694
        }
      }
    ],
    "detail": "RF scores one global candidate from pooled multi-path samples; per-path bw_bps can diverge"
  },
  "G_global_coeffs_shared": {
    "verdict": "True",
    "detail": "All path_id rows at each timestamp carry identical alpha/beta/gamma from one JSON",
    "gamma_end": 0.1528
  }
}

## Ranked causes
1. **[5/5] overly conservative gain/backoff** — gain after updates 1.1452 vs before 1.1542; backoff 0.9477 vs 0.9431
2. **[5/5] global coefficients shared by multiple paths** — Single qaccess_t_runtime_coefficients.json reloaded by all paths; RF trained on pooled samples
3. **[4/5] local target vs aggregate objective mismatch** — Path B lost -0.00 Mbps mean wire vs baseline without Path A compensation (-0.51 Mbps gain)
4. **[4/5] repeated updates / insufficient settling time** — 8 buffer_full requests, 4 pairs <8s apart; 4 accepted updates in ~200s
5. **[4/5] scheduler not reacting to predicted path improvement** — Worker predicted +N/A bps gains but total throughput fell -0.51 Mbps vs baseline
6. **[3/5] high gamma penalty** — gamma rose 0.10→0.153; backoff formula adds 0.03*gamma*5*normD; utility penalizes delay harder
