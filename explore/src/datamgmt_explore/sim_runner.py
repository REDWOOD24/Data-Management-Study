from __future__ import annotations

import csv
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from datamgmt_explore.policy_builder import PolicyConfigBuilder
from datamgmt_explore.settings import Settings


@dataclass
class TrialSetup:
    trial_dir: Path
    config_dir: Path
    config_path: Path
    policy_path: Path
    events_db_path: Path
    jobs_file_path: Path | None


@dataclass
class SimResult:
    success: bool
    returncode: int
    events_db_path: Path
    stderr: str
    stdout: str
    trial_dir: Path
    elapsed_sec: float = 0.0


class CgSimRunner:
    def __init__(
        self,
        settings: Settings,
        policy_builder: PolicyConfigBuilder,
    ) -> None:
        self.settings = settings
        self.policy_builder = policy_builder

    def prepare_trial(
        self,
        trial_dir: Path,
        action: dict[str, Any],
        *,
        max_jobs: int | None = None,
        limited_sites: list[str] | None = None,
    ) -> TrialSetup:
        trial_dir.mkdir(parents=True, exist_ok=True)
        config_dir = trial_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        base_config_dir = self.settings.base_config.parent
        for filename in (
            self.settings.base_config.name,
            self.settings.base_topology.name,
            self.settings.base_connections.name,
        ):
            source = base_config_dir / filename
            if source.is_file():
                shutil.copy2(source, config_dir / filename)

        policy_path = config_dir / "data_policy_config.json"
        self.policy_builder.write(action, policy_path)

        config_path = config_dir / "config.json"
        config_data = self._load_json(config_path)
        custom = config_data.setdefault("Custom_Parameters", {})
        custom["output_file"] = "../events.db"
        custom["data_policy"] = "data_policy_config.json"
        custom["Num_of_Jobs"] = str(self.settings.workload.num_jobs)

        jobs_file_path: Path | None = None
        if int(self.settings.workload.num_jobs) < 0:
            source_jobs = self.settings.resolve(self.settings.workload.jobs_file)
            if max_jobs is not None and max_jobs > 0:
                jobs_file_path = trial_dir / "jobs_truncated.csv"
                self._truncate_jobs_csv(source_jobs, jobs_file_path, max_jobs)
                custom["jobs_file"] = "../jobs_truncated.csv"
            else:
                jobs_file_path = trial_dir / "jobs.csv"
                shutil.copy2(source_jobs, jobs_file_path)
                custom["jobs_file"] = "../jobs.csv"

        if limited_sites:
            config_data["Limited_Sites"] = limited_sites

        self._resolve_dispatcher_plugin(config_data, base_config_dir)

        self._write_json(config_path, config_data)

        events_db_path = trial_dir / "events.db"
        if events_db_path.exists():
            events_db_path.unlink()

        return TrialSetup(
            trial_dir=trial_dir,
            config_dir=config_dir,
            config_path=config_path,
            policy_path=policy_path,
            events_db_path=events_db_path,
            jobs_file_path=jobs_file_path,
        )

    def run_trial(
        self,
        trial_dir: Path,
        action: dict[str, Any],
        *,
        max_jobs: int | None = None,
        limited_sites: list[str] | None = None,
        dry_run: bool = False,
    ) -> SimResult:
        setup = self.prepare_trial(
            trial_dir,
            action,
            max_jobs=max_jobs,
            limited_sites=limited_sites,
        )

        action_path = trial_dir / "action.json"
        self._write_json(action_path, action)

        if dry_run:
            return SimResult(
                success=True,
                returncode=0,
                events_db_path=setup.events_db_path,
                stderr="",
                stdout="dry-run",
                trial_dir=trial_dir,
                elapsed_sec=0.0,
            )

        if not self.settings.cg_sim_bin.is_file():
            stderr = f"CGSim executable not found: {self.settings.cg_sim_bin}"
            (trial_dir / "stderr.log").write_text(stderr, encoding="utf-8")
            return SimResult(
                success=False,
                returncode=-1,
                events_db_path=setup.events_db_path,
                stderr=stderr,
                stdout="",
                trial_dir=trial_dir,
                elapsed_sec=0.0,
            )

        command = [str(self.settings.cg_sim_bin), "-c", "config.json"]
        sim_start = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                cwd=setup.config_dir,
                capture_output=True,
                text=True,
                timeout=self.settings.sim_timeout_sec,
                check=False,
            )
            elapsed_sec = time.perf_counter() - sim_start
        except subprocess.TimeoutExpired as exc:
            elapsed_sec = time.perf_counter() - sim_start
            stderr = exc.stderr or f"Simulation timed out after {self.settings.sim_timeout_sec}s"
            (trial_dir / "stderr.log").write_text(stderr, encoding="utf-8")
            return SimResult(
                success=False,
                returncode=-2,
                events_db_path=setup.events_db_path,
                stderr=stderr,
                stdout=exc.stdout or "",
                trial_dir=trial_dir,
                elapsed_sec=elapsed_sec,
            )

        stderr = completed.stderr or ""
        stdout = completed.stdout or ""
        if stderr:
            (trial_dir / "stderr.log").write_text(stderr, encoding="utf-8")

        success = completed.returncode == 0 and setup.events_db_path.is_file()
        return SimResult(
            success=success,
            returncode=completed.returncode,
            events_db_path=setup.events_db_path,
            stderr=stderr,
            stdout=stdout,
            trial_dir=trial_dir,
            elapsed_sec=elapsed_sec,
        )

    @staticmethod
    def _resolve_dispatcher_plugin(config_data: dict[str, Any], base_config_dir: Path) -> None:
        plugin_ref = config_data.get("Dispatcher_Plugin")
        if not plugin_ref:
            return
        plugin_path = Path(str(plugin_ref))
        if not plugin_path.is_absolute():
            plugin_path = (base_config_dir / plugin_path).resolve()
        config_data["Dispatcher_Plugin"] = str(plugin_path)

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=4)
            handle.write("\n")

    @staticmethod
    def _truncate_jobs_csv(source: Path, destination: Path, max_jobs: int) -> None:
        with source.open(newline="", encoding="utf-8") as infile:
            reader = csv.DictReader(infile)
            if reader.fieldnames is None:
                raise ValueError(f"Jobs file has no header: {source}")
            rows = []
            for index, row in enumerate(reader):
                if index >= max_jobs:
                    break
                rows.append(row)
        with destination.open("w", newline="", encoding="utf-8") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
            writer.writeheader()
            writer.writerows(rows)
