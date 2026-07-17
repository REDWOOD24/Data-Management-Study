from __future__ import annotations

import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from datamgmt_explore.metrics import (
    JobRecord,
    filter_records_with_input_files,
    load_job_records,
)

logger = logging.getLogger(__name__)

FONT_SIZES = {
    "title": 18,
    "label": 18,
    "tick": 18,
}


def _job_sort_key(job_id: str) -> tuple[int, int | str]:
    try:
        return (0, int(job_id))
    except ValueError:
        return (1, job_id)


def _ordered_records(records: list[JobRecord]) -> list[JobRecord]:
    return sorted(records, key=lambda record: _job_sort_key(record.job_id))


def _resolve_jobs_csv(db_path: Path, jobs_csv: Path | None = None) -> Path | None:
    if jobs_csv is not None and jobs_csv.is_file():
        return jobs_csv
    trial_dir = Path(db_path).parent
    for candidate in (trial_dir / "jobs.csv", trial_dir / "jobs_truncated.csv"):
        if candidate.is_file():
            return candidate
    return None


def plot_job_staging_times(
    db_path: Path,
    output_path: Path,
    *,
    title: str | None = None,
    jobs_csv: Path | None = None,
) -> Path | None:
    """Plot per-job staging time for jobs with non-zero input-file requirements."""
    records = load_job_records(db_path)
    if not records:
        logger.warning("Skipping job staging plot; no job records in %s", db_path)
        return None

    resolved_jobs_csv = _resolve_jobs_csv(db_path, jobs_csv=jobs_csv)
    plot_records = filter_records_with_input_files(
        records,
        jobs_csv=resolved_jobs_csv,
    )
    if not plot_records:
        logger.warning(
            "Skipping job staging plot; no input-requiring jobs in %s",
            db_path,
        )
        return None

    ordered = _ordered_records(plot_records)
    job_ids = [record.job_id for record in ordered]
    staging = np.asarray([record.staging_time for record in ordered], dtype=float)
    xs = np.arange(len(ordered))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig_w = max(10.0, min(22.0, 0.012 * len(ordered) + 8.0))
    fig, ax = plt.subplots(figsize=(fig_w, 6.5))
    ax.scatter(xs, staging, s=18, alpha=0.75, color="#264653", linewidths=0, zorder=2)
    ax.plot(xs, staging, color="#2a9d8f", alpha=0.35, linewidth=0.8, zorder=1)

    ax.set_xlabel("Job ID", fontsize=FONT_SIZES["label"])
    ax.set_ylabel("Staging time (s, log scale)", fontsize=FONT_SIZES["label"])
    default_title = (
        f"Job staging times (input-requiring jobs only, "
        f"n={len(ordered)}/{len(records)})"
    )
    ax.set_title(title or default_title, fontsize=FONT_SIZES["title"])
    ax.tick_params(axis="both", labelsize=FONT_SIZES["tick"])
    ax.set_xlim(-0.5, len(ordered) - 0.5)
    positive = staging[staging > 0]
    ymin = float(np.min(positive)) if positive.size else 1.0
    ax.set_yscale("log")
    ax.set_ylim(bottom=max(1.0, ymin * 0.8))
    ax.grid(True, axis="y", alpha=0.3, which="both")

    max_ticks = 20
    if len(ordered) <= max_ticks:
        tick_idx = xs
    else:
        tick_idx = np.linspace(0, len(ordered) - 1, num=max_ticks, dtype=int)
        tick_idx = np.unique(tick_idx)
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([job_ids[i] for i in tick_idx], rotation=45, ha="right")

    mean_staging = float(np.mean(staging))
    ax.axhline(
        mean_staging,
        color="#e76f51",
        linestyle="--",
        linewidth=1.2,
        label=f"mean = {mean_staging:.1f}s",
    )
    ax.legend(loc="upper right", fontsize=FONT_SIZES["tick"])

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    logger.info("Wrote job staging plot: %s", output_path)
    return output_path


def plot_trial_job_staging_times(
    db_path: Path,
    output_dir: Path,
    *,
    trial_index: int | None = None,
    jobs_csv: Path | None = None,
) -> Path | None:
    """Write ``job_staging_times.png`` under a trial plots directory."""
    title = None
    if trial_index is not None:
        title = f"Trial {trial_index}: job staging times (input-requiring only)"
    return plot_job_staging_times(
        db_path,
        Path(output_dir) / "job_staging_times.png",
        title=title,
        jobs_csv=jobs_csv,
    )
