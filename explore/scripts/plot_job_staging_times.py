#!/usr/bin/env python3
"""Plot job staging time vs job ID for one trial or all trials under a method/run."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXPLORE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EXPLORE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from datamgmt_explore.plotting.job_staging_time_plot import (
    plot_job_staging_times,
    plot_trial_job_staging_times,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plot job staging time distribution (x=job ID, y=staging seconds) "
            "for a trial events.db or every trial under a method/experiment directory."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--events-db",
        type=Path,
        help="Path to a single trial events.db",
    )
    group.add_argument(
        "--trial-dir",
        type=Path,
        help="Path to a trial directory containing events.db",
    )
    group.add_argument(
        "--experiment-dir",
        type=Path,
        help="Path to a method or experiment dir; plots every trial_*/events.db found",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output PNG path (only with --events-db / --trial-dir).",
    )
    return parser.parse_args()


def _plot_one(trial_dir: Path, output: Path | None = None) -> Path | None:
    db_path = trial_dir / "events.db"
    if not db_path.is_file():
        print(f"skip (no events.db): {trial_dir}", file=sys.stderr)
        return None
    trial_name = trial_dir.name
    trial_index = None
    if trial_name.startswith("trial_"):
        try:
            trial_index = int(trial_name.split("_", 1)[1])
        except ValueError:
            trial_index = None
    if output is not None:
        title = f"Trial {trial_index}: job staging times" if trial_index is not None else None
        return plot_job_staging_times(db_path, output, title=title)
    return plot_trial_job_staging_times(
        db_path,
        trial_dir / "plots",
        trial_index=trial_index,
    )


def main() -> int:
    args = parse_args()

    if args.events_db is not None:
        db_path = args.events_db.resolve()
        if not db_path.is_file():
            raise SystemExit(f"events.db not found: {db_path}")
        output = (args.output or db_path.parent / "plots" / "job_staging_times.png").resolve()
        path = plot_job_staging_times(db_path, output)
        if path is None:
            raise SystemExit("No job records to plot.")
        print(path)
        return 0

    if args.trial_dir is not None:
        trial_dir = args.trial_dir.resolve()
        if not trial_dir.is_dir():
            raise SystemExit(f"Trial directory not found: {trial_dir}")
        path = _plot_one(trial_dir, output=args.output.resolve() if args.output else None)
        if path is None:
            raise SystemExit("No job records to plot.")
        print(path)
        return 0

    experiment_dir = args.experiment_dir.resolve()
    if not experiment_dir.is_dir():
        raise SystemExit(f"Experiment directory not found: {experiment_dir}")

    trial_dirs = sorted(experiment_dir.glob("trial_*"))
    if not trial_dirs:
        trial_dirs = sorted(experiment_dir.glob("methods/*/trial_*"))
    if not trial_dirs:
        raise SystemExit(f"No trial_* directories under {experiment_dir}")

    written = 0
    for trial_dir in trial_dirs:
        path = _plot_one(trial_dir)
        if path is not None:
            print(path)
            written += 1
    if written == 0:
        raise SystemExit("No job staging plots written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
