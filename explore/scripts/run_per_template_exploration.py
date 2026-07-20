#!/usr/bin/env python3
"""Run separate explorations for each proactive policy template.

Creates a parent experiment directory with one child run per template, each using
a narrowed action space that pins ``proactive.transfer_template`` and only exposes
that template's searchable parameters (plus shared reactive / mode knobs).

Layout::

    explore/runs/{experiment-name}/
      manifest.json
      action_spaces/{template}.yaml
      drop_in_transfers.json          # optional, shared
      storage_rebalance/              # full run_exploration output
      network_aware_rebalance/
      hotset_replication/
      job_input_prefetch/
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

EXPLORE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EXPLORE_ROOT / "src"
SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from datamgmt_explore.settings import load_settings  # noqa: E402

DEFAULT_AGENTS = "bayesian_opt,rl_policy,random_search"
DEFAULT_OBJECTIVE = "avg_staging_time"
DEFAULT_SEED = 42
DEFAULT_TRIALS = 50
BASE_ACTION_SPACE = EXPLORE_ROOT / "config" / "action_space.yaml"

# Shared knobs kept in every per-template search space.
SHARED_PARAM_KEYS = (
    "reactive.prefer_local_replica",
    "reactive.remote_source_template",
    "reactive.random_seed",
    "proactive.data_transfer_mode",
    "proactive.transfer_template",
)

# Templates that also search site-staging bias.
BIAS_TEMPLATES = frozenset({"hotset_replication", "job_input_prefetch"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare and/or run per-template explorations under one parent "
            "experiment folder."
        ),
    )
    parser.add_argument("--settings", default=str(EXPLORE_ROOT / "config" / "settings.yaml"))
    parser.add_argument(
        "--experiment-name",
        default=None,
        help="Parent experiment folder name under runs/. Default: explore_per_template_<UTC>.",
    )
    parser.add_argument(
        "--templates",
        default="all",
        help=(
            "Comma-separated template names/indices, or 'all'. "
            "Names: storage_rebalance, network_aware_rebalance, "
            "hotset_replication, job_input_prefetch."
        ),
    )
    parser.add_argument("--agents", default=DEFAULT_AGENTS)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--objective", default=DEFAULT_OBJECTIVE)
    parser.add_argument("--aggregation", default="mean")
    parser.add_argument("--window-mode", default="full")
    parser.add_argument("--window-size", type=float, default=None)
    parser.add_argument("--window-stride", type=float, default=None)
    parser.add_argument("--window-anchor", default="sim_start")
    parser.add_argument("--max-jobs", type=int, default=None)
    parser.add_argument("--reactive-delta-every", type=int, default=5)
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--enable-drop-in-transfers", action="store_true")
    parser.add_argument("--drop-in-transfers-file", default=None)
    parser.add_argument(
        "--base-action-space",
        default=str(BASE_ACTION_SPACE),
        help="Full action_space.yaml used as the source for narrowing.",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Write narrowed action spaces + manifest and print commands; do not run.",
    )
    parser.add_argument(
        "--print-commands",
        action="store_true",
        help="Print the per-template run_exploration commands (also implied by --prepare-only).",
    )
    return parser.parse_args()


def default_experiment_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"explore_per_template_{stamp}"


def load_base_action_space(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"Invalid action space YAML: {path}")
    return raw


def resolve_templates(raw: str, template_names: list[str]) -> list[tuple[int, str]]:
    text = raw.strip().lower()
    if text in {"all", "*"}:
        return list(enumerate(template_names))

    selected: list[tuple[int, str]] = []
    seen: set[str] = set()
    for token in raw.split(","):
        item = token.strip()
        if not item:
            continue
        if item.isdigit():
            index = int(item)
            if index < 0 or index >= len(template_names):
                raise SystemExit(f"Unknown template index: {item}")
            name = template_names[index]
        else:
            if item not in template_names:
                raise SystemExit(
                    f"Unknown template '{item}'. Expected one of: {', '.join(template_names)}"
                )
            name = item
            index = template_names.index(name)
        if name in seen:
            continue
        seen.add(name)
        selected.append((index, name))
    if not selected:
        raise SystemExit("No templates selected.")
    return selected


def _constraints_for_template(constraints: list[dict[str, Any]], template: str) -> list[dict[str, Any]]:
    keep: list[dict[str, Any]] = []
    for constraint in constraints:
        params = constraint.get("params") or []
        if not params:
            keep.append(constraint)
            continue
        # Keep only constraints whose params all belong to this template prefix
        # or to shared (non-template) keys.
        ok = True
        for param in params:
            if "." not in str(param):
                continue
            prefix = str(param).split(".", 1)[0]
            if prefix in {
                "storage_rebalance",
                "network_aware_rebalance",
                "hotset_replication",
                "job_input_prefetch",
            } and prefix != template:
                ok = False
                break
        if ok and any(str(p).startswith(f"{template}.") for p in params):
            keep.append(constraint)
    return keep


def build_narrowed_action_space(
    base: dict[str, Any],
    *,
    template_index: int,
    template_name: str,
) -> dict[str, Any]:
    narrowed = deepcopy(base)
    parameters = narrowed.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise SystemExit("action_space.yaml parameters must be a mapping")

    keep_keys = set(SHARED_PARAM_KEYS)
    if template_name in BIAS_TEMPLATES:
        keep_keys.add("proactive.site_staging_bias")

    for name, spec in list(parameters.items()):
        if name in keep_keys:
            continue
        if isinstance(spec, dict) and spec.get("template") == template_name:
            keep_keys.add(name)
            continue

    narrowed["parameters"] = {
        key: deepcopy(parameters[key]) for key in parameters if key in keep_keys
    }

    transfer = narrowed["parameters"]["proactive.transfer_template"]
    transfer["min"] = template_index
    transfer["max"] = template_index
    transfer["default"] = template_index

    # Preserve full template name list so plugin indices remain canonical.
    # Constraints only for the active template.
    narrowed["constraints"] = _constraints_for_template(
        list(narrowed.get("constraints") or []),
        template_name,
    )
    narrowed["narrowed_for_template"] = {
        "index": template_index,
        "name": template_name,
    }
    return narrowed


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False)


def build_run_command(
    *,
    args: argparse.Namespace,
    parent_name: str,
    template_name: str,
    action_space_path: Path,
    drop_in_path: Path | None,
) -> list[str]:
    child_name = f"{parent_name}/{template_name}"
    cmd = [
        sys.executable,
        str(SCRIPTS_ROOT / "run_exploration.py"),
        "--settings",
        str(args.settings),
        "--experiment-name",
        child_name,
        "--action-space",
        str(action_space_path),
        "--agents",
        args.agents,
        "--trials",
        str(args.trials),
        "--seed",
        str(args.seed),
        "--objective",
        args.objective,
        "--aggregation",
        args.aggregation,
        "--window-mode",
        args.window_mode,
        "--window-anchor",
        args.window_anchor,
        "--reactive-delta-every",
        str(args.reactive_delta_every),
    ]
    if args.window_size is not None:
        cmd.extend(["--window-size", str(args.window_size)])
    if args.window_stride is not None:
        cmd.extend(["--window-stride", str(args.window_stride)])
    if args.max_jobs is not None:
        cmd.extend(["--max-jobs", str(args.max_jobs)])
    if args.no_plot:
        cmd.append("--no-plot")
    if args.dry_run:
        cmd.append("--dry-run")
    if args.enable_drop_in_transfers:
        cmd.append("--enable-drop-in-transfers")
        if drop_in_path is not None:
            cmd.extend(["--drop-in-transfers-file", str(drop_in_path)])
        elif args.drop_in_transfers_file:
            cmd.extend(["--drop-in-transfers-file", str(args.drop_in_transfers_file)])
    return cmd


def resolve_shared_drop_in(
    args: argparse.Namespace,
    settings,
    parent_dir: Path,
) -> Path | None:
    if not args.enable_drop_in_transfers:
        return None

    if args.drop_in_transfers_file:
        source = settings.resolve(args.drop_in_transfers_file)
    else:
        with settings.base_policy.open(encoding="utf-8") as handle:
            base_policy = json.load(handle)
        rel = (
            base_policy.get("Data_Management_Policy", {}).get("drop_in_transfers_file")
        )
        if not rel:
            raise SystemExit(
                "Base policy has no drop_in_transfers_file; pass --drop-in-transfers-file."
            )
        source = (settings.base_policy.parent / str(rel)).resolve()

    if not source.is_file():
        raise SystemExit(f"Drop-in transfers file not found: {source}")

    dest = parent_dir / "drop_in_transfers.json"
    if source.resolve() != dest.resolve():
        shutil.copy2(source, dest)
    return dest


def format_command(cmd: list[str]) -> str:
    parts: list[str] = []
    for part in cmd:
        if any(ch.isspace() for ch in part):
            parts.append(f'"{part}"')
        else:
            parts.append(part)
    return " \\\n  ".join(parts)


def main() -> int:
    args = parse_args()
    settings = load_settings(args.settings)
    parent_name = args.experiment_name or default_experiment_name()
    parent_dir = settings.runs_dir / parent_name
    parent_dir.mkdir(parents=True, exist_ok=True)

    base_path = Path(args.base_action_space)
    if not base_path.is_absolute():
        base_path = settings.resolve(base_path)
    base = load_base_action_space(base_path)
    template_names = list(base.get("proactive_template_names") or [])
    if not template_names:
        raise SystemExit(f"No proactive_template_names in {base_path}")

    selected = resolve_templates(args.templates, template_names)
    action_spaces_dir = parent_dir / "action_spaces"
    action_spaces_dir.mkdir(parents=True, exist_ok=True)

    drop_in_path = resolve_shared_drop_in(args, settings, parent_dir)

    runs: list[dict[str, Any]] = []
    commands: list[list[str]] = []

    for template_index, template_name in selected:
        narrowed = build_narrowed_action_space(
            base,
            template_index=template_index,
            template_name=template_name,
        )
        action_space_path = action_spaces_dir / f"{template_name}.yaml"
        write_yaml(action_space_path, narrowed)

        searchable = sorted(
            key
            for key in narrowed["parameters"]
            if key != "proactive.transfer_template"
        )
        cmd = build_run_command(
            args=args,
            parent_name=parent_name,
            template_name=template_name,
            action_space_path=action_space_path,
            drop_in_path=drop_in_path,
        )
        commands.append(cmd)
        runs.append(
            {
                "template_index": template_index,
                "template_name": template_name,
                "action_space": str(action_space_path),
                "experiment_name": f"{parent_name}/{template_name}",
                "experiment_dir": str(parent_dir / template_name),
                "searchable_parameters": searchable,
                "pinned_transfer_template": template_index,
                "command": cmd,
            }
        )

    manifest = {
        "experiment_name": parent_name,
        "parent_dir": str(parent_dir),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_action_space": str(base_path),
        "objective": args.objective,
        "agents": args.agents,
        "trials": args.trials,
        "seed": args.seed,
        "drop_in_transfers_enabled": bool(args.enable_drop_in_transfers),
        "drop_in_transfers_file": str(drop_in_path) if drop_in_path else None,
        "templates": runs,
    }
    manifest_path = parent_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"Parent experiment: {parent_name}")
    print(f"Parent dir: {parent_dir}")
    print(f"Manifest: {manifest_path}")
    for run in runs:
        print(
            f"  - {run['template_name']}: pinned template={run['pinned_transfer_template']}, "
            f"{len(run['searchable_parameters'])} searchable params"
        )

    if args.prepare_only or args.print_commands:
        print("\n# Per-template commands:")
        for cmd in commands:
            print()
            print(format_command(cmd))

    if args.prepare_only:
        return 0

    for run, cmd in zip(runs, commands):
        print(f"\n=== Running template: {run['template_name']} ===")
        print(format_command(cmd))
        result = subprocess.run(cmd, cwd=str(EXPLORE_ROOT))
        if result.returncode != 0:
            print(
                f"Template {run['template_name']} failed with exit code {result.returncode}",
                file=sys.stderr,
            )
            return int(result.returncode)

    print(f"\nAll template runs finished under: {parent_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
