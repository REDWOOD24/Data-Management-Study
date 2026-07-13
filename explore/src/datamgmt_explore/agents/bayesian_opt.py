from __future__ import annotations

from datamgmt_explore.agents.base import BaseAgent


class BayesianOptAgent(BaseAgent):
    """Optional Optuna TPE wrapper over action parameters."""

    def __init__(self, env, seed: int = 0) -> None:
        super().__init__(env, seed=seed)
        self._study = None
        self._pending_trial = None
        self._optuna_seed = self.seed

    def _ensure_study(self):
        if self._study is not None:
            return
        try:
            import optuna
        except ImportError as exc:
            raise ImportError(
                "Optuna is required for bayesian_opt agent. Install with: pip install optuna"
            ) from exc

        self._optuna = optuna
        self._study = optuna.create_study(
            direction="minimize",
            sampler=optuna.samplers.TPESampler(seed=self._optuna_seed),
        )

    def _suggest_action(self, trial) -> dict:
        action: dict = {}
        for param in self.env.action_space_spec.parameters:
            name = param.name.replace(".", "_")
            if param.kind == "bool":
                action[param.name] = trial.suggest_categorical(name, [False, True])
            elif param.kind == "enum":
                action[param.name] = trial.suggest_categorical(name, list(param.choices or ()))
            elif param.kind == "int":
                low = int(param.minimum if param.minimum is not None else 0)
                high = int(param.maximum if param.maximum is not None else low)
                action[param.name] = trial.suggest_int(name, low, high)
            elif param.kind == "float":
                low = float(param.minimum if param.minimum is not None else 0.0)
                high = float(param.maximum if param.maximum is not None else 1.0)
                action[param.name] = trial.suggest_float(name, low, high)
            else:
                action[param.name] = param.default
        return self.env.action_space_spec.apply_masks(action)

    def propose(self) -> dict:
        self._ensure_study()
        assert self._study is not None

        for _ in range(100):
            trial = self._study.ask()
            action = self._suggest_action(trial)
            if self.env.action_space_spec.validate(action):
                self._pending_trial = trial
                return action
            self._study.tell(trial, state=self._optuna.trial.TrialState.PRUNED)

        action = self.env.decoder.sample(self.rng)
        self._pending_trial = None
        return action

    def update(self, action: dict, reward: float, info: dict) -> None:
        super().update(action, reward, info)
        if self._study is None:
            return
        if self._pending_trial is not None:
            self._study.tell(self._pending_trial, reward)
            self._pending_trial = None
            return

        running = [trial for trial in self._study.trials if trial.state.name == "RUNNING"]
        if running:
            self._study.tell(running[-1], reward)
