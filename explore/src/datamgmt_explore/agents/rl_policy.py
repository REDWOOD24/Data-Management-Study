from __future__ import annotations

from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

from datamgmt_explore.agents.base import BaseAgent
from datamgmt_explore.agents.policy_network import (
    PolicyNetwork,
    PolicyNetworkConfig,
    load_policy,
    observation_to_tensor,
    save_policy,
    vector_to_action,
)
from datamgmt_explore.rl_observations import write_observation_spec
from datamgmt_explore.seeds import set_torch_seed


class RlPolicyAgent(BaseAgent):
    """Minimal REINFORCE agent over the normalized action vector."""

    def __init__(
        self,
        env,
        seed: int = 0,
        *,
        hidden_dim: int = 128,
        lr: float = 1e-3,
        checkpoint_path: Path | None = None,
    ) -> None:
        super().__init__(env, seed=seed)
        set_torch_seed(self.seed)
        self.action_dim = len(self.env.action_space_spec.parameters)
        self.observation_spec = self.env.observation_spec
        self.obs_dim = self.observation_spec.obs_dim
        self.network = PolicyNetwork(
            self.action_dim,
            config=PolicyNetworkConfig(hidden_dim=hidden_dim),
            obs_dim=self.obs_dim,
        )
        self.optimizer = torch.optim.Adam(self.network.parameters(), lr=lr)
        self.checkpoint_path = checkpoint_path
        self.reward_baseline = 0.0
        self.baseline_momentum = 0.9
        self._last_log_prob: torch.Tensor | None = None
        self._last_action_vector: np.ndarray | None = None
        self._pending_observation: torch.Tensor | None = None

        if self.env.run_store is not None:
            write_observation_spec(self.env.run_store.experiment_dir, self.observation_spec)

        if checkpoint_path is not None and checkpoint_path.is_file():
            if not load_policy(self.network, checkpoint_path):
                checkpoint_path.unlink(missing_ok=True)

    def propose(self) -> dict:
        observation = observation_to_tensor(self.env.current_observation)
        action_vector, log_prob = self._sample_valid_action(observation)
        self._pending_observation = observation
        self._last_log_prob = log_prob
        self._last_action_vector = action_vector
        return vector_to_action(self.env.action_space_spec, action_vector)

    def _sample_valid_action(self, observation: torch.Tensor) -> tuple[np.ndarray, torch.Tensor]:
        for _ in range(100):
            vector, log_prob = self.network.sample(observation.unsqueeze(0))
            candidate = vector.squeeze(0)
            action = vector_to_action(
                self.env.action_space_spec,
                candidate.detach().cpu().numpy(),
            )
            if self.env.action_space_spec.validate(action):
                return candidate.detach().cpu().numpy(), log_prob.squeeze(0)

        fallback = self.env.decoder.sample(self.rng)
        vector = self.env.action_space_spec.vectorize(fallback)
        return vector, torch.tensor(0.0, requires_grad=True)

    def update(self, action: dict, reward: float, info: dict) -> None:
        super().update(action, reward, info)
        if self._last_log_prob is None:
            return

        reward_value = float(reward)
        if not np.isfinite(reward_value):
            reward_value = float(self.env.settings.failure_penalty)

        self.reward_baseline = (
            self.baseline_momentum * self.reward_baseline
            + (1.0 - self.baseline_momentum) * reward_value
        )
        advantage = self.reward_baseline - reward_value
        loss = -self._last_log_prob * advantage

        self.optimizer.zero_grad()
        loss.backward()
        nn.utils.clip_grad_norm_(self.network.parameters(), max_norm=1.0)
        self.optimizer.step()

        self._last_log_prob = None
        self._last_action_vector = None
        self._pending_observation = None

        if self.checkpoint_path is not None:
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            save_policy(self.network, self.checkpoint_path)
