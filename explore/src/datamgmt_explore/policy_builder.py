from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from datamgmt_explore.action_space import ActionSpace

PROACTIVE_INTERVAL_SEC = 500.0
MAX_TRANSFERS_PER_TICK = 1


class PolicyConfigBuilder:
    """Build a full data_policy_config.json from an action dictionary."""

    def __init__(
        self,
        action_space: ActionSpace,
        base_policy_path: Path | None = None,
    ) -> None:
        self.action_space = action_space
        self.base_policy_path = base_policy_path
        self._base_policy = self._load_base_policy(base_policy_path)

    def _load_base_policy(self, base_policy_path: Path | None) -> dict[str, Any]:
        if base_policy_path is None or not base_policy_path.is_file():
            return {
                "Data_Management_Policy": {
                    "enabled": True,
                    "reactive": {},
                    "proactive": {"template_params": {}},
                }
            }
        with base_policy_path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def build(self, action: dict[str, Any]) -> dict[str, Any]:
        if not self.action_space.validate(action):
            raise ValueError(f"Invalid action for policy build: {action}")

        policy = copy.deepcopy(self._base_policy)
        root = policy.setdefault("Data_Management_Policy", {})
        root["enabled"] = True

        reactive = root.setdefault("reactive", {})
        reactive["enabled"] = bool(action["reactive.enabled"])
        reactive["prefer_local_replica"] = bool(action["reactive.prefer_local_replica"])
        reactive_index = int(action["reactive.remote_source_template"])
        reactive["remote_source_template"] = [
            reactive_index,
            self.action_space.reactive_source_names
            + ["custom_policy_agent"],
        ]
        reactive["random_seed"] = int(action["reactive.random_seed"])
        reactive.setdefault("copy_to_move_threshold", 3)

        proactive = root.setdefault("proactive", {})
        proactive["enabled"] = bool(action["proactive.enabled"])
        proactive["interval"] = PROACTIVE_INTERVAL_SEC
        proactive["data_transfer_mode"] = str(action["proactive.data_transfer_mode"])
        proactive.setdefault("random_seed", 1337)
        template_index = int(action["proactive.transfer_template"])
        proactive["transfer_template"] = [
            template_index,
            self.action_space.proactive_template_names + ["custom_policy_agent"],
        ]

        template_params = proactive.setdefault("template_params", {})
        self._apply_template_params(template_params, action)
        self._ensure_static_template_defaults(template_params)
        return policy

    def _apply_template_params(
        self,
        template_params: dict[str, Any],
        action: dict[str, Any],
    ) -> None:
        active = self.action_space.active_template(action)

        storage = template_params.setdefault("storage_rebalance", {})
        storage["high_utilization_threshold"] = float(
            action["storage_rebalance.high_utilization_threshold"]
        )
        storage["low_utilization_threshold"] = float(
            action["storage_rebalance.low_utilization_threshold"]
        )
        storage["file_pick"] = self._indexed_names(
            int(action["storage_rebalance.file_pick"]),
            self.action_space.file_pick_names,
        )
        storage["max_transfers_per_tick"] = MAX_TRANSFERS_PER_TICK
        storage["skip_if_already_replica_on_destination"] = bool(
            action["storage_rebalance.skip_if_already_replica_on_destination"]
        )

        network = template_params.setdefault("network_aware_rebalance", {})
        network["high_utilization_threshold"] = float(
            action["network_aware_rebalance.high_utilization_threshold"]
        )
        network["low_utilization_threshold"] = float(
            action["network_aware_rebalance.low_utilization_threshold"]
        )
        network["path_metric"] = self._indexed_names(
            int(action["network_aware_rebalance.path_metric"]),
            self.action_space.path_metric_names,
        )
        network["max_path_load"] = float(action["network_aware_rebalance.max_path_load"])
        network["file_pick"] = self._indexed_names(
            int(action["network_aware_rebalance.file_pick"]),
            self.action_space.file_pick_names,
        )
        network["max_transfers_per_tick"] = MAX_TRANSFERS_PER_TICK

        hotset = template_params.setdefault("hotset_replication", {})
        hotset.setdefault("hotness_window", 100)
        hotset["hotness_threshold"] = float(action["hotset_replication.hotness_threshold"])
        hotset.setdefault("prediction_horizon", 50)
        hotset["target_replica_count"] = int(action["hotset_replication.target_replica_count"])
        hotset.setdefault(
            "candidate_destination_policy",
            [0, ["requesting_sites_first", "least_utilized_among_requesting"]],
        )
        hotset["max_transfers_per_tick"] = MAX_TRANSFERS_PER_TICK

        if active is not None:
            _ = active

    @staticmethod
    def _indexed_names(index: int, names: list[str]) -> list[Any]:
        bounded = max(0, min(index, len(names) - 1))
        return [bounded, names]

    @staticmethod
    def _ensure_static_template_defaults(template_params: dict[str, Any]) -> None:
        template_params.setdefault(
            "custom_policy_agent",
            {
                "policy_id": "my_agent_v1",
                "metric_keys": [
                    "site.storage_utilization",
                    "path.bandwidth",
                    "path.link_load",
                    "path.latency",
                    "file.hotness",
                ],
                "decision_timeout_ms": 5,
                "fallback_template_index": 0,
            },
        )

    def write(self, action: dict[str, Any], output_path: Path) -> dict[str, Any]:
        policy = self.build(action)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(policy, handle, indent=2)
            handle.write("\n")
        return policy
