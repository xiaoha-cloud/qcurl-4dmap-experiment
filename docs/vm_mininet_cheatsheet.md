# VM cheat sheet (Mininet + 4D-MAP + evaluate)

**Typical layout on the lab VM (adjust if your home differs):**

- Repo root: `/home/mininet/Project/4D-MAP`
- Videos: `/home/mininet/Videos/`
- Logs: `/home/mininet/Project/4D-MAP/logs_exp/vm_run_<RUN_ID>/`

Replace `4D-MAP` with your folder name if you clone into `qcurl-4dmap-experiment` instead.

---

## 1. Git: update from GitHub (VM)

```bash
cd /home/mininet/Project/4D-MAP
git fetch origin
git status
git checkout feature/online-projection-weights
git pull origin feature/online-projection-weights
```

If `pull` complains about local changes to tracked files, either **stash** or **discard** only what you intend:

```bash
git stash push -m "vm local"   # or: git restore <file>
```

**Do not** commit `logs_exp/`, `4dmap` binary, or `server/qserver` if your `.gitignore` is set up; rebuild binaries locally after pull.

**Push from VM** (after commit):

```bash
git add <files>
git commit -m "msg"
git push origin feature/online-projection-weights
```

**Push from Mac** is the same remote; always `pull` on the VM before long runs if you work on two machines.

---

## 2. Build binaries (after code / submodule changes)

From **repo root** `/home/mininet/Project/4D-MAP`:

```bash
cd /home/mininet/Project/4D-MAP
GO111MODULE=on go build -o 4dmap .
```

```bash
cd /home/mininet/Project/4D-MAP/server
GO111MODULE=on go build -o qserver .
cd /home/mininet/Project/4D-MAP
```

`mp_topo.py` expects `./4dmap` and `./qserver` in those locations.

---

## 3. Media: MP4 is often rejected by push; remux to FLV

Error `unsupport` in `push_*.log` → use FLV.

```bash
ffmpeg -i /home/mininet/Videos/new_video_200s.mp4 -c copy /home/mininet/Videos/new_video_200s.flv
```

If that fails, re-encode (slower): `ffmpeg -i ... -c:v libx264 -c:a aac ... out.flv`

**Duration:** a ~3m30s file is fine; set Mininet `--timeout` slightly above the segment you need (e.g. `220` for a 200s experiment design).

---

## 4. One Route A run (dynamic bandwidth on path A, learn mode example)

**Must run with `sudo` (Mininet).** From repo root:

```bash
cd /home/mininet/Project/4D-MAP
sudo python3 scripts/mininet/mp_topo.py --run-exp --timeout 220 \
  --utility-mode learn --log-control \
  --dynamic-bw-profile scripts/mininet/bw_profile.route_a_200s.env \
  --input-flv /home/mininet/Videos/new_video_200s.flv
```

Swap `learn` for `T`, `D`, `L`, `auto`, or `baseline` for comparisons.

**Success checks:**

- No `unsupport` in `push_*.log`.
- Run lasts until timeout or media ends, **not** seconds-only exit.
- `tc_bw_*.log` shows **all** steps (0 / 50 / 100 s) for the current profile, not just step 1.

```bash
cat /home/mininet/Project/4D-MAP/logs_exp/vm_run_<RUN_ID>/tc_bw_<RUN_ID>.log
```

---

## 5. Quick log inspection (VM)

```bash
RUN=/home/mininet/Project/4D-MAP/logs_exp/vm_run_<RUN_ID>
tail -50 "$RUN/push_<RUN_ID>.log"
grep '\[m\]monitor' "$RUN/pull_<RUN_ID>.log" | head -20
grep '\[utility\]' "$RUN/pull_<RUN_ID>.log" | head -20
```

---

## 6. Evaluate (no sudo): CSV + figures

Install once if needed: `pip3 install --user pandas numpy matplotlib`

From repo root, **one run**:

```bash
cd /home/mininet/Project/4D-MAP
python3 scripts/analyze/route_a_evaluate.py \
  --out /home/mininet/Project/4D-MAP/derived/report_$(date +%Y%m%d_%H%M) \
  -r learn:/home/mininet/Project/4D-MAP/logs_exp/vm_run_<RUN_ID>
```

**Several methods** (edit paths and run IDs):

```bash
python3 scripts/analyze/route_a_evaluate.py \
  --out /home/mininet/Project/4D-MAP/derived/report_$(date +%Y%m%d) \
  -r T:/home/mininet/Project/4D-MAP/logs_exp/vm_run_<ID_T> \
  -r learn:/home/mininet/Project/4D-MAP/logs_exp/vm_run_<ID_learn>
```

**Replicates** (mean±std): `-r T:/path/run1,/path/run2`

Outputs:

- `summary_by_run_phase.csv` — per run, per phase, metrics
- `summary_method_phase_meanstd.csv` — aggregated
- `figs/bar_*.png`, `figs/line_*.png` — bar = compare methods *within* phase; line = trend *across* phases
- `figs/timeseries_<method>.png` — one long run per method

Optional offline gradient check (learn mode):

```bash
python3 scripts/analyze/verify_projected_gradient.py /home/mininet/Project/4D-MAP/logs_exp/vm_run_<RUN_ID>/pull_<RUN_ID>.log
```

---

## 7. What to *expect* from a good evaluate (Route A + FLV + full `tc` steps)

- **Phases:** Up to **four** steady windows (P1–P4) if `route_a_four_steady_windows` applies; otherwise **three** segments from `tc` steps.
- **Throughput:** Should **react** to capacity steps (not flat noise only); may not match link Mbps exactly (app rate, two paths, scheduler).
- **OWD/RTT/loss:** May rise under congestion or when cap drops; cross-method differences show in bar/line plots.
- **If the run was short** (push died early, only one `tc` step): phase summaries are still computed but **are not valid** for a 200s design — rerun with FLV and full duration first.

---

## 8. Session folder layout (if you use `run_experiment_matrix.sh`)

Session paths look like: `logs_exp/session_YYYYMMDD_HHMMSS/phase2_*/…`  
Use the **directory that contains** `pull_*.log` and `tc_bw_*.log` in `-r method:path`.

---

## 9. Common problems

| Symptom | Check |
|--------|--------|
| `unsupport` in push | Use `.flv` from ffmpeg remux. |
| Push exits in seconds | `push_*.log` tail; server log; rebuild `4dmap` / `qserver`. |
| `tc_bw` only 1 line | Connection ended before next step; extend timeout or fix push. |
| `flag not defined: -run-id` | Rebuild `4dmap` from current branch. |

---

*Last aligned with profile `scripts/mininet/bw_profile.route_a_200s.env` (steps at 0, 50, 100 s) and branch `feature/online-projection-weights`.*
