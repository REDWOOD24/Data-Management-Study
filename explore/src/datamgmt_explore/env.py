from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path
from collections.abc import Callable
from typing import Any

import gymnasium as gym
import numpy as np

from datamgmt_explore.action_space import ActionDecoder, ActionSpace
from datamgmt_explore.metrics import (
    filter_records_with_input_files,
    load_job_records,
    write_metrics,
)
from datamgmt_explore.objectives.base import ObjectiveResult, load_objective
from datamgmt_explore.policy_builder import PolicyConfigBuilder
from datamgmt_explore.plotting.trial_plots import plot_trial
from datamgmt_explore.run_store import RunStore
from datamgmt_explore.settings import Settings
from datamgmt_explore.sim_runner import CgSimRunner
from datamgmt_explore.rl_observations import (
    ObservationMemory,
    build_context_features,
    build_context_vector,
    build_outcome_summary,
    build_site_utilization_report,
    build_network_usage_report,
    build_trial_observation_bundle,
    observation_spec_for_action_space,
    update_memory_from_outcome,
    write_observation_artifacts,
    zero_observation,
)
from datamgmt_explore.windowing import EvaluationWindow, WindowConfig, WindowContext, WindowMode


class DataMgmtEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(
        self,
        settings: Settings,
        *,
        action_space_path: Path | str | None = None,
        objectives_config_path: Path | str | None = None,
        window_config: WindowConfig | None = None,
        experiment_dir: Path | None = None,
        objective_name: str = "tail_bulk_staging_cost",
        aggregation: str = "mean",
        reward_transform: str = "neg_log1p",
        max_jobs: int | None = None,
        max_trials: int | None = None,
        save_rl_context: bool = True,
        dry_run: bool = False,
        plot_enabled: bool = True,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        explore_root = settings.explore_root
        self.action_space_spec = ActionSpace.from_yaml(
            action_space_path or explore_root / "config" / "action_space.yaml"
        )
        self.objectives_config_path = objectives_config_path or (
            explore_root / "config" / "objectives.yaml"
        )
        self.decoder = ActionDecoder(self.action_space_spec)
        self.policy_builder = PolicyConfigBuilder(
            self.action_space_spec,
            base_policy_path=settings.base_policy,
            drop_in_transfers_file=settings.drop_in_transfers_file,
        )
        self.runner = CgSimRunner(settings, self.policy_builder)
        self.objective_name = objective_name
        self.objective = load_objective(
            objective_name,
            str(self.objectives_config_path),
        )
        self.aggregation = aggregation
        self.reward_transform = reward_transform
        self.window_config = window_config or WindowConfig(mode=WindowMode.FULL)
        self.evaluator = EvaluationWindow(self.window_config)
        self.max_jobs = max_jobs
        self.max_trials = max_trials
        self.save_rl_context = save_rl_context
        self.dry_run = dry_run
        self.plot_enabled = plot_enabled and not dry_run
        self.progress_callback = progress_callback

        self.experiment_dir = experiment_dir
        self.run_store = RunStore(experiment_dir) if experiment_dir else None
        self.trial_index = 0
        self.last_result: ObjectiveResult | None = None
        self.observation_spec = observation_spec_for_action_space(self.action_space_spec)
        self.observation_memory = ObservationMemory()
        self.current_observation = zero_observation(self.observation_spec)
        self._pending_action_vector = np.zeros(
            self.observation_spec.action_dim,
            dtype=np.float32,
        )

        dim = len(self.action_space_spec.parameters)
        self.action_space = gym.spaces.Box(
            low=np.zeros(dim, dtype=np.float32),
            high=np.ones(dim, dtype=np.float32),
            dtype=np.float32,
        )
        self.observation_space = gym.spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=(self.observation_spec.obs_dim,),
            dtype=np.float32,
        )

    def reset(self, *, seed: int | None = None, options: dict[str, Any] | None = None):
        super().reset(seed=seed)
        if seed is not None:
            self.np_random = np.random.default_rng(seed)
        self.trial_index = 0
        self.last_result = None
        self.observation_memory = ObservationMemory()
        self.current_observation = zero_observation(self.observation_spec)
        self._pending_action_vector = np.zeros(
            self.observation_spec.action_dim,
            dtype=np.float32,
        )
        return self.current_observation.copy(), {}

    def step(self, action: dict[str, Any] | np.ndarray):
        if isinstance(action, np.ndarray):
            action = self.action_space_spec.from_vector(action)
        action = self.decoder.decode(action)
        self._pending_action_vector = self.action_space_spec.normalize_vector(action)

        trial_index = self.trial_index
        trial_dir = (
            self.run_store.trial_dir(trial_index)
            if self.run_store
            else Path.cwd() / f"trial_{trial_index:04d}"
        )
        if self.progress_callback is not None:
            self.progress_callback(trial_index, "simulation")
        sim_result = self.runner.run_trial(
            trial_dir,
            action,
            max_jobs=self.max_jobs,
            dry_run=self.dry_run,
        )

        objective_result: ObjectiveResult | None = None
        reward = float(self.settings.failure_penalty)
        partial = False
        exploration_elapsed_sec = 0.0
        info: dict[str, Any] = {
            "trial_index": trial_index,
            "trial_dir": str(trial_dir),
            "sim_success": sim_result.success,
            "returncode": sim_result.returncode,
            "sim_elapsed_sec": sim_result.elapsed_sec,
        }

        records: list = []
        objective_records: list = []
        window_records: list = []
        objective_dict: dict[str, Any] | None = None

        has_events_db = not self.dry_run and sim_result.events_db_path.is_file()

        if self.dry_run:
            reward = 0.0
            info["dry_run"] = True
        elif has_events_db:
            if self.progress_callback is not None:
                self.progress_callback(trial_index, "exploration")
            explore_start = time.perf_counter()
            records = load_job_records(sim_result.events_db_path)
            write_metrics(trial_dir / "metrics.json", records)

            jobs_csv = trial_dir / "jobs.csv"
            if not jobs_csv.is_file():
                jobs_csv = trial_dir / "jobs_truncated.csv"
            if not jobs_csv.is_file():
                jobs_csv = self.settings.resolve(self.settings.workload.jobs_file)
            objective_records = filter_records_with_input_files(
                records,
                jobs_csv=jobs_csv if jobs_csv.is_file() else None,
            )
            info["all_job_count"] = len(records)
            info["objective_job_count"] = len(objective_records)
            if records:
                info["input_requiring_job_fraction"] = len(objective_records) / len(records)

            if self.plot_enabled:
                plot_paths = plot_trial(
                    sim_result.events_db_path,
                    trial_dir / "plots",
                    repo_root=self.settings.repo_root,
                )
                info["plot_paths"] = [str(path) for path in plot_paths]

            window_records = self.evaluator.select(objective_records)
            info["window_job_count"] = len(window_records)

            if window_records:
                objective_result = self.objective.compute(
                    window_records,
                    WindowContext(config=self.window_config),
                    aggregation=self.aggregation,
                    reward_transform=self.reward_transform,
                )
                if objective_result.metadata is not None:
                    objective_result.metadata["all_job_count"] = len(records)
                    objective_result.metadata["objective_job_count"] = len(objective_records)
                    objective_result.metadata["input_jobs_only"] = True
                partial = not sim_result.success
                if partial:
                    objective_result.metadata["partial"] = True
                    objective_result.metadata["sim_success"] = False
                    objective_result.metadata["returncode"] = sim_result.returncode
                    reward = float(self.settings.failure_penalty)
                    info["partial"] = True
                    objective_dict = asdict(objective_result)
                    info["partial_objective"] = objective_dict
                else:
                    reward = objective_result.reward
                    objective_result.metadata["sim_success"] = True
                    self.last_result = objective_result
                    objective_dict = asdict(objective_result)
                    info["objective"] = objective_dict

                if self.run_store and objective_result is not None:
                    self.run_store.write_reward(trial_dir, objective_result)
            elif not sim_result.success:
                info["stderr"] = sim_result.stderr

            if self.run_store:
                self.run_store.record_trial(
                    trial_index,
                    action,
                    objective_result if sim_result.success else None,
                    sim_success=sim_result.success,
                    extra={
                        "returncode": sim_result.returncode,
                        "partial": partial,
                        "partial_objective": asdict(objective_result) if partial and objective_result else None,
                    },
                )
            exploration_elapsed_sec = time.perf_counter() - explore_start
        else:
            info["stderr"] = sim_result.stderr

        info["exploration_elapsed_sec"] = exploration_elapsed_sec

        (trial_dir / "returncode.txt").write_text(
            str(sim_result.returncode),
            encoding="utf-8",
        )

        if self.run_store and not has_events_db and not self.dry_run:
            self.run_store.record_trial(
                trial_index,
                action,
                None,
                sim_success=sim_result.success,
                extra={
                    "returncode": sim_result.returncode,
                    "partial": False,
                    "partial_objective": None,
                },
            )

        observation_outcome: dict[str, Any] | None = None
        site_report: dict[str, Any] = {}
        network_report: dict[str, Any] = {}
        context_payload: dict[str, Any] | None = None

        if not self.dry_run:
            db_path = sim_result.events_db_path
            if has_events_db:
                outcome, site_report, network_report, _transfer = build_trial_observation_bundle(
                    db_path,
                    repo_root=self.settings.repo_root,
                    trial_index=trial_index,
                    sim_success=sim_result.success and not partial,
                    returncode=sim_result.returncode,
                    records=objective_records,
                    objective_result=objective_dict,
                )
            else:
                outcome = build_outcome_summary(
                    trial_index=trial_index,
                    sim_success=False,
                    returncode=sim_result.returncode,
                    records=[],
                    objective_result=None,
                    transfer=None,
                    site_report=build_site_utilization_report(db_path),
                    network_report=build_network_usage_report(db_path),
                )

            if not sim_result.success or partial:
                outcome["sim_success"] = False
                outcome["cost"] = float(reward)

            observation_outcome = outcome
            next_trial_index = trial_index + 1
            max_trials = self.max_trials or max(next_trial_index, 1)

            context_vector = build_context_vector(
                outcome,
                memory=self.observation_memory,
                spec=self.observation_spec,
                trial_index=next_trial_index,
                max_trials=max_trials,
            )
            context_features = build_context_features(
                outcome,
                memory=self.observation_memory,
                trial_index=next_trial_index,
                max_trials=max_trials,
            )

            if self.save_rl_context:
                context_payload = {
                    "for_trial_index": next_trial_index,
                    "built_from_trial_index": trial_index,
                    "vector": context_vector.tolist(),
                    "features": context_features,
                    "last_action_vector": self._pending_action_vector.tolist(),
                }

            write_observation_artifacts(
                trial_dir,
                outcome=outcome,
                context=context_payload,
                site_report=site_report,
                network_report=network_report,
            )

            update_memory_from_outcome(
                self.observation_memory,
                outcome,
                self._pending_action_vector,
            )
            self.current_observation = context_vector

            info["observation_outcome"] = outcome
            if context_payload is not None:
                info["observation_context"] = context_payload

        self.trial_index += 1
        safe_reward = reward if np.isfinite(reward) else 1e6
        return self.current_observation.copy(), safe_reward, True, False, info


def build_window_config(
    mode: str,
    *,
    size: float | None = None,
    stride: float | None = None,
    anchor: str = "sim_start",
    start_time: float | None = None,
) -> WindowConfig:
    from datamgmt_explore.windowing import WindowAnchor

    return WindowConfig(
        mode=WindowMode(mode),
        size=size,
        stride=stride,
        anchor=WindowAnchor(anchor),
        start_time=start_time,
    )
