from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from datamgmt_explore.objectives.base import ObjectiveResult


class RunStore:
    def __init__(self, experiment_dir: Path) -> None:
        self.experiment_dir = experiment_dir
        self.experiment_dir.mkdir(parents=True, exist_ok=True)
        self.trials: list[dict[str, Any]] = []

    def trial_dir(self, trial_index: int) -> Path:
        path = self.experiment_dir / f"trial_{trial_index:04d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def record_trial(
        self,
        trial_index: int,
        action: dict[str, Any],
        objective_result: ObjectiveResult | None,
        *,
        sim_success: bool,
        extra: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "trial_index": trial_index,
            "sim_success": sim_success,
            "action": action,
            "objective": asdict(objective_result) if objective_result else None,
            "reward": objective_result.reward if objective_result else None,
            "extra": extra or {},
        }
        self.trials.append(entry)

    def write_reward(self, trial_dir: Path, objective_result: ObjectiveResult) -> None:
        with (trial_dir / "reward.json").open("w", encoding="utf-8") as handle:
            json.dump(asdict(objective_result), handle, indent=2)
            handle.write("\n")

    def write_observation_artifacts(
        self,
        trial_dir: Path,
        *,
        outcome: dict[str, Any],
        context: dict[str, Any] | None,
        site_report: dict[str, Any],
        network_report: dict[str, Any],
    ) -> Path:
        from datamgmt_explore.rl_observations import write_observation_artifacts

        return write_observation_artifacts(
            trial_dir,
            outcome=outcome,
            context=context,
            site_report=site_report,
            network_report=network_report,
        )

    def write_run_config(self, config: dict[str, Any]) -> Path:
        path = self.experiment_dir / "run_config.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
            handle.write("\n")
        return path

    def write_summary(self, *, run_config: dict[str, Any] | None = None) -> Path:
        successful = [trial for trial in self.trials if trial.get("sim_success")]
        best = None
        if successful:
            best = min(
                successful,
                key=lambda trial: trial.get("reward")
                if trial.get("reward") is not None
                else float("inf"),
            )

        if run_config is None:
            run_config_path = self.experiment_dir / "run_config.json"
            if run_config_path.is_file():
                with run_config_path.open(encoding="utf-8") as handle:
                    run_config = json.load(handle)

        summary = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trial_count": len(self.trials),
            "successful_trials": len(successful),
            "best_trial": best,
            "trials": sorted(self.trials, key=lambda trial: trial["trial_index"]),
        }
        if run_config:
            summary["run_config"] = run_config
        git_commit = self._git_commit()
        if git_commit:
            summary["git_commit"] = git_commit

        path = self.experiment_dir / "summary.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
            handle.write("\n")
        return path

    @classmethod
    def merge_experiment_trials(cls, experiment_dir: Path) -> Path:
        store = cls(experiment_dir)
        for trial_dir in sorted(experiment_dir.glob("trial_*")):
            action_path = trial_dir / "action.json"
            if not action_path.is_file():
                continue
            with action_path.open(encoding="utf-8") as handle:
                action = json.load(handle)
            trial_index = int(trial_dir.name.split("_", 1)[1])
            reward_path = trial_dir / "reward.json"
            objective = None
            reward = None
            sim_success = False
            partial = False
            returncode = None
            returncode_path = trial_dir / "returncode.txt"
            if returncode_path.is_file():
                returncode = int(returncode_path.read_text(encoding="utf-8").strip())
            if reward_path.is_file():
                with reward_path.open(encoding="utf-8") as handle:
                    objective = json.load(handle)
                reward = objective.get("reward")
                metadata = objective.get("metadata") or {}
                partial = bool(metadata.get("partial"))
                sim_success = bool(metadata.get("sim_success", not partial))
            store.trials.append(
                {
                    "trial_index": trial_index,
                    "sim_success": sim_success,
                    "action": action,
                    "objective": objective,
                    "reward": reward if sim_success else None,
                    "extra": {
                        "returncode": returncode,
                        "partial": partial,
                        "partial_objective": objective if partial else None,
                    },
                }
            )
        return store.write_summary()

    @staticmethod
    def _git_commit() -> str | None:
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip() or None
