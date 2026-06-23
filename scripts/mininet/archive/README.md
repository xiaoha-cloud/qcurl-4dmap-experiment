# Legacy Mininet scripts (not part of the current Q-ACCeSS-T workflow)

Historical Fig.7 capacity-change entry points have been removed. The current
evaluation pipeline uses the combined deterioration runner in the parent
directory. Shared topology and traffic-control helpers remain there because
they are still used by active experiments.

- `mp_topo.py`
- `tc_bw_steps.sh`
- `bw_profile.fig7_200s.env`

Archived here:

- `run_experiment_matrix_legacy.sh` — old Phase 1/2/3 matrix (T/D/L/learn/auto)
- `run_single_experiment.sh` — quick single-run wrapper with `fig7_um_*` labels
- Example / Route-A bandwidth and tc profiles

Supported utility modes: `baseline`, `qaccess_collect`, `qaccess_t`.
