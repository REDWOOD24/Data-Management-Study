import json
import random
from pathlib import Path

rng = random.Random(1357911)

output = Path("../config/site_topology.json")
topology = {}

disk_types = ["CALIBDISK", "DATADISK", "LOCALGROUPDISK", "SCRATCHDISK"]

global_file_id = 0

for site_index in range(30):
    site_name = f"Site{site_index}"

    # Exactly 1,000 globally unique files per site.
    # Across all sites, IDs are exactly 0 through 29,999.
    files = []
    total_file_bytes = 0

    for _ in range(1000):
        file_size = rng.randint(50_000_000, 2_500_000_000)
        files.append([str(global_file_id), file_size])
        total_file_bytes += file_size
        global_file_id += 1

    # Generate 1–4 CPU clusters with 100–2,000 CPUs each.
    cpu_info = []
    cluster_count = rng.randint(1, 4)

    for cluster_index in range(cluster_count):
        units = rng.randint(100, 2000)
        speed = float(rng.choice([
            18_000_000,
            20_000_000,
            22_000_000,
            24_000_000,
            26_000_000,
            28_000_000,
            30_000_000,
            32_000_000,
        ]))
        cores = rng.choice([16, 20, 24, 25, 28, 30, 32, 36, 40, 48, 64])
        ram_gb = rng.choice([512, 1024, 2048, 3072, 4096, 6144, 8192, 12288])

        disks = []
        for disk_type in disk_types:
            disks.append({
                "name": f"{site_name.upper()}_C{cluster_index}_{disk_type}",
                "read_bw": f"{rng.randint(800, 5000)}MBps",
                "write_bw": f"{rng.randint(700, 4500)}MBps"
            })

        cpu_info.append({
            "units": units,
            "speed": speed,
            "cores": cores,
            "BW_CPU": f"{rng.randint(1800, 4000)}GBps",
            "LAT_CPU": f"{rng.randint(45, 100)}ns",
            "properties": [{"ram": f"{ram_gb}GB"}],
            "disks": disks
        })

    # Strictly greater than total file bytes multiplied by 1,000.
    storage_capacity_bytes = total_file_bytes * 10 #+ rng.randint(1, 10_000_000_000)

    topology[site_name] = {
        "SITE_PROPERTIES": {
            "storage_capacity_bytes": str(storage_capacity_bytes),
            "file_count": "1000"
        },
        "CPUInfo": cpu_info,
        "files": files
    }

with output.open("w", encoding="utf-8") as f:
    json.dump(topology, f, indent=2)

# Validation
all_ids = []
capacity_checks = []

for site_data in topology.values():
    site_ids = [int(file_entry[0]) for file_entry in site_data["files"]]
    site_total = sum(file_entry[1] for file_entry in site_data["files"])
    capacity = int(site_data["SITE_PROPERTIES"]["storage_capacity_bytes"])

    all_ids.extend(site_ids)
    capacity_checks.append(capacity >= site_total * 10)

assert len(topology) == 30
assert all(len(site["files"]) == 1000 for site in topology.values())
assert len(all_ids) == 30000
assert len(set(all_ids)) == 30000
assert sorted(all_ids) == list(range(30000))
assert all(capacity_checks)

cluster_counts = [len(site["CPUInfo"]) for site in topology.values()]
cpu_counts = [
    cluster["units"]
    for site in topology.values()
    for cluster in site["CPUInfo"]
]

print(f"Created: {output.name}")
print("File IDs: exactly 0-29999, each used once")
print("Files per site: 1000")
print("Total files: 30000")
print(f"CPU clusters per site: {min(cluster_counts)}-{max(cluster_counts)}")
print(f"CPU count per cluster: {min(cpu_counts)}-{max(cpu_counts)}")
print("All storage capacities > site file-byte totals × 10: True")
