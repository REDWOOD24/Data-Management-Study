from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from datamgmt_explore.settings import load_yaml

PROACTIVE_INTERVAL_SEC = 500.0


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    kind: str
    default: Any
    minimum: float | int | None = None
    maximum: float | int | None = None
    choices: tuple[str, ...] | None = None
    template: str | None = None


@dataclass
class ActionSpace:
    parameters: list[ParameterSpec]
    constraints: list[dict[str, Any]]
    reactive_source_names: list[str]
    proactive_template_names: list[str]
    file_pick_names: list[str]
    path_metric_names: list[str]
    transfer_modes: list[str]
    hotset_destination_policy_names: list[str]
    site_staging_bias_names: list[str]

    @classmethod
    def from_yaml(cls, path: Path | str) -> ActionSpace:
        raw = load_yaml(path)
        parameters: list[ParameterSpec] = []
        for name, spec in raw.get("parameters", {}).items():
            choices = spec.get("choices")
            parameters.append(
                ParameterSpec(
                    name=name,
                    kind=str(spec["type"]),
                    default=spec.get("default"),
                    minimum=spec.get("min"),
                    maximum=spec.get("max"),
                    choices=tuple(choices) if choices else None,
                    template=spec.get("template"),
                )
            )
        return cls(
            parameters=parameters,
            constraints=list(raw.get("constraints", [])),
            reactive_source_names=list(raw.get("reactive_source_names", [])),
            proactive_template_names=list(raw.get("proactive_template_names", [])),
            file_pick_names=list(raw.get("file_pick_names", [])),
            path_metric_names=list(raw.get("path_metric_names", [])),
            transfer_modes=list(raw.get("transfer_modes", ["COPY", "MOVE"])),
            hotset_destination_policy_names=list(
                raw.get(
                    "hotset_destination_policy_names",
                    ["requesting_sites_first", "least_utilized_among_requesting"],
                )
            ),
            site_staging_bias_names=list(
                raw.get(
                    "site_staging_bias_names",
                    ["off", "high_staging_queue", "high_recent_staging"],
                )
            ),
        )

    @property
    def names(self) -> list[str]:
        return [param.name for param in self.parameters]

    def defaults(self) -> dict[str, Any]:
        return {param.name: param.default for param in self.parameters}

    def sample(
        self,
        rng: np.random.Generator,
        *,
        fixed: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for _ in range(1000):
            action = self._sample_once(rng)
            if fixed:
                action.update(fixed)
            action = self.apply_masks(action)
            if self.validate(action):
                return action
        raise RuntimeError("Failed to sample a valid action after 1000 attempts")

    def apply_masks(self, action: dict[str, Any]) -> dict[str, Any]:
        """Use defaults for inactive template params; always enable reactive and proactive."""
        masked = dict(action)
        masked["reactive.enabled"] = True
        masked["proactive.enabled"] = True
        masked["proactive.interval"] = PROACTIVE_INTERVAL_SEC
        defaults = self.defaults()

        active_template = self.active_template(masked)
        for param in self.parameters:
            if param.template and param.template != active_template:
                masked[param.name] = defaults[param.name]
        return masked

    def _sample_once(self, rng: np.random.Generator) -> dict[str, Any]:
        action: dict[str, Any] = {}
        for param in self.parameters:
            if param.kind == "bool":
                action[param.name] = bool(rng.integers(0, 2))
            elif param.kind == "enum":
                action[param.name] = rng.choice(list(param.choices or ()))
            elif param.kind == "int":
                low = int(param.minimum if param.minimum is not None else 0)
                high = int(param.maximum if param.maximum is not None else low)
                action[param.name] = int(rng.integers(low, high + 1))
            elif param.kind == "float":
                low = float(param.minimum if param.minimum is not None else 0.0)
                high = float(param.maximum if param.maximum is not None else 1.0)
                action[param.name] = float(rng.uniform(low, high))
            else:
                action[param.name] = param.default
        return action

    def validate(self, action: dict[str, Any]) -> bool:
        for param in self.parameters:
            if param.name not in action:
                return False
            value = action[param.name]
            if param.kind == "bool":
                if not isinstance(value, (bool, np.bool_)):
                    return False
            elif param.kind == "enum":
                if value not in (param.choices or ()):
                    return False
            elif param.kind == "int":
                if not isinstance(value, (int, np.integer)):
                    return False
                if param.minimum is not None and value < param.minimum:
                    return False
                if param.maximum is not None and value > param.maximum:
                    return False
            elif param.kind == "float":
                if not isinstance(value, (int, float, np.integer, np.floating)):
                    return False
                if param.minimum is not None and float(value) < float(param.minimum):
                    return False
                if param.maximum is not None and float(value) > float(param.maximum):
                    return False

        for constraint in self.constraints:
            rule = constraint.get("rule")
            if rule == "high_lt_low":
                high_name, low_name = constraint["params"]
                if float(action[high_name]) >= float(action[low_name]):
                    return False
            elif rule == "min_separation":
                high_name, low_name = constraint["params"]
                min_gap = float(constraint.get("min_gap", 0.25))
                if float(action[low_name]) - float(action[high_name]) < min_gap:
                    return False
        return True

    def active_template(self, action: dict[str, Any]) -> str | None:
        if not action.get("proactive.enabled", False):
            return None
        index = int(action["proactive.transfer_template"])
        return self.proactive_template_names[index]

    def vectorize(self, action: dict[str, Any]) -> np.ndarray:
        values: list[float] = []
        for param in self.parameters:
            value = action[param.name]
            if param.kind == "bool":
                values.append(float(bool(value)))
            elif param.kind == "enum":
                values.append(float(list(param.choices or []).index(str(value))))
            else:
                values.append(float(value))
        return np.asarray(values, dtype=np.float64)

    def normalize_vector(self, action: dict[str, Any]) -> np.ndarray:
        """Map a decoded action dict to [0, 1] per parameter (for RL observations)."""
        values: list[float] = []
        for param in self.parameters:
            value = action[param.name]
            if param.kind == "bool":
                values.append(1.0 if bool(value) else 0.0)
            elif param.kind == "enum":
                choices = list(param.choices or ())
                index = choices.index(str(value)) if str(value) in choices else 0
                values.append(float(index / max(len(choices) - 1, 1)))
            elif param.kind == "int":
                low = float(param.minimum if param.minimum is not None else 0)
                high = float(param.maximum if param.maximum is not None else low)
                span = max(high - low, 1e-9)
                values.append(float(np.clip((float(value) - low) / span, 0.0, 1.0)))
            elif param.kind == "float":
                low = float(param.minimum if param.minimum is not None else 0.0)
                high = float(param.maximum if param.maximum is not None else 1.0)
                span = max(high - low, 1e-9)
                values.append(float(np.clip((float(value) - low) / span, 0.0, 1.0)))
            else:
                values.append(0.0)
        return np.asarray(values, dtype=np.float32)

    def from_vector(self, vector: np.ndarray) -> dict[str, Any]:
        action: dict[str, Any] = {}
        for index, param in enumerate(self.parameters):
            raw = vector[index]
            if param.kind == "bool":
                action[param.name] = bool(round(raw))
            elif param.kind == "enum":
                choices = list(param.choices or ())
                choice_index = int(np.clip(round(raw), 0, len(choices) - 1))
                action[param.name] = choices[choice_index]
            elif param.kind == "int":
                low = int(param.minimum if param.minimum is not None else raw)
                high = int(param.maximum if param.maximum is not None else raw)
                action[param.name] = int(np.clip(round(raw), low, high))
            elif param.kind == "float":
                low = float(param.minimum if param.minimum is not None else raw)
                high = float(param.maximum if param.maximum is not None else raw)
                action[param.name] = float(np.clip(raw, low, high))
            else:
                action[param.name] = param.default
        return action


class ActionDecoder:
    def __init__(self, action_space: ActionSpace) -> None:
        self.action_space = action_space

    def decode(self, action: dict[str, Any]) -> dict[str, Any]:
        masked = self.action_space.apply_masks(action)
        if not self.action_space.validate(masked):
            raise ValueError(f"Invalid action: {action}")
        return masked

    def sample(
        self,
        rng: np.random.Generator,
        *,
        fixed: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.action_space.sample(rng, fixed=fixed)
