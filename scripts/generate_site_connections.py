import json
import random
from pathlib import Path

rng = random.Random(24681012)

output = Path("../config/site_connections.json")
connections = {}
site_names = [f"Site{i}" for i in range(30)]

for i, source in enumerate(site_names):
    for destination in site_names[i + 1:]:
        bandwidth = round(rng.uniform(2500.0, 3500.0), 2)
        latency = rng.randint(1, 50)

        connections[f"{source}:{destination}"] = {
            "bandwidth": f"{bandwidth}Mbps",
            "latency": f"{latency}ms"
        }

with output.open("w", encoding="utf-8") as f:
    json.dump(connections, f, indent=2)

expected = len(site_names) * (len(site_names) - 1) // 2
assert len(connections) == expected == 435
assert len(set(connections)) == 435

print(f"Created: {output.name}")
print(f"Sites: {len(site_names)}")
print(f"Full-mesh connections: {len(connections)}")
print(f"File size: {output.stat().st_size / 1024:.2f} KiB")
