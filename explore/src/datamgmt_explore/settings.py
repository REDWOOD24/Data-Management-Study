from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class WorkloadSettings:
    jobs_file: str = "../input/mimic_job.csv"
    num_jobs: int = -1


@dataclass
class Settings:
    cg_sim_bin: Path
    repo_root: Path
    base_config: Path
    base_topology: Path
    base_connections: Path
    base_policy: Path
    workload: WorkloadSettings
    sim_timeout_sec: int = 3600
    parallel_trials: int = 1
    failure_penalty: float = 1e6
    runs_dir: Path = field(default_factory=lambda: Path("runs"))
    explore_root: Path = field(default_factory=Path.cwd)

    def resolve(self, path: Path | str) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return (self.explore_root / candidate).resolve()


def load_settings(path: Path | str) -> Settings:
    config_path = Path(path).resolve()
    explore_root = config_path.parent.parent
    with config_path.open(encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle) or {}

    workload_raw = raw.get("workload", {})
    workload = WorkloadSettings(
        jobs_file=str(workload_raw.get("jobs_file", "../input/mimic_job.csv")),
        num_jobs=int(workload_raw.get("num_jobs", -1)),
    )

    settings = Settings(
        cg_sim_bin=Path(str(raw["cg_sim_bin"])),
        repo_root=Path(str(raw.get("repo_root", ".."))),
        base_config=Path(str(raw.get("base_config", "../config/config.json"))),
        base_topology=Path(str(raw.get("base_topology", "../config/site_topology.json"))),
        base_connections=Path(
            str(raw.get("base_connections", "../config/site_connections.json"))
        ),
        base_policy=Path(str(raw.get("base_policy", "../config/data_policy_config.json"))),
        workload=workload,
        sim_timeout_sec=int(raw.get("sim_timeout_sec", 3600)),
        parallel_trials=int(raw.get("parallel_trials", 1)),
        failure_penalty=float(raw.get("failure_penalty", 1e6)),
        runs_dir=Path(str(raw.get("runs_dir", "runs"))),
        explore_root=explore_root,
    )

    settings.cg_sim_bin = settings.resolve(settings.cg_sim_bin)
    settings.repo_root = settings.resolve(settings.repo_root)
    settings.base_config = settings.resolve(settings.base_config)
    settings.base_topology = settings.resolve(settings.base_topology)
    settings.base_connections = settings.resolve(settings.base_connections)
    settings.base_policy = settings.resolve(settings.base_policy)
    settings.runs_dir = settings.resolve(settings.runs_dir)
    return settings


def load_yaml(path: Path | str) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}
