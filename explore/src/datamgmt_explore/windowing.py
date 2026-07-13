from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from datamgmt_explore.metrics import JobRecord


class WindowMode(str, Enum):
    FULL = "full"
    TIME = "time"
    JOB_COUNT = "job_count"
    PER_JOB = "per_job"


class WindowAnchor(str, Enum):
    SIM_START = "sim_start"
    LAST_WINDOW = "last_window"


@dataclass(frozen=True)
class WindowConfig:
    mode: WindowMode = WindowMode.FULL
    size: float | None = None
    stride: float | None = None
    anchor: WindowAnchor = WindowAnchor.SIM_START
    start_time: float | None = None


@dataclass(frozen=True)
class WindowContext:
    config: WindowConfig
    window_index: int = 0
    window_start: float | None = None
    window_end: float | None = None


class EvaluationWindow:
    def __init__(self, config: WindowConfig) -> None:
        self.config = config

    def select(self, records: list[JobRecord]) -> list[JobRecord]:
        if not records:
            return []

        mode = self.config.mode
        if mode == WindowMode.FULL:
            return list(records)
        if mode == WindowMode.PER_JOB:
            return list(records)
        if mode == WindowMode.JOB_COUNT:
            size = int(self.config.size or 1)
            ordered = sorted(records, key=lambda item: item.end_time)
            return ordered[-size:]
        if mode == WindowMode.TIME:
            return self._select_time_window(records)
        raise ValueError(f"Unsupported window mode: {mode}")

    def iter_windows(self, records: list[JobRecord]) -> Iterable[tuple[WindowContext, list[JobRecord]]]:
        if self.config.mode != WindowMode.TIME or self.config.stride is None:
            context = WindowContext(config=self.config)
            yield context, self.select(records)
            return

        if not records:
            yield WindowContext(config=self.config), []
            return

        size = float(self.config.size or 0.0)
        stride = float(self.config.stride or size)
        sim_start = min(record.exec_start_time for record in records)
        sim_end = max(record.exec_start_time for record in records)

        start = self.config.start_time
        if start is None:
            start = sim_start if self.config.anchor == WindowAnchor.SIM_START else sim_end - size

        index = 0
        while start < sim_end:
            end = start + size
            window_records = [
                record
                for record in records
                if start <= record.exec_start_time < end
            ]
            context = WindowContext(
                config=self.config,
                window_index=index,
                window_start=start,
                window_end=end,
            )
            yield context, window_records
            start += stride
            index += 1

    def _select_time_window(self, records: list[JobRecord]) -> list[JobRecord]:
        size = float(self.config.size or 0.0)
        if self.config.start_time is not None:
            start = float(self.config.start_time)
        elif self.config.anchor == WindowAnchor.LAST_WINDOW and records:
            start = max(record.exec_start_time for record in records) - size
        elif records:
            start = min(record.exec_start_time for record in records)
        else:
            start = 0.0
        end = start + size
        return [
            record
            for record in records
            if start <= record.exec_start_time < end
        ]
