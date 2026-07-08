#!/usr/bin/env python3
"""Plot transfer-volume analysis from simulation events in output/events.db."""

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter, MaxNLocator

DEFAULT_DB = Path(__file__).resolve().parent.parent / "output" / "events.db"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output" / "plots"
REACTIVE_EVENT = "FileTransfer"
PROACTIVE_EVENT = "BackGroundFileTransfer"
TRANSFER_EVENTS = (REACTIVE_EVENT, PROACTIVE_EVENT)
SITE_PATTERN = re.compile(r"^Site(\d+)$")


def site_sort_key(site_name: str) -> tuple[int, str]:
    match = SITE_PATTERN.match(site_name)
    if match:
        return int(match.group(1)), site_name
    return 10**9, site_name


def load_finished_transfers(db_path: Path) -> list[dict]:
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")

    placeholders = ", ".join("?" for _ in TRANSFER_EVENTS)
    query = f"""
        SELECT EVENT, METADATA
        FROM EVENTS
        WHERE STATE = 'Finished'
          AND EVENT IN ({placeholders})
    """

    transfers = []
    with sqlite3.connect(db_path) as conn:
        for event_type, metadata_raw in conn.execute(query, TRANSFER_EVENTS):
            metadata = json.loads(metadata_raw or "{}")
            source_site = metadata.get("source_site")
            destination_site = metadata.get("destination_site")
            size = metadata.get("size")

            if not source_site or not destination_site or size is None:
                continue

            transfers.append(
                {
                    "event": event_type,
                    "source_site": source_site,
                    "destination_site": destination_site,
                    "size": float(size),
                }
            )

    return transfers


def aggregate_transfer_data(
    transfers: list[dict],
) -> tuple[
    list[str],
    np.ndarray,
    dict[str, float],
    dict[str, float],
    dict[str, float],
    dict[str, float],
    dict[tuple[str, str], float],
    dict[tuple[str, str], float],
    dict[tuple[str, str], float],
]:
    sites = sorted(
        {t["source_site"] for t in transfers} | {t["destination_site"] for t in transfers},
        key=site_sort_key,
    )
    site_to_index = {site: index for index, site in enumerate(sites)}

    matrix = np.zeros((len(sites), len(sites)), dtype=np.float64)
    ingress_reactive = defaultdict(float)
    ingress_proactive = defaultdict(float)
    egress_reactive = defaultdict(float)
    egress_proactive = defaultdict(float)
    connection_reactive = defaultdict(float)
    connection_proactive = defaultdict(float)
    connection_totals = defaultdict(float)

    for transfer in transfers:
        source = transfer["source_site"]
        destination = transfer["destination_site"]
        size = transfer["size"]
        is_reactive = transfer["event"] == REACTIVE_EVENT
        connection_key = (source, destination)

        matrix[site_to_index[source], site_to_index[destination]] += size
        if is_reactive:
            ingress_reactive[destination] += size
            egress_reactive[source] += size
            connection_reactive[connection_key] += size
        else:
            ingress_proactive[destination] += size
            egress_proactive[source] += size
            connection_proactive[connection_key] += size
        connection_totals[connection_key] += size

    return (
        sites,
        matrix,
        dict(ingress_reactive),
        dict(ingress_proactive),
        dict(egress_reactive),
        dict(egress_proactive),
        dict(connection_reactive),
        dict(connection_proactive),
        connection_totals,
    )


def load_job_site_metrics(db_path: Path) -> tuple[dict[str, float], dict[str, float]]:
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")

    query = """
        WITH job_times AS (
            SELECT
                jf.JOB_ID,
                json_extract(jf.METADATA, '$.site') AS site,
                ja.TIME AS alloc_time,
                js.TIME AS exec_start_time,
                (
                    SELECT MAX(TIME)
                    FROM EVENTS e
                    WHERE e.JOB_ID = jf.JOB_ID
                ) AS end_time
            FROM EVENTS jf
            JOIN EVENTS ja
              ON ja.JOB_ID = jf.JOB_ID
             AND ja.EVENT = 'JobAllocation'
             AND ja.STATE = 'Finished'
            JOIN EVENTS js
              ON js.JOB_ID = jf.JOB_ID
             AND js.EVENT = 'JobExecution'
             AND js.STATE = 'Started'
            WHERE jf.EVENT = 'JobExecution'
              AND jf.STATE = 'Finished'
        )
        SELECT
            site,
            AVG(end_time - alloc_time) AS avg_end_to_end,
            AVG(exec_start_time - alloc_time) AS avg_staging_time
        FROM job_times
        WHERE site IS NOT NULL
        GROUP BY site
    """

    avg_end_to_end: dict[str, float] = {}
    avg_staging_time: dict[str, float] = {}
    with sqlite3.connect(db_path) as conn:
        for site, end_to_end, staging_time in conn.execute(query):
            avg_end_to_end[site] = float(end_to_end)
            avg_staging_time[site] = float(staging_time)

    return avg_end_to_end, avg_staging_time


def format_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    amount = float(value)
    for unit in units:
        if abs(amount) < 1024.0 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{amount:.1f} PB"


def plot_heatmap(sites: list[str], matrix: np.ndarray, output_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(max(10, len(sites) * 0.35), max(8, len(sites) * 0.35)))

    data = matrix / (1024**3)
    im = ax.imshow(data, cmap="YlOrRd")

    ax.set_xticks(range(len(sites)))
    ax.set_yticks(range(len(sites)))
    ax.set_xticklabels(sites, rotation=90, fontsize=8)
    ax.set_yticklabels(sites, fontsize=8)
    ax.set_xlabel("Destination site")
    ax.set_ylabel("Source site")
    ax.set_title("Transfer volume heatmap (reactive + proactive)\nCell = total bytes source → destination")

    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("Transferred size (GiB)")

    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def setup_primary_y_axis(ax: plt.Axes, *, n_ticks: int = 6) -> None:
    """Use round primary-axis ticks driven by transfer-volume scale."""
    y_top = max(ax.get_ylim()[1], 1e-9)
    ax.set_ylim(0, y_top)
    ax.margins(y=0)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=n_ticks, min_n_ticks=4))


def setup_secondary_timing_axis(
    ax2: plt.Axes,
    values: list[float],
    *,
    n_ticks: int = 6,
) -> None:
    """Pick an integer secondary range with padding so timing lines use the axis well."""
    finite = np.array([value for value in values if not np.isnan(value)], dtype=float)
    if finite.size == 0:
        ax2.set_ylim(0, 1)
        return

    vmin = float(finite.min())
    vmax = float(finite.max())
    span = max(vmax - vmin, vmax * 0.2, 20.0)
    pad = span * 0.18

    y_min = 0.0
    y_max = vmax + pad

    tick_step = max(5, int(np.ceil((y_max - y_min) / (n_ticks - 1) / 5) * 5))
    y_max = int(np.ceil(y_max / tick_step) * tick_step)

    # Give the lines a bit more vertical room when the peak is tight to the top.
    if vmax > y_max - tick_step * 0.75:
        y_max += tick_step

    ax2.set_ylim(y_min, y_max)
    ax2.margins(y=0)
    ax2.yaxis.set_major_locator(
        MaxNLocator(nbins=n_ticks, min_n_ticks=4, integer=True, steps=[1, 2, 5, 10])
    )
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{int(round(value))}"))
    ax2.grid(False)


SITE_MEAN_GAP = 1.2
SITE_PLOT_FONT_TITLE = 16
SITE_PLOT_FONT_LABEL = 13
SITE_PLOT_FONT_TICK = 11
SITE_PLOT_FONT_LEGEND = 11

def plot_site_ingress_egress(
    sites: list[str],
    ingress_reactive: dict[str, float],
    ingress_proactive: dict[str, float],
    egress_reactive: dict[str, float],
    egress_proactive: dict[str, float],
    avg_end_to_end: dict[str, float],
    avg_staging_time: dict[str, float],
    output_path: Path,
) -> None:
    to_gib = lambda volumes: [volumes.get(site, 0.0) / (1024**3) for site in sites]

    ingress_reactive_values = to_gib(ingress_reactive)
    ingress_proactive_values = to_gib(ingress_proactive)
    egress_reactive_values = to_gib(egress_reactive)
    egress_proactive_values = to_gib(egress_proactive)
    end_to_end_values = [avg_end_to_end.get(site, np.nan) for site in sites]
    staging_time_values = [avg_staging_time.get(site, np.nan) for site in sites]

    mean_ingress_reactive = float(np.mean(ingress_reactive_values))
    mean_ingress_proactive = float(np.mean(ingress_proactive_values))
    mean_egress_reactive = float(np.mean(egress_reactive_values))
    mean_egress_proactive = float(np.mean(egress_proactive_values))
    mean_end_to_end = float(np.nanmean(end_to_end_values))
    mean_staging_time = float(np.nanmean(staging_time_values))

    bar_ingress_reactive = ingress_reactive_values + [mean_ingress_reactive]
    bar_ingress_proactive = ingress_proactive_values + [mean_ingress_proactive]
    bar_egress_reactive = egress_reactive_values + [mean_egress_reactive]
    bar_egress_proactive = egress_proactive_values + [mean_egress_proactive]
    x_labels = sites + ["Mean"]

    x_sites = np.arange(len(sites), dtype=float)
    x_site_mean = len(sites) + SITE_MEAN_GAP
    x_bar = np.concatenate([x_sites, [x_site_mean]])

    fig, ax = plt.subplots(figsize=(max(10, (len(sites) + SITE_MEAN_GAP + 1) * 0.3), 6.5))

    bottom_ingress_proactive = bar_ingress_reactive
    bottom_egress_reactive = [
        bar_ingress_reactive[i] + bar_ingress_proactive[i] for i in range(len(x_labels))
    ]
    bottom_egress_proactive = [
        bottom_egress_reactive[i] + bar_egress_reactive[i] for i in range(len(x_labels))
    ]

    ax.bar(x_bar, bar_ingress_reactive, width=0.85, label="Ingress (reactive)", color="#2a9d8f")
    ax.bar(
        x_bar,
        bar_ingress_proactive,
        width=0.85,
        bottom=bottom_ingress_proactive,
        label="Ingress (proactive)",
        color="#8ecae6",
    )
    ax.bar(
        x_bar,
        bar_egress_reactive,
        width=0.85,
        bottom=bottom_egress_reactive,
        label="Egress (reactive)",
        color="#e76f51",
    )
    ax.bar(
        x_bar,
        bar_egress_proactive,
        width=0.85,
        bottom=bottom_egress_proactive,
        label="Egress (proactive)",
        color="#f4a261",
    )

    ax.set_xticks(x_bar)
    ax.set_xticklabels(x_labels, rotation=90, fontsize=SITE_PLOT_FONT_TICK)
    ax.set_xlim(-0.6, x_site_mean + 0.6)
    ax.set_xlabel("Site", fontsize=SITE_PLOT_FONT_LABEL)
    ax.set_ylabel("Transferred size (GiB)", fontsize=SITE_PLOT_FONT_LABEL)
    ax.set_title(
        "Per-site transfer volume (ingress/egress by transfer type)",
        fontsize=SITE_PLOT_FONT_TITLE,
    )
    ax.tick_params(axis="y", labelsize=SITE_PLOT_FONT_TICK)

    ax2 = ax.twinx()
    ax2.plot(
        x_sites,
        staging_time_values,
        color="#6d597a",
        marker="s",
        linestyle="--",
        linewidth=1.5,
        markersize=5,
        label="Avg job staging time (alloc → exec start)",
    )
    ax2.plot(
        x_sites,
        end_to_end_values,
        color="#264653",
        marker="o",
        linestyle="-",
        linewidth=2.0,
        markersize=6,
        markerfacecolor="#264653",
        markeredgecolor="white",
        markeredgewidth=0.8,
        label="Avg job end-to-end time",
    )
    ax2.plot(
        x_site_mean,
        mean_staging_time,
        color="#6d597a",
        marker="s",
        linestyle="none",
        markersize=5,
    )
    ax2.plot(
        x_site_mean,
        mean_end_to_end,
        color="#264653",
        marker="o",
        linestyle="none",
        markersize=6,
        markerfacecolor="#264653",
        markeredgecolor="white",
        markeredgewidth=0.8,
    )
    ax2.set_ylabel("Average time (s)", fontsize=SITE_PLOT_FONT_LABEL)
    ax2.tick_params(axis="y", labelsize=SITE_PLOT_FONT_TICK)

    bar_totals = [
        bar_ingress_reactive[i]
        + bar_ingress_proactive[i]
        + bar_egress_reactive[i]
        + bar_egress_proactive[i]
        for i in range(len(x_labels))
    ]
    timing_values = [
        value
        for value in (
            staging_time_values
            + end_to_end_values
            + [mean_staging_time, mean_end_to_end]
        )
        if not np.isnan(value)
    ]
    ax.set_ylim(0, max(bar_totals) * 1.08)
    setup_primary_y_axis(ax)
    setup_secondary_timing_axis(ax2, timing_values)

    ax.margins(x=0.01, y=0)

    ax.grid(axis="y", alpha=0.3, linestyle="-", linewidth=0.8)
    ax.set_axisbelow(True)

    bar_handles, bar_labels = ax.get_legend_handles_labels()
    line_handles, line_labels = ax2.get_legend_handles_labels()
    ax.legend(
        bar_handles + line_handles,
        bar_labels + line_labels,
        loc="best",
        fontsize=SITE_PLOT_FONT_LEGEND,
        framealpha=0.92,
        ncol=2,
        borderaxespad=0.4,
        handlelength=1.6,
    )

    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def select_top_connections(
    connection_totals: dict[tuple[str, str], float],
    top_k: int | None,
    min_bytes: float,
) -> list[tuple[str, str]]:
    ranked = sorted(connection_totals.items(), key=lambda item: item[1], reverse=True)

    selected = []
    for (source, destination), total in ranked:
        if total < min_bytes:
            continue
        selected.append((source, destination))
        if top_k is not None and len(selected) >= top_k:
            break

    return selected


def plot_connection_totals(
    connections: list[tuple[str, str]],
    connection_reactive: dict[tuple[str, str], float],
    connection_proactive: dict[tuple[str, str], float],
    output_path: Path,
) -> None:
    if not connections:
        raise ValueError("No connections matched the filtering criteria.")

    labels = [f"{source} → {destination}" for source, destination in connections]
    reactive_values = [
        connection_reactive.get((source, destination), 0.0) / (1024**3)
        for source, destination in connections
    ]
    proactive_values = [
        connection_proactive.get((source, destination), 0.0) / (1024**3)
        for source, destination in connections
    ]

    fig_width = max(12, len(labels) * 0.35)
    fig, ax = plt.subplots(figsize=(fig_width, 6))
    x = np.arange(len(labels))

    ax.bar(x, reactive_values, label="Reactive", color="#2a9d8f")
    ax.bar(x, proactive_values, bottom=reactive_values, label="Proactive", color="#8ecae6")

    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=90, fontsize=8)
    ax.set_xlabel("Site-to-site connection")
    ax.set_ylabel("Transferred size (GiB)")
    ax.set_title("Top site-to-site connections by total transfer volume")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot transfer heatmap and volume charts from output/events.db."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to the SQLite events database (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for generated plot images (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=40,
        help="Maximum number of connections to show in the connection plot (default: 40)",
    )
    parser.add_argument(
        "--min-bytes",
        type=float,
        default=0.0,
        help="Minimum total bytes for a connection to appear in the connection plot",
    )
    args = parser.parse_args()

    transfers = load_finished_transfers(args.db)
    if not transfers:
        raise SystemExit("No finished transfer events found in the database.")

    (
        sites,
        matrix,
        ingress_reactive,
        ingress_proactive,
        egress_reactive,
        egress_proactive,
        connection_reactive,
        connection_proactive,
        connection_totals,
    ) = aggregate_transfer_data(transfers)
    avg_end_to_end, avg_staging_time = load_job_site_metrics(args.db)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    heatmap_path = args.output_dir / "transfer_heatmap.png"
    site_bars_path = args.output_dir / "site_ingress_egress.png"
    connections_path = args.output_dir / "top_connections.png"

    plot_heatmap(sites, matrix, heatmap_path)
    plot_site_ingress_egress(
        sites,
        ingress_reactive,
        ingress_proactive,
        egress_reactive,
        egress_proactive,
        avg_end_to_end,
        avg_staging_time,
        site_bars_path,
    )

    top_connections = select_top_connections(
        connection_totals,
        top_k=args.top_k,
        min_bytes=args.min_bytes,
    )
    plot_connection_totals(
        top_connections,
        connection_reactive,
        connection_proactive,
        connections_path,
    )

    total_bytes = float(matrix.sum())
    print(f"Loaded {len(transfers)} finished transfer events across {len(sites)} sites.")
    print(f"Total transferred volume: {format_bytes(total_bytes)}")
    print(f"Wrote {heatmap_path}")
    print(f"Wrote {site_bars_path}")
    print(f"Wrote {connections_path} ({len(top_connections)} connections)")


if __name__ == "__main__":
    main()
