from __future__ import annotations

from typing import Any

import numpy as np

from datamgmt_explore.metrics import JobRecord
from datamgmt_explore.objectives.base import ObjectiveResult, register_objective
from datamgmt_explore.windowing import WindowContext, WindowMode


def aggregate_staging(
    records: list[JobRecord],
    aggregation: str,
) -> tuple[float, dict[str, float], list[float]]:
    if not records:
        return float("nan"), {}, []

    per_job_values = [record.staging_time for record in records]
    site_buckets: dict[str, list[float]] = {}
    for record in records:
        site_buckets.setdefault(record.site, []).append(record.staging_time)

    per_site = {
        site: float(np.mean(values))
        for site, values in site_buckets.items()
    }

    if aggregation == "mean_of_site_means":
        value = float(np.mean(list(per_site.values()))) if per_site else float("nan")
    elif aggregation == "max_site_mean":
        value = float(np.max(list(per_site.values()))) if per_site else float("nan")
    else:
        value = float(np.mean(per_job_values))

    return value, per_site, per_job_values


@register_objective("avg_staging_time")
class AvgStagingTimeObjective:
    name = "avg_staging_time"

    def compute(
        self,
        job_records: list[JobRecord],
        window_ctx: WindowContext,
        *,
        aggregation: str = "mean",
        reward_transform: str = "neg_log1p",
    ) -> ObjectiveResult:
        value, per_site, per_job_values = aggregate_staging(job_records, aggregation)

        if window_ctx.config.mode == WindowMode.PER_JOB and per_job_values:
            rewards = [-float(np.log1p(max(v, 0.0))) for v in per_job_values]
            reward = float(np.mean(rewards))
        else:
            reward = self._to_reward(value, reward_transform)

        return ObjectiveResult(
            name=self.name,
            value=value,
            reward=reward,
            aggregation=aggregation,
            job_count=len(job_records),
            per_site=per_site,
            per_job_values=per_job_values,
            metadata={
                "window_mode": window_ctx.config.mode.value,
                "window_index": window_ctx.window_index,
                "window_start": window_ctx.window_start,
                "window_end": window_ctx.window_end,
                "reward_transform": reward_transform,
            },
        )

    @staticmethod
    def _to_reward(value: float, reward_transform: str) -> float:
        if np.isnan(value):
            return float("-inf")
        if reward_transform == "neg_log1p":
            return -float(np.log1p(max(value, 0.0)))
        if reward_transform == "neg":
            return -float(value)
        raise ValueError(f"Unknown reward transform: {reward_transform}")
