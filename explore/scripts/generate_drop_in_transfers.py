#!/usr/bin/env python3
"""Generate a drop_in_transfers.json from a completed trial's events.db.

Reads the staging (FileTransfer) events of chosen tail jobs and plans drop-in
transfers that prestage those files to the site the job ran on, early enough
to complete before the job's allocation. Transfer durations are estimated from
site_connections.json bandwidths; when the direct source->destination link is
too slow to make the deadline (e.g. the 0.1 Mbps pathological links), the file
is relayed through a fast intermediate hub: COPY source->hub, then MOVE
hub->destination (the MOVE cleans the temporary hub copy).

Use --jobs for explicit job IDs or --top to auto-select the jobs with the
longest staging time in the trial.

Example:
    python explore/scripts/generate_drop_in_transfers.py \
        --trial-dir explore/runs/<run>/methods/bayesian_opt/trial_0000 \
        --top 3 --output config/drop_in_transfers.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

EXPLORE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EXPLORE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from datamgmt_explore.metrics import load_job_records

MIN_DROP_IN_TIME = 1.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trial-dir",
        type=Path,
        required=True,
        help="Trial directory containing events.db",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--jobs",
        nargs="+",
        help="Explicit job IDs whose staging transfers become drop-ins",
    )
    group.add_argument(
        "--top",
        type=int,
        help="Auto-select the N jobs with the longest staging time",
    )
    parser.add_argument(
        "--connections",
        type=Path,
        default=Path("config/site_connections.json"),
        help="site_connections.json used to estimate link bandwidths",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=2000.0,
        help="Seconds every prestage must complete before the job's allocation (default 2000)",
    )
    parser.add_argument(
        "--safety",
        type=float,
        default=3.0,
        help="Multiplier on estimated transfer durations to absorb link contention (default 3)",
    )
    parser.add_argument(
        "--max-lead",
        type=float,
        default=5400.0,
        help=(
            "Maximum seconds before job allocation that a drop-in may start "
            "(default 5400)"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON path (default <trial-dir>/drop_in_transfers.json)",
    )
    return parser.parse_args()


def select_tail_jobs(db_path: Path, top_n: int) -> list[str]:
    records = load_job_records(db_path)
    records.sort(key=lambda record: record.staging_time, reverse=True)
    selected = records[:top_n]
    for record in selected:
        print(
            f"selected job {record.job_id} @ {record.site}: "
            f"staging {record.staging_time:.0f}s"
        )
    return [record.job_id for record in selected]


def staging_transfers_for_job(
    conn: sqlite3.Connection,
    job_id: str,
) -> tuple[float | None, list[dict]]:
    """Return (allocation_time, staged transfers) recorded for one job."""
    alloc_row = conn.execute(
        "SELECT MIN(TIME) FROM EVENTS WHERE EVENT='JobAllocation' AND JOB_ID=?",
        (job_id,),
    ).fetchone()
    alloc_time = alloc_row[0] if alloc_row and alloc_row[0] is not None else None

    transfers = []
    rows = conn.execute(
        "SELECT METADATA FROM EVENTS "
        "WHERE EVENT='FileTransfer' AND STATE='Started' AND JOB_ID=?",
        (job_id,),
    )
    for (metadata,) in rows:
        meta = json.loads(metadata)
        transfers.append(
            {
                "file": meta["file"],
                "source_site": meta["source_site"],
                "destination_site": meta["destination_site"],
                "size": float(meta.get("size", 0.0)),
            }
        )
    return alloc_time, transfers


class LinkPlanner:
    """Plan non-overlapping transfers on shared links."""

    def __init__(self, connections: dict, safety: float) -> None:
        self.safety = safety
        self.bandwidth_bps: dict[frozenset, float] = {}
        for key, props in connections.items():
            a, _, b = key.partition(":")
            mbps = float(str(props["bandwidth"]).replace("Mbps", ""))
            self.bandwidth_bps[frozenset((a, b))] = mbps * 1e6 / 8.0
        self.sites = sorted({site for pair in self.bandwidth_bps for site in pair})
        self.reservations: dict[frozenset, list[tuple[float, float]]] = {}

    def bandwidth(self, a: str, b: str) -> float | None:
        return self.bandwidth_bps.get(frozenset((a, b)))

    def duration(self, size: float, a: str, b: str) -> float | None:
        bw = self.bandwidth(a, b)
        if bw is None or bw <= 0:
            return None
        return size / bw * self.safety

    def latest_slot(
        self,
        a: str,
        b: str,
        *,
        earliest: float,
        latest_end: float,
        size: float,
    ) -> tuple[float, float] | None:
        """Return the latest free slot within [earliest, latest_end]."""
        dur = self.duration(size, a, b)
        if dur is None:
            return None

        end = latest_end
        reservations = sorted(
            self.reservations.get(frozenset((a, b)), []),
            reverse=True,
        )
        for busy_start, busy_end in reservations:
            start = end - dur
            if end <= busy_start or start >= busy_end:
                continue
            end = busy_start

        start = end - dur
        if start < max(MIN_DROP_IN_TIME, earliest):
            return None
        return start, end

    def commit(self, a: str, b: str, start: float, end: float) -> None:
        key = frozenset((a, b))
        self.reservations.setdefault(key, []).append((start, end))


def plan_transfer(
    planner: LinkPlanner,
    transfer: dict,
    deadline: float,
    earliest_start: float,
    job_id: str,
) -> tuple[list[dict], str]:
    """Plan a single prestage as direct or hub-relayed drop-in entries.

    Returns (entries, note). Entries are committed to the planner's link
    schedule. Falls back to a direct transfer at the earliest possible time
    (with a warning note) when no plan can meet the deadline.
    """
    src = transfer["source_site"]
    dst = transfer["destination_site"]
    size = transfer["size"]
    filename = transfer["file"]

    direct = planner.latest_slot(
        src,
        dst,
        earliest=earliest_start,
        latest_end=deadline,
        size=size,
    )
    if direct is not None:
        start, end = direct
        planner.commit(src, dst, start, end)
        return (
            [
                {
                    "time": round(start, 1),
                    "duration_sec": round(end - start, 3),
                    "file": filename,
                    "source_site": src,
                    "destination_site": dst,
                    "mode": "COPY",
                    "job_id": job_id,
                }
            ],
            "direct",
        )

    # Direct link cannot fit in the presence window. Try a two-hop relay,
    # scheduling hop 2 backward from the deadline and hop 1 immediately before it.
    best = None
    for hub in planner.sites:
        if hub in (src, dst):
            continue
        hop2 = planner.latest_slot(
            hub,
            dst,
            earliest=earliest_start,
            latest_end=deadline,
            size=size,
        )
        if hop2 is None:
            continue
        hop1 = planner.latest_slot(
            src,
            hub,
            earliest=earliest_start,
            latest_end=hop2[0],
            size=size,
        )
        if hop1 is None:
            continue
        # Prefer the route that starts latest, minimizing replica residence.
        if best is None or (hop1[0], hop2[0]) > (best[1][0], best[2][0]):
            best = (hub, hop1, hop2)
    if best is not None:
        hub, hop1, hop2 = best
        planner.commit(src, hub, *hop1)
        planner.commit(hub, dst, *hop2)
        return (
            [
                {
                    "time": round(hop1[0], 1),
                    "duration_sec": round(hop1[1] - hop1[0], 3),
                    "file": filename,
                    "source_site": src,
                    "destination_site": hub,
                    "mode": "COPY",
                    "job_id": job_id,
                },
                {
                    "time": round(hop2[0], 1),
                    "duration_sec": round(hop2[1] - hop2[0], 3),
                    "file": filename,
                    "source_site": hub,
                    "destination_site": dst,
                    "mode": "MOVE",
                    "job_id": job_id,
                },
            ],
            f"relay via {hub}",
        )

    print(
        f"WARNING: no route for {filename} {src}->{dst} fits "
        f"t={earliest_start:.0f}..{deadline:.0f}; skipped",
        file=sys.stderr,
    )
    return [], "skipped (outside presence window)"


def main() -> int:
    args = parse_args()
    trial_dir = args.trial_dir.resolve()
    db_path = trial_dir / "events.db"
    if not db_path.is_file():
        raise SystemExit(f"events.db not found: {db_path}")
    if not args.connections.is_file():
        raise SystemExit(f"connections file not found: {args.connections}")
    if args.margin < 0:
        raise SystemExit("--margin must be non-negative")
    if args.max_lead <= args.margin:
        raise SystemExit("--max-lead must be greater than --margin")
    if args.safety <= 0:
        raise SystemExit("--safety must be positive")

    with args.connections.open(encoding="utf-8") as handle:
        planner = LinkPlanner(json.load(handle), args.safety)

    if args.jobs:
        job_ids = list(args.jobs)
    else:
        job_ids = select_tail_jobs(db_path, args.top)
        if not job_ids:
            raise SystemExit("No job records found in events.db")

    # Plan earliest-deadline-first so tight jobs get the links first.
    jobs: list[tuple[float, str, list[dict]]] = []
    with sqlite3.connect(db_path) as conn:
        for job_id in job_ids:
            alloc_time, transfers = staging_transfers_for_job(conn, job_id)
            if alloc_time is None:
                print(f"skip job {job_id}: no JobAllocation event", file=sys.stderr)
                continue
            if not transfers:
                print(f"skip job {job_id}: no staging transfers recorded", file=sys.stderr)
                continue
            jobs.append((alloc_time, job_id, transfers))
    jobs.sort()

    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()  # (file, destination_site)
    for alloc_time, job_id, transfers in jobs:
        deadline = alloc_time - args.margin
        earliest_start = max(MIN_DROP_IN_TIME, alloc_time - args.max_lead)
        direct_count = relay_count = skipped_count = 0
        for transfer in transfers:
            key = (transfer["file"], transfer["destination_site"])
            if key in seen:
                continue
            seen.add(key)
            planned, note = plan_transfer(
                planner,
                transfer,
                deadline,
                earliest_start,
                job_id,
            )
            entries.extend(planned)
            if note.startswith("relay"):
                relay_count += 1
            elif note.startswith("direct"):
                direct_count += 1
            else:
                skipped_count += 1
        print(
            f"job {job_id}: allocation t={alloc_time:.0f}, "
            f"window t={earliest_start:.0f}..{deadline:.0f}, "
            f"{direct_count} direct + {relay_count} relayed + "
            f"{skipped_count} skipped prestage(s)"
        )

    if not entries:
        raise SystemExit("No drop-in transfers generated.")

    entries.sort(key=lambda entry: (entry["time"], entry["file"]))
    payload = {
        "description": (
            f"Auto-generated from {trial_dir} for jobs {', '.join(job_ids)} "
            f"(margin {args.margin:.0f}s, max lead {args.max_lead:.0f}s, "
            f"safety x{args.safety:g}). Schedules each prestage as late as possible "
            "inside its job's presence window, relaying via a fast hub (COPY then "
            "MOVE) when the direct link cannot fit."
        ),
        "source": {
            "events_db": str(db_path),
            "critical_jobs": [
                {
                    "job_id": job_id,
                    "alloc_finish_time_sec": alloc_time,
                    "drop_in_earliest_start_sec": max(
                        MIN_DROP_IN_TIME, alloc_time - args.max_lead
                    ),
                    "drop_in_deadline_sec": alloc_time - args.margin,
                }
                for alloc_time, job_id, _ in jobs
            ],
            "margin_sec": args.margin,
            "max_lead_sec": args.max_lead,
            "safety_multiplier": args.safety,
        },
        "drop_in_transfers": entries,
    }

    output = (args.output or trial_dir / "drop_in_transfers.json").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(entries)} drop-in transfer(s) -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
