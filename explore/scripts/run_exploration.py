#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXPLORE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EXPLORE_ROOT / "src"
SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from datamgmt_explore.agents.bandit import BanditAgent
from datamgmt_explore.agents.bayesian_opt import BayesianOptAgent
from datamgmt_explore.agents.random_search import RandomSearchAgent
from datamgmt_explore.agents.rl_policy import RlPolicyAgent
from datamgmt_explore.env import DataMgmtEnv, build_window_config
from datamgmt_explore.plotting.experiment_plots import plot_experiment_progress, write_failure_report
from datamgmt_explore.plotting.method_comparison_live import (
    LiveMethodComparisonPlot,
    MethodComparisonTracker,
    plot_methods_comparison_static,
)
from datamgmt_explore.plotting.method_timing_live import LiveMethodTimingCsv, MethodTimingTracker
from datamgmt_explore.plotting.trial_plots import plot_all_trials, plot_trial
from datamgmt_explore.run_store import RunStore
from datamgmt_explore.settings import load_settings
from datamgmt_explore.seeds import method_seed, method_seeds

# Sibling script: reactive transfer delta reports at trial checkpoints.
from report_reactive_transfer_deltas import maybe_run_checkpoint_report

DEFAULT_AGENTS = ("bayesian_opt", "rl_policy", "random_search")
DEFAULT_OBJECTIVE = "tail_bulk_staging_cost"
DEFAULT_AGGREGATION = "mean"
DEFAULT_SEED = 42
DEFAULT_REACTIVE_DELTA_EVERY = 5
SUPPORTED_AGENTS = ("bayesian_opt", "rl_policy", "bandit", "random_search")


@dataclass
class _MethodProgressState:
    total_trials: int
    trial_index: int = 0
    phase: str = "waiting"
    trial_start: float = 0.0
    active: bool = False
    completed: bool = False


class MethodProgressDisplay:
    """One in-place terminal line per method; redraws without adding lines each second."""

    _lock = threading.Lock()
    _methods: list[str] = []
    _states: dict[str, _MethodProgressState] = {}
    _rendered = False
    _stop = threading.Event()
    _refresh_thread: threading.Thread | None = None

    @classmethod
    def start(cls, methods: list[str], total_trials: int) -> None:
        with cls._lock:
            cls._methods = list(methods)
            cls._states = {
                method: _MethodProgressState(total_trials=total_trials)
                for method in methods
            }
            cls._rendered = False
            cls._stop.clear()
        cls._start_refresh()

    @classmethod
    def stop(cls) -> None:
        cls._stop.set()
        if cls._refresh_thread is not None:
            cls._refresh_thread.join(timeout=0.5)
            cls._refresh_thread = None
        with cls._lock:
            cls._render()

    @classmethod
    def update(cls, method: str, **kwargs: Any) -> None:
        with cls._lock:
            state = cls._states[method]
            if "phase" in kwargs and kwargs.get("trial_index", state.trial_index) == state.trial_index:
                if kwargs["phase"] == state.phase:
                    return
            for key, value in kwargs.items():
                setattr(state, key, value)
            cls._render(changed_method=method)

    @classmethod
    def _start_refresh(cls) -> None:
        if not sys.stdout.isatty():
            return

        def loop() -> None:
            while not cls._stop.wait(1.0):
                with cls._lock:
                    if cls._should_refresh():
                        cls._render()

        cls._refresh_thread = threading.Thread(target=loop, daemon=True, name="progress-refresh")
        cls._refresh_thread.start()

    @classmethod
    def _should_refresh(cls) -> bool:
        return any(
            state.active and not state.completed and state.phase
            for state in cls._states.values()
        )

    @classmethod
    def _format_line(cls, method: str) -> str:
        state = cls._states[method]
        if state.completed:
            return f"[{method}] completed"
        if not state.active:
            return f"[{method}] waiting..."
        elapsed = time.perf_counter() - state.trial_start if state.trial_start else 0.0
        return (
            f"[{method}] trial {state.trial_index + 1}/{state.total_trials} "
            f"| {state.phase} | {elapsed:.1f}s"
        )

    @classmethod
    def _render(cls, *, changed_method: str | None = None) -> None:
        if not sys.stdout.isatty():
            if changed_method is not None:
                print(cls._format_line(changed_method), flush=True)
            return

        lines = [cls._format_line(method) for method in cls._methods]
        if not cls._rendered:
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()
            cls._rendered = True
            return

        line_count = len(lines)
        sys.stdout.write(f"\033[{line_count}F")
        for line in lines:
            sys.stdout.write("\r\033[K" + line + "\n")
        sys.stdout.flush()


class TrialProgressReporter:
    """Update shared multi-line progress display for one exploration method."""

    def __init__(self, method: str) -> None:
        self.method = method

    def begin_trial(self, trial_index: int) -> None:
        MethodProgressDisplay.update(
            self.method,
            trial_index=trial_index,
            trial_start=time.perf_counter(),
            phase="",
            active=True,
        )

    def set_phase(self, trial_index: int, phase: str) -> None:
        MethodProgressDisplay.update(
            self.method,
            trial_index=trial_index,
            phase=phase,
        )

    def end_trial(self) -> None:
        MethodProgressDisplay.update(self.method)

    @staticmethod
    def mark_completed(method: str) -> None:
        MethodProgressDisplay.update(method, completed=True, active=False)


@dataclass(frozen=True)
class TrialUpdate:
    method: str
    trial_index: int
    trial_dir: Path
    reward: float
    sim_sec: float
    explore_sec: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run exploration methods in parallel with live comparison plotting. "
            "Default: bayesian_opt, rl_policy, and random_search."
        ),
    )
    parser.add_argument("--settings", default=str(EXPLORE_ROOT / "config" / "settings.yaml"))
    parser.add_argument(
        "--agents",
        default=",".join(DEFAULT_AGENTS),
        help=f"Comma-separated methods (default: {','.join(DEFAULT_AGENTS)})",
    )
    parser.add_argument(
        "--agent",
        default=None,
        help="Deprecated single-method alias for --agents",
    )
    parser.add_argument("--trials", type=int, default=50)
    parser.add_argument("--window-mode", default="full", choices=["full", "time", "job_count", "per_job"])
    parser.add_argument("--window-size", type=float, default=None)
    parser.add_argument("--window-stride", type=float, default=None)
    parser.add_argument("--window-anchor", default="sim_start", choices=["sim_start", "last_window"])
    parser.add_argument(
        "--aggregation",
        default=DEFAULT_AGGREGATION,
        choices=["mean", "mean_of_site_means", "max_site_mean"],
    )
    parser.add_argument("--experiment-name", default=None)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Base random seed for reproducible runs")
    parser.add_argument("--max-jobs", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-plot",
        action="store_true",
        help="Disable per-trial plots and method comparison plot",
    )
    parser.add_argument(
        "--reactive-delta-every",
        type=int,
        default=DEFAULT_REACTIVE_DELTA_EVERY,
        help=(
            "After every N completed trials per method (default: 10), write a "
            "reactive-transfer delta report over the 3×N (or M×N) finished trials. "
            "Set 0 to disable."
        ),
    )
    parser.add_argument(
        "--enable-drop-in-transfers",
        action="store_true",
        help=(
            "Copy drop_in_transfers.json into each trial config and reference it "
            "from data_policy_config.json."
        ),
    )
    parser.add_argument(
        "--drop-in-transfers-file",
        default=None,
        help=(
            "Path to drop_in_transfers.json. Default: drop_in_transfers_file "
            "from base_policy when --enable-drop-in-transfers is set."
        ),
    )
    return parser.parse_args()


def parse_agents(args: argparse.Namespace) -> list[str]:
    if args.agent:
        if args.agents != ",".join(DEFAULT_AGENTS):
            raise SystemExit("Use either --agent or --agents, not both with conflicting values.")
        print("DEPRECATED: --agent is deprecated. Use --agents instead.", file=sys.stderr)
        raw = args.agent
    else:
        raw = args.agents
    methods = [item.strip() for item in raw.split(",") if item.strip()]
    if not methods:
        raise SystemExit("At least one agent must be specified.")
    unknown = [method for method in methods if method not in SUPPORTED_AGENTS]
    if unknown:
        raise SystemExit(f"Unknown agents: {', '.join(unknown)}")
    return methods


def build_agent(name: str, env: DataMgmtEnv, seed: int, method_dir: Path):
    if name == "bayesian_opt":
        return BayesianOptAgent(env, seed=seed)
    if name == "rl_policy":
        return RlPolicyAgent(env, seed=seed, checkpoint_path=method_dir / "policy.pt")
    if name == "bandit":
        return BanditAgent(env, seed=seed)
    if name == "random_search":
        return RandomSearchAgent(env, seed=seed)
    raise ValueError(f"Unknown agent: {name}")


def default_experiment_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"explore_{stamp}"


def resolve_drop_in_transfers_file(args: argparse.Namespace, settings) -> None:
    if not args.enable_drop_in_transfers:
        return

    if args.drop_in_transfers_file:
        candidate = settings.resolve(args.drop_in_transfers_file)
    else:
        with settings.base_policy.open(encoding="utf-8") as handle:
            base_policy = json.load(handle)
        rel_path = (
            base_policy.get("Data_Management_Policy", {}).get("drop_in_transfers_file")
        )
        if not rel_path:
            raise SystemExit(
                "Base policy has no drop_in_transfers_file; pass --drop-in-transfers-file."
            )
        candidate = (settings.base_policy.parent / str(rel_path)).resolve()

    if not candidate.is_file():
        raise SystemExit(f"Drop-in transfers file not found: {candidate}")
    settings.drop_in_transfers_file = candidate


def build_run_config(args: argparse.Namespace, settings, methods: list[str]) -> dict:
    seeds = method_seeds(args.seed, methods)
    return {
        "agents": methods,
        "seed": args.seed,
        "method_seeds": seeds,
        "trials": args.trials,
        "objective": DEFAULT_OBJECTIVE,
        "aggregation": args.aggregation,
        "window_mode": args.window_mode,
        "window_size": args.window_size,
        "window_stride": args.window_stride,
        "window_anchor": args.window_anchor,
        "max_jobs": args.max_jobs,
        "dry_run": args.dry_run,
        "plot_enabled": not args.no_plot,
        "live_plot_enabled": not args.no_plot,
        "reactive_delta_every": args.reactive_delta_every,
        "drop_in_transfers_enabled": args.enable_drop_in_transfers,
        "drop_in_transfers_file": (
            str(settings.drop_in_transfers_file)
            if settings.drop_in_transfers_file is not None
            else None
        ),
        "settings": str(args.settings),
        "cg_sim_bin": str(settings.cg_sim_bin),
    }


def run_method(
    agent_name: str,
    method_dir: Path,
    trials: int,
    base_seed: int,
    base_kwargs: dict[str, Any],
    update_queue: queue.Queue[TrialUpdate | None],
) -> dict[str, Any]:
    seed = method_seed(base_seed, agent_name)
    method_kwargs = dict(base_kwargs)
    method_kwargs["experiment_dir"] = method_dir
    method_kwargs["max_trials"] = trials
    progress = TrialProgressReporter(agent_name)
    method_kwargs["progress_callback"] = progress.set_phase
    env = DataMgmtEnv(**method_kwargs)
    agent = build_agent(agent_name, env, seed=seed, method_dir=method_dir)
    env.reset(seed=seed)

    results: list[dict[str, Any]] = []
    for _ in range(trials):
        trial_index = env.trial_index
        progress.begin_trial(trial_index)
        progress.set_phase(trial_index, "exploration")

        propose_start = time.perf_counter()
        action = agent.propose()
        propose_sec = time.perf_counter() - propose_start

        _, reward, _, _, info = env.step(action)

        update_start = time.perf_counter()
        agent.update(action, float(reward), info)
        update_sec = time.perf_counter() - update_start
        progress.end_trial()

        sim_sec = float(info.get("sim_elapsed_sec", 0.0))
        explore_sec = propose_sec + float(info.get("exploration_elapsed_sec", 0.0)) + update_sec

        results.append({"action": action, "reward": float(reward), "info": info})
        update_queue.put(
            TrialUpdate(
                method=agent_name,
                trial_index=int(info["trial_index"]),
                trial_dir=Path(info["trial_dir"]),
                reward=float(reward),
                sim_sec=sim_sec,
                explore_sec=explore_sec,
            )
        )

    summary_path = env.run_store.write_summary() if env.run_store else None
    TrialProgressReporter.mark_completed(agent_name)
    return {
        "method": agent_name,
        "results": results,
        "summary_path": summary_path,
        "method_dir": method_dir,
    }


def drain_trial_updates(
    update_queue: queue.Queue[TrialUpdate | None],
    tracker: MethodComparisonTracker,
    plotter: LiveMethodComparisonPlot | None,
    timing_tracker: MethodTimingTracker,
    timing_csv: LiveMethodTimingCsv | None,
    *,
    settings=None,
    plot_trials: bool = False,
    experiment_dir: Path | None = None,
    methods: list[str] | None = None,
    reactive_delta_every: int = DEFAULT_REACTIVE_DELTA_EVERY,
    reactive_delta_fired: set[int] | None = None,
    dry_run: bool = False,
) -> int:
    drained = 0
    while True:
        try:
            update = update_queue.get_nowait()
        except queue.Empty:
            break
        if update is None:
            continue
        drained += 1
        timing_tracker.set(update.method, update.trial_index, update.sim_sec, update.explore_sec)
        if timing_csv is not None:
            timing_csv.write(timing_tracker)
        if plot_trials and settings is not None:
            events_db = update.trial_dir / "events.db"
            if events_db.is_file():
                plot_trial(
                    events_db,
                    update.trial_dir / "plots",
                    repo_root=settings.repo_root,
                )
        if plotter is not None:
            point = plotter.load_point_from_trial(update.method, update.trial_dir)
            if point is not None:
                tracker.add(update.method, point.bars)
                plotter.update(tracker)

        if (
            not dry_run
            and experiment_dir is not None
            and methods is not None
            and reactive_delta_fired is not None
        ):
            report_dir = maybe_run_checkpoint_report(
                experiment_dir,
                methods,
                timing_tracker,
                trial_index=update.trial_index,
                every_n=reactive_delta_every,
                fired_checkpoints=reactive_delta_fired,
            )
            if report_dir is not None:
                print(f"Reactive transfer delta report: {report_dir}")
    return drained


def main() -> int:
    args = parse_args()
    methods = parse_agents(args)
    settings = load_settings(args.settings)
    resolve_drop_in_transfers_file(args, settings)
    experiment_name = args.experiment_name or default_experiment_name()
    experiment_dir = settings.runs_dir / experiment_name
    methods_root = experiment_dir / "methods"
    methods_root.mkdir(parents=True, exist_ok=True)

    run_config = build_run_config(args, settings, methods)
    RunStore(experiment_dir).write_run_config(run_config)
    seeds = run_config["method_seeds"]
    print(f"Experiment: {experiment_name}")
    print(f"Base seed: {args.seed}")
    if settings.drop_in_transfers_file is not None:
        print(f"Drop-in transfers: {settings.drop_in_transfers_file}")
    for method_name in methods:
        print(f"  {method_name}: seed {seeds[method_name]}")

    window_config = build_window_config(
        args.window_mode,
        size=args.window_size,
        stride=args.window_stride,
        anchor=args.window_anchor,
    )

    base_kwargs = {
        "settings": settings,
        "window_config": window_config,
        "objective_name": DEFAULT_OBJECTIVE,
        "aggregation": args.aggregation,
        "max_jobs": args.max_jobs,
        "dry_run": args.dry_run,
        # Per-trial plots run on the main thread (matplotlib is not thread-safe).
        "plot_enabled": False,
    }

    plot_trials = not args.no_plot and not args.dry_run

    update_queue: queue.Queue[TrialUpdate | None] = queue.Queue()
    tracker = MethodComparisonTracker(methods=methods)
    timing_tracker = MethodTimingTracker(methods=methods, max_trials=args.trials)
    plotter: LiveMethodComparisonPlot | None = None
    timing_csv: LiveMethodTimingCsv | None = None
    if not args.no_plot and not args.dry_run:
        plotter = LiveMethodComparisonPlot(
            experiment_dir,
            methods,
            repo_root=settings.repo_root,
            max_trials=args.trials,
        )
        timing_csv = LiveMethodTimingCsv(
            experiment_dir,
            methods,
            max_trials=args.trials,
        )

    method_outputs: list[dict[str, Any]] = []
    reactive_delta_fired: set[int] = set()

    MethodProgressDisplay.start(methods, args.trials)
    try:
        with ThreadPoolExecutor(max_workers=len(methods)) as executor:
            futures = {
                executor.submit(
                    run_method,
                    agent_name,
                    methods_root / agent_name,
                    args.trials,
                    args.seed,
                    base_kwargs,
                    update_queue,
                ): agent_name
                for agent_name in methods
            }
            remaining = set(futures.keys())
            while remaining:
                done, remaining = wait_any(remaining)
                drain_trial_updates(
                    update_queue,
                    tracker,
                    plotter,
                    timing_tracker,
                    timing_csv,
                    settings=settings,
                    plot_trials=plot_trials,
                    experiment_dir=experiment_dir,
                    methods=methods,
                    reactive_delta_every=args.reactive_delta_every,
                    reactive_delta_fired=reactive_delta_fired,
                    dry_run=args.dry_run,
                )
                for future in done:
                    agent_name = futures[future]
                    method_outputs.append(future.result())
    finally:
        MethodProgressDisplay.stop()

    drain_trial_updates(
        update_queue,
        tracker,
        plotter,
        timing_tracker,
        timing_csv,
        settings=settings,
        plot_trials=plot_trials,
        experiment_dir=experiment_dir,
        methods=methods,
        reactive_delta_every=args.reactive_delta_every,
        reactive_delta_fired=reactive_delta_fired,
        dry_run=args.dry_run,
    )

    if plotter is not None:
        plotter.update(tracker)
        plotter.close()
    elif not args.no_plot and not args.dry_run:
        plot_methods_comparison_static(
            experiment_dir,
            methods,
            repo_root=settings.repo_root,
            max_trials=args.trials,
        )
    if timing_csv is not None:
        timing_csv.write(timing_tracker)

    if not args.no_plot and not args.dry_run:
        for method_name in methods:
            method_dir = methods_root / method_name
            plot_all_trials(method_dir, repo_root=settings.repo_root)
            for path in plot_experiment_progress(method_dir):
                print(f"{method_name} plot: {path}")
        failure_report = write_failure_report(experiment_dir)
        print(f"Failure report: {failure_report}")

    print(f"Experiment: {experiment_name}")
    print(f"Base seed: {args.seed}")
    print(f"Methods: {', '.join(methods)}")
    print(f"Trials per method: {args.trials}")
    for output in method_outputs:
        results = output["results"]
        best = min(results, key=lambda item: item["reward"]) if results else None
        print(f"  {output['method']}: completed {len(results)} trials")
        if best:
            print(f"    best reward: {best['reward']:.6f} (lower is better)")
        if output["summary_path"]:
            print(f"    summary: {output['summary_path']}")
    comparison_path = experiment_dir / "plots" / "methods_comparison.png"
    if comparison_path.is_file():
        print(f"Comparison plot: {comparison_path}")
    timing_path = experiment_dir / "plots" / "methods_timing.csv"
    if timing_path.is_file():
        print(f"Timing CSV: {timing_path}")
    return 0


def wait_any(remaining: set):
    return wait(remaining, timeout=0.3, return_when=FIRST_COMPLETED)


if __name__ == "__main__":
    raise SystemExit(main())
