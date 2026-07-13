"""Memoryless uniform sampling baseline for method comparison."""

from __future__ import annotations

from datamgmt_explore.agents.base import BaseAgent


class RandomSearchAgent(BaseAgent):
    """Sample a valid action uniformly at random each trial (no learning)."""

    def propose(self) -> dict:
        return self.env.decoder.sample(self.rng)
