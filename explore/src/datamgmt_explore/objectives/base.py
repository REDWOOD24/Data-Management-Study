from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any, Protocol

from datamgmt_explore.metrics import JobRecord
from datamgmt_explore.settings import load_yaml
from datamgmt_explore.windowing import WindowContext


@dataclass(frozen=True)
class ObjectiveResult:
    name: str
    value: float
    reward: float
    aggregation: str
    job_count: int
    per_site: dict[str, float]
    per_job_values: list[float]
    metadata: dict[str, Any]


class Objective(Protocol):
    name: str

    def compute(
        self,
        job_records: list[JobRecord],
        window_ctx: WindowContext,
        *,
        aggregation: str,
    ) -> ObjectiveResult: ...


_REGISTRY: dict[str, type] = {}


def register_objective(name: str):
    def decorator(cls: type) -> type:
        _REGISTRY[name] = cls
        return cls

    return decorator


def load_objective(name: str, objectives_config_path: str | None = None) -> Objective:
    if name == "avg_staging_time":
        from datamgmt_explore.objectives import avg_staging_time as _avg_staging_time  # noqa: F401
    if name == "p95_max_staging_reward":
        from datamgmt_explore.objectives import p95_max_staging_reward as _p95_max_staging_reward  # noqa: F401
    if name == "tail_bulk_staging_cost":
        from datamgmt_explore.objectives import tail_bulk_staging_cost as _tail_bulk_staging_cost  # noqa: F401

    if objectives_config_path:
        config = load_yaml(objectives_config_path)
        spec = config.get("objectives", {}).get(name)
        if spec:
            module = import_module(spec["module"])
            cls = getattr(module, spec["class"])
            return cls()

    if name in _REGISTRY:
        return _REGISTRY[name]()

    module = import_module(f"datamgmt_explore.objectives.{name}")
    for attr in dir(module):
        candidate = getattr(module, attr)
        if isinstance(candidate, type) and getattr(candidate, "name", None) == name:
            return candidate()

    raise KeyError(f"Unknown objective: {name}")
