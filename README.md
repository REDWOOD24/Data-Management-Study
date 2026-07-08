# Data Management Study

A CGSim-based data management simulation study focused on studying data placement strategies in a CGSim grid environment.

This repository contains a C++ dispatcher plugin, configuration files for a generated multi-site grid environment, Python scripts for generating simulation topology data, and SQLite output for simulation events.

## Overview

The project simulates a distributed computing environment with multiple sites, files, compute resources, storage capacity, and network links. It includes custom logic for:

* Generating a multi-site grid topology
* Creating full-mesh site-to-site network connections
* Producing randomized workloads
* Assigning jobs to compute sites based on file locality and storage availability
* Selecting available CPU resources
* Logging simulation events to SQLite
* Running background file-management policies such as:

  * Storage Rebalance Policy
  * Network Aware Rebalance Policy
  * Hotset Replication Policy

## Repository Structure

```text id="w3n594"
Data-Management-Study/
├── config/
│   ├── config.json
│   ├── site_connections.json
│   └── site_topology.json
├── output/
│   └── events.db
├── plugin/
│   ├── CMakeLists.txt
│   ├── CMakeModules/
│   ├── include/
│   │   ├── dispatcher.h
│   │   ├── output.h
│   │   ├── policy.h
│   │   └── workload_manager.h
│   └── src/
│       ├── DataManagementPlugin.cpp
│       ├── dispatcher.cpp
│       ├── output.cpp
│       ├── policy.cpp
│       └── workload_manager.cpp
├── scripts/
│   ├── generate_site_connections.py
│   └── generate_site_topology.py
└── README.md
```

## Features

### Custom Dispatcher

The dispatcher assigns jobs to sites by prioritizing file locality. It checks where a job’s input files are located, chooses the site with the highest file overlap, and verifies that the site has enough remaining storage for the job’s output files.

### Workload Generation

The workload manager creates synthetic jobs with randomized:

* Job creation times
* Core requirements
* FLOP counts
* Input files
* Output files
* Output file sizes

The number of generated jobs is configured in `config/config.json`.

### Data Management Policies

The plugin registers several background data-management policies.

#### Storage Rebalance Policy

Moves files from highly utilized sites to lower-utilized sites.

#### Network Aware Rebalance Policy

Chooses file movement candidates based on network conditions such as link load, bandwidth, latency, and estimated transfer time.

#### Hotset Replication Policy

Replicates frequently available or “hot” files until a target replica count is reached.

### Event Logging

Simulation events are written to a SQLite database at:

```text id="4rhypd"
output/events.db
```

The event table records:

* Event type
* Event state
* Job ID
* Job status
* Simulation timestamp
* JSON metadata payload

Logged event types include:

* Job allocation
* Job execution
* File transfer
* File read
* File write
* Background file transfer

## Requirements

This project requires:

* C++17-compatible compiler
* CMake 3.12 or newer
* CGSim
* Boost
* SQLite3
* Python 3

## Configuration

The main simulation configuration is located at:

```text id="4mx56d"
config/config.json
```

Example settings include:

```json id="x9dipk"
{
  "Grid_Name": "GRID",
  "Sites_Information": "site_topology.json",
  "Sites_Connection_Information": "site_connections.json",
  "Dispatcher_Plugin": "../plugin/build/libDataManagementPlugin.dylib",
  "Limited_Sites": [],
  "Custom_Parameters": {
    "Num_of_Jobs": "200",
    "output_file": "../output/events.db",
    "data_policy": "data_policy_config.json"
  }
}
```

### Important Parameters

| Parameter                      | Description                                 |
| ------------------------------ | ------------------------------------------- |
| `Grid_Name`                    | Name of the simulated grid                  |
| `Sites_Information`            | Site topology JSON file                     |
| `Sites_Connection_Information` | Site connection JSON file                   |
| `Dispatcher_Plugin`            | Path to the compiled dispatcher plugin      |
| `Num_of_Jobs`                  | Number of synthetic jobs to generate        |
| `output_file`                  | SQLite database file used for event logging |
| `data_policy`                  | data policy config JSON file                |

## Generating Configuration Files

The repository includes Python scripts to generate the topology and network connection files.

From the repository root:

```bash id="gwxfqo"
cd scripts
python3 generate_site_topology.py
python3 generate_site_connections.py
```

These scripts create or update:

```text id="8t6ije"
config/site_topology.json
config/site_connections.json
```

### Topology Generation

`generate_site_topology.py` creates:

* 30 sites
* 1,000 files per site
* 30,000 globally unique files
* Randomized CPU clusters per site
* Randomized storage and disk characteristics

### Connection Generation

`generate_site_connections.py` creates a full-mesh network between all 30 sites, resulting in 435 site-to-site connections.

Each connection includes randomized:

* Bandwidth
* Latency

## Building the Plugin

From the repository root:

```bash id="7cdd4h"
cd plugin
mkdir -p build
cd build
cmake ..
make
```

This builds the shared library used by CGSim.

On macOS, the output may look like:

```text id="8k35kc"
libDataManagementPlugin.dylib
```

On Linux, the output may look like:

```text id="npzw95"
libDataManagementPlugin.so
```

If your generated library name or extension differs, update the `Dispatcher_Plugin` path in `config/config.json`.

## Running a Simulation

After building the plugin and generating the configuration files, run the simulation with your CGSim setup using:

```text id="d765aq"
config/config.json
```

The exact run command depends on your local CGSim installation and executable name.

Example:
```
config % ../../CGSim/build/cg-sim -c ./config.json  
```

## Output

Simulation results are stored in SQLite format:

```text id="s0tnbn"
output/events.db
```

You can inspect the logged events with SQLite:

```bash id="0knpi8"
sqlite3 output/events.db
```

Then run:

```sql id="nkbsjl"
.tables
SELECT * FROM EVENTS LIMIT 10;
```

## Example SQLite Queries

Count events by type:

```sql id="ip3wn6"
SELECT EVENT, COUNT(*)
FROM EVENTS
GROUP BY EVENT;
```

View finished job executions:

```sql id="skjx1f"
SELECT JOB_ID, TIME, METADATA
FROM EVENTS
WHERE EVENT = 'JobExecution'
  AND STATE = 'Finished'
LIMIT 20;
```

View background file transfers:

```sql id="epnvg2"
SELECT TIME, METADATA
FROM EVENTS
WHERE EVENT = 'BackGroundFileTransfer'
ORDER BY TIME
LIMIT 20;
```

## Other Scripts Usage
Convert db output file to csv file (offline, after execution)

```
  python3 scripts/export_events_to_csv.py \
  --db /path/to/events.db \
  --output /path/to/events.csv
```

Plot figures

change directory to ```scripts/```, output figures under ```output/plots/``` 
```
pip install -r requirements.txt
python plot_transfer_analysis.py
```
## Development Notes

The plugin is organized into the following main components:

| Component                  | Purpose                                           |
| -------------------------- | ------------------------------------------------- |
| `DataManagementPlugin.cpp` | Main plugin entry point and CGSim callback wiring, mainly hooking reactive transfers to CGSim on demand |
| `dispatcher.cpp`           | Job placement and CPU selection logic             |
| `workload_manager.cpp`     | Synthetic workload generation                     |
| `policy.cpp`               | data-management policies implementation           |
| `output.cpp`               | SQLite event logging and utilization metrics      |

