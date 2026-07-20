from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from datamgmt_explore.action_space import ActionSpace

PROACTIVE_INTERVAL_SEC = 500.0
MAX_TRANSFERS_PER_TICK = 1

_FULL_ACTION_SPACE: ActionSpace | None = None


def _full_action_space() -> ActionSpace:
    global _FULL_ACTION_SPACE
    if _FULL_ACTION_SPACE is None:
        full_path = Path(__file__).resolve().parents[2] / "config" / "action_space.yaml"
        _FULL_ACTION_SPACE = ActionSpace.from_yaml(full_path)
    return _FULL_ACTION_SPACE


def _full_action_defaults() -> dict[str, Any]:
    """Defaults from the full explore action space (for filling omitted template keys)."""
    return _full_action_space().defaults()


class PolicyConfigBuilder:
    """Build a full data_policy_config.json from an action dictionary."""

    def __init__(
        self,
        action_space: ActionSpace,
        base_policy_path: Path | None = None,
        *,
        drop_in_transfers_file: Path | None = None,
    ) -> None:
        self.action_space = action_space
        self.base_policy_path = base_policy_path
        self.drop_in_transfers_file = drop_in_transfers_file
        self._base_policy = self._load_base_policy(base_policy_path)
        full = _full_action_space()
        self._fill_defaults = {
            **full.defaults(),
            **action_space.defaults(),
        }
        # Keep canonical name lists so plugin template indices stay stable.
        self._reactive_source_names = full.reactive_source_names
        self._proactive_template_names = full.proactive_template_names
        self._file_pick_names = full.file_pick_names
        self._path_metric_names = full.path_metric_names
        self._hotset_destination_policy_names = full.hotset_destination_policy_names
        self._site_staging_bias_names = full.site_staging_bias_names

    def _action_value(self, action: dict[str, Any], key: str) -> Any:
        if key in action:
            return action[key]
        if key in self._fill_defaults:
            return self._fill_defaults[key]
        raise KeyError(f"Missing action key with no default: {key}")

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
        if self.drop_in_transfers_file is not None:
            root["drop_in_transfers_file"] = self.drop_in_transfers_file.name
        else:
            root.pop("drop_in_transfers_file", None)

        reactive = root.setdefault("reactive", {})
        reactive["enabled"] = bool(self._action_value(action, "reactive.enabled"))
        reactive["prefer_local_replica"] = bool(
            self._action_value(action, "reactive.prefer_local_replica")
        )
        reactive_index = int(self._action_value(action, "reactive.remote_source_template"))
        reactive["remote_source_template"] = [
            reactive_index,
            self._reactive_source_names + ["custom_policy_agent"],
        ]
        reactive["random_seed"] = int(self._action_value(action, "reactive.random_seed"))
        reactive.setdefault("copy_to_move_threshold", 3)

        proactive = root.setdefault("proactive", {})
        proactive["enabled"] = bool(self._action_value(action, "proactive.enabled"))
        proactive["interval"] = PROACTIVE_INTERVAL_SEC
        proactive["data_transfer_mode"] = str(
            self._action_value(action, "proactive.data_transfer_mode")
        )
        proactive.setdefault("random_seed", 1337)
        proactive["site_staging_bias"] = self._indexed_names(
            int(self._action_value(action, "proactive.site_staging_bias")),
            self._site_staging_bias_names,
        )
        template_index = int(self._action_value(action, "proactive.transfer_template"))
        proactive["transfer_template"] = [
            template_index,
            self._proactive_template_names + ["custom_policy_agent"],
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
        storage = template_params.setdefault("storage_rebalance", {})
        storage["high_utilization_threshold"] = float(
            self._action_value(action, "storage_rebalance.high_utilization_threshold")
        )
        storage["low_utilization_threshold"] = float(
            self._action_value(action, "storage_rebalance.low_utilization_threshold")
        )
        storage["file_pick"] = self._indexed_names(
            int(self._action_value(action, "storage_rebalance.file_pick")),
            self._file_pick_names,
        )
        storage["max_transfers_per_tick"] = MAX_TRANSFERS_PER_TICK
        storage["skip_if_already_replica_on_destination"] = bool(
            self._action_value(
                action, "storage_rebalance.skip_if_already_replica_on_destination"
            )
        )

        network = template_params.setdefault("network_aware_rebalance", {})
        network["high_utilization_threshold"] = float(
            self._action_value(action, "network_aware_rebalance.high_utilization_threshold")
        )
        network["low_utilization_threshold"] = float(
            self._action_value(action, "network_aware_rebalance.low_utilization_threshold")
        )
        network["path_metric"] = self._indexed_names(
            int(self._action_value(action, "network_aware_rebalance.path_metric")),
            self._path_metric_names,
        )
        network["max_path_load"] = float(
            self._action_value(action, "network_aware_rebalance.max_path_load")
        )
        network["file_pick"] = self._indexed_names(
            int(self._action_value(action, "network_aware_rebalance.file_pick")),
            self._file_pick_names,
        )
        network["max_transfers_per_tick"] = MAX_TRANSFERS_PER_TICK

        hotset = template_params.setdefault("hotset_replication", {})
        hotset.setdefault("hotness_window", 100)
        hotset["hotness_threshold"] = float(
            self._action_value(action, "hotset_replication.hotness_threshold")
        )
        hotset.setdefault("prediction_horizon", 50)
        hotset["target_replica_count"] = int(
            self._action_value(action, "hotset_replication.target_replica_count")
        )
        hotset["candidate_destination_policy"] = self._indexed_names(
            int(self._action_value(action, "hotset_replication.candidate_destination_policy")),
            self._hotset_destination_policy_names,
        )
        hotset["max_transfers_per_tick"] = MAX_TRANSFERS_PER_TICK

        prefetch = template_params.setdefault("job_input_prefetch", {})
        prefetch["max_transfers_per_tick"] = int(
            self._action_value(action, "job_input_prefetch.max_transfers_per_tick")
        )
        prefetch["max_jobs_per_tick"] = int(
            self._action_value(action, "job_input_prefetch.max_jobs_per_tick")
        )

    @staticmethod
    def _indexed_names(index: int, names: list[str]) -> list[Any]:
        bounded = max(0, min(index, len(names) - 1))
        return [bounded, names]

    @staticmethod
    def _ensure_static_template_defaults(template_params: dict[str, Any]) -> None:
        template_params.setdefault(
            "job_input_prefetch",
            {
                "max_transfers_per_tick": 2,
                "max_jobs_per_tick": 8,
            },
        )
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
