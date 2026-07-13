from __future__ import annotations

import numpy as np

from datamgmt_explore.metrics import (
    JobRecord,
    STAGING_TAIL_FRACTION,
    TAIL_BULK_BOTTOM_WEIGHT,
    TAIL_BULK_TOP_WEIGHT,
    compute_tail_bulk_staging_cost,
)
from datamgmt_explore.objectives.base import ObjectiveResult, register_objective
from datamgmt_explore.windowing import WindowContext


@register_objective("tail_bulk_staging_cost")
class TailBulkStagingCostObjective:
    name = "tail_bulk_staging_cost"

    def compute(
        self,
        job_records: list[JobRecord],
        window_ctx: WindowContext,
        *,
        aggregation: str = "mean",
        reward_transform: str = "neg_log1p",
        bottom_weight: float = TAIL_BULK_BOTTOM_WEIGHT,
        top_weight: float = TAIL_BULK_TOP_WEIGHT,
        tail_fraction: float = STAGING_TAIL_FRACTION,
    ) -> ObjectiveResult:
        del aggregation, reward_transform

        avg_bottom, avg_top, cost = compute_tail_bulk_staging_cost(
            job_records,
            bottom_weight=bottom_weight,
            top_weight=top_weight,
            tail_fraction=tail_fraction,
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
            value=cost,
            reward=cost,
            aggregation="tail_bulk_log_weighted_mean",
            job_count=len(job_records),
            per_site=per_site,
            per_job_values=per_job_values,
            metadata={
                "window_mode": window_ctx.config.mode.value,
                "window_index": window_ctx.window_index,
                "window_start": window_ctx.window_start,
                "window_end": window_ctx.window_end,
                "avg_bottom_staging": avg_bottom,
                "avg_top_staging": avg_top,
                "bottom_weight": bottom_weight,
                "top_weight": top_weight,
                "tail_fraction": tail_fraction,
                "bulk_fraction": 1.0 - tail_fraction,
                "reward_formula": (
                    f"{bottom_weight}*log1p(mean(bottom {int((1-tail_fraction)*100)}%)) + "
                    f"{top_weight}*log1p(mean(top {int(tail_fraction*100)}%))"
                ),
                "lower_is_better": True,
            },
        )
