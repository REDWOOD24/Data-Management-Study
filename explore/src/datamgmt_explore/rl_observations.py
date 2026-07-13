from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from datamgmt_explore.action_space import ActionSpace
from datamgmt_explore.metrics import (
    JobRecord,
    compute_tail_bulk_staging_cost,
    load_job_records,
)
from datamgmt_explore.transfer_summary import TransferSummary, transfer_summary_from_db

OBSERVATION_SPEC_VERSION = 1
STAGING_GT_3600S = 3600.0
TOP_K = 3
REACTIVE_TRANSFER_EVENT = "FileTransfer"
PROACTIVE_TRANSFER_EVENT = "BackGroundFileTransfer"
TRANSFER_EVENTS = (REACTIVE_TRANSFER_EVENT, PROACTIVE_TRANSFER_EVENT)

BASE_FEATURE_NAMES: tuple[str, ...] = (
    "cost",
    "log1p_avg_bottom_staging",
    "log1p_avg_top_staging",
    "log1p_max_staging",
    "log1p_p99_staging",
    "frac_staging_gt_0",
    "frac_staging_gt_3600s",
    "log1p_mean_site_staging",
    "log1p_max_site_staging",
    "log1p_top_site_staging_1",
    "log1p_top_site_staging_2",
    "log1p_top_site_staging_3",
    "log1p_total_ingress_gib",
    "log1p_total_egress_gib",
    "proactive_volume_fraction",
    "grid_storage_util_mean",
    "grid_storage_util_max",
    "site_storage_util_mean",
    "site_storage_util_max",
    "site_storage_util_p90",
    "top_site_storage_util_1",
    "top_site_storage_util_2",
    "top_site_storage_util_3",
    "link_load_mean",
    "link_load_max",
    "link_load_mean_reactive",
    "link_load_mean_proactive",
    "link_load_size_weighted_mean",
    "top_link_load_1",
    "top_link_load_2",
    "top_link_load_3",
    "trial_index_norm",
    "best_cost_so_far",
    "delta_cost",
    "last_sim_success",
)


def _stat(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "max": 0.0, "p90": 0.0, "final": 0.0, "n": 0.0}
    arr = np.asarray(values, dtype=np.float64)
    return {
        "mean": float(np.mean(arr)),
        "max": float(np.max(arr)),
        "p90": float(np.percentile(arr, 90)),
        "final": float(arr[-1]),
        "n": float(len(arr)),
    }


def _top_k(values: list[float], k: int = TOP_K) -> list[float]:
    if not values:
        return [0.0] * k
    sorted_vals = sorted((float(v) for v in values), reverse=True)
    padded = sorted_vals + [0.0] * k
    return padded[:k]


@dataclass(frozen=True)
class ObservationSpec:
    version: int
    base_feature_names: tuple[str, ...]
    action_dim: int

    @property
    def obs_dim(self) -> int:
        return len(self.base_feature_names) + self.action_dim

    @property
    def feature_names(self) -> list[str]:
        action_names = [f"last_action_{index}" for index in range(self.action_dim)]
        return list(self.base_feature_names) + action_names

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "base_feature_names": list(self.base_feature_names),
            "action_dim": self.action_dim,
            "obs_dim": self.obs_dim,
            "feature_names": self.feature_names,
        }


@dataclass
class ObservationMemory:
    cost_history: list[float] = field(default_factory=list)
    best_cost: float = float("inf")
    last_sim_success: float = 1.0
    last_action_vector: np.ndarray | None = None


def build_site_utilization_report(db_path: Path) -> dict[str, Any]:
    if not db_path.is_file():
        return {"grid": _stat([]), "sites": {}, "top_sites_by_storage_util": []}

    grid_samples: list[float] = []
    site_storage: dict[str, list[float]] = defaultdict(list)
    site_cpu: dict[str, list[float]] = defaultdict(list)

    query = """
        SELECT EVENT, STATE, METADATA, TIME
        FROM EVENTS
        WHERE METADATA IS NOT NULL
        ORDER BY TIME
    """

    with sqlite3.connect(db_path) as conn:
        for _event, _state, metadata_raw, _time in conn.execute(query):
            try:
                metadata = json.loads(metadata_raw or "{}")
            except json.JSONDecodeError:
                continue

            grid_val = metadata.get("grid_storage_util")
            if grid_val is not None:
                grid_samples.append(float(grid_val))

            site = metadata.get("site")
            if site is not None:
                if metadata.get("site_storage_util") is not None:
                    site_storage[str(site)].append(float(metadata["site_storage_util"]))
                if metadata.get("site_cpu_util") is not None:
                    site_cpu[str(site)].append(float(metadata["site_cpu_util"]))

            for key in ("src_site_storage_util", "dst_site_storage_util"):
                src_site = metadata.get("source_site") if key == "src_site_storage_util" else metadata.get(
                    "destination_site"
                )
                if src_site is None:
                    continue
                val = metadata.get(key)
                if val is not None:
                    site_storage[str(src_site)].append(float(val))

    sites: dict[str, Any] = {}
    all_site_storage_max: list[tuple[str, float, float]] = []
    all_site_storage_samples: list[float] = []

    for site_name in sorted(set(site_storage) | set(site_cpu)):
        storage_stats = _stat(site_storage.get(site_name, []))
        cpu_stats = _stat(site_cpu.get(site_name, []))
        sites[site_name] = {
            "storage_util": storage_stats,
            "cpu_util": cpu_stats,
        }
        if storage_stats["n"] > 0:
            all_site_storage_max.append((site_name, storage_stats["max"], storage_stats["mean"]))
            all_site_storage_samples.extend(site_storage.get(site_name, []))

    top_sites = sorted(all_site_storage_max, key=lambda item: item[1], reverse=True)[:10]
    site_summary = _stat(all_site_storage_samples)

    return {
        "grid": _stat(grid_samples),
        "sites": sites,
        "site_storage_summary": site_summary,
        "top_sites_by_storage_util": [
            {"site": site, "max": max_val, "mean": mean_val} for site, max_val, mean_val in top_sites
        ],
    }


def build_network_usage_report(db_path: Path) -> dict[str, Any]:
    if not db_path.is_file():
        empty = _stat([])
        return {
            "summary": {
                "link_load": empty,
                "link_load_reactive": empty,
                "link_load_proactive": empty,
                "size_weighted_link_load": 0.0,
            },
            "links": [],
            "top_links_by_volume": [],
        }

    placeholders = ", ".join("?" for _ in TRANSFER_EVENTS)
    query = f"""
        SELECT EVENT, METADATA
        FROM EVENTS
        WHERE STATE = 'Finished'
          AND EVENT IN ({placeholders})
    """

    link_buckets: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
        lambda: {"sizes": [], "link_loads": [], "bandwidths": []}
    )
    reactive_loads: list[float] = []
    proactive_loads: list[float] = []
    all_loads: list[float] = []
    weighted_sum = 0.0
    weighted_weight = 0.0
    per_transfer_top_loads: list[float] = []

    gib = 1024**3

    with sqlite3.connect(db_path) as conn:
        for event_type, metadata_raw in conn.execute(query, TRANSFER_EVENTS):
            try:
                metadata = json.loads(metadata_raw or "{}")
            except json.JSONDecodeError:
                continue

            source = metadata.get("source_site")
            destination = metadata.get("destination_site")
            size = metadata.get("size")
            link_load = metadata.get("link_load")
            bandwidth = metadata.get("bandwidth")
            if not source or not destination or size is None or link_load is None:
                continue

            size_f = float(size)
            load_f = float(link_load)
            key = (str(source), str(destination))
            bucket = link_buckets[key]
            bucket["sizes"].append(size_f)
            bucket["link_loads"].append(load_f)
            if bandwidth is not None:
                bucket["bandwidths"].append(float(bandwidth))

            all_loads.append(load_f)
            per_transfer_top_loads.append(load_f)
            weighted_sum += load_f * size_f
            weighted_weight += size_f

            if event_type == REACTIVE_TRANSFER_EVENT:
                reactive_loads.append(load_f)
            else:
                proactive_loads.append(load_f)

    links: list[dict[str, Any]] = []
    top_by_volume: list[tuple[tuple[str, str], float]] = []

    for (source, destination), bucket in link_buckets.items():
        total_bytes = sum(bucket["sizes"])
        total_gib = total_bytes / gib
        load_stats = _stat(bucket["link_loads"])
        bw_stats = _stat(bucket["bandwidths"])
        links.append(
            {
                "source": source,
                "destination": destination,
                "transfer_count": int(len(bucket["sizes"])),
                "total_gib": total_gib,
                "link_load_mean": load_stats["mean"],
                "link_load_max": load_stats["max"],
                "bandwidth_mean": bw_stats["mean"],
            }
        )
        top_by_volume.append(((source, destination), total_gib))

    top_by_volume.sort(key=lambda item: item[1], reverse=True)

    return {
        "summary": {
            "link_load": _stat(all_loads),
            "link_load_reactive": _stat(reactive_loads),
            "link_load_proactive": _stat(proactive_loads),
            "size_weighted_link_load": float(weighted_sum / weighted_weight) if weighted_weight > 0 else 0.0,
            "top_link_loads": _top_k(per_transfer_top_loads),
        },
        "links": sorted(links, key=lambda item: item["total_gib"], reverse=True),
        "top_links_by_volume": [
            {"source": key[0], "destination": key[1], "total_gib": total_gib}
            for key, total_gib in top_by_volume[:10]
        ],
    }


def _staging_tail_features(records: list[JobRecord]) -> dict[str, float]:
    if not records:
        return {
            "log1p_max_staging": 0.0,
            "log1p_p99_staging": 0.0,
            "frac_staging_gt_0": 0.0,
            "frac_staging_gt_3600s": 0.0,
            "log1p_mean_site_staging": 0.0,
            "log1p_max_site_staging": 0.0,
            "top_site_staging": [0.0, 0.0, 0.0],
        }

    staging = [record.staging_time for record in records]
    site_buckets: dict[str, list[float]] = defaultdict(list)
    for record in records:
        site_buckets[record.site].append(record.staging_time)
    site_means = [float(np.mean(values)) for values in site_buckets.values()]

    return {
        "log1p_max_staging": float(np.log1p(max(staging))),
        "log1p_p99_staging": float(np.log1p(float(np.percentile(staging, 99)))),
        "frac_staging_gt_0": float(sum(1 for value in staging if value > 0) / len(staging)),
        "frac_staging_gt_3600s": float(
            sum(1 for value in staging if value > STAGING_GT_3600S) / len(staging)
        ),
        "log1p_mean_site_staging": float(np.log1p(max(float(np.mean(site_means)), 0.0))),
        "log1p_max_site_staging": float(np.log1p(max(site_means) if site_means else 0.0)),
        "top_site_staging": _top_k(site_means),
    }


def _utilization_obs_features(site_report: dict[str, Any]) -> dict[str, float]:
    grid = site_report.get("grid", {})
    site_summary = site_report.get("site_storage_summary", _stat([]))
    top_sites = site_report.get("top_sites_by_storage_util", [])
    top_vals = [float(item.get("max", 0.0)) for item in top_sites]
    return {
        "grid_storage_util_mean": float(grid.get("mean", 0.0)),
        "grid_storage_util_max": float(grid.get("max", 0.0)),
        "site_storage_util_mean": float(site_summary.get("mean", 0.0)),
        "site_storage_util_max": float(site_summary.get("max", 0.0)),
        "site_storage_util_p90": float(site_summary.get("p90", 0.0)),
        "top_site_storage_util": _top_k(top_vals),
    }


def _network_obs_features(network_report: dict[str, Any]) -> dict[str, float]:
    summary = network_report.get("summary", {})
    link_load = summary.get("link_load", {})
    reactive = summary.get("link_load_reactive", {})
    proactive = summary.get("link_load_proactive", {})
    top_loads = summary.get("top_link_loads", [0.0, 0.0, 0.0])
    return {
        "link_load_mean": float(link_load.get("mean", 0.0)),
        "link_load_max": float(link_load.get("max", 0.0)),
        "link_load_mean_reactive": float(reactive.get("mean", 0.0)),
        "link_load_mean_proactive": float(proactive.get("mean", 0.0)),
        "link_load_size_weighted_mean": float(summary.get("size_weighted_link_load", 0.0)),
        "top_link_loads": list(top_loads)[:TOP_K],
    }


def build_outcome_summary(
    *,
    trial_index: int,
    sim_success: bool,
    returncode: int,
    records: list[JobRecord],
    objective_result: dict[str, Any] | None,
    transfer: TransferSummary | None,
    site_report: dict[str, Any],
    network_report: dict[str, Any],
) -> dict[str, Any]:
    metadata = (objective_result or {}).get("metadata") or {}
    avg_bottom = float(metadata.get("avg_bottom_staging", 0.0))
    avg_top = float(metadata.get("avg_top_staging", 0.0))
    cost = float((objective_result or {}).get("reward", (objective_result or {}).get("value", 0.0)))

    staging_features = _staging_tail_features(records)
    per_site = (objective_result or {}).get("per_site") or {}
    top_sites_by_staging = sorted(
        ((site, float(value)) for site, value in per_site.items()),
        key=lambda item: item[1],
        reverse=True,
    )[:10]

    transfer = transfer or TransferSummary(0.0, 0.0, 0.0, 0.0, 0.0)
    util_features = _utilization_obs_features(site_report)
    network_features = _network_obs_features(network_report)

    return {
        "feature_schema_version": OBSERVATION_SPEC_VERSION,
        "trial_index": trial_index,
        "sim_success": sim_success,
        "returncode": returncode,
        "job_count": len(records),
        "cost": cost,
        "avg_bottom_staging": avg_bottom,
        "avg_top_staging": avg_top,
        "top_sites_by_staging": [
            {"site": site, "avg_staging_time": value} for site, value in top_sites_by_staging
        ],
        "staging_features": staging_features,
        "transfer": {
            "total_ingress_gib": transfer.total_ingress_gib,
            "total_egress_gib": transfer.total_egress_gib,
            "proactive_volume_gib": transfer.proactive_volume_gib,
            "reactive_volume_gib": transfer.reactive_volume_gib,
            "proactive_fraction": transfer.proactive_fraction,
        },
        "utilization_summary": util_features,
        "network_summary": network_features,
    }


def build_context_features(
    outcome: dict[str, Any],
    *,
    memory: ObservationMemory,
    trial_index: int,
    max_trials: int,
) -> dict[str, float]:
    staging = outcome.get("staging_features") or {}
    transfer = outcome.get("transfer") or {}
    util = outcome.get("utilization_summary") or {}
    network = outcome.get("network_summary") or {}

    cost = float(outcome.get("cost", 0.0))
    prev_cost = memory.cost_history[-1] if memory.cost_history else cost
    delta_cost = prev_cost - cost if memory.cost_history else 0.0

    top_site_staging = staging.get("top_site_staging", [0.0, 0.0, 0.0])
    top_site_storage = util.get("top_site_storage_util", [0.0, 0.0, 0.0])
    top_link_loads = network.get("top_link_loads", [0.0, 0.0, 0.0])

    avg_bottom = float(outcome.get("avg_bottom_staging", 0.0))
    avg_top = float(outcome.get("avg_top_staging", 0.0))

    return {
        "cost": cost,
        "log1p_avg_bottom_staging": float(np.log1p(max(avg_bottom, 0.0))),
        "log1p_avg_top_staging": float(np.log1p(max(avg_top, 0.0))),
        "log1p_max_staging": float(staging.get("log1p_max_staging", 0.0)),
        "log1p_p99_staging": float(staging.get("log1p_p99_staging", 0.0)),
        "frac_staging_gt_0": float(staging.get("frac_staging_gt_0", 0.0)),
        "frac_staging_gt_3600s": float(staging.get("frac_staging_gt_3600s", 0.0)),
        "log1p_mean_site_staging": float(staging.get("log1p_mean_site_staging", 0.0)),
        "log1p_max_site_staging": float(staging.get("log1p_max_site_staging", 0.0)),
        "log1p_top_site_staging_1": float(np.log1p(max(top_site_staging[0], 0.0))),
        "log1p_top_site_staging_2": float(np.log1p(max(top_site_staging[1], 0.0))),
        "log1p_top_site_staging_3": float(np.log1p(max(top_site_staging[2], 0.0))),
        "log1p_total_ingress_gib": float(np.log1p(max(float(transfer.get("total_ingress_gib", 0.0)), 0.0))),
        "log1p_total_egress_gib": float(np.log1p(max(float(transfer.get("total_egress_gib", 0.0)), 0.0))),
        "proactive_volume_fraction": float(transfer.get("proactive_fraction", 0.0)),
        "grid_storage_util_mean": float(util.get("grid_storage_util_mean", 0.0)),
        "grid_storage_util_max": float(util.get("grid_storage_util_max", 0.0)),
        "site_storage_util_mean": float(util.get("site_storage_util_mean", 0.0)),
        "site_storage_util_max": float(util.get("site_storage_util_max", 0.0)),
        "site_storage_util_p90": float(util.get("site_storage_util_p90", 0.0)),
        "top_site_storage_util_1": float(top_site_storage[0]),
        "top_site_storage_util_2": float(top_site_storage[1]),
        "top_site_storage_util_3": float(top_site_storage[2]),
        "link_load_mean": float(network.get("link_load_mean", 0.0)),
        "link_load_max": float(network.get("link_load_max", 0.0)),
        "link_load_mean_reactive": float(network.get("link_load_mean_reactive", 0.0)),
        "link_load_mean_proactive": float(network.get("link_load_mean_proactive", 0.0)),
        "link_load_size_weighted_mean": float(network.get("link_load_size_weighted_mean", 0.0)),
        "top_link_load_1": float(top_link_loads[0]),
        "top_link_load_2": float(top_link_loads[1]),
        "top_link_load_3": float(top_link_loads[2]),
        "trial_index_norm": float(trial_index / max(max_trials, 1)),
        "best_cost_so_far": float(memory.best_cost if np.isfinite(memory.best_cost) else cost),
        "delta_cost": float(delta_cost),
        "last_sim_success": float(outcome.get("sim_success", False)),
    }


def build_context_vector(
    outcome: dict[str, Any],
    *,
    memory: ObservationMemory,
    spec: ObservationSpec,
    trial_index: int,
    max_trials: int,
) -> np.ndarray:
    features = build_context_features(
        outcome,
        memory=memory,
        trial_index=trial_index,
        max_trials=max_trials,
    )
    base = np.asarray([features[name] for name in spec.base_feature_names], dtype=np.float32)
    if memory.last_action_vector is not None and len(memory.last_action_vector) == spec.action_dim:
        action_part = np.asarray(memory.last_action_vector, dtype=np.float32)
    else:
        action_part = np.zeros(spec.action_dim, dtype=np.float32)
    return np.concatenate([base, action_part])


def zero_observation(spec: ObservationSpec) -> np.ndarray:
    return np.zeros(spec.obs_dim, dtype=np.float32)


def observation_spec_for_action_space(action_space: ActionSpace) -> ObservationSpec:
    return ObservationSpec(
        version=OBSERVATION_SPEC_VERSION,
        base_feature_names=BASE_FEATURE_NAMES,
        action_dim=len(action_space.parameters),
    )


def write_observation_spec(method_dir: Path, spec: ObservationSpec) -> Path:
    method_dir.mkdir(parents=True, exist_ok=True)
    path = method_dir / "observation_spec.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(spec.to_dict(), handle, indent=2)
        handle.write("\n")
    return path


def write_observation_artifacts(
    trial_dir: Path,
    *,
    outcome: dict[str, Any],
    context: dict[str, Any] | None,
    site_report: dict[str, Any],
    network_report: dict[str, Any],
) -> Path:
    obs_dir = trial_dir / "observation"
    obs_dir.mkdir(parents=True, exist_ok=True)

    with (obs_dir / "outcome.json").open("w", encoding="utf-8") as handle:
        json.dump(outcome, handle, indent=2)
        handle.write("\n")

    with (obs_dir / "site_utilization_report.json").open("w", encoding="utf-8") as handle:
        json.dump(site_report, handle, indent=2)
        handle.write("\n")

    with (obs_dir / "network_usage_report.json").open("w", encoding="utf-8") as handle:
        json.dump(network_report, handle, indent=2)
        handle.write("\n")

    if context is not None:
        with (obs_dir / "context.json").open("w", encoding="utf-8") as handle:
            json.dump(context, handle, indent=2)
            handle.write("\n")

    return obs_dir


def build_trial_observation_bundle(
    db_path: Path,
    *,
    repo_root: Path,
    trial_index: int,
    sim_success: bool,
    returncode: int,
    records: list[JobRecord],
    objective_result: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    site_report = build_site_utilization_report(db_path)
    network_report = build_network_usage_report(db_path)
    transfer = transfer_summary_from_db(db_path, repo_root=repo_root)
    outcome = build_outcome_summary(
        trial_index=trial_index,
        sim_success=sim_success,
        returncode=returncode,
        records=records,
        objective_result=objective_result,
        transfer=transfer,
        site_report=site_report,
        network_report=network_report,
    )
    return outcome, site_report, network_report, transfer or TransferSummary(0.0, 0.0, 0.0, 0.0)


def update_memory_from_outcome(
    memory: ObservationMemory,
    outcome: dict[str, Any],
    last_action_vector: np.ndarray,
) -> None:
    cost = float(outcome.get("cost", float("inf")))
    memory.cost_history.append(cost)
    if np.isfinite(cost):
        memory.best_cost = min(memory.best_cost, cost)
    memory.last_sim_success = 1.0 if outcome.get("sim_success") else 0.0
    memory.last_action_vector = np.asarray(last_action_vector, dtype=np.float32)
