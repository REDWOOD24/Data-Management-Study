from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_PLOT_TRANSFER_ANALYSIS = None


def _load_transfer_analysis(repo_root: Path):
    global _PLOT_TRANSFER_ANALYSIS
    if _PLOT_TRANSFER_ANALYSIS is not None:
        return _PLOT_TRANSFER_ANALYSIS

    scripts_dir = repo_root / "scripts"
    if not scripts_dir.is_dir():
        raise FileNotFoundError(f"Scripts directory not found: {scripts_dir}")

    scripts_path = str(scripts_dir)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)

    import plot_transfer_analysis

    _PLOT_TRANSFER_ANALYSIS = plot_transfer_analysis
    return plot_transfer_analysis


@dataclass(frozen=True)
class TransferSummary:
    total_ingress_gib: float
    total_egress_gib: float
    proactive_volume_gib: float
    reactive_volume_gib: float
    proactive_fraction: float


def transfer_summary_from_db(db_path: Path, *, repo_root: Path) -> TransferSummary | None:
    if not db_path.is_file():
        return None

    pta = _load_transfer_analysis(repo_root)
    try:
        transfers = pta.load_finished_transfers(db_path)
    except FileNotFoundError:
        return None

    if not transfers:
        return TransferSummary(0.0, 0.0, 0.0, 0.0, 0.0)

    gib = 1024**3
    ingress = 0.0
    egress = 0.0
    proactive = 0.0
    reactive = 0.0

    for transfer in transfers:
        size_gib = float(transfer["size"]) / gib
        is_proactive = transfer["event"] == pta.PROACTIVE_EVENT
        ingress += size_gib
        egress += size_gib
        if is_proactive:
            proactive += size_gib
        else:
            reactive += size_gib

    total = proactive + reactive
    proactive_fraction = proactive / total if total > 0 else 0.0
    return TransferSummary(
        total_ingress_gib=ingress,
        total_egress_gib=egress,
        proactive_volume_gib=proactive,
        reactive_volume_gib=reactive,
        proactive_fraction=proactive_fraction,
    )
