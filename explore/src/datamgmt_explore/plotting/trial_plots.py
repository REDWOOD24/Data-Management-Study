from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

from datamgmt_explore.metrics import load_global_job_timing
from datamgmt_explore.plotting.job_staging_time_plot import plot_trial_job_staging_times

_TRANSFER_ANALYSIS = None


def _load_transfer_analysis(repo_root: Path):
    global _TRANSFER_ANALYSIS
    if _TRANSFER_ANALYSIS is not None:
        return _TRANSFER_ANALYSIS

    scripts_dir = repo_root / "scripts"
    if not scripts_dir.is_dir():
        raise FileNotFoundError(f"Scripts directory not found: {scripts_dir}")

    scripts_path = str(scripts_dir)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)

    import plot_transfer_analysis

    _TRANSFER_ANALYSIS = plot_transfer_analysis
    return plot_transfer_analysis


def plot_trial(
    db_path: Path,
    output_dir: Path,
    *,
    repo_root: Path,
    top_k: int = 40,
    min_bytes: float = 0.0,
    show_end_to_end: bool = False,
) -> list[Path]:
    """Generate trial plots: job staging times plus transfer analysis PNGs."""
    if not db_path.is_file():
        logger.warning("Skipping trial plots; events.db not found: %s", db_path)
        return []

    try:
        pta = _load_transfer_analysis(repo_root)
    except ImportError as exc:
        raise ImportError(
            "matplotlib is required for plotting. Install with: pip install -r explore/requirements.txt"
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    trial_index: int | None = None
    parent_name = db_path.parent.name
    if parent_name.startswith("trial_"):
        try:
            trial_index = int(parent_name.split("_", 1)[1])
        except ValueError:
            trial_index = None
    staging_path = plot_trial_job_staging_times(
        db_path,
        output_dir,
        trial_index=trial_index,
    )
    if staging_path is not None:
        written.append(staging_path)

    try:
        transfers = pta.load_finished_transfers(db_path)
    except FileNotFoundError:
        logger.warning("Skipping transfer trial plots; database missing: %s", db_path)
        return written

    if not transfers:
        logger.warning("Skipping transfer trial plots; no finished transfer events in %s", db_path)
        _write_skip_note(output_dir, "No finished transfer events found in events.db")
        return written

    (
        sites,
        matrix,
        ingress_reactive,
        ingress_proactive,
        egress_reactive,
        egress_proactive,
        connection_reactive,
        connection_proactive,
        connection_totals,
    ) = pta.aggregate_transfer_data(transfers)
    avg_end_to_end, avg_staging_time = pta.load_job_site_metrics(db_path)
    global_avg_staging_time, global_avg_end_to_end_time = load_global_job_timing(db_path)

    heatmap_path = output_dir / "transfer_heatmap.png"
    site_bars_path = output_dir / "site_ingress_egress.png"
    connections_path = output_dir / "top_connections.png"

    pta.plot_heatmap(sites, matrix, heatmap_path)
    pta.plot_site_ingress_egress(
        sites,
        ingress_reactive,
        ingress_proactive,
        egress_reactive,
        egress_proactive,
        avg_end_to_end,
        avg_staging_time,
        site_bars_path,
        show_end_to_end=show_end_to_end,
        global_avg_staging_time=global_avg_staging_time,
        global_avg_end_to_end_time=global_avg_end_to_end_time,
    )

    top_connections = pta.select_top_connections(
        connection_totals,
        top_k=top_k,
        min_bytes=min_bytes,
    )
    pta.plot_connection_totals(
        top_connections,
        connection_reactive,
        connection_proactive,
        connections_path,
    )

    written.extend([heatmap_path, site_bars_path, connections_path])
    logger.info("Wrote trial plots to %s", output_dir)
    return written


def plot_all_trials(
    experiment_dir: Path,
    *,
    repo_root: Path,
    top_k: int = 40,
    min_bytes: float = 0.0,
    show_end_to_end: bool = False,
) -> dict[int, list[Path]]:
    """Plot all trials under an experiment that have an events.db."""
    results: dict[int, list[Path]] = {}
    for trial_dir in sorted(experiment_dir.glob("trial_*")):
        trial_index = int(trial_dir.name.split("_", 1)[1])
        db_path = trial_dir / "events.db"
        if not db_path.is_file():
            continue
        output_dir = trial_dir / "plots"
        written = plot_trial(
            db_path,
            output_dir,
            repo_root=repo_root,
            top_k=top_k,
            min_bytes=min_bytes,
            show_end_to_end=show_end_to_end,
        )
        results[trial_index] = written
    return results


def _write_skip_note(output_dir: Path, message: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    note_path = output_dir / "plot_skipped.txt"
    note_path.write_text(message + "\n", encoding="utf-8")
