from __future__ import annotations

from typing import Any

import numpy as np

from datamgmt_explore.metrics import (
    JobRecord,
    P95_MAX_STAGING_TAIL_WEIGHT,
    compute_p95_max_staging_reward,
)
from datamgmt_explore.objectives.base import ObjectiveResult, register_objective
from datamgmt_explore.windowing import WindowContext


@register_objective("p95_max_staging_reward")
class P95MaxStagingRewardObjective:
    name = "p95_max_staging_reward"

    def compute(
        self,
        job_records: list[JobRecord],
        window_ctx: WindowContext,
        *,
        aggregation: str = "mean",
        reward_transform: str = "neg_log1p",
        tail_weight: float = P95_MAX_STAGING_TAIL_WEIGHT,
    ) -> ObjectiveResult:
        del aggregation, reward_transform  # fixed reward formula for this objective

        p95, max_staging, reward = compute_p95_max_staging_reward(
            job_records,
            tail_weight=tail_weight,
        )
        per_job_values = [record.staging_time for record in job_records]

        site_buckets: dict[str, list[float]] = {}
        for record in job_records:
            site_buckets.setdefault(record.site, []).append(record.staging_time)
        per_site = {
            site: float(np.mean(values))
            for site, values in site_buckets.items()
        }

        return ObjectiveResult(
            name=self.name,
            value=reward,
            reward=reward,
            aggregation="p95_max_log",
            job_count=len(job_records),
            per_site=per_site,
            per_job_values=per_job_values,
            metadata={
                "window_mode": window_ctx.config.mode.value,
                "window_index": window_ctx.window_index,
                "window_start": window_ctx.window_start,
                "window_end": window_ctx.window_end,
                "p95_staging": p95,
                "max_staging": max_staging,
                "tail_weight": tail_weight,
                "reward_formula": "log1p(p95) + tail_weight*log1p(max_staging)",
                "lower_is_better": True,
            },
        )
