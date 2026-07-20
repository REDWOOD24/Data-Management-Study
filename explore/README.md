# Data Management Policy Exploration

Black-box exploration framework for tuning data management policies in the
[Data-Management-Study](../) CGSim simulation. One or more **proposer agents**
run in parallel—each learning independently from its own trial history—and
results are compared live with grouped transfer and staging plots.

This package does **not** modify the C++ plugin. It generates per-trial
`data_policy_config.json` files, invokes CGSim as a subprocess, and analyzes
`events.db` output.

## Proposer agents

| Agent | Status | Role |
|-------|--------|------|
| **`bayesian_opt`** | Default | Optuna TPE black-box optimization; learns from prior trials |
| **`rl_policy`** | Default | PyTorch REINFORCE policy with structured post-episode observation context (~51-dim) |
| **`random_search`** | Default | Memoryless uniform baseline for comparison |
| `bandit` | Supported | UCB1 over reactive/proactive template pairs (16 arms: 4×4) |

**Entry point:** [`scripts/run_exploration.py`](scripts/run_exploration.py) — defaults to
`bayesian_opt`, `rl_policy`, and `random_search` running in parallel (one thread per method).

## Overview

Each **trial** is one full CGSim run with a specific policy configuration:

1. Sample or select an action vector (policy parameters)
2. Build `data_policy_config.json` from the action
3. Run CGSim with an isolated trial directory
4. Extract per-job metrics from `events.db`
5. Aggregate reward over a configurable evaluation window
6. Log artifacts and feed the reward back to the agent

```text
Action → PolicyConfigBuilder → cg-sim → events.db → Metrics → Window → Objective → Reward
```

### Multi-method parallelism

Methods run **in parallel** (independent threads), but trials within each method
are **sequential** so learning agents retain memory between trials.

```text
explore/runs/{experiment}/
  run_config.json
  methods/
    bayesian_opt/trial_0000/ ...
    rl_policy/trial_0000/ ...
  plots/
    methods_comparison.png   # updated live after each completed trial
    methods_timing.csv       # updated live after each completed trial
```

### Two-level episode model

| Level | Meaning |
|-------|---------|
| **Training episode** | One full CGSim run (one policy config) |
| **Evaluation window** | How reward is computed from that run's events (full sim, time slice, job batch, or per-job) |

Evaluation windows are applied **post-simulation**—no plugin changes required.

## Prerequisites

- Python 3.10+
- Built CGSim executable and compiled plugin (see [main README](../README.md))
- Simulation config and topology already generated under `config/`

## Installation

```bash
cd explore
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` includes `optuna` (Bayesian optimization) and `torch` (RL policy).

## Configuration

Edit [`config/settings.yaml`](config/settings.yaml) before your first run:

```yaml
cg_sim_bin: /path/to/CGSim/build/cg-sim   # required
repo_root: ..
base_config: ../config/config.json
workload:
  jobs_file: ../input/mimic_job.csv
  num_jobs: -1
sim_timeout_sec: 3600
parallel_trials: 1
```

| File | Purpose |
|------|---------|
| [`config/action_space.yaml`](config/action_space.yaml) | Parameter bounds, types, and validity constraints |
| [`config/objectives.yaml`](config/objectives.yaml) | Objective plugins and aggregation defaults |

---

## Step-by-step workflow

Run these commands **from the repository root** (`Data-Management-Study/`).
Activate the virtualenv first if you use one:

```bash
cd explore && source .venv/bin/activate && cd ..
export PYTHONPATH=explore/src
```

### Step 0 — One-time setup

Point `cg_sim_bin` in `explore/config/settings.yaml` at your built `cg-sim` binary, then verify imports:

```bash
python -c "import datamgmt_explore; print('ok')"
```

**Why:** All later commands call CGSim as a subprocess. If the path is wrong, every trial fails with a penalty reward.

---

### Step 1 — (Optional) Quick smoke test with 30 jobs

Before a long run, confirm the pipeline works on a truncated workload:

```bash
python explore/scripts/run_exploration.py \
  --trials 5 \
  --max-jobs 30 \
  --no-plot \
  --experiment-name smoke_test
```

**Why:** Each full trial runs all 966 jobs and can take several minutes. `--max-jobs 30` finishes faster so you can catch config/plugin errors early.

**Check:** `explore/runs/smoke_test/methods/bayesian_opt/summary.json` and
`explore/runs/smoke_test/methods/rl_policy/summary.json` should show completed trials.

---

### Step 2 — Run exploration (the main command)

```bash
python explore/scripts/run_exploration.py
```

That is the full command — no flags required. Defaults:

| Setting | Default | Meaning |
|---------|---------|---------|
| `--agents` | `bayesian_opt,rl_policy,random_search` | Three methods in parallel |
| `--trials` | `50` | Trials per method |
| `--aggregation` | `mean` | Mean staging time across sites |
| `--seed` | `42` | Reproducible search |
| Objective | `avg_staging_time` | Average job staging time (fixed) |

Optional: name the run with `--experiment-name my_run`.

**Live comparison plot:** `explore/runs/{experiment}/plots/methods_comparison.png` is
updated after every completed trial (file only — no GUI window).

**Live timing CSV:** `explore/runs/{experiment}/plots/methods_timing.csv` is updated
after every completed trial. One row per trial; two columns per method:
`{method}_sim_sec` (CGSim wall time) and `{method}_explore_sec` (propose, analysis,
and agent update). Cells stay blank until that method finishes that trial.

**Outputs:**

- `explore/runs/{experiment}/run_config.json` — methods, seed, objective
- `explore/runs/{experiment}/methods/{agent}/summary.json` — per-method best trial and rewards
- `explore/runs/{experiment}/methods/{agent}/trial_XXXX/` — per-trial configs, `events.db`, plots
- `explore/runs/{experiment}/plots/methods_comparison.png` — grouped method comparison
- `explore/runs/{experiment}/plots/methods_timing.csv` — per-trial sim vs exploration timing

---

### Step 3 — Review plots and best trial

Regenerate or add experiment-level plots if needed:

```bash
python explore/scripts/plot_exploration.py \
  --experiment explore/runs/my_run \
  --experiment-plots
```

Per-method artifacts:

- `methods/{agent}/plots/objective_progress.png` — reward curve over trials
- `methods/{agent}/plots/trial_mean_stacked_bars.png` — mean stacked transfer bars per trial
- `plots/methods_comparison.png` — grouped comparison across methods

Open each method's `summary.json` and note `"best_trial"` → `trial_index` and `action`.

---

### Step 4 — (Alternative) Single-method or bandit runs

Run only Bayesian optimization:

```bash
python explore/scripts/run_exploration.py \
  --agents bayesian_opt \
  --trials 50 \
  --experiment-name bo_only
```

Template sweep with the bandit agent:

```bash
python explore/scripts/run_exploration.py \
  --agents bandit \
  --trials 24 \
  --seed 42 \
  --experiment-name bandit_templates
```

**Why:** The bandit pre-registers all 16 template arms (4 reactive × 4 proactive)
and runs UCB1 over them. It does **not** fine-tune continuous thresholds — use
`bayesian_opt` for that.

---

### Automatic framework behavior

- **Action-space masking** — inactive proactive/reactive params are set to defaults, not randomized ([`action_space.py`](src/datamgmt_explore/action_space.py))
- **Invalid BO suggestions** — Optuna prunes invalid threshold orderings instead of corrupting the surrogate model
- **Method-parallel orchestration** — each agent learns in its own thread and directory under `methods/`

---

### What **not** to do

| Mistake | Why it fails |
|---------|----------------|
| Running only one method when comparing learners | Default runs BO + RL + random search side by side |

---

### Quick reference — which command when?

| Goal | Command |
|------|---------|
| First-time sanity check | Step 1 (`run_exploration.py --max-jobs 30`) |
| **Main exploration run** | Step 2 (`run_exploration.py`) |
| Inspect plots / best policy | Step 3 (`plot_exploration.py`) |
| Single-method BO | Step 4 (`--agents bayesian_opt`) |
| Template-only exploration | Step 4 (`--agents bandit`) |
| Optional ablation baseline | `--agents random_search` only |

---

## CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--agents` | `bayesian_opt,rl_policy,random_search` | Comma-separated methods to run in parallel |
| `--agent` | — | Deprecated alias for a single method |
| `--trials` | `50` | Trials **per method** |
| `--window-mode` | `full` | `full`, `time`, `job_count`, or `per_job` |
| `--window-size` | — | Window size in seconds (time) or jobs (job_count) |
| `--window-stride` | — | Sliding window stride |
| `--aggregation` | `mean` | `mean`, `mean_of_site_means`, or `max_site_mean` |
| `--experiment-name` | auto-generated | Output directory under `runs/` |
| `--seed` | `42` | Random seed |
| `--max-jobs` | — | Truncate workload for faster iteration |
| `--settings` | `config/settings.yaml` | Settings file path |
| `--dry-run` | off | Build configs without running CGSim |
| `--no-plot` | off | Disable per-trial and comparison plotting |

## Plotting

During exploration (unless `--no-plot` or `--dry-run`), each trial with an `events.db`
immediately generates the same three plots as
[`scripts/plot_transfer_analysis.py`](../scripts/plot_transfer_analysis.py) under
`methods/{agent}/trial_{k}/plots/`:

- `transfer_heatmap.png`
- `site_ingress_egress.png`
- `top_connections.png`

After all trials, per-method plots and `failure_report.json` are written when sim errors are detected.

The live comparison plot (`plots/methods_comparison.png`) shows grouped stacked
transfer bars and average staging markers per method per trial.

The live timing CSV (`plots/methods_timing.csv`) records wall-clock seconds per
trial per method: CGSim simulation time and exploration overhead (propose,
metrics/objective analysis, agent update). Rows fill in as each method completes trials.

```bash
PYTHONPATH=explore/src python explore/scripts/plot_exploration.py \
  --experiment explore/runs/my_run \
  --experiment-plots
```

## Failure diagnosis

When CGSim aborts (non-zero exit), the framework still plots from partial `events.db`
and writes `failure_report.json` with parsed SimGrid errors. Common patterns:

| Error | Likely policy cause |
|-------|---------------------|
| `File X already exists at Site Y` | COPY/transfer to a site that already holds the file |
| `File X does not exist at Site Y` | MOVE removed the source replica before a later transfer |

## Action Space

Actions map to fields in [`config/data_policy_config.json`](../config/data_policy_config.json).
The searchable surface is declared in [`config/action_space.yaml`](config/action_space.yaml)
and applied by [`policy_builder.py`](src/datamgmt_explore/policy_builder.py).
Only **implemented** plugin parameters are exposed (see `plugin/src/policy.cpp`
and the main [Data Management Policies](../README.md#data-management-policies) section).

Reactive and proactive transfers are **always enabled** in exploration (masked on).
Explorers tune templates, thresholds, modes, destination policy, and staging bias.
Several intensity knobs remain fixed outside the action space today:

| Fixed knob | Value | Notes |
|------------|-------|-------|
| `proactive.interval` | **500 s** | Proactive tick period |
| `max_transfers_per_tick` (storage / network / hotset) | **1** | Per-tick background transfer cap |
| `hotset_replication.hotness_window` / `prediction_horizon` | **100** / **50** | Hardcoded defaults in the policy builder |

Inactive template-scoped parameters are reset to defaults each trial (action-space
masking), so only the active proactive template’s knobs affect the simulation.

### Reactive transfer

| Parameter | Type | Range / choices | Description |
|-----------|------|-----------------|-------------|
| `reactive.prefer_local_replica` | bool | T/F | Prefer a resting local replica when present |
| `reactive.remote_source_template` | int | `0–3` | Remote source strategy when multiple replicas exist: `first_replica`, `least_utilized_source`, `most_utilized_source`, `random_replica` |
| `reactive.random_seed` | int | `0–999999` | Seed for `random_replica` |

### Proactive transfer (global)

| Parameter | Type | Range / choices | Description |
|-----------|------|-----------------|-------------|
| `proactive.data_transfer_mode` | enum | `COPY`, `MOVE` | Background transfer mode for the active template |
| `proactive.transfer_template` | int | `0–3` | Active template: `0` storage_rebalance, `1` network_aware_rebalance, `2` hotset_replication, `3` job_input_prefetch |
| `proactive.site_staging_bias` | int | `0–2` | Destination/job ranking bias: `0` off, `1` high_staging_queue, `2` high_recent_staging (used by hotset + job-input prefetch) |

### Template `0` — storage_rebalance

Moves/copies files from high- to low-utilization sites.

| Parameter | Type | Range | Default | Description |
|-----------|------|-------|---------|-------------|
| `storage_rebalance.high_utilization_threshold` | float | `0.10–0.50` | `0.2` | Source-side util threshold |
| `storage_rebalance.low_utilization_threshold` | float | `0.55–0.95` | `0.8` | Destination-side util threshold |
| `storage_rebalance.file_pick` | int | `0–3` | `0` | `first_fit` / `largest_fit` / `smallest_fit` / `random_fit` |
| `storage_rebalance.skip_if_already_replica_on_destination` | bool | T/F | `true` | Skip files already present at destination |

Constraints: high &lt; low, and low − high ≥ `0.25`.

### Template `1` — network_aware_rebalance

Rebalance with path-aware filtering/ranking.

| Parameter | Type | Range | Default | Description |
|-----------|------|-------|---------|-------------|
| `network_aware_rebalance.high_utilization_threshold` | float | `0.05–0.40` | `0.05` | Source util threshold |
| `network_aware_rebalance.low_utilization_threshold` | float | `0.60–1.0` | `1.0` | Destination util threshold |
| `network_aware_rebalance.path_metric` | int | `0–2` | `0` | `estimated_transfer_time` / `link_load` / `bandwidth_only` |
| `network_aware_rebalance.max_path_load` | float | `0.1–1.0` | `1.0` | Maximum acceptable path load |
| `network_aware_rebalance.file_pick` | int | `0–3` | `0` | Same file-pick modes as storage rebalance |

Constraints: high &lt; low, and low − high ≥ `0.25`.

### Template `2` — hotset_replication

Replicate files above a prevalence/hotness threshold toward a target replica count.

| Parameter | Type | Range | Default | Description |
|-----------|------|-------|---------|-------------|
| `hotset_replication.hotness_threshold` | float | `0.01–0.5` | `0.08` | Minimum replica prevalence to treat a file as hot |
| `hotset_replication.target_replica_count` | int | `2–10` | `4` | Stop replicating a file once this many replicas exist |
| `hotset_replication.candidate_destination_policy` | int | `0–1` | `0` | `0` = `requesting_sites_first` (sites with waiting jobs that need the file, then others); `1` = `least_utilized_among_requesting` (requesting sites only when available) |

Destination ordering also respects `proactive.site_staging_bias` when that bias is not `off`.

### Template `3` — job_input_prefetch

Prefetch missing inputs for allocated-but-not-started jobs to their compute sites
(online analogue of drop-in prestaging, driven by the live waiting-job set).

| Parameter | Type | Range | Default | Description |
|-----------|------|-------|---------|-------------|
| `job_input_prefetch.max_transfers_per_tick` | int | `1–8` | `2` | Max background transfers started per proactive tick |
| `job_input_prefetch.max_jobs_per_tick` | int | `1–32` | `8` | Max waiting jobs considered per tick |

Job prioritization can use `proactive.site_staging_bias` (deeper staging queues or
worse recent staging EMA first). Sources prefer higher-utilization replica sites.

### Drop-ins vs searchable policies

Drop-in schedules (`--enable-drop-in-transfers`) are **not** part of the action
vector. They are attached to each trial’s policy config as an external schedule
and run independently of which proactive template is selected. Use them to remove
known long-tail staging outliers while agents search residual policy effects.

## Objectives

### `avg_staging_time` (default)

```
JobExecution.Started.TIME − JobAllocation.Finished.TIME
```

Reward is the negative (log-scaled) aggregated staging time with `aggregation=mean` by default.

## Objectives and agents (summary)

| Component | Options | When to use |
|-----------|---------|-------------|
| Agents | `bayesian_opt,rl_policy,random_search` | **Default** — parallel comparison with random baseline |
| Agent | `bayesian_opt` | Black-box optimization only |
| Agent | `rl_policy` | REINFORCE policy network |
| Agent | `random_search` | Memoryless uniform baseline |
| Agent | `bandit` | Template-pair sweep (16 arms: 4 reactive × 4 proactive) |
| Objective | `avg_staging_time` | Default |
| Aggregation | `mean` | Default |

See **Step-by-step workflow** above for exact commands.

## Evaluation Windows

| Mode | `window_size` | Behavior |
|------|---------------|----------|
| `full` | ignored | All completed jobs |
| `time` | seconds | Jobs starting execution in `[t, t + size)` |
| `job_count` | N | Last N completed jobs |
| `per_job` | 1 | One reward per job; agent uses mean for fitness |

## Trial Artifacts

Each trial under `explore/runs/{experiment}/methods/{agent}/trial_{k}/` contains:

| File | Description |
|------|-------------|
| `action.json` | Sampled action vector |
| `data_policy_config.json` | Generated policy config |
| `config.json` | Trial-specific CGSim config |
| `events.db` | Simulation event log |
| `metrics.json` | Extracted per-job and per-site metrics |
| `reward.json` | Objective value and window metadata |
| `stderr.log` | CGSim stderr (on failure) |
| `observation/outcome.json` | Post-episode staging, transfer, utilization, and network summaries |
| `observation/context.json` | Observation vector for the **next** trial (RL context) |
| `observation/site_utilization_report.json` | Per-site/grid storage and CPU utilization aggregates |
| `observation/network_usage_report.json` | Per-link transfer volume and link_load stats |

For RL runs, `methods/rl_policy/observation_spec.json` documents the fixed observation
schema (feature names, `obs_dim`, schema version). The RL agent reads
`env.current_observation` built from the previous trial's outcome plus memory features
(cost history, last action vector, trial progress).

**Episode model:** one `env.step()` runs one full simulation. The observation at trial `t`
is built from trial `t−1` outcome (plus memory); trial 0 starts from a zero vector.

At the **experiment root** (`explore/runs/{experiment}/`):

| File | Description |
|------|-------------|
| `run_config.json` | Methods, seed, objective (written at start) |
| `plots/methods_comparison.png` | Live grouped method comparison |
| `plots/methods_timing.csv` | Live per-trial sim and exploration timing (seconds) |
| `failure_report.json` | Parsed sim errors across all methods (when crashes occur) |

Each method directory also has `summary.json`, per-method `plots/`, and per-trial `plots/`.

## Project Layout

```text
explore/
├── README.md
├── requirements.txt
├── config/
├── src/datamgmt_explore/
├── scripts/
└── runs/
```

## Extending

- **RL policy network:** [`agents/policy_network.py`](src/datamgmt_explore/agents/policy_network.py), [`agents/rl_policy.py`](src/datamgmt_explore/agents/rl_policy.py), and [`rl_observations.py`](src/datamgmt_explore/rl_observations.py)
- **New objective:** add under `src/datamgmt_explore/objectives/`, register in `objectives.yaml`
- **New agent:** subclass `agents/base.py`, wire into `run_exploration.py`
- **Online RL (future):** may also use the reserved `custom_policy_agent` template
  hook (index `4` in the plugin; not part of the searchable action space today)

## Related Documentation

- [Main project README](../README.md)
- [`config/data_policy_config.json`](../config/data_policy_config.json)
- [`scripts/plot_transfer_analysis.py`](../scripts/plot_transfer_analysis.py)
