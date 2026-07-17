#!/usr/bin/env python3
"""Plot planned drop-in transfers on a timeline for schedule validation."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

REACTIVE_EVENT = "FileTransfer"
DEFAULT_INPUT = (
    Path(__file__).resolve().parent.parent / "config" / "drop_in_transfers.json"
)
DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parent.parent / "output" / "dropin_test" / "plots"
)


@dataclass(frozen=True)
class PlannedDropIn:
    transfer_id: int
    start_time: float
    end_time: float
    file_id: str
    source_site: str
    destination_site: str
    job_id: str | None
    mode: str


def load_hindsight_durations(events_db: Path) -> dict[tuple[str, str, str, str], float]:
    """Map (job_id, file, source, destination) -> reactive transfer duration."""
    finished_query = """
        SELECT JOB_ID, METADATA
        FROM EVENTS
        WHERE EVENT = ?
          AND STATE = 'Finished'
    """
    lookup: dict[tuple[str, str, str, str], float] = {}
    with sqlite3.connect(events_db) as conn:
        for job_id, metadata_raw in conn.execute(finished_query, (REACTIVE_EVENT,)):
            metadata = json.loads(metadata_raw or "{}")
            source = metadata.get("source_site")
            destination = metadata.get("destination_site")
            file_id = metadata.get("file")
            duration = metadata.get("duration")
            if not source or not destination or file_id is None or duration is None:
                continue
            key = (str(job_id or ""), str(file_id), str(source), str(destination))
            lookup[key] = float(duration)
    return lookup


def load_hindsight_job_lookup(events_db: Path) -> dict[tuple[str, str, str], str]:
    """Map (file, source, destination) -> job_id from reactive staging."""
    query = """
        SELECT JOB_ID, METADATA
        FROM EVENTS
        WHERE EVENT = ?
          AND STATE = 'Started'
    """
    lookup: dict[tuple[str, str, str], str] = {}
    with sqlite3.connect(events_db) as conn:
        for job_id, metadata_raw in conn.execute(query, (REACTIVE_EVENT,)):
            metadata = json.loads(metadata_raw or "{}")
            source = metadata.get("source_site")
            destination = metadata.get("destination_site")
            file_id = metadata.get("file")
            if not source or not destination or file_id is None:
                continue
            lookup[(str(file_id), str(source), str(destination))] = str(job_id or "")
    return lookup


def estimate_duration(metadata: dict) -> float | None:
    size = metadata.get("size")
    bandwidth = metadata.get("bandwidth")
    latency = metadata.get("latency") or 0.0
    if size is not None and bandwidth and float(bandwidth) > 0:
        return float(size) / float(bandwidth) + 2.0 * float(latency)
    return None


def load_started_metadata(events_db: Path) -> dict[tuple[str, str, str, str], dict]:
    query = """
        SELECT JOB_ID, METADATA
        FROM EVENTS
        WHERE EVENT = ?
          AND STATE = 'Started'
    """
    lookup: dict[tuple[str, str, str, str], dict] = {}
    with sqlite3.connect(events_db) as conn:
        for job_id, metadata_raw in conn.execute(query, (REACTIVE_EVENT,)):
            metadata = json.loads(metadata_raw or "{}")
            source = metadata.get("source_site")
            destination = metadata.get("destination_site")
            file_id = metadata.get("file")
            if not source or not destination or file_id is None:
                continue
            key = (str(job_id or ""), str(file_id), str(source), str(destination))
            lookup[key] = metadata
    return lookup


def build_planned_drop_ins(payload: dict, *, events_db: Path | None) -> list[PlannedDropIn]:
    entries = payload.get("drop_in_transfers", [])
    if not entries:
        raise ValueError("No drop_in_transfers entries found in input JSON")

    source = payload.get("source", {})
    if events_db is None:
        events_db_path = source.get("events_db")
        events_db = Path(events_db_path) if events_db_path else None

    duration_lookup: dict[tuple[str, str, str, str], float] = {}
    job_lookup: dict[tuple[str, str, str], str] = {}
    started_lookup: dict[tuple[str, str, str, str], dict] = {}
    if events_db and events_db.is_file():
        duration_lookup = load_hindsight_durations(events_db)
        job_lookup = load_hindsight_job_lookup(events_db)
        started_lookup = load_started_metadata(events_db)

    planned: list[PlannedDropIn] = []
    for index, entry in enumerate(entries):
        start_time = float(entry["time"])
        file_id = str(entry["file"])
        source_site = str(entry["source_site"])
        destination_site = str(entry["destination_site"])
        mode = str(entry.get("mode", "COPY"))

        duration = entry.get("duration_sec")
        job_id = entry.get("job_id")

        if duration is None:
            job_id = job_id or job_lookup.get((file_id, source_site, destination_site))
            if job_id:
                duration = duration_lookup.get((job_id, file_id, source_site, destination_site))

        if duration is None and job_id:
            duration = duration_lookup.get((job_id, file_id, source_site, destination_site))

        if duration is None:
            for key, value in duration_lookup.items():
                if key[1:] == (file_id, source_site, destination_site):
                    duration = value
                    job_id = job_id or key[0]
                    break

        if duration is None:
            for key, metadata in started_lookup.items():
                if key[1:] == (file_id, source_site, destination_site):
                    duration = estimate_duration(metadata)
                    job_id = job_id or key[0]
                    if duration is not None:
                        break

        if duration is None:
            raise ValueError(
                f"Could not resolve duration for transfer file={file_id} "
                f"{source_site}->{destination_site}"
            )

        planned.append(
            PlannedDropIn(
                transfer_id=index,
                start_time=start_time,
                end_time=start_time + float(duration),
                file_id=file_id,
                source_site=source_site,
                destination_site=destination_site,
                job_id=job_id,
                mode=mode,
            )
        )

    planned.sort(key=lambda item: (item.start_time, item.file_id, item.source_site))
    return [
        PlannedDropIn(
            transfer_id=index,
            start_time=item.start_time,
            end_time=item.end_time,
            file_id=item.file_id,
            source_site=item.source_site,
            destination_site=item.destination_site,
            job_id=item.job_id,
            mode=item.mode,
        )
        for index, item in enumerate(planned)
    ]


def plot_drop_in_timeline(
    planned: list[PlannedDropIn],
    *,
    critical_jobs: list[dict],
    output_path: Path,
    title: str | None = None,
) -> Path:
    if not planned:
        raise ValueError("No planned drop-in transfers to plot")

    # Build the job universe from both sources. A critical job may have no
    # surviving transfer after schedule deduplication, while a transfer may
    # name a job omitted from source.critical_jobs. Neither should disappear.
    critical_job_ids = [
        str(job["job_id"])
        for job in critical_jobs
        if job.get("job_id") is not None
    ]
    transfer_job_ids = [
        item.job_id
        for item in planned
        if item.job_id
    ]
    job_ids = list(dict.fromkeys(critical_job_ids + transfer_job_ids))

    # Assign colors dynamically instead of recognizing only a few hard-coded
    # job IDs. tab20 is discrete and remains readable for typical critical-job
    # sets; requesting N samples also supports sets larger than 20.
    job_cmap = plt.get_cmap("tab20", max(1, len(job_ids)))
    job_colors = {
        job_id: job_cmap(index)
        for index, job_id in enumerate(job_ids)
    }
    unassigned_color = "#264653"
    font_title = 18
    font_label = 18
    font_tick = 18
    font_legend = 18
    font_job_marker = 18

    fig_h = max(6.0, min(24.0, 0.12 * len(planned) + 4.0))
    fig, ax = plt.subplots(figsize=(14.0, fig_h))

    y_labels: list[str] = []
    for y_index, item in enumerate(planned):
        color = job_colors.get(item.job_id or "", unassigned_color)
        ax.plot(
            [item.start_time, item.end_time],
            [y_index, y_index],
            color=color,
            linewidth=2.0,
            solid_capstyle="butt",
        )
        ax.plot(item.start_time, y_index, marker="|", color=color, markersize=8)
        ax.plot(item.end_time, y_index, marker="|", color=color, markersize=8)
        label = (
            f"#{item.transfer_id} file={item.file_id} "
            f"{item.source_site}->{item.destination_site}"
        )
        if item.job_id:
            label += f" job={item.job_id}"
        y_labels.append(label)

    for marker_index, job in enumerate(critical_jobs):
        alloc_time = job.get("alloc_finish_time_sec")
        job_id = str(job.get("job_id", ""))
        if alloc_time is None:
            continue
        color = job_colors.get(job_id, unassigned_color)
        ax.axvline(float(alloc_time), color=color, linestyle="--", linewidth=1.4, alpha=0.9)

        # Stagger labels vertically so nearby critical-job allocation times do
        # not completely cover one another.
        label_y = len(planned) - 0.5 - (marker_index % 6) * max(
            1.0, len(planned) * 0.045
        )
        ax.text(
            float(alloc_time),
            label_y,
            f"job {job_id}\nalloc t={float(alloc_time):.0f}",
            rotation=90,
            va="top",
            ha="right",
            fontsize=font_job_marker,
            color=color,
        )

    ax.set_xlabel("Simulated time (s)", fontsize=font_label)
    ax.set_ylabel("Drop-in transfer ID", fontsize=font_label)
    ax.set_title(
        title
        or (
            f"Drop-in transfer timeline ({len(planned)} transfers, "
            f"{len(critical_jobs)} critical jobs)"
        ),
        fontsize=font_title,
    )
    ax.set_yticks(range(len(planned)))
    ax.set_yticklabels([f"T{item.transfer_id}" for item in planned], fontsize=font_tick)
    ax.tick_params(axis="x", labelsize=font_tick)
    ax.set_xlim(left=0.0)
    ax.set_ylim(-0.8, len(planned) - 0.2)
    ax.grid(True, axis="x", alpha=0.25)

    legend_handles = [
        mpatches.Patch(
            color=job_colors[job_id],
            label=(
                f"job {job_id} "
                f"({sum(item.job_id == job_id for item in planned)} transfers)"
            ),
        )
        for job_id in job_ids
    ]
    if any(item.job_id is None for item in planned):
        legend_handles.append(
            mpatches.Patch(color=unassigned_color, label="unassigned transfer")
        )
    if legend_handles:
        ax.legend(
            handles=legend_handles,
            loc="lower right",
            fontsize=font_legend,
            ncol=max(1, min(3, (len(legend_handles) + 5) // 6)),
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot drop-in transfer schedule as a timeline.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"drop_in_transfers.json path (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for output PNG (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional explicit output PNG path",
    )
    parser.add_argument(
        "--events-db",
        type=Path,
        help="Optional hindsight events.db for durations (default: source.events_db)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input.resolve()
    if not input_path.is_file():
        raise SystemExit(f"Input not found: {input_path}")

    with input_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)

    planned = build_planned_drop_ins(payload, events_db=args.events_db)
    critical_jobs = payload.get("source", {}).get("critical_jobs", [])

    if args.output is not None:
        output_path = args.output.resolve()
    else:
        output_path = args.output_dir.resolve() / "drop_in_transfers_timeline.png"

    plot_drop_in_timeline(
        planned,
        critical_jobs=critical_jobs,
        output_path=output_path,
        title=f"Drop-in transfer timeline ({input_path.name})",
    )
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
