#!/usr/bin/env bash
# Top-level launcher: per-template exploration under one parent experiment folder.
#
# Usage (from anywhere):
#   bash explore/scripts/run_per_template_dropins.sh
#
# Optional overrides:
#   EXP_NAME=my_run TRIALS=30 SEED=7 bash explore/scripts/run_per_template_dropins.sh
#   TEMPLATES=hotset_replication bash explore/scripts/run_per_template_dropins.sh
#   PREPARE_ONLY=1 bash explore/scripts/run_per_template_dropins.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPLORE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${EXPLORE_ROOT}/.." && pwd)"

EXP_NAME="${EXP_NAME:-explore_per_template_dropins_avg}"
TRIALS="${TRIALS:-50}"
SEED="${SEED:-42}"
OBJECTIVE="${OBJECTIVE:-avg_staging_time}"
AGENTS="${AGENTS:-bayesian_opt,rl_policy,random_search}"
TEMPLATES="${TEMPLATES:-all}"
PREPARE_ONLY="${PREPARE_ONLY:-0}"

SRC_DROPIN_DEFAULT="${REPO_ROOT}/explore/runs/explore_20260714T194729Z_drop-ins_avg/drop_in_transfers.json"
SRC_DROPIN="${SRC_DROPIN:-${SRC_DROPIN_DEFAULT}}"

if [[ ! -f "${SRC_DROPIN}" ]]; then
  echo "Drop-in schedule not found: ${SRC_DROPIN}" >&2
  echo "Set SRC_DROPIN=/path/to/drop_in_transfers.json" >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-${EXPLORE_ROOT}/.venv/bin/python}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Python not found/executable: ${PYTHON_BIN}" >&2
  echo "Set PYTHON_BIN=... or create explore/.venv" >&2
  exit 1
fi

EXP_DIR="${EXPLORE_ROOT}/runs/${EXP_NAME}"
mkdir -p "${EXP_DIR}"

export MPLBACKEND="${MPLBACKEND:-Agg}"
export MPLCONFIGDIR="${MPLCONFIGDIR:-${EXP_DIR}/.mplcache}"
export PYTHONUNBUFFERED=1
mkdir -p "${MPLCONFIGDIR}"

cd "${EXPLORE_ROOT}"

CMD=(
  "${PYTHON_BIN}" scripts/run_per_template_exploration.py
  --settings config/settings.yaml
  --experiment-name "${EXP_NAME}"
  --templates "${TEMPLATES}"
  --objective "${OBJECTIVE}"
  --agents "${AGENTS}"
  --trials "${TRIALS}"
  --seed "${SEED}"
  --window-mode full
  --aggregation mean
  --reactive-delta-every 5
  --enable-drop-in-transfers
  --drop-in-transfers-file "${SRC_DROPIN}"
)

if [[ "${PREPARE_ONLY}" == "1" ]]; then
  CMD+=(--prepare-only --print-commands)
fi

LOG_FILE="${EXP_DIR}/exploration_all.log"
echo "Parent experiment: ${EXP_NAME}"
echo "Parent dir:        ${EXP_DIR}"
echo "Drop-ins:          ${SRC_DROPIN}"
echo "Templates:         ${TEMPLATES}"
echo "Log:               ${LOG_FILE}"
echo

if [[ "${PREPARE_ONLY}" == "1" ]]; then
  "${CMD[@]}"
else
  "${CMD[@]}" 2>&1 | tee "${LOG_FILE}"
fi
