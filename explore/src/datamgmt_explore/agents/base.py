from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from datamgmt_explore.env import DataMgmtEnv


class BaseAgent(ABC):
    def __init__(self, env: DataMgmtEnv, seed: int = 0) -> None:
        self.env = env
        self.seed = int(seed)
        self.rng = np.random.default_rng(self.seed)
        self.history: list[dict[str, Any]] = []

    @abstractmethod
    def propose(self) -> dict[str, Any]:
        raise NotImplementedError

    def update(self, action: dict[str, Any], reward: float, info: dict[str, Any]) -> None:
        self.history.append({"action": action, "reward": reward, "info": info})

    def run(self, trials: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        self.env.reset(seed=self.seed)
        for _ in range(trials):
            action = self.propose()
            _, reward, _, _, info = self.env.step(action)
            self.update(action, float(reward), info)
            results.append({"action": action, "reward": float(reward), "info": info})
        return results
