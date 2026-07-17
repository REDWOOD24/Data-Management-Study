from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from datamgmt_explore.metrics import (
    STAGING_TAIL_FRACTION,
    TAIL_BULK_BOTTOM_WEIGHT,
    TAIL_BULK_TOP_WEIGHT,
)
from datamgmt_explore.plotting.trial_comparison_plots import (
    TrialMeanBars,
    load_trial_mean_bars,
)
from datamgmt_explore.plotting.trial_plots import _load_transfer_analysis

FONT_SIZES = {
    "title": 20,
    "label": 17,
    "tick": 14,
    "legend": 14,
}

# Ingress volume equals egress volume, so stacked bars use reactive vs proactive only.
STACK_COLORS = ["#2a9d8f", "#e76f51"]
STACK_LABELS = ["Reactive", "Proactive"]

METHOD_LABELS = {
    "bayesian_opt": "Bayesian opt",
    "rl_policy": "RL policy",
    "random_search": "Random search",
    "bandit": "Bandit",
}


@dataclass
class MethodTrialPoint:
    method: str
    trial_index: int
    bars: TrialMeanBars


@dataclass
class MethodComparisonTracker:
    methods: list[str]
    points: list[MethodTrialPoint] = field(default_factory=list)

    def add(self, method: str, bars: TrialMeanBars) -> None:
        self.points.append(MethodTrialPoint(method=method, trial_index=bars.trial_index, bars=bars))

    def points_for(self, method: str) -> list[MethodTrialPoint]:
        return [point for point in self.points if point.method == method]

    def max_trial_index(self) -> int:
        if not self.points:
            return -1
        return max(point.trial_index for point in self.points)


class LiveMethodComparisonPlot:
    """Grouped per-trial method comparison; saves PNG after each update (no GUI window)."""

    METHOD_COLORS = {
        "bayesian_opt": "#264653",
        "rl_policy": "#e76f51",
        "bandit": "#2a9d8f",
        "random_search": "#6d597a",
    }

    def __init__(
        self,
        experiment_dir: Path,
        methods: list[str],
        *,
        repo_root: Path,
        max_trials: int | None = None,
    ) -> None:
        self.experiment_dir = experiment_dir
        self.methods = methods
        self.repo_root = repo_root
        self.max_trials = max_trials
        self.output_dir = experiment_dir / "plots"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_path = self.output_dir / "methods_comparison.png"
        self._pta = _load_transfer_analysis(repo_root)
        self._fig: plt.Figure | None = None
        self._ax: plt.Axes | None = None
        self._ax2: plt.Axes | None = None
        self._input_job_pct: float | None = None
        self._input_job_counts: tuple[int, int] | None = None  # (with_input, total)
        self._refresh_input_job_stats()

    def _refresh_input_job_stats(self) -> None:
        """Resolve input-job count/fraction from trial CSVs or the default workload."""
        if self._input_job_pct is not None and self._input_job_counts is not None:
            return

        from datamgmt_explore.metrics import load_n_input_files_by_job_id

        candidates: list[Path] = []
        for method in self.methods:
            method_dir = self.experiment_dir / "methods" / method
            if not method_dir.is_dir():
                continue
            for trial_dir in sorted(method_dir.glob("trial_*")):
                candidates.append(trial_dir / "jobs.csv")
                candidates.append(trial_dir / "jobs_truncated.csv")
        candidates.append(self.repo_root / "input" / "mimic_job.csv")

        seen: set[Path] = set()
        for jobs_csv in candidates:
            try:
                resolved = jobs_csv.resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            counts = load_n_input_files_by_job_id(resolved)
            if not counts:
                continue
            total = len(counts)
            with_input = sum(1 for value in counts.values() if value > 0)
            self._input_job_counts = (with_input, total)
            self._input_job_pct = 100.0 * with_input / total
            return

    def _method_label(self, method: str) -> str:
        return METHOD_LABELS.get(method, method.replace("_", " ").title())

    def _plot_title(self) -> str:
        self._refresh_input_job_stats()
        bulk_pct = int(round((1.0 - STAGING_TAIL_FRACTION) * 100))
        tail_pct = int(round(STAGING_TAIL_FRACTION * 100))
        split = f"{bulk_pct}/{tail_pct} split"
        if self._input_job_counts is not None and self._input_job_pct is not None:
            with_input, total = self._input_job_counts
            return (
                "Method comparison: mean transfer volume and staging cost "
                f"({with_input}/{total} jobs with input files = {self._input_job_pct:.1f}%; {split})"
            )
        return (
            "Method comparison: mean transfer volume and staging cost "
            f"({split}; input-requiring jobs only)"
        )

    def _cost_ylabel(self) -> str:
        bulk_pct = int(round((1.0 - STAGING_TAIL_FRACTION) * 100))
        tail_pct = int(round(STAGING_TAIL_FRACTION * 100))
        return (
            f"Cost: {TAIL_BULK_BOTTOM_WEIGHT:g}·log1p(avg bottom {bulk_pct}%) + "
            f"{TAIL_BULK_TOP_WEIGHT:g}·log1p(avg top {tail_pct}%) "
            "(input jobs only; lower is better)"
        )

    def load_point_from_trial(self, method: str, trial_dir: Path) -> MethodTrialPoint | None:
        trial_index = int(trial_dir.name.split("_", 1)[1])
        bars = load_trial_mean_bars(trial_dir / "events.db", trial_index, self._pta)
        if bars is None:
            return None
        return MethodTrialPoint(method=method, trial_index=trial_index, bars=bars)

    def update(self, tracker: MethodComparisonTracker) -> Path:
        trial_count = self.max_trials if self.max_trials is not None else tracker.max_trial_index() + 1
        trial_count = max(trial_count, 1)
        method_count = len(self.methods)
        group_width = 0.8
        bar_width = group_width / max(method_count, 1)

        if self._fig is None:
            width = max(12.0, trial_count * 1.4)
            self._fig, self._ax = plt.subplots(figsize=(width, 8.0))
            self._ax2 = self._ax.twinx()
        assert self._ax is not None and self._ax2 is not None

        self._ax.cla()
        self._ax2.cla()

        ymax_left = 0.0
        ymax_right = 0.0
        reward_handles: list[Any] = []
        reward_labels: list[str] = []
        seen_methods: set[str] = set()
        stack_labels_set = False

        for method_index, method in enumerate(self.methods):
            offset = -group_width / 2 + bar_width / 2 + method_index * bar_width
            edge_color = self.METHOD_COLORS.get(method, "#333333")
            method_points = sorted(tracker.points_for(method), key=lambda item: item.trial_index)

            reward_xs: list[float] = []
            reward_ys: list[float] = []

            for point in method_points:
                x = point.trial_index + offset
                # Mean ingress == mean egress; plot reactive vs proactive once.
                layers = [
                    point.bars.ingress_reactive,
                    point.bars.ingress_proactive,
                ]
                bottom = 0.0
                for layer_index, value in enumerate(layers):
                    self._ax.bar(
                        x,
                        value,
                        width=bar_width * 0.95,
                        bottom=bottom,
                        color=STACK_COLORS[layer_index],
                        edgecolor="black",
                        linewidth=0.8,
                        hatch=None,
                        label=(
                            STACK_LABELS[layer_index]
                            if not stack_labels_set
                            else None
                        ),
                    )
                    bottom += value
                stack_labels_set = True
                ymax_left = max(ymax_left, bottom)

                if point.bars.staging_reward is not None and np.isfinite(point.bars.staging_reward):
                    reward_xs.append(x)
                    reward_ys.append(float(point.bars.staging_reward))
                    ymax_right = max(ymax_right, float(point.bars.staging_reward))

            if reward_xs:
                (line,) = self._ax2.plot(
                    reward_xs,
                    reward_ys,
                    marker="s",
                    linestyle=(0, (1.2, 2.2)),
                    linewidth=1.6,
                    color=edge_color,
                    markersize=8,
                    zorder=5,
                )
                if method not in seen_methods:
                    seen_methods.add(method)
                    reward_handles.append(line)
                    reward_labels.append(self._method_label(method))

        self._ax.set_xticks(range(trial_count))
        self._ax.set_xticklabels([str(index) for index in range(trial_count)], fontsize=FONT_SIZES["tick"])
        self._ax.set_xlabel("Trial", fontsize=FONT_SIZES["label"], labelpad=10)
        self._ax.set_ylabel("Mean transfer volume (GiB)", fontsize=FONT_SIZES["label"], labelpad=12)
        self._ax2.set_ylabel("")
        self._ax.set_title(
            self._plot_title(),
            fontsize=FONT_SIZES["title"],
            pad=28,
        )
        self._ax.tick_params(axis="both", labelsize=FONT_SIZES["tick"])
        self._ax2.tick_params(axis="y", labelsize=FONT_SIZES["tick"])

        ymax_left = max(ymax_left, 0.01) * 1.12
        self._ax.set_ylim(0, ymax_left)
        if ymax_right > 0.0:
            self._ax2.set_ylim(0.0, ymax_right * 1.12)
        else:
            self._ax2.set_ylim(0.0, 50.0)
        self._ax.grid(True, axis="y", alpha=0.3)
        self._ax2.set_ylabel(
            self._cost_ylabel(),
            fontsize=FONT_SIZES["label"],
            labelpad=14,
        )
        self._ax2.yaxis.set_label_position("right")
        self._ax2.tick_params(axis="y", labelsize=FONT_SIZES["tick"], labelright=True, right=True)

        stack_handles, legend_stack_labels = self._ax.get_legend_handles_labels()
        # Legends sit just above the axes (below the title), side-by-side with gap.
        stack_legend = None
        if stack_handles:
            stack_legend = self._ax.legend(
                stack_handles,
                legend_stack_labels,
                loc="lower center",
                bbox_to_anchor=(0.28, 1.0),
                ncol=min(len(legend_stack_labels), 2),
                fontsize=FONT_SIZES["legend"],
                frameon=True,
                borderpad=0.5,
            )
        if reward_handles:
            method_legend = self._ax2.legend(
                reward_handles,
                reward_labels,
                title="Cost (method)",
                loc="lower center",
                bbox_to_anchor=(0.78, 1.0),
                ncol=min(len(reward_labels), 3),
                fontsize=FONT_SIZES["legend"],
                title_fontsize=FONT_SIZES["legend"],
                frameon=True,
                borderpad=0.5,
            )
            if stack_legend is not None:
                self._ax.add_artist(stack_legend)

        self._fig.subplots_adjust(left=0.10, right=0.88, bottom=0.10, top=0.88)
        self._fig.savefig(self.output_path, dpi=150, bbox_inches="tight", pad_inches=0.2)
        return self.output_path

    def close(self) -> None:
        if self._fig is not None:
            plt.close(self._fig)
            self._fig = None
            self._ax = None
            self._ax2 = None


def build_tracker_from_experiment(experiment_dir: Path, methods: list[str], *, repo_root: Path) -> MethodComparisonTracker:
    tracker = MethodComparisonTracker(methods=methods)
    pta = _load_transfer_analysis(repo_root)
    for method in methods:
        method_dir = experiment_dir / "methods" / method
        if not method_dir.is_dir():
            continue
        for trial_dir in sorted(method_dir.glob("trial_*")):
            trial_index = int(trial_dir.name.split("_", 1)[1])
            bars = load_trial_mean_bars(trial_dir / "events.db", trial_index, pta)
            if bars is not None:
                tracker.add(method, bars)
    return tracker


def plot_methods_comparison_static(
    experiment_dir: Path,
    methods: list[str],
    *,
    repo_root: Path,
    max_trials: int | None = None,
) -> Path | None:
    tracker = build_tracker_from_experiment(experiment_dir, methods, repo_root=repo_root)
    if not tracker.points:
        return None
    plotter = LiveMethodComparisonPlot(
        experiment_dir,
        methods,
        repo_root=repo_root,
        max_trials=max_trials,
    )
    output = plotter.update(tracker)
    plotter.close()
    return output
