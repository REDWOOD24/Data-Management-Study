from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

P95_MAX_STAGING_TAIL_WEIGHT = 3.0

# Tail/bulk split objective: mean staging of bottom 95% vs top 5% of jobs.
STAGING_TAIL_FRACTION = 0.05
# Heavy top weight keeps cost sensitive to fixing tail jobs instead of the flat bulk.
TAIL_BULK_BOTTOM_WEIGHT = 0.05
TAIL_BULK_TOP_WEIGHT = 0.95


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    site: str
    alloc_finish_time: float
    exec_start_time: float
    end_time: float
    staging_time: float
    end_to_end_time: float


PER_JOB_QUERY = """
SELECT
    jf.JOB_ID AS job_id,
    json_extract(jf.METADATA, '$.site') AS site,
    ja.TIME AS alloc_finish_time,
    js.TIME AS exec_start_time,
    (
        SELECT MAX(TIME)
        FROM EVENTS e
        WHERE e.JOB_ID = jf.JOB_ID
    ) AS end_time
FROM EVENTS jf
JOIN EVENTS ja
  ON ja.JOB_ID = jf.JOB_ID
 AND ja.EVENT = 'JobAllocation'
 AND ja.STATE = 'Finished'
JOIN EVENTS js
  ON js.JOB_ID = jf.JOB_ID
 AND js.EVENT = 'JobExecution'
 AND js.STATE = 'Started'
WHERE jf.EVENT = 'JobExecution'
  AND jf.STATE = 'Finished'
  AND json_extract(jf.METADATA, '$.site') IS NOT NULL
ORDER BY exec_start_time
"""


def load_job_records(db_path: Path) -> list[JobRecord]:
    if not db_path.is_file():
        return []

    records: list[JobRecord] = []
    with sqlite3.connect(db_path) as conn:
        for job_id, site, alloc_finish, exec_start, end_time in conn.execute(PER_JOB_QUERY):
            alloc_finish_time = float(alloc_finish)
            exec_start_time = float(exec_start)
            end = float(end_time)
            records.append(
                JobRecord(
                    job_id=str(job_id),
                    site=str(site),
                    alloc_finish_time=alloc_finish_time,
                    exec_start_time=exec_start_time,
                    end_time=end,
                    staging_time=exec_start_time - alloc_finish_time,
                    end_to_end_time=end - alloc_finish_time,
                )
            )
    return records


def load_n_input_files_by_job_id(jobs_csv: Path) -> dict[str, int]:
    """Map pandaid / job_id → ninputdatafiles from a workload jobs CSV."""
    if not jobs_csv.is_file():
        return {}

    counts: dict[str, int] = {}
    with jobs_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            job_id = str(row.get("pandaid") or row.get("job_id") or "").strip()
            if not job_id:
                continue
            raw = row.get("ninputdatafiles") or "0"
            try:
                counts[job_id] = int(raw)
            except ValueError:
                counts[job_id] = 0
    return counts


def filter_records_with_input_files(
    records: list[JobRecord],
    *,
    jobs_csv: Path | None = None,
    n_input_by_job: dict[str, int] | None = None,
) -> list[JobRecord]:
    """Keep only jobs that declare a non-zero input-file requirement.

    Used for every objective so scoring ignores jobs that never need input data
    (and therefore always have zero staging time).
    """
    counts = n_input_by_job
    if counts is None:
        if jobs_csv is None:
            return list(records)
        counts = load_n_input_files_by_job_id(jobs_csv)
    if not counts:
        return list(records)
    return [record for record in records if counts.get(record.job_id, 0) > 0]


def input_requiring_job_percentage(
    *,
    jobs_csv: Path | None = None,
    n_input_by_job: dict[str, int] | None = None,
) -> float | None:
    """Percentage of workload jobs with ninputdatafiles > 0 (None if unknown)."""
    counts = n_input_by_job
    if counts is None:
        if jobs_csv is None or not jobs_csv.is_file():
            return None
        counts = load_n_input_files_by_job_id(jobs_csv)
    if not counts:
        return None
    total = len(counts)
    if total == 0:
        return None
    with_input = sum(1 for value in counts.values() if value > 0)
    return 100.0 * with_input / total


def split_staging_by_tail_fraction(
    records: list[JobRecord],
    *,
    tail_fraction: float = STAGING_TAIL_FRACTION,
) -> tuple[list[float], list[float]]:
    """Partition job staging times into bottom bulk and top tail buckets by count."""
    staging = sorted(record.staging_time for record in records)
    n = len(staging)
    if n == 0:
        return [], []

    n_top = max(1, int(np.ceil(n * tail_fraction)))
    if n_top >= n:
        return [], staging
    return staging[:-n_top], staging[-n_top:]


def compute_tail_bulk_staging_cost(
    records: list[JobRecord],
    *,
    bottom_weight: float = TAIL_BULK_BOTTOM_WEIGHT,
    top_weight: float = TAIL_BULK_TOP_WEIGHT,
    tail_fraction: float = STAGING_TAIL_FRACTION,
) -> tuple[float, float, float]:
    """Return (avg_bottom_staging, avg_top_staging, cost).

    cost = bottom_weight * log1p(mean(bottom)) + top_weight * log1p(mean(top)).
    Log-scaled terms spread mid-tier policies apart while still penalizing outliers.
    Lower cost is better. Minimum is 0 when all staging times are 0.
    """
    if not records:
        return float("nan"), float("nan"), float("inf")

    bottom, top = split_staging_by_tail_fraction(records, tail_fraction=tail_fraction)
    avg_bottom = float(np.mean(bottom)) if bottom else 0.0
    avg_top = float(np.mean(top)) if top else 0.0
    cost = bottom_weight * float(np.log1p(max(avg_bottom, 0.0))) + top_weight * float(
        np.log1p(max(avg_top, 0.0))
    )
    return avg_bottom, avg_top, cost


def compute_p95_max_staging_reward(
    records: list[JobRecord],
    *,
    tail_weight: float = P95_MAX_STAGING_TAIL_WEIGHT,
) -> tuple[float, float, float]:
    """Return (p95_staging, max_staging, reward) with reward = log1p(p95) + w*log1p(max).

    Lower reward is better. The minimum is 0 when all staging times are 0.
    """
    if not records:
        return float("nan"), float("nan"), float("inf")

    staging = [record.staging_time for record in records]
    p95 = float(np.percentile(staging, 95))
    max_staging = float(max(staging))
    reward = float(np.log1p(max(p95, 0.0))) + tail_weight * float(
        np.log1p(max(max_staging, 0.0))
    )
    return p95, max_staging, reward


def mean_staging_time_all_jobs(records: list[JobRecord]) -> float | None:
    """Mean staging time across every job, weighted implicitly by job count."""
    if not records:
        return None
    return float(sum(record.staging_time for record in records) / len(records))


def mean_end_to_end_time_all_jobs(records: list[JobRecord]) -> float | None:
    """Mean end-to-end time across every job, weighted implicitly by job count."""
    if not records:
        return None
    return float(sum(record.end_to_end_time for record in records) / len(records))


def load_global_job_timing(db_path: Path) -> tuple[float | None, float | None]:
    records = load_job_records(db_path)
    return mean_staging_time_all_jobs(records), mean_end_to_end_time_all_jobs(records)


def aggregate_by_site(records: list[JobRecord]) -> dict[str, dict[str, float]]:
    buckets: dict[str, list[JobRecord]] = {}
    for record in records:
        buckets.setdefault(record.site, []).append(record)

    summary: dict[str, dict[str, float]] = {}
    for site, site_records in buckets.items():
        staging = [item.staging_time for item in site_records]
        end_to_end = [item.end_to_end_time for item in site_records]
        summary[site] = {
            "job_count": float(len(site_records)),
            "avg_staging_time": sum(staging) / len(staging),
            "avg_end_to_end_time": sum(end_to_end) / len(end_to_end),
        }
    return summary


def metrics_to_dict(records: list[JobRecord]) -> dict:
    return {
        "job_records": [asdict(record) for record in records],
        "site_summary": aggregate_by_site(records),
        "job_count": len(records),
    }


def write_metrics(path: Path, records: list[JobRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(metrics_to_dict(records), handle, indent=2)
        handle.write("\n")
