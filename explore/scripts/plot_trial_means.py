#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXPLORE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EXPLORE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from datamgmt_explore.plotting.trial_comparison_plots import plot_trial_mean_stacked_bars
from datamgmt_explore.settings import load_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot per-trial mean stacked transfer bars and avg job staging time.",
    )
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--settings", default=str(EXPLORE_ROOT / "config" / "settings.yaml"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings(args.settings)
    experiment_dir = args.experiment.resolve()
    if not experiment_dir.is_dir():
        raise SystemExit(f"Experiment directory not found: {experiment_dir}")

    output_path = plot_trial_mean_stacked_bars(experiment_dir, repo_root=settings.repo_root)
    if output_path is None:
        raise SystemExit("No trial events.db files with plottable metrics found.")
    print(f"Trial mean plot: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
