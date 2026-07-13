from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from datamgmt_explore.metrics import (
    compute_tail_bulk_staging_cost,
    load_job_records,
    mean_staging_time_all_jobs,
)
from datamgmt_explore.plotting.trial_plots import _load_transfer_analysis

STACK_COLORS = ["#2a9d8f", "#8ecae6", "#e76f51", "#f4a261"]
STACK_LABELS = [
    "Ingress (reactive)",
    "Ingress (proactive)",
    "Egress (reactive)",
    "Egress (proactive)",
]


@dataclass(frozen=True)
class TrialMeanBars:
    trial_index: int
    ingress_reactive: float
    ingress_proactive: float
    egress_reactive: float
    egress_proactive: float
    avg_staging_time: float | None
    staging_reward: float | None


def load_trial_mean_bars(db_path: Path, trial_index: int, pta) -> TrialMeanBars | None:
    if not db_path.is_file():
        return None

    try:
        transfers = pta.load_finished_transfers(db_path)
    except FileNotFoundError:
        return None

    ingress_reactive = 0.0
    ingress_proactive = 0.0
    egress_reactive = 0.0
    egress_proactive = 0.0

    if transfers:
        (
            sites,
            _matrix,
            ingress_reactive_map,
            ingress_proactive_map,
            egress_reactive_map,
            egress_proactive_map,
            *_rest,
        ) = pta.aggregate_transfer_data(transfers)

        to_gib = lambda volumes: [volumes.get(site, 0.0) / (1024**3) for site in sites]
        ingress_reactive = float(np.mean(to_gib(ingress_reactive_map))) if sites else 0.0
        ingress_proactive = float(np.mean(to_gib(ingress_proactive_map))) if sites else 0.0
        egress_reactive = float(np.mean(to_gib(egress_reactive_map))) if sites else 0.0
        egress_proactive = float(np.mean(to_gib(egress_proactive_map))) if sites else 0.0

    records = load_job_records(db_path)
    mean_staging = mean_staging_time_all_jobs(records)
    _, _, staging_reward = compute_tail_bulk_staging_cost(records)

    if not transfers and mean_staging is None:
        return None

    return TrialMeanBars(
        trial_index=trial_index,
        ingress_reactive=ingress_reactive,
        ingress_proactive=ingress_proactive,
        egress_reactive=egress_reactive,
        egress_proactive=egress_proactive,
        avg_staging_time=mean_staging,
        staging_reward=staging_reward if np.isfinite(staging_reward) else None,
    )


def load_experiment_trial_means(experiment_dir: Path, *, repo_root: Path) -> list[TrialMeanBars]:
    pta = _load_transfer_analysis(repo_root)
    summaries: list[TrialMeanBars] = []

    for trial_dir in sorted(experiment_dir.glob("trial_*")):
        trial_index = int(trial_dir.name.split("_", 1)[1])
        summary = load_trial_mean_bars(trial_dir / "events.db", trial_index, pta)
        if summary is not None:
            summaries.append(summary)

    return sorted(summaries, key=lambda item: item.trial_index)


def plot_trial_mean_stacked_bars(
    experiment_dir: Path,
    *,
    repo_root: Path,
    summaries: list[TrialMeanBars] | None = None,
) -> Path | None:
    """Plot per-trial mean stacked transfer bars with avg job staging time."""
    summaries = summaries if summaries is not None else load_experiment_trial_means(
        experiment_dir,
        repo_root=repo_root,
    )
    if not summaries:
        return None

    output_dir = experiment_dir / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "trial_mean_stacked_bars.png"

    trial_indices = [summary.trial_index for summary in summaries]
    x = np.arange(len(trial_indices), dtype=float)

    ingress_reactive = np.array([summary.ingress_reactive for summary in summaries], dtype=float)
    ingress_proactive = np.array([summary.ingress_proactive for summary in summaries], dtype=float)
    egress_reactive = np.array([summary.egress_reactive for summary in summaries], dtype=float)
    egress_proactive = np.array([summary.egress_proactive for summary in summaries], dtype=float)
    staging_times = np.array(
        [summary.avg_staging_time if summary.avg_staging_time is not None else np.nan for summary in summaries],
        dtype=float,
    )

    bottom_ingress_proactive = ingress_reactive
    bottom_egress_reactive = ingress_reactive + ingress_proactive
    bottom_egress_proactive = bottom_egress_reactive + egress_reactive

    fig, ax = plt.subplots(figsize=(max(10, len(trial_indices) * 0.75), 6.5))

    ax.bar(x, ingress_reactive, width=0.75, label=STACK_LABELS[0], color=STACK_COLORS[0])
    ax.bar(
        x,
        ingress_proactive,
        width=0.75,
        bottom=bottom_ingress_proactive,
        label=STACK_LABELS[1],
        color=STACK_COLORS[1],
    )
    ax.bar(
        x,
        egress_reactive,
        width=0.75,
        bottom=bottom_egress_reactive,
        label=STACK_LABELS[2],
        color=STACK_COLORS[2],
    )
    ax.bar(
        x,
        egress_proactive,
        width=0.75,
        bottom=bottom_egress_proactive,
        label=STACK_LABELS[3],
        color=STACK_COLORS[3],
    )

    ax.set_xticks(x)
    ax.set_xticklabels([str(index) for index in trial_indices])
    ax.set_xlabel("Trial")
    ax.set_ylabel("Mean transferred size (GiB)")
    ax.set_title("Per-trial mean transfer volume and average job staging time")

    ax2 = ax.twinx()
    ax2.plot(
        x,
        staging_times,
        color="#6d597a",
        marker="s",
        linestyle="--",
        linewidth=1.5,
        markersize=6,
        label="Avg job staging time (alloc → exec start)",
    )
    ax2.set_ylabel("Average time (s)")

    bar_totals = ingress_reactive + ingress_proactive + egress_reactive + egress_proactive
    timing_finite = staging_times[np.isfinite(staging_times)]
    ymax = max(float(bar_totals.max(initial=0.0)), float(timing_finite.max(initial=0.0))) * 1.08
    if ymax <= 0:
        ymax = 1.0
    ax.set_ylim(0, ymax)
    ax2.set_ylim(0, ymax)

    ax.grid(True, axis="y", alpha=0.3)
    ax2.grid(False)

    handles_left, labels_left = ax.get_legend_handles_labels()
    handles_right, labels_right = ax2.get_legend_handles_labels()
    ax.legend(handles_left + handles_right, labels_left + labels_right, loc="upper left")

    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path
