#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXPLORE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EXPLORE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from datamgmt_explore.plotting.experiment_plots import (
    diagnose_experiment,
    plot_experiment_progress,
    write_failure_report,
)
from datamgmt_explore.plotting.method_comparison_live import plot_methods_comparison_static
from datamgmt_explore.plotting.trial_comparison_plots import plot_trial_mean_stacked_bars
from datamgmt_explore.plotting.trial_plots import plot_all_trials
from datamgmt_explore.settings import load_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate per-trial and experiment plots; write failure diagnosis.",
    )
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--settings", default=str(EXPLORE_ROOT / "config" / "settings.yaml"))
    parser.add_argument("--experiment-plots", action="store_true")
    parser.add_argument("--diagnose", action="store_true", default=True)
    parser.add_argument("--top-k", type=int, default=40)
    parser.add_argument("--min-bytes", type=float, default=0.0)
    parser.add_argument("--show-end-to-end", action="store_true")
    return parser.parse_args()


def load_methods(experiment_dir: Path) -> list[str]:
    methods_root = experiment_dir / "methods"
    if methods_root.is_dir():
        return sorted(path.name for path in methods_root.iterdir() if path.is_dir())

    run_config_path = experiment_dir / "run_config.json"
    if run_config_path.is_file():
        run_config = json.loads(run_config_path.read_text(encoding="utf-8"))
        agents = run_config.get("agents")
        if isinstance(agents, list) and agents:
            return [str(item) for item in agents]

    return []


def method_dirs(experiment_dir: Path, methods: list[str]) -> list[Path]:
    methods_root = experiment_dir / "methods"
    if methods_root.is_dir():
        return [methods_root / method for method in methods]

    if any(experiment_dir.glob("trial_*")):
        return [experiment_dir]
    return []


def main() -> int:
    args = parse_args()
    settings = load_settings(args.settings)
    experiment_dir = args.experiment.resolve()
    if not experiment_dir.is_dir():
        raise SystemExit(f"Experiment directory not found: {experiment_dir}")

    methods = load_methods(experiment_dir)
    targets = method_dirs(experiment_dir, methods) if methods else [experiment_dir]

    for target_dir in targets:
        trial_plots = plot_all_trials(
            target_dir,
            repo_root=settings.repo_root,
            top_k=args.top_k,
            min_bytes=args.min_bytes,
            show_end_to_end=args.show_end_to_end,
        )
        label = target_dir.name if target_dir != experiment_dir else "experiment"
        for trial_index, paths in sorted(trial_plots.items()):
            print(f"{label}/trial_{trial_index:04d}: {len(paths)} plot(s)")

        if args.experiment_plots:
            for path in plot_experiment_progress(target_dir):
                print(f"{label} experiment plot: {path}")
            trial_mean_path = plot_trial_mean_stacked_bars(
                target_dir,
                repo_root=settings.repo_root,
            )
            if trial_mean_path:
                print(f"{label} experiment plot: {trial_mean_path}")

    if args.experiment_plots and methods:
        comparison_path = plot_methods_comparison_static(
            experiment_dir,
            methods,
            repo_root=settings.repo_root,
        )
        if comparison_path:
            print(f"Experiment plot: {comparison_path}")

    if args.diagnose:
        failures = diagnose_experiment(experiment_dir)
        report_path = write_failure_report(experiment_dir)
        print(f"Failure report: {report_path}")
        for failure in failures:
            print(
                f"  trial_{failure.trial_index:04d}: {failure.error_type} "
                f"(file={failure.file_id}, site={failure.site}, t={failure.sim_time})"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
