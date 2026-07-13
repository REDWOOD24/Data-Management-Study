from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from datamgmt_explore.agents.base import BaseAgent


@dataclass
class BanditArm:
    key: str
    pulls: int = 0
    total_reward: float = 0.0

    @property
    def mean_reward(self) -> float:
        if self.pulls == 0:
            return 0.0
        return self.total_reward / self.pulls


class BanditAgent(BaseAgent):
    """UCB1 over discretized proactive/reactive template combinations."""

    TEMPLATE_KEYS = (
        "reactive.remote_source_template",
        "proactive.transfer_template",
    )

    def __init__(self, env, seed: int = 0) -> None:
        super().__init__(env, seed=seed)
        self.arms: dict[str, BanditArm] = {}
        self.current_key: str | None = None
        self._initialize_arms()

    def _arm_key(self, action: dict) -> str:
        reactive = int(action["reactive.remote_source_template"])
        proactive = int(action["proactive.transfer_template"])
        return f"r{reactive}_p{proactive}"

    def _initialize_arms(self) -> None:
        space = self.env.action_space_spec
        reactive_count = len(space.reactive_source_names)
        proactive_count = len(space.proactive_template_names)
        for reactive in range(reactive_count):
            for proactive in range(proactive_count):
                key = f"r{reactive}_p{proactive}"
                self.arms[key] = BanditArm(key=key)

    def _fixed_for_arm(self, key: str) -> dict:
        reactive = int(key.split("_", 1)[0][1:])
        proactive = int(key.split("_", 1)[1][1:])
        return {
            "reactive.remote_source_template": reactive,
            "proactive.transfer_template": proactive,
        }

    def _select_arm(self) -> str:
        if not self.arms:
            self._initialize_arms()

        for key, arm in self.arms.items():
            if arm.pulls == 0:
                return key

        total_pulls = sum(arm.pulls for arm in self.arms.values())
        best_key = next(iter(self.arms))
        best_score = float("inf")
        for key, arm in self.arms.items():
            exploitation = arm.mean_reward
            exploration = np.sqrt(2.0 * np.log(max(total_pulls, 1)) / arm.pulls)
            score = exploitation - exploration
            if score < best_score:
                best_score = score
                best_key = key
        return best_key

    def propose(self) -> dict:
        key = self._select_arm()
        self.current_key = key
        fixed = self._fixed_for_arm(key)
        return self.env.decoder.sample(self.rng, fixed=fixed)

    def update(self, action: dict, reward: float, info: dict) -> None:
        super().update(action, reward, info)
        key = self._arm_key(action)
        arm = self.arms.setdefault(key, BanditArm(key=key))
        arm.pulls += 1
        arm.total_reward += reward
