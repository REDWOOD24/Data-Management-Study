#!/usr/bin/env python3
"""Emit drop_in_transfers.json from hindsight analysis of a completed trial.

Selects only critical (long-tail) jobs, extracts every cross-site reactive staging
transfer those jobs required, and schedules one-shot drop-ins using a
proactive-safe balanced strategy:

  * MOVE by default (avoids extra replicas that trigger hotset thrashing).
  * Tight max-presence window: files must arrive shortly before job allocation.
    Early proactive placement drives CGSim pending-activities growth via background
    policy work, even when no drop-ins are in flight during bulk dispatch.
  * Per-link serialization inside that window; overflow drops lower-priority files.
  * Global caps on total transfers, per-bucket starts, and concurrent in-flight
    drop-ins as secondary guards.

Typical workflow:
  1. Run exploration → pick a trial with long-tail staging.
  2. python explore/scripts/emit_drop_in_transfers.py --trial-dir path/to/trial_0000
  3. Point data_policy_config.json at the generated drop_in_transfers.json and re-sim.
  4. Compare job_staging_times.png before vs after.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

EXPLORE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EXPLORE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from datamgmt_explore.metrics import (  # noqa: E402
    filter_records_with_input_files,
    load_job_records,
)

REACTIVE_EVENT = "FileTransfer"
DEFAULT_FINISH_BUFFER_SEC = 300.0
DEFAULT_LINK_GAP_SEC = 1.0
DEFAULT_CRITICAL_MIN_STAGING_SEC = 50_000.0
DEFAULT_MAX_LEAD_SEC = 5_400.0
DEFAULT_MAX_PRESENCE_SEC = 5_400.0
DEFAULT_BULK_DISPATCH_CUTOFF_SEC = 65_000.0
DEFAULT_PLACEMENT_FRACTION = 1.0
DEFAULT_MAX_SIMULTANEOUS_STARTS = 1
DEFAULT_MAX_CONCURRENT_INFLIGHT = 3
DEFAULT_MAX_TOTAL_TRANSFERS = 80
DEFAULT_MAX_TRANSFERS_PER_JOB = 30
DEFAULT_TIME_BUCKET_SEC = 300.0
DEFAULT_MODE = "MOVE"


@dataclass(frozen=True)
class PlannedTransfer:
    job_id: str
    file_id: str
    source_site: str
    destination_site: str
    duration_sec: float
    deadline_sec: float
    size_bytes: float | None = None
    bandwidth_bps: float | None = None
    latency_sec: float = 0.0

    @property
    def dedupe_key(self) -> tuple[str, str, str]:
        return (self.file_id, self.source_site, self.destination_site)

    @property
    def connection(self) -> tuple[str, str]:
        return (self.source_site, self.destination_site)


@dataclass(frozen=True)
class TransferWave:
    connection: tuple[str, str]
    job_id: str
    items: tuple[PlannedTransfer, ...]
    wave_duration_sec: float
    finish_deadline_sec: float
    earliest_start_sec: float
    latest_start_sec: float


def _resolve_events_db(args: argparse.Namespace) -> Path:
    if args.events_db is not None:
        db_path = args.events_db.resolve()
    elif args.trial_dir is not None:
        db_path = args.trial_dir.resolve() / "events.db"
    else:
        raise SystemExit("Provide --trial-dir or --events-db")
    if not db_path.is_file():
        raise SystemExit(f"events.db not found: {db_path}")
    return db_path


def _resolve_jobs_csv(args: argparse.Namespace, events_db: Path) -> Path | None:
    if args.jobs_csv is not None:
        path = args.jobs_csv.resolve()
        return path if path.is_file() else None
    if args.trial_dir is not None:
        trial_dir = args.trial_dir.resolve()
        for name in ("jobs.csv", "jobs_truncated.csv"):
            candidate = trial_dir / name
            if candidate.is_file():
                return candidate
    return None


def estimate_transfer_duration(metadata: dict) -> float:
    """Estimate uncongested transfer time from hindsight Started metadata."""
    size = metadata.get("size")
    bandwidth = metadata.get("bandwidth")
    latency = metadata.get("latency") or 0.0
    if size is not None and bandwidth and float(bandwidth) > 0:
        return float(size) / float(bandwidth) + 2.0 * float(latency)
    duration = metadata.get("duration")
    if duration is not None:
        return float(duration)
    return 3600.0


def load_reactive_staging_planned(events_db: Path) -> list[PlannedTransfer]:
    """Load cross-site reactive staging transfers with hindsight durations."""
    started_query = """
        SELECT JOB_ID, TIME, METADATA
        FROM EVENTS
        WHERE EVENT = ?
          AND STATE = 'Started'
    """
    finished_query = """
        SELECT JOB_ID, TIME, METADATA
        FROM EVENTS
        WHERE EVENT = ?
          AND STATE = 'Finished'
    """

    finished_lookup: dict[tuple[str, str, str, str], float] = {}
    with sqlite3.connect(events_db) as conn:
        for job_id, finished_time, metadata_raw in conn.execute(finished_query, (REACTIVE_EVENT,)):
            metadata = json.loads(metadata_raw or "{}")
            source = metadata.get("source_site")
            destination = metadata.get("destination_site")
            file_id = metadata.get("file")
            if not source or not destination or file_id is None:
                continue
            duration = metadata.get("duration")
            if duration is None:
                continue
            key = (str(job_id or ""), str(file_id), str(source), str(destination))
            finished_lookup[key] = float(duration)

        transfers: list[PlannedTransfer] = []
        for job_id, started_time, metadata_raw in conn.execute(started_query, (REACTIVE_EVENT,)):
            metadata = json.loads(metadata_raw or "{}")
            source = metadata.get("source_site")
            destination = metadata.get("destination_site")
            file_id = metadata.get("file")
            if not source or not destination or file_id is None:
                continue
            if source == destination:
                continue

            key = (str(job_id or ""), str(file_id), str(source), str(destination))
            duration = finished_lookup.get(key)
            if duration is None:
                duration = estimate_transfer_duration(metadata)

            size = metadata.get("size")
            bandwidth = metadata.get("bandwidth")
            latency = float(metadata.get("latency") or 0.0)
            transfers.append(
                PlannedTransfer(
                    job_id=str(job_id or ""),
                    file_id=str(file_id),
                    source_site=str(source),
                    destination_site=str(destination),
                    duration_sec=float(duration),
                    deadline_sec=0.0,
                    size_bytes=float(size) if size is not None else None,
                    bandwidth_bps=float(bandwidth) if bandwidth else None,
                    latency_sec=latency,
                )
            )
    return transfers


def select_critical_job_ids(
    records: list,
    *,
    top_n: int | None,
    job_ids: list[str] | None,
    min_staging_sec: float,
) -> list[str]:
    """Keep only jobs with extreme staging times; never include the bulk cluster."""
    if job_ids:
        return list(dict.fromkeys(job_ids))

    ordered = sorted(records, key=lambda record: record.staging_time, reverse=True)
    critical = [record for record in ordered if record.staging_time >= min_staging_sec]
    if top_n is not None:
        critical = critical[: max(top_n, 1)]
    if not critical and ordered:
        critical = [ordered[0]]
    return [record.job_id for record in critical]


def dedupe_planned_transfers(
    transfers: list[PlannedTransfer],
    *,
    critical_job_ids: set[str],
    deadlines_by_job: dict[str, float],
) -> list[PlannedTransfer]:
    """Keep one plan per (file, source, destination); earliest deadline wins."""
    best: dict[tuple[str, str, str], PlannedTransfer] = {}
    for transfer in transfers:
        if transfer.job_id not in critical_job_ids:
            continue
        deadline = deadlines_by_job[transfer.job_id]
        candidate = PlannedTransfer(
            job_id=transfer.job_id,
            file_id=transfer.file_id,
            source_site=transfer.source_site,
            destination_site=transfer.destination_site,
            duration_sec=transfer.duration_sec,
            deadline_sec=deadline,
            size_bytes=transfer.size_bytes,
            bandwidth_bps=transfer.bandwidth_bps,
            latency_sec=transfer.latency_sec,
        )
        existing = best.get(candidate.dedupe_key)
        if existing is None or candidate.deadline_sec < existing.deadline_sec:
            best[candidate.dedupe_key] = candidate
    return list(best.values())


def _time_bucket(time_sec: float, bucket_sec: float) -> int:
    return int(time_sec // bucket_sec)


def _bucket_load(
    start_sec: float,
    n_transfers: int,
    bucket_counts: Counter[int],
    bucket_sec: float,
) -> int:
    return bucket_counts[_time_bucket(start_sec, bucket_sec)] + n_transfers


def _candidate_start_times(
    earliest: float,
    latest: float,
    *,
    bucket_sec: float,
    preferred: float,
) -> list[float]:
    if earliest > latest + 1e-6:
        return [earliest]

    window = latest - earliest
    step = min(bucket_sec, max(30.0, window / 8.0 if window > 0 else 30.0))
    staggered = []
    cursor = earliest
    while cursor <= latest + 1e-6:
        staggered.append(round(cursor, 3))
        cursor += step

    first_bucket = int(earliest // bucket_sec)
    last_bucket = int(latest // bucket_sec)
    bucket_aligned = [
        bucket_index * bucket_sec
        for bucket_index in range(first_bucket, last_bucket + 1)
        if earliest <= bucket_index * bucket_sec <= latest + 1e-6
    ]

    candidates = staggered + bucket_aligned + [earliest, latest, preferred]
    return sorted(
        set(candidates),
        key=lambda time: (_bucket_load_rank(time, preferred), -time),
    )


def _bucket_load_rank(time_sec: float, preferred: float) -> tuple[float, float]:
    return (abs(time_sec - preferred), time_sec)


def prioritize_planned_transfers(
    transfers: list[PlannedTransfer],
    *,
    staging_by_job: dict[str, float],
    max_total_transfers: int,
    max_transfers_per_job: int,
) -> tuple[list[PlannedTransfer], int]:
    """Keep the highest-impact files per critical job under global caps."""
    if not transfers:
        return [], 0

    ordered = sorted(
        transfers,
        key=lambda item: (
            -staging_by_job.get(item.job_id, 0.0),
            -item.duration_sec,
            item.job_id,
            item.file_id,
        ),
    )
    selected: list[PlannedTransfer] = []
    per_job: Counter[str] = Counter()
    for transfer in ordered:
        if len(selected) >= max_total_transfers:
            break
        if per_job[transfer.job_id] >= max_transfers_per_job:
            continue
        selected.append(transfer)
        per_job[transfer.job_id] += 1
    return selected, len(transfers) - len(selected)


def _inflight_count(
    active_intervals: list[tuple[float, float]],
    start_sec: float,
    end_sec: float,
) -> int:
    peak = 0
    samples = {start_sec, end_sec}
    for other_start, other_end in active_intervals:
        samples.update((other_start, other_end))
    for sample in samples:
        if sample < start_sec or sample > end_sec:
            continue
        count = sum(
            1
            for other_start, other_end in active_intervals
            if other_start <= sample <= other_end
        )
        peak = max(peak, count)
    return peak


def _peak_inflight(active_intervals: list[tuple[float, float]]) -> int:
    if not active_intervals:
        return 0
    events: list[tuple[float, int]] = []
    for start, end in active_intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort()
    current = 0
    peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def _pick_balanced_start(
    wave: TransferWave,
    *,
    link_cursor: float,
    placement_fraction: float,
    bucket_counts: Counter[int],
    bucket_sec: float,
    max_simultaneous_starts: int,
    active_intervals: list[tuple[float, float]],
    max_concurrent_inflight: int,
) -> float | None:
    """Pick a start time inside the wave window, biasing late but capping peaks."""
    earliest = max(wave.earliest_start_sec, link_cursor)
    latest = wave.latest_start_sec
    if earliest > latest + 1e-6:
        return None

    window = latest - earliest
    preferred = earliest + placement_fraction * window
    preferred = min(max(preferred, earliest), latest)
    n_transfers = len(wave.items)
    duration = wave.wave_duration_sec

    candidates = _candidate_start_times(
        earliest,
        latest,
        bucket_sec=bucket_sec,
        preferred=preferred,
    )
    feasible: list[float] = []
    for time in candidates:
        finish = time + duration
        if _bucket_load(time, n_transfers, bucket_counts, bucket_sec) > max_simultaneous_starts:
            continue
        if _inflight_count(active_intervals, time, finish) >= max_concurrent_inflight:
            continue
        feasible.append(time)
    if not feasible:
        return None

    # Prefer the earliest feasible start to maximize per-link slack (fit more
    # files before allocation), while bucket/inflight caps spread cross-link work.
    return min(
        feasible,
        key=lambda time: (
            _bucket_load(time, n_transfers, bucket_counts, bucket_sec),
            time,
            abs(time - preferred),
        ),
    )


def schedule_balanced_drop_ins(
    transfers: list[PlannedTransfer],
    *,
    finish_buffer_sec: float,
    link_gap_sec: float,
    max_lead_sec: float,
    max_presence_sec: float,
    bulk_dispatch_cutoff_sec: float,
    placement_fraction: float,
    max_simultaneous_starts: int,
    max_concurrent_inflight: int,
    time_bucket_sec: float,
    mode: str,
) -> tuple[list[dict], list[dict], dict]:
    """Balance proactive exposure vs concurrent transfer bottlenecks."""
    if not transfers:
        return [], [], {}

    grouped: dict[tuple[tuple[str, str], str], list[PlannedTransfer]] = {}
    for transfer in transfers:
        grouped.setdefault((transfer.connection, transfer.job_id), []).append(transfer)

    scheduled_entries: list[dict] = []
    schedule_debug: list[dict] = []
    bucket_counts: Counter[int] = Counter()
    link_cursors: dict[tuple[str, str], float] = {}
    active_intervals: list[tuple[float, float]] = []
    overflow_skipped = 0
    inflight_skipped = 0
    presence_skipped = 0
    lead_times: list[float] = []
    presence_slacks: list[float] = []

    group_order = sorted(
        grouped.items(),
        key=lambda item: (
            min(t.deadline_sec for t in item[1]),
            item[0][0],
            item[0][1],
        ),
    )

    for (connection, job_id), items in group_order:
        alloc_time = items[0].deadline_sec
        finish_earliest = alloc_time - max_presence_sec
        finish_deadline = min(alloc_time - finish_buffer_sec, bulk_dispatch_cutoff_sec)
        window_earliest = max(0.0, alloc_time - max_lead_sec)
        link_cursor = max(link_cursors.get(connection, window_earliest), window_earliest)

        ordered_items = sorted(
            items,
            key=lambda item: (-item.duration_sec, item.file_id),
        )

        for transfer in ordered_items:
            latest_start = finish_deadline - transfer.duration_sec
            presence_earliest_start = finish_earliest - transfer.duration_sec
            earliest_start = max(presence_earliest_start, window_earliest, link_cursor)

            if presence_earliest_start > latest_start + 1e-6:
                presence_skipped += 1
                schedule_debug.append(
                    {
                        "job_id": transfer.job_id,
                        "file": transfer.file_id,
                        "connection": f"{transfer.source_site}->{transfer.destination_site}",
                        "start": None,
                        "planned_finish": None,
                        "deadline": round(finish_deadline, 3),
                        "duration_sec": round(transfer.duration_sec, 3),
                        "earliest_start": round(earliest_start, 3),
                        "latest_start": round(latest_start, 3),
                        "on_time": False,
                        "in_window": False,
                        "skipped": "presence_overflow",
                    }
                )
                continue

            if earliest_start > latest_start + 1e-6:
                overflow_skipped += 1
                schedule_debug.append(
                    {
                        "job_id": transfer.job_id,
                        "file": transfer.file_id,
                        "connection": f"{transfer.source_site}->{transfer.destination_site}",
                        "start": None,
                        "planned_finish": None,
                        "deadline": round(finish_deadline, 3),
                        "duration_sec": round(transfer.duration_sec, 3),
                        "earliest_start": round(earliest_start, 3),
                        "latest_start": round(latest_start, 3),
                        "on_time": False,
                        "in_window": False,
                        "skipped": "link_overflow",
                    }
                )
                continue

            effective_wave = TransferWave(
                connection=connection,
                job_id=job_id,
                items=(transfer,),
                wave_duration_sec=transfer.duration_sec,
                finish_deadline_sec=finish_deadline,
                earliest_start_sec=earliest_start,
                latest_start_sec=latest_start,
            )
            start_time = _pick_balanced_start(
                effective_wave,
                link_cursor=link_cursor,
                placement_fraction=placement_fraction,
                bucket_counts=bucket_counts,
                bucket_sec=time_bucket_sec,
                max_simultaneous_starts=max_simultaneous_starts,
                active_intervals=active_intervals,
                max_concurrent_inflight=max_concurrent_inflight,
            )
            if start_time is None:
                inflight_skipped += 1
                schedule_debug.append(
                    {
                        "job_id": transfer.job_id,
                        "file": transfer.file_id,
                        "connection": f"{transfer.source_site}->{transfer.destination_site}",
                        "start": None,
                        "planned_finish": None,
                        "deadline": round(finish_deadline, 3),
                        "duration_sec": round(transfer.duration_sec, 3),
                        "earliest_start": round(earliest_start, 3),
                        "latest_start": round(latest_start, 3),
                        "on_time": False,
                        "in_window": False,
                        "skipped": "inflight_cap",
                    }
                )
                continue

            finish_time = start_time + transfer.duration_sec

            on_time = (
                finish_time <= finish_deadline + 1e-6
                and finish_time >= finish_earliest - 1e-6
            )
            presence_slack = alloc_time - finish_time
            schedule_debug.append(
                {
                    "job_id": transfer.job_id,
                    "file": transfer.file_id,
                    "connection": f"{transfer.source_site}->{transfer.destination_site}",
                    "start": round(start_time, 3),
                    "planned_finish": round(finish_time, 3),
                    "deadline": round(finish_deadline, 3),
                    "duration_sec": round(transfer.duration_sec, 3),
                    "earliest_start": round(earliest_start, 3),
                    "latest_start": round(latest_start, 3),
                    "lead_before_alloc_sec": round(alloc_time - start_time, 3),
                    "presence_slack_sec": round(presence_slack, 3),
                    "on_time": on_time,
                    "in_window": earliest_start <= start_time <= latest_start + 1e-6,
                    "skipped": None,
                }
            )
            if not on_time:
                continue

            bucket_counts[_time_bucket(start_time, time_bucket_sec)] += 1
            lead_times.append(alloc_time - start_time)
            presence_slacks.append(presence_slack)
            scheduled_entries.append(
                {
                    "time": round(start_time, 3),
                    "file": transfer.file_id,
                    "source_site": transfer.source_site,
                    "destination_site": transfer.destination_site,
                    "mode": mode,
                }
            )
            link_cursor = finish_time + link_gap_sec
            link_cursors[connection] = link_cursor
            active_intervals.append((start_time, finish_time))

    scheduled_entries.sort(key=lambda item: (item["time"], item["file"], item["source_site"]))
    scheduled_rows = [row for row in schedule_debug if row.get("skipped") is None and row["on_time"]]
    scheduled_rows.sort(key=lambda item: (item["start"], item["file"]))

    late_count = sum(
        1 for row in schedule_debug if row.get("skipped") is None and not row["on_time"]
    ) + overflow_skipped + inflight_skipped + presence_skipped

    if not scheduled_entries:
        return [], [], {
            "late_transfers": late_count,
            "overflow_skipped": overflow_skipped,
            "inflight_skipped": inflight_skipped,
            "presence_skipped": presence_skipped,
        }

    starts = [row["start"] for row in scheduled_rows]
    peak_by_bucket: Counter[int] = Counter()
    for row in scheduled_rows:
        peak_by_bucket[_time_bucket(row["start"], time_bucket_sec)] += 1
    schedule_stats = {
        "n_file_tasks": len(transfers),
        "n_scheduled": len(scheduled_rows),
        "late_transfers": late_count,
        "overflow_skipped": overflow_skipped,
        "inflight_skipped": inflight_skipped,
        "presence_skipped": presence_skipped,
        "start_min_sec": min(starts),
        "start_max_sec": max(starts),
        "mean_lead_before_alloc_sec": sum(lead_times) / len(lead_times) if lead_times else 0.0,
        "max_lead_before_alloc_sec": max(lead_times) if lead_times else 0.0,
        "mean_presence_slack_sec": (
            sum(presence_slacks) / len(presence_slacks) if presence_slacks else 0.0
        ),
        "max_presence_slack_sec": max(presence_slacks) if presence_slacks else 0.0,
        "peak_starts_per_bucket": max(peak_by_bucket.values()) if peak_by_bucket else 0,
        "peak_concurrent_inflight": _peak_inflight(active_intervals),
        "max_lead_sec": max_lead_sec,
        "max_presence_sec": max_presence_sec,
        "bulk_dispatch_cutoff_sec": bulk_dispatch_cutoff_sec,
        "placement_fraction": placement_fraction,
        "max_simultaneous_starts": max_simultaneous_starts,
        "max_concurrent_inflight": max_concurrent_inflight,
        "time_bucket_sec": time_bucket_sec,
        "mode": mode,
    }
    return scheduled_entries, scheduled_rows, schedule_stats


def emit_drop_in_transfers(
    events_db: Path,
    *,
    jobs_csv: Path | None,
    top_n: int | None,
    job_ids: list[str] | None,
    min_staging_sec: float,
    finish_buffer_sec: float,
    link_gap_sec: float,
    max_lead_sec: float,
    max_presence_sec: float,
    bulk_dispatch_cutoff_sec: float,
    placement_fraction: float,
    max_simultaneous_starts: int,
    max_concurrent_inflight: int,
    max_total_transfers: int,
    max_transfers_per_job: int,
    time_bucket_sec: float,
    mode: str,
    source_label: str,
) -> dict:
    records = load_job_records(events_db)
    objective_records = filter_records_with_input_files(records, jobs_csv=jobs_csv)
    if not objective_records:
        raise SystemExit("No input-requiring job records found in events.db")

    selected_job_ids = select_critical_job_ids(
        objective_records,
        top_n=top_n,
        job_ids=job_ids,
        min_staging_sec=min_staging_sec,
    )
    if not selected_job_ids:
        raise SystemExit("No critical jobs selected")

    critical_job_set = set(selected_job_ids)
    deadlines_by_job = {
        record.job_id: record.alloc_finish_time
        for record in objective_records
        if record.job_id in critical_job_set
    }
    staging_by_job = {
        record.job_id: record.staging_time
        for record in objective_records
        if record.job_id in critical_job_set
    }

    transfers = load_reactive_staging_planned(events_db)
    planned = dedupe_planned_transfers(
        transfers,
        critical_job_ids=critical_job_set,
        deadlines_by_job=deadlines_by_job,
    )
    if not planned:
        raise SystemExit(
            "No cross-site reactive staging transfers found for the selected critical jobs"
        )

    planned, deprioritized_skipped = prioritize_planned_transfers(
        planned,
        staging_by_job=staging_by_job,
        max_total_transfers=max_total_transfers,
        max_transfers_per_job=max_transfers_per_job,
    )

    entries, schedule_debug, schedule_stats = schedule_balanced_drop_ins(
        planned,
        finish_buffer_sec=finish_buffer_sec,
        link_gap_sec=link_gap_sec,
        max_lead_sec=max_lead_sec,
        max_presence_sec=max_presence_sec,
        bulk_dispatch_cutoff_sec=bulk_dispatch_cutoff_sec,
        placement_fraction=placement_fraction,
        max_simultaneous_starts=max_simultaneous_starts,
        max_concurrent_inflight=max_concurrent_inflight,
        time_bucket_sec=time_bucket_sec,
        mode=mode,
    )
    schedule_stats = {
        **schedule_stats,
        "deprioritized_skipped": deprioritized_skipped,
        "max_total_transfers": max_total_transfers,
        "max_transfers_per_job": max_transfers_per_job,
    }
    for entry in entries:
        entry["mode"] = mode

    files_by_job: dict[str, set[str]] = {}
    for transfer in planned:
        files_by_job.setdefault(transfer.job_id, set()).add(transfer.file_id)

    late_count = int(schedule_stats.get("late_transfers", 0))
    overflow_skipped = int(schedule_stats.get("overflow_skipped", 0))
    inflight_skipped = int(schedule_stats.get("inflight_skipped", 0))
    deprioritized_skipped = int(schedule_stats.get("deprioritized_skipped", 0))
    presence_skipped = int(schedule_stats.get("presence_skipped", 0))
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    description = (
        f"Hindsight just-in-time drop-in schedule from {source_label} ({generated_at}). "
        f"Critical jobs only ({len(selected_job_ids)}): {', '.join(selected_job_ids)}. "
        f"Schedules up to {max_total_transfers} {mode} transfers within "
        f"{max_presence_sec:g}s of each job allocation (finish before "
        f"{bulk_dispatch_cutoff_sec:g}s bulk dispatch), with at most "
        f"{max_concurrent_inflight} concurrent in-flight drop-ins."
    )

    return {
        "description": description,
        "source": {
            "events_db": str(events_db),
            "jobs_csv": str(jobs_csv) if jobs_csv else None,
            "min_staging_sec": min_staging_sec,
            "top_n": top_n,
            "job_ids": job_ids,
            "finish_buffer_sec": finish_buffer_sec,
            "link_gap_sec": link_gap_sec,
            "max_lead_sec": max_lead_sec,
            "max_presence_sec": max_presence_sec,
            "bulk_dispatch_cutoff_sec": bulk_dispatch_cutoff_sec,
            "placement_fraction": placement_fraction,
            "max_simultaneous_starts": max_simultaneous_starts,
            "max_concurrent_inflight": max_concurrent_inflight,
            "max_total_transfers": max_total_transfers,
            "max_transfers_per_job": max_transfers_per_job,
            "time_bucket_sec": time_bucket_sec,
            "mode": mode,
            "schedule_stats": schedule_stats,
            "critical_jobs": [
                {
                    "job_id": job_id,
                    "alloc_finish_time_sec": deadlines_by_job.get(job_id),
                    "staging_time_sec": staging_by_job.get(job_id),
                    "n_staging_files": len(files_by_job.get(job_id, [])),
                }
                for job_id in selected_job_ids
            ],
            "schedule_warnings": {
                "late_transfers": late_count,
                "overflow_skipped": overflow_skipped,
                "inflight_skipped": inflight_skipped,
                "presence_skipped": presence_skipped,
                "deprioritized_skipped": deprioritized_skipped,
                "late_examples": [],
            },
        },
        "drop_in_transfers": entries,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a balanced drop-in transfer schedule for critical long-tail jobs "
            "from hindsight reactive staging analysis."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--trial-dir", type=Path, help="Trial directory containing events.db")
    group.add_argument("--events-db", type=Path, help="Path to events.db")

    parser.add_argument(
        "--jobs-csv",
        type=Path,
        help="Workload CSV for ninputdatafiles filter (default: trial jobs.csv)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output JSON path (default: <trial-dir>/drop_in_transfers.generated.json)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        help="Cap critical job count after min-staging filter",
    )
    parser.add_argument(
        "--job-ids",
        type=str,
        help="Comma-separated critical job IDs (overrides automatic selection)",
    )
    parser.add_argument(
        "--min-staging-sec",
        type=float,
        default=DEFAULT_CRITICAL_MIN_STAGING_SEC,
        help=(
            "Only jobs with staging time at least this value are critical "
            f"(default: {DEFAULT_CRITICAL_MIN_STAGING_SEC:g})"
        ),
    )
    parser.add_argument(
        "--finish-buffer",
        type=float,
        default=DEFAULT_FINISH_BUFFER_SEC,
        help=(
            "Each drop-in must finish this many seconds before job allocation "
            f"(default: {DEFAULT_FINISH_BUFFER_SEC:g})"
        ),
    )
    parser.add_argument(
        "--link-gap",
        type=float,
        default=DEFAULT_LINK_GAP_SEC,
        help=f"Gap between consecutive drop-ins on the same link (default: {DEFAULT_LINK_GAP_SEC:g})",
    )
    parser.add_argument(
        "--max-lead",
        type=float,
        default=DEFAULT_MAX_LEAD_SEC,
        help=(
            "Do not start a drop-in earlier than this many seconds before the "
            f"job allocation time (default: {DEFAULT_MAX_LEAD_SEC:g})"
        ),
    )
    parser.add_argument(
        "--max-presence",
        type=float,
        default=DEFAULT_MAX_PRESENCE_SEC,
        help=(
            "Each drop-in must finish at most this many seconds before job allocation "
            "to limit proactive background work from early file placement "
            f"(default: {DEFAULT_MAX_PRESENCE_SEC:g})"
        ),
    )
    parser.add_argument(
        "--bulk-dispatch-cutoff",
        type=float,
        default=DEFAULT_BULK_DISPATCH_CUTOFF_SEC,
        help=(
            "All drop-ins must finish before this simulated time to avoid overlapping "
            f"the bulk job-dispatch window (default: {DEFAULT_BULK_DISPATCH_CUTOFF_SEC:g})"
        ),
    )
    parser.add_argument(
        "--placement-fraction",
        type=float,
        default=DEFAULT_PLACEMENT_FRACTION,
        help=(
            "Where inside the feasible start window to place each wave: 0=earliest, "
            f"1=latest (default: {DEFAULT_PLACEMENT_FRACTION})"
        ),
    )
    parser.add_argument(
        "--max-simultaneous",
        type=int,
        default=DEFAULT_MAX_SIMULTANEOUS_STARTS,
        help=(
            "Maximum drop-in starts allowed in the same time bucket across all links "
            f"(default: {DEFAULT_MAX_SIMULTANEOUS_STARTS})"
        ),
    )
    parser.add_argument(
        "--max-concurrent-inflight",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_INFLIGHT,
        help=(
            "Maximum drop-in transfers that may overlap in simulated time "
            f"(default: {DEFAULT_MAX_CONCURRENT_INFLIGHT})"
        ),
    )
    parser.add_argument(
        "--max-total-transfers",
        type=int,
        default=DEFAULT_MAX_TOTAL_TRANSFERS,
        help=(
            "Global cap on scheduled drop-ins after prioritizing longest files "
            f"(default: {DEFAULT_MAX_TOTAL_TRANSFERS})"
        ),
    )
    parser.add_argument(
        "--max-per-job",
        type=int,
        default=DEFAULT_MAX_TRANSFERS_PER_JOB,
        help=(
            "Maximum drop-ins per critical job after prioritization "
            f"(default: {DEFAULT_MAX_TRANSFERS_PER_JOB})"
        ),
    )
    parser.add_argument(
        "--time-bucket-sec",
        type=float,
        default=DEFAULT_TIME_BUCKET_SEC,
        help=(
            "Bucket width used for global start-time peak capping "
            f"(default: {DEFAULT_TIME_BUCKET_SEC:g})"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("COPY", "MOVE"),
        default=DEFAULT_MODE,
        help=(
            "Transfer mode for every drop-in entry. MOVE (default) avoids leaving "
            "extra replicas that can trigger proactive hotset thrashing."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary to stdout without writing a file",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    events_db = _resolve_events_db(args)
    jobs_csv = _resolve_jobs_csv(args, events_db)

    job_ids = None
    if args.job_ids:
        job_ids = [part.strip() for part in args.job_ids.split(",") if part.strip()]

    source_label = str(args.trial_dir.resolve()) if args.trial_dir else str(events_db)
    payload = emit_drop_in_transfers(
        events_db,
        jobs_csv=jobs_csv,
        top_n=args.top_n,
        job_ids=job_ids,
        min_staging_sec=args.min_staging_sec,
        finish_buffer_sec=args.finish_buffer,
        link_gap_sec=args.link_gap,
        max_lead_sec=args.max_lead,
        max_presence_sec=args.max_presence,
        bulk_dispatch_cutoff_sec=args.bulk_dispatch_cutoff,
        placement_fraction=args.placement_fraction,
        max_simultaneous_starts=args.max_simultaneous,
        max_concurrent_inflight=args.max_concurrent_inflight,
        max_total_transfers=args.max_total_transfers,
        max_transfers_per_job=args.max_per_job,
        time_bucket_sec=args.time_bucket_sec,
        mode=args.mode,
        source_label=source_label,
    )

    critical_rows = sorted(
        payload["source"]["critical_jobs"],
        key=lambda row: row.get("staging_time_sec") or 0.0,
        reverse=True,
    )
    n_entries = len(payload["drop_in_transfers"])
    warnings = payload["source"]["schedule_warnings"]

    print(f"Critical jobs: {len(critical_rows)} (min_staging>={args.min_staging_sec:g}s)")
    for row in critical_rows:
        alloc = row["alloc_finish_time_sec"]
        staging = row["staging_time_sec"]
        print(
            f"  {row['job_id']}: staging={staging:.1f}s, "
            f"alloc_finish={alloc:.0f}s, reactive_files={row['n_staging_files']}"
        )
    print(
        f"Drop-in transfers: {n_entries} "
        f"(finish_buffer={args.finish_buffer:g}s, max_presence={args.max_presence:g}s, "
        f"max_lead={args.max_lead:g}s, bulk_cutoff={args.bulk_dispatch_cutoff:g}s, "
        f"placement={args.placement_fraction:g}, max_simultaneous={args.max_simultaneous}, "
        f"max_inflight={args.max_concurrent_inflight}, max_total={args.max_total_transfers}, "
        f"max_per_job={args.max_per_job}, time_bucket={args.time_bucket_sec:g}s, "
        f"link_gap={args.link_gap:g}s, mode={args.mode})"
    )
    stats = payload["source"].get("schedule_stats") or {}
    if stats:
        print(
            "Schedule stats: "
            f"start_range=[{stats.get('start_min_sec', '?'):g}, {stats.get('start_max_sec', '?'):g}]s, "
            f"mean_presence_slack={stats.get('mean_presence_slack_sec', '?'):g}s, "
            f"max_presence_slack={stats.get('max_presence_slack_sec', '?'):g}s, "
            f"peak_starts_per_bucket={stats.get('peak_starts_per_bucket', '?')}, "
            f"peak_concurrent_inflight={stats.get('peak_concurrent_inflight', '?')}"
        )
    if warnings["late_transfers"] or warnings.get("deprioritized_skipped"):
        print(
            f"WARNING: skipped "
            f"{warnings.get('deprioritized_skipped', 0)} deprioritized, "
            f"{warnings.get('presence_skipped', 0)} presence-overflow, "
            f"{warnings.get('overflow_skipped', 0)} link-overflow, "
            f"{warnings.get('inflight_skipped', 0)} in-flight-cap, "
            f"{warnings['late_transfers']} total not scheduled on time."
        )

    if args.dry_run:
        return 0

    if args.output is not None:
        output_path = args.output.resolve()
    elif args.trial_dir is not None:
        output_path = args.trial_dir.resolve() / "drop_in_transfers.generated.json"
    else:
        output_path = events_db.parent / "drop_in_transfers.generated.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
