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

    def __init__(
        self,
        bottom_weight: float = TAIL_BULK_BOTTOM_WEIGHT,
        top_weight: float = TAIL_BULK_TOP_WEIGHT,
        tail_fraction: float = STAGING_TAIL_FRACTION,
    ) -> None:
        self.bottom_weight = float(bottom_weight)
        self.top_weight = float(top_weight)
        self.tail_fraction = float(tail_fraction)

    def compute(
        self,
        job_records: list[JobRecord],
        window_ctx: WindowContext,
        *,
        aggregation: str = "mean",
        reward_transform: str = "neg_log1p",
        bottom_weight: float | None = None,
        top_weight: float | None = None,
        tail_fraction: float | None = None,
    ) -> ObjectiveResult:
        del aggregation, reward_transform

        bottom_w = self.bottom_weight if bottom_weight is None else float(bottom_weight)
        top_w = self.top_weight if top_weight is None else float(top_weight)
        tail_f = self.tail_fraction if tail_fraction is None else float(tail_fraction)

        avg_bottom, avg_top, cost = compute_tail_bulk_staging_cost(
            job_records,
            bottom_weight=bottom_w,
            top_weight=top_w,
            tail_fraction=tail_f,
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
                "bottom_weight": bottom_w,
                "top_weight": top_w,
                "tail_fraction": tail_f,
                "bulk_fraction": 1.0 - tail_f,
                "reward_formula": (
                    f"{bottom_w}*log1p(mean(bottom {int((1 - tail_f) * 100)}%)) + "
                    f"{top_w}*log1p(mean(top {int(tail_f * 100)}%))"
                ),
                "lower_is_better": True,
            },
        )
