from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch
import torch.nn as nn

from datamgmt_explore.action_space import ActionSpace


@dataclass(frozen=True)
class PolicyNetworkConfig:
    hidden_dim: int = 128
    log_std_init: float = -0.5


class PolicyNetwork(nn.Module):
    """MLP policy over normalized action vectors in [0, 1]."""

    def __init__(
        self,
        action_dim: int,
        config: PolicyNetworkConfig | None = None,
        *,
        obs_dim: int = 1,
    ) -> None:
        super().__init__()
        self.config = config or PolicyNetworkConfig()
        self.obs_dim = obs_dim
        self.action_dim = action_dim
        hidden = self.config.hidden_dim
        self.net = nn.Sequential(
            nn.Linear(obs_dim, hidden),
            nn.Tanh(),
            nn.Linear(hidden, hidden),
            nn.Tanh(),
            nn.Linear(hidden, action_dim),
        )
        self.log_std = nn.Parameter(
            torch.full((action_dim,), float(self.config.log_std_init))
        )

    def forward(self, observation: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        mean = torch.sigmoid(self.net(observation))
        std = torch.exp(self.log_std).clamp_min(1e-4)
        return mean, std

    def sample(self, observation: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        mean, std = self.forward(observation)
        dist = torch.distributions.Normal(mean, std)
        raw = dist.rsample()
        action = torch.clamp(raw, 0.0, 1.0)
        log_prob = dist.log_prob(raw).sum()
        return action, log_prob


def vector_to_action(action_space: ActionSpace, vector: np.ndarray) -> dict:
    action = action_space.from_vector(vector)
    return action_space.apply_masks(action)


def observation_from_history(reward_history: list[float]) -> torch.Tensor:
    """Legacy scalar observation from reward history (fallback only)."""
    if not reward_history:
        value = 0.0
    else:
        value = float(np.mean(reward_history[-10:]))
    return torch.tensor([value], dtype=torch.float32)


def observation_to_tensor(observation: np.ndarray) -> torch.Tensor:
    return torch.tensor(observation, dtype=torch.float32)


def save_policy(network: PolicyNetwork, path) -> None:
    torch.save(network.state_dict(), path)


def load_policy(network: PolicyNetwork, path) -> bool:
    state = torch.load(path, weights_only=True)
    first_weight = state.get("net.0.weight")
    if first_weight is not None and first_weight.shape[1] != network.obs_dim:
        return False
    network.load_state_dict(state)
    return True
