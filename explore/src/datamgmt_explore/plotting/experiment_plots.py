from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

_TRANSFER_EXISTS_RE = re.compile(
    r"File: (?P<file>\S+) already exists at Site: (?P<site>\S+) so no transfer"
)
_TRANSFER_MISSING_RE = re.compile(
    r"File: (?P<file>\S+) does not exist at Site: (?P<site>\S+) so no transfer"
)


@dataclass(frozen=True)
class TrialPoint:
    trial_index: int
    reward: float | None
    objective_value: float | None
    objective_name: str | None
    sim_success: bool
    job_count: int
    partial: bool = False


@dataclass(frozen=True)
class TrialFailure:
    trial_index: int
    returncode: int | None
    error_type: str
    file_id: str | None
    site: str | None
    sim_time: float | None
    message: str
    proactive_enabled: bool | None
    proactive_template: int | None
    transfer_mode: str | None


def load_trial_series(experiment_dir: Path) -> list[TrialPoint]:
    summary_path = experiment_dir / "summary.json"
    if summary_path.is_file():
        with summary_path.open(encoding="utf-8") as handle:
            summary = json.load(handle)
        points = [_trial_dict_to_point(entry) for entry in summary.get("trials", [])]
        if points:
            return sorted(points, key=lambda item: item.trial_index)

    points: list[TrialPoint] = []
    for trial_dir in sorted(experiment_dir.glob("trial_*")):
        reward_path = trial_dir / "reward.json"
        if not reward_path.is_file():
            continue
        with reward_path.open(encoding="utf-8") as handle:
            objective = json.load(handle)
        trial_index = int(trial_dir.name.split("_", 1)[1])
        points.append(
            TrialPoint(
                trial_index=trial_index,
                reward=_finite_or_none(objective.get("reward")),
                objective_value=_finite_or_none(objective.get("value")),
                objective_name=objective.get("name"),
                sim_success=bool(objective.get("metadata", {}).get("sim_success", True)),
                job_count=int(objective.get("job_count", 0)),
                partial=bool(objective.get("metadata", {}).get("partial", False)),
            )
        )
    return sorted(points, key=lambda item: item.trial_index)


def _trial_dict_to_point(entry: dict) -> TrialPoint:
    extra = entry.get("extra") or {}
    partial = bool(extra.get("partial"))
    objective = entry.get("objective") or {}
    if partial and extra.get("partial_objective"):
        objective = extra["partial_objective"]
    metadata = objective.get("metadata") or {}
    sim_success = bool(entry.get("sim_success"))
    return TrialPoint(
        trial_index=int(entry["trial_index"]),
        reward=_finite_or_none(objective.get("reward") if objective else entry.get("reward")),
        objective_value=_finite_or_none(objective.get("value")),
        objective_name=objective.get("name"),
        sim_success=sim_success,
        job_count=int(objective.get("job_count", 0)),
        partial=partial or bool(metadata.get("partial")),
    )


def _finite_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def parse_stderr_failure(stderr: str) -> tuple[str, str | None, str | None, float | None, str]:
    first_line = stderr.strip().splitlines()[0] if stderr.strip() else ""
    sim_time = None
    time_match = re.search(r"(\d+\.\d+)\]", first_line)
    if time_match:
        sim_time = float(time_match.group(1))

    exists = _TRANSFER_EXISTS_RE.search(first_line)
    if exists:
        return (
            "duplicate_replica_transfer",
            exists.group("file"),
            exists.group("site"),
            sim_time,
            first_line,
        )

    missing = _TRANSFER_MISSING_RE.search(first_line)
    if missing:
        return (
            "missing_source_replica",
            missing.group("file"),
            missing.group("site"),
            sim_time,
            first_line,
        )

    if "Access violation" in first_line or "Bus error" in stderr:
        return ("simgrid_crash", None, None, sim_time, first_line)

    if "CGSim executable not found" in stderr:
        return ("cg_sim_missing", None, None, None, first_line)

    if "timed out" in stderr.lower():
        return ("timeout", None, None, None, first_line)

    return ("unknown", None, None, sim_time, first_line or stderr[:200])


def diagnose_trial(trial_dir: Path) -> TrialFailure | None:
    stderr_path = trial_dir / "stderr.log"
    returncode_path = trial_dir / "returncode.txt"
    returncode = None
    if returncode_path.is_file():
        returncode = int(returncode_path.read_text(encoding="utf-8").strip())

    if not stderr_path.is_file():
        if returncode not in (None, 0):
            return TrialFailure(
                trial_index=int(trial_dir.name.split("_", 1)[1]),
                returncode=returncode,
                error_type="nonzero_exit",
                file_id=None,
                site=None,
                sim_time=None,
                message=f"Process exited with code {returncode}",
                proactive_enabled=None,
                proactive_template=None,
                transfer_mode=None,
            )
        return None

    stderr = stderr_path.read_text(encoding="utf-8")
    if not stderr.strip():
        return None

    error_type, file_id, site, sim_time, message = parse_stderr_failure(stderr)
    if error_type == "unknown" and returncode == 0:
        return None
    if error_type == "unknown" and not any(
        token in stderr for token in ("CRITICAL", "runtime_error", "Access violation", "Bus error")
    ):
        return None

    trial_index = int(trial_dir.name.split("_", 1)[1])

    action_path = trial_dir / "action.json"
    proactive_enabled = None
    proactive_template = None
    transfer_mode = None
    if action_path.is_file():
        action = json.loads(action_path.read_text(encoding="utf-8"))
        proactive_enabled = bool(action.get("proactive.enabled"))
        proactive_template = int(action.get("proactive.transfer_template", -1))
        transfer_mode = str(action.get("proactive.data_transfer_mode", ""))

    return TrialFailure(
        trial_index=trial_index,
        returncode=returncode,
        error_type=error_type,
        file_id=file_id,
        site=site,
        sim_time=sim_time,
        message=message,
        proactive_enabled=proactive_enabled,
        proactive_template=proactive_template,
        transfer_mode=transfer_mode,
    )


def diagnose_experiment(experiment_dir: Path) -> list[TrialFailure]:
    failures: list[TrialFailure] = []
    methods_root = experiment_dir / "methods"
    if methods_root.is_dir():
        for method_dir in sorted(methods_root.iterdir()):
            if method_dir.is_dir():
                failures.extend(diagnose_experiment(method_dir))
        return failures

    for trial_dir in sorted(experiment_dir.glob("trial_*")):
        failure = diagnose_trial(trial_dir)
        if failure is not None:
            failures.append(failure)
    return failures


def write_failure_report(experiment_dir: Path) -> Path:
    failures = diagnose_experiment(experiment_dir)
    report = {
        "experiment": str(experiment_dir),
        "failure_count": len(failures),
        "failures": [
            {
                **failure.__dict__,
                "policy_implication": _policy_implication(failure),
            }
            for failure in failures
        ],
    }
    output_path = experiment_dir / "failure_report.json"
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    return output_path


def _policy_implication(failure: TrialFailure) -> str:
    if failure.error_type == "duplicate_replica_transfer":
        if failure.proactive_enabled:
            if failure.proactive_template == 2:
                return (
                    "Hotset replication likely attempted COPY/MOVE to a site that "
                    "already holds the file. Add a destination replica check in "
                    "run_hotset_replication() (similar to storage_rebalance "
                    "skip_if_already_replica_on_destination)."
                )
            return (
                "Proactive rebalance attempted a transfer to a destination that "
                "already has the file. Enforce skip_if_already_replica_on_destination "
                "for network_aware_rebalance and guard against in-flight races."
            )
        return (
            "Reactive/on-demand staging attempted to transfer a file to a compute "
            "site that already has a replica. CGSim aborts instead of no-op; "
            "plugin should detect local replica before initiating transfer."
        )

    if failure.error_type == "missing_source_replica":
        return (
            "Transfer sourced a file from a site that no longer has it—common when "
            "proactive MOVE removes the only/source replica while jobs still expect "
            "it elsewhere. Prefer COPY for replication, or track replica catalog "
            "before issuing MOVE."
        )

    return "See stderr.log for details."


def plot_objective_progress(
    experiment_dir: Path,
    *,
    points: list[TrialPoint] | None = None,
) -> Path | None:
    points = points if points is not None else load_trial_series(experiment_dir)
    if not points:
        return None

    output_dir = experiment_dir / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "objective_progress.png"

    indices = [point.trial_index for point in points]
    rewards = [point.reward for point in points]
    values = [point.objective_value for point in points]
    successes = [point.sim_success and point.job_count > 0 for point in points]
    partials = [point.partial and point.job_count > 0 for point in points]

    fig, axes = plt.subplots(2, 1, figsize=(10, 8), sharex=True)

    _plot_series(
        axes[0],
        indices,
        values,
        successes,
        partials,
        ylabel="Objective value (lower is better)",
        title="Objective value over exploration trials",
        color="#2563eb",
    )
    _plot_series(
        axes[1],
        indices,
        rewards,
        successes,
        partials,
        ylabel="Reward (lower is better)",
        title="Reward over exploration trials",
        color="#16a34a",
    )

    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def plot_cumulative_best(
    experiment_dir: Path,
    *,
    points: list[TrialPoint] | None = None,
) -> Path | None:
    points = points if points is not None else load_trial_series(experiment_dir)
    if not points:
        return None

    output_dir = experiment_dir / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "cumulative_best_reward.png"

    indices = [point.trial_index for point in points]
    running_best: list[float | None] = []
    best_so_far = float("inf")
    for point in points:
        if point.reward is not None and point.sim_success:
            best_so_far = min(best_so_far, point.reward)
            running_best.append(best_so_far)
        else:
            running_best.append(None)

    fig, ax = plt.subplots(figsize=(10, 5))
    valid_x = [idx for idx, value in zip(indices, running_best) if value is not None]
    valid_y = [value for value in running_best if value is not None]
    if valid_x:
        ax.plot(valid_x, valid_y, marker="o", color="#9333ea", linewidth=2)
    ax.set_xlabel("Trial index")
    ax.set_ylabel("Cumulative best reward (lower is better)")
    ax.set_title("Best reward seen so far (successful trials only; lower is better)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def plot_experiment_progress(experiment_dir: Path) -> list[Path]:
    points = load_trial_series(experiment_dir)
    written: list[Path] = []
    objective_path = plot_objective_progress(experiment_dir, points=points)
    if objective_path:
        written.append(objective_path)
    cumulative_path = plot_cumulative_best(experiment_dir, points=points)
    if cumulative_path:
        written.append(cumulative_path)
    return written


def _plot_series(
    ax: plt.Axes,
    indices: list[int],
    values: list[float | None],
    successes: list[bool],
    partials: list[bool],
    *,
    ylabel: str,
    title: str,
    color: str,
) -> None:
    success_x = [
        idx
        for idx, ok, partial, value in zip(indices, successes, partials, values)
        if ok and not partial and value is not None
    ]
    success_y = [
        value
        for ok, partial, value in zip(successes, partials, values)
        if ok and not partial and value is not None
    ]
    partial_x = [
        idx
        for idx, partial, value in zip(indices, partials, values)
        if partial and value is not None
    ]
    partial_y = [value for partial, value in zip(partials, values) if partial and value is not None]
    fail_x = [
        idx
        for idx, ok, partial, value in zip(indices, successes, partials, values)
        if not ok and not partial and value is None
    ]

    if success_x:
        ax.plot(success_x, success_y, marker="o", color=color, linewidth=2, label="Successful trial")
    if partial_x:
        ax.scatter(
            partial_x,
            partial_y,
            marker="D",
            color="#f59e0b",
            label="Partial (sim crashed, metrics from events.db)",
        )
    if fail_x:
        ax.scatter(fail_x, [0.0] * len(fail_x), marker="x", color="#9ca3af", label="Failed (no metrics)")

    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    if success_x or partial_x or fail_x:
        ax.legend(loc="best")
