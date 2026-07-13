from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MethodTimingTracker:
    methods: list[str]
    rows: dict[int, dict[str, tuple[float, float]]] = field(default_factory=dict)
    max_trials: int | None = None

    def set(self, method: str, trial_index: int, sim_sec: float, explore_sec: float) -> None:
        self.rows.setdefault(trial_index, {})[method] = (sim_sec, explore_sec)

    def trial_indices(self) -> list[int]:
        if self.max_trials is not None:
            return list(range(self.max_trials))
        if not self.rows:
            return []
        return list(range(max(self.rows) + 1))

    def get(self, trial_index: int, method: str) -> tuple[float, float] | None:
        return self.rows.get(trial_index, {}).get(method)


class LiveMethodTimingCsv:
    """Live-updated per-trial timing CSV for parallel method comparison."""

    def __init__(self, experiment_dir: Path, methods: list[str], *, max_trials: int | None = None) -> None:
        self.experiment_dir = experiment_dir
        self.methods = methods
        self.max_trials = max_trials
        self.output_dir = experiment_dir / "plots"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_path = self.output_dir / "methods_timing.csv"

    def column_names(self) -> list[str]:
        columns = ["trial"]
        for method in self.methods:
            columns.append(f"{method}_sim_sec")
            columns.append(f"{method}_explore_sec")
        return columns

    def write(self, tracker: MethodTimingTracker) -> Path:
        tracker.max_trials = self.max_trials
        fieldnames = self.column_names()
        with self.output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for trial_index in tracker.trial_indices():
                row: dict[str, str | int] = {"trial": trial_index}
                for method in self.methods:
                    timing = tracker.get(trial_index, method)
                    sim_key = f"{method}_sim_sec"
                    explore_key = f"{method}_explore_sec"
                    if timing is None:
                        row[sim_key] = ""
                        row[explore_key] = ""
                    else:
                        row[sim_key] = f"{timing[0]:.3f}"
                        row[explore_key] = f"{timing[1]:.3f}"
                writer.writerow(row)
        return self.output_path
