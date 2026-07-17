#!/usr/bin/env python3
"""Run matched CGSim comparisons with and without drop-ins for one trial."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXPLORE_SRC = REPO / "explore" / "src"
if str(EXPLORE_SRC) not in sys.path:
    sys.path.insert(0, str(EXPLORE_SRC))

CGSIM = Path("/Users/kaun-chiehhsu/Documents/projects/redwood/CGSim/build/cg-sim")
EMIT = REPO / "explore" / "scripts" / "emit_drop_in_transfers.py"
VENV_PY = REPO / "explore" / ".venv" / "bin" / "python"


def parse_log_stats(log_path: Path) -> dict:
    pending: list[int] = []
    jobs = 0
    sim_t = None
    wall_last = None
    if not log_path.is_file():
        return {
            "pending_max": None,
            "pending_last": None,
            "jobs_dispatched": 0,
            "sim_time_last": None,
            "wall_last": None,
            "log_bytes": 0,
        }
    text = log_path.read_text(errors="ignore")
    for line in text.splitlines():
        m = re.search(r"Pending activities: (\d+)", line, re.I)
        if m:
            pending.append(int(m.group(1)))
        m = re.search(r"Dispatched: (\d+) / 966", line)
        if m:
            jobs = max(jobs, int(m.group(1)))
        m = re.search(r"(\d+) / 966 jobs dispatched", line)
        if m:
            jobs = max(jobs, int(m.group(1)))
        m = re.search(r"Sim time: ([0-9.]+)", line)
        if m:
            sim_t = float(m.group(1))
        m = re.search(r"Current Simulated Time: ([0-9.]+)", line)
        if m:
            sim_t = float(m.group(1))
        m = re.search(r"\[status\] wall time: ([0-9.]+)s", line)
        if m:
            wall_last = float(m.group(1))
    return {
        "pending_max": max(pending) if pending else None,
        "pending_last": pending[-1] if pending else None,
        "jobs_dispatched": jobs,
        "sim_time_last": sim_t,
        "wall_last": wall_last,
        "log_bytes": len(text.encode("utf-8", errors="ignore")),
    }


def setup_harness(trial_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = out_dir / "config"
    cfg.mkdir(exist_ok=True)
    for name in ("config.json", "data_policy_config.json", "site_topology.json", "site_connections.json"):
        shutil.copy2(trial_dir / "config" / name, cfg / name)
    shutil.copy2(trial_dir / "jobs.csv", out_dir / "jobs.csv")


def run_sim(run_dir: Path, *, label: str, timeout_sec: int) -> dict:
    cfg = run_dir / "config"
    for name in ("events.db", "events.db-wal", "events.db-shm", "cgsim.log", "timing.txt"):
        p = run_dir / name
        if p.exists():
            p.unlink()
    log_path = run_dir / "cgsim.log"
    timing_path = run_dir / "timing.txt"
    cmd = ["/usr/bin/time", "-p", "-o", str(timing_path), str(CGSIM), "-c", "config.json"]
    start = time.perf_counter()
    try:
        with log_path.open("w", encoding="utf-8") as log_handle:
            completed = subprocess.run(
                cmd,
                cwd=cfg,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                timeout=timeout_sec,
                check=False,
            )
        elapsed = time.perf_counter() - start
        timed_out = False
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - start
        completed = None
        timed_out = True
    stats = parse_log_stats(log_path)
    timing_text = timing_path.read_text() if timing_path.exists() else ""
    real_sec = None
    m = re.search(r"^real (.+)$", timing_text, re.M)
    if m:
        real_sec = float(m.group(1))
    return {
        "label": label,
        "wall_sec": real_sec if real_sec is not None else round(elapsed, 1),
        "returncode": None if completed is None else completed.returncode,
        "timed_out": timed_out,
        "events_db_exists": (run_dir / "events.db").is_file(),
        "events_db_bytes": (run_dir / "events.db").stat().st_size if (run_dir / "events.db").is_file() else 0,
        **stats,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trial-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--timeout-sec", type=int, default=3600)
    parser.add_argument("--only", choices=("baseline", "dropin", "both"), default="both")
    args = parser.parse_args()

    trial_dir = args.trial_dir.resolve()
    out_dir = args.out_dir.resolve()
    setup_harness(trial_dir, out_dir)

    baseline_dir = out_dir / "no_dropin"
    dropin_dir = out_dir / "with_dropin"
    baseline_dir.mkdir(exist_ok=True)
    dropin_dir.mkdir(exist_ok=True)
    setup_harness(trial_dir, baseline_dir)
    setup_harness(trial_dir, dropin_dir)

    results: list[dict] = []

    if args.only in ("baseline", "both"):
        print(f"Running baseline (no drop-ins) in {baseline_dir}")
        results.append(run_sim(baseline_dir, label="no_dropin", timeout_sec=args.timeout_sec))

    if args.only in ("dropin", "both"):
        dropin_json = dropin_dir / "config" / "drop_in_transfers.json"
        emit_cmd = [
            str(VENV_PY),
            str(EMIT),
            "--trial-dir",
            str(trial_dir),
            "-o",
            str(dropin_json),
        ]
        subprocess.run(
            emit_cmd,
            cwd=REPO,
            env={**dict(subprocess.os.environ), "PYTHONPATH": str(EXPLORE_SRC)},
            check=True,
        )
        policy_path = dropin_dir / "config" / "data_policy_config.json"
        policy = json.loads(policy_path.read_text())
        policy["Data_Management_Policy"]["drop_in_transfers_file"] = "drop_in_transfers.json"
        policy_path.write_text(json.dumps(policy, indent=2) + "\n")
        print(f"Running with drop-ins in {dropin_dir}")
        results.append(run_sim(dropin_dir, label="with_dropin", timeout_sec=args.timeout_sec))

    summary_path = out_dir / "compare_summary.json"
    summary_path.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
