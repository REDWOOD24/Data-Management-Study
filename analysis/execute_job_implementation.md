# Implementation of `execute_job` from the first-principles table

This document explains how CGSim now implements the scenario universe and completeness reasoning in `analysis/execute_job_first_principles.md` (especially Sections 3.1, 3.2, and 3.5). It points at real code symbols and line ranges in the CGSim tree. CGSim was rebuilt after these changes; no simulation was run as part of this write-up.

Primary sources:

- `CGSim/include/job_executor.h`
- `CGSim/util/job_executor.cpp`
- `CGSim/include/file_manager.h`
- `CGSim/util/file_manager.cpp`
- Design reference: `analysis/execute_job_first_principles.md`

---

## 1. High-level flow

A host receiver actor still calls only `JOB_EXECUTOR::execute_job(Job* j)` (`job_executor.cpp` around lines 768–792, invoked from `receiver` at line 804).

Inside that call the pipeline is:

1. **Observe / plan** each input → one of $\{L, I, U, B\}$ (`plan_all_inputs` → `plan_one_input`)
2. **Reject $B$ early** with soft restage (do not build a partial graph)
3. **Build** the SimGrid activity graph (`build_job_graph`)
4. **Register** owned activities into `pending_activities` (`register_job_graph`)
5. **Start** owned roots in HEAD order (`start_job_graph`)

Assignment staging (`job_needs_transfer_staging`, `try_release_staged_jobs`) sits **before** dispatch so jobs stuck in $B$ never need a host until a resting replica exists again.

That matches Claim 3 in Section 3.5: $L$, $I$, and $U$ get terminating graph branches; $B$ waits and re-observes.

---

## 2. Encoding the four observation labels

In `job_executor.h` (lines 38–44), `InputAccessKind` is the code form of $\mathrm{obs}(R,T)$:

| Section 3.5 label | Enum value | Meaning |
|-------------------|------------|---------|
| $L$ | `LOCAL_READ` | Resting replica at compute site |
| $I$ | `WAIT_IN_FLIGHT_THEN_LOCAL_READ` | Inbound transfer to compute site |
| $U$ | `TRANSFER_THEN_READ` | Remote resting replica; this job starts a new transfer |
| $B$ | `REMOTE_BOUND_WAIT` | Catalog empty; only remote-bound in-flight account(s) |

`InputAccessPlan` (lines 46–52) carries `filename`, `kind`, `src_site`, `mode` (COPY/MOVE), and `in_flight_comm` (for $I$ join).

`JobActivityGraph` (lines 54–61) holds `exec`, owned `transfers`, `joined` (not started by this job), `reads`, and `writes`.

---

## 3. FileManager support for $R$ and $T$

Section 3.5 defines catalog $R$ and in-flight set $T$. Code maps them as:

- **$R$:** `FileManager::exists` / `request_file_sites` (via helper `live_replica_sites` at the top of `job_executor.cpp`)
- **$T$:** `FileManager::active_transfers`, queried by:
  - `find_in_flight_to_destination(filename, dst)` — any $t$ with $\mathrm{dst}(t)=s$ (supports $I$; destination uniqueness)
  - `find_in_flight_comm(filename, src, dst)` — exact route (duplicate-route defense on $U$)
  - `find_any_in_flight(filename)` — **new** (`file_manager.cpp` lines 92–113): any transfer of the file, any destination (supports $B$ when $R=\emptyset$)

Destination uniqueness (axiom 3) remains: at most one active delivery per `(filename, destination)`. Concurrent transfers of the same file to **different** destinations are allowed and show up as multiple keys in `active_transfers`.

---

## 4. Observation order in `plan_one_input` (the heart of completeness)

Function: `JOB_EXECUTOR::plan_one_input` (`job_executor.cpp` lines 456–515).

This is a direct implementation of Section 3.5 Claim 2:

$$
\mathrm{obs}(R,T)=\begin{cases}L & \text{if }L\\ I & \text{else if }I\\ U & \text{else if }U\\ B & \text{otherwise}\end{cases}
$$

### Step $L$ (lines 467–471)

```cpp
if (fm->exists(filename, job->comp_site)) {
  plan.kind = InputAccessKind::LOCAL_READ;
  return plan;
}
```

Table rows: “Local resting replica already present”; Phase B of “Held in assignment staging until local…”.

### Step $I$ (lines 473–479)

```cpp
if (auto inbound = fm->find_in_flight_to_destination(filename, job->comp_site)) {
  plan.kind = InputAccessKind::WAIT_IN_FLIGHT_THEN_LOCAL_READ;
  plan.src_site = inbound->src_site;
  plan.in_flight_comm = inbound->comm;
  return plan;
}
```

Table rows: inbound owned by another job; inbound proactive/background/drop-in; sole-replica MOVE toward the compute site (catalog may be empty — $I$ is checked **before** requiring resting replicas).

This was the critical fix versus earlier code that called `resolve_input_file_source` first and could throw “no replica” during sole-replica MOVE even though $T$ still accounted for the file.

### Step $U$ (lines 481–503)

If `live_replica_sites(filename)` is non-empty, call `resolve_input_file_source` (lines 410–454) so the plugin may choose source and COPY/MOVE (`dispatcher->onFileRequest`). Stale policy strings are corrected against the live catalog (lines 439–448; table row “Policy returns a stale or empty source string”).

A second inbound check (lines 492–498) handles the race “$U$ planned, then an inbound appeared” so the job upgrades to $I$ instead of starting a duplicate delivery to the compute site (destination uniqueness / Claim 4).

Otherwise:

```cpp
plan.kind = InputAccessKind::TRANSFER_THEN_READ;
plan.src_site = filelocation;
```

Table rows: remote resting COPY/MOVE; concurrent transfers to other destinations may exist and are ignored except for destination uniqueness at the compute site.

### Step $B$ (lines 505–509)

```cpp
if (fm->find_any_in_flight(filename)) {
  plan.kind = InputAccessKind::REMOTE_BOUND_WAIT;
  return plan;
}
```

Table rows: sole-replica MOVE toward a **different** site; remote-bound with possibly several in-flight destinations. Accountability invariant: file is not missing.

### Illegal corner (lines 511–514)

If $R=\emptyset$ and $T=\emptyset$, throw `NoGlobalReplicaError`. Section 3.5 excludes this from the partition; `execute_job` maps it to `fail_job_clean`.

---

## 5. From plan labels to graph branches (Claim 3 actions)

`build_job_graph` (`job_executor.cpp` lines 636–668) creates `graph.exec`, then for each plan calls `revalidate_plan` and switches on `kind`.

### $L$ → `build_local_read_branch` (lines 583–589)

- `Actions::read_file_async` (pins inside Actions)
- wire `read → exec`
- push into `graph.reads`

### $I$ → `build_join_in_flight_branch` (lines 621–634)

- require `plan.in_flight_comm`
- `in_flight_comm->add_successor(read)` then `read → exec`
- store Comm in `graph.joined` (for bookkeeping only)
- **do not** create a new transfer

### $U$ → `build_transfer_read_branch` (lines 591–619)

- If a delivery to the compute site already exists, downgrade to join ($I$) instead of `transfer()` again (lines 595–610)
- Else `Actions::transfer_file_async` then read; wire `comm → read → exec`
- owned Comm goes to `graph.transfers`

### $B$ → soft restage, no graph (lines 654–658 and 774–779)

Throw `SoftRestageSignal` so `execute_job` calls `return_job_to_staging` without registering activities. This is Claim 3’s $B$ action: wait until some $t\in T$ completes, then re-observe.

Outputs (table row “Output write after execution”) are unchanged HEAD wiring at lines 662–666: `exec → write` for each output file.

---

## 6. Register and start (HEAD activation; Claim 3 start rules)

### `register_job_graph` (lines 670–685)

Tracks **owned** transfers, all reads, exec, and writes via `track_pending_activity`.

Joined inbound Comms are **not** pushed into `pending_activities`. Reason (design + destination uniqueness): the originator already owns waiting on that Comm; this job only depends through `add_successor`. Double-tracking joined Comms was a likely contributor to earlier WAITING piles.

### `start_job_graph` (lines 687–700)

Order preserved from HEAD:

1. start every Comm in `graph.transfers` (owned $U$ only)
2. start every read
3. start exec
4. start every write

Joined Comms are never started again (comment at line 692).

---

## 7. Staging vs join (table timelines)

### `job_needs_transfer_staging` (lines 373–408)

- Returns true for $B$ (`REMOTE_BOUND_WAIT`) so assignment holds the job until a resting replica appears (table: remote-bound / staged-until-local timelines).
- Does **not** stage solely for $I$: inbound is joinable inside `execute_job` (Moment 0 / Claim 3 for $I$).
- If planning hits `NoGlobalReplicaError`, stages rather than dispatching a doomed activation (defensive; illegal under axioms if persistent).

### `try_release_staged_jobs` (lines 752–766)

Re-runs `job_needs_transfer_staging`; when false, `dispatch_job_to_host`. After a remote-bound landing, observation typically becomes $U$ or $L$, so the job can leave staging.

### `return_job_to_staging` (lines 702–710)

Used when `execute_job` catches `SoftRestageSignal` (lines 785–787): decrement `DISPATCHED_JOBS`, set status `"staged"`, push onto `staging_jobs`.

---

## 8. How table rows map onto code paths

| Table scenario (Section 3.2) | Code path |
|------------------------------|-----------|
| Local resting replica | `plan_one_input` $L$ → `build_local_read_branch` |
| Remote resting COPY/MOVE | `plan_one_input` $U` + `resolve_input_file_source` → `build_transfer_read_branch` |
| Concurrent in-flight to different destinations | Same `obs` order; `find_in_flight_to_destination` only cares about compute site; `find_any_in_flight` used when $R=\emptyset$ |
| Inbound (other job / proactive / drop-in) | `plan_one_input` $I` → `build_join_in_flight_branch` |
| Staged until local, then dispatched | Assignment: $B$ staged; after landing, plan becomes $L$ or $U$; `execute_job` builds ordinary branch |
| Sole MOVE toward compute site | Empty catalog still hits $I$ first via `find_in_flight_to_destination` |
| Sole MOVE toward other site | $B$ via `find_any_in_flight` → stage / SoftRestage |
| Pin / multi-reader | Unchanged in Actions/`FileManager` pin APIs (not reimplemented in `execute_job`) |
| Stale policy source | `resolve_input_file_source` live fallback loop |
| Multi-input mixed placements | `plan_all_inputs` loop; one branch per file into shared `graph.exec` |
| Outputs | `build_job_graph` write loop |

---

## 9. Completeness reasoning ↔ control flow

Section 3.5 Claim 1 (exhaustiveness) is the if-chain in `plan_one_input`: every legal $(R,T)$ hits $L$, $I$, $U$, or $B$ before the final throw.

Claim 2 (ordered unique label) is the same chain’s early returns; `InputAccessKind` stores exactly one label per input.

Claim 3 (terminating actions) is the switch in `build_job_graph` plus SoftRestage for $B$ and HEAD register/start.

Claim 4 (multi-destination $T$) is supported by per-destination queries plus `find_any_in_flight` without requiring $|T|=1$.

Claim 5 (table is a covering) is why the enum has four values, not one enum per table row: rows are instances of $\{L,I,U,B\}$.

Claim 6 (multi-input) is `plan_all_inputs` + AND-join at `graph.exec`.

---

## 10. Files touched and build

| File | Change |
|------|--------|
| `CGSim/include/job_executor.h` | Added `REMOTE_BOUND_WAIT`; documented $L/I/U/B$ |
| `CGSim/util/job_executor.cpp` | Observation-order planning; staging policy; join/register/start; SoftRestage for $B$ |
| `CGSim/include/file_manager.h` | Declared `find_any_in_flight` |
| `CGSim/util/file_manager.cpp` | Implemented `find_any_in_flight` |

Build: `cmake --build CGSim/build` succeeded (`libCGSim.dylib` and `cg-sim`). No simulation execution was performed for this document.

---

## 11. What to validate next (not done here)

When a simulation is run, the Section 3.5 gate still applies: FileRead Started equals Finished, jobs reach terminal success (or explicit clean fail only on the illegal empty corner), and no ker_engine WAITING dump. That validates the implementation against the completeness proof; this document only describes how the proof was coded.

<span style="color:red">

**Post-implementation run (2026-07-16, `output/dropin_test`, binary `CGSim/build/cg-sim` built 16:24, `events.db` written 16:29).** The Section 3.5 gate **failed**. Terminal `23` shows `cg-sim -c config.json` exited with code 0 after `real 44.13` / `user 39.01` / `sys 1.56`, but the process ended in a SimGrid `ker_engine` deadlock dump: a long list of activities left in `WAITING` (terminal buffer retains about 74 I/O + 36 execution lines; the dump is truncated in the scrollback). Exit code 0 is therefore **not** a successful workload finish.

**`events.db` counts (same failure class as before the L/I/U/B rewrite):** FileRead Started 11456 / Finished 7774 (**3682 unfinished**); JobExecution Started 611 / Finished 288 (**323 unfinished**); FileTransfer Started=Finished 607; BackGroundFileTransfer Started=Finished 173; FileWrite Started 288 / Finished 274. Peak concurrent open FileReads ≈ **3713** near sim time 7.8e4. Max event time ≈ **8.09e6**. All job FileTransfers finished by ≈ 9.6e4, so the stall is **not** “transfers never complete”; it is **FileReads / JobExecutions stuck in WAITING** after (or without) their predecessor Comms having finished.

**Interpretation against this implementation.** Observation-order planning ($L \succ I \succ U \succ B$) and `find_any_in_flight` did **not** clear the Started-but-never-Finished I/O failure. The remaining suspect aligned with Claim 3’s $I$ action is **`build_join_in_flight_branch`**: wiring `in_flight_comm->add_successor(read)` then starting the Read while the Comm may already be running or already finished. That can leave Reads permanently WAITING even when every FileTransfer/BackGroundFileTransfer event shows Finished—matching “all FT finished, thousands of FR unfinished.” Assignment no longer stages solely for $I$, so join is exercised on the hot path. Soft-restage for $B$ alone is insufficient if $I$-join is unsafe in SimGrid.

**Gate status:** not validated. Completeness of the table as a decision partition is not disproved by this run; **completeness of the coded $I$ (join) action is not demonstrated** and is the leading hypothesis for the deadlock. Next validation should either stage until local for inbound (proven earlier as EXEC_HEADJOB: 966/966) or prove that join-after-start/finish is well-defined before claiming the implementation matches Section 3.5 Claim 3.

</span>

---

## 3.12 Fix plan for unsafe inbound join ($I$)

This section is description only. No code changes are specified here as patches; it is the plan for how to fix the issue called out in Section 11.

### The scenario in simple English

Think of two jobs that both need the same file at the same site.

Job A (or a background policy) starts a network transfer that will bring the file to that site. While the transfer is still moving, Job B is assigned to the same site and also needs that file. The catalog may still say “file not here yet,” but the simulator knows a transfer is already on the way. Under observation label $I$ (inbound), Job B is told: do not start a second transfer; attach your local file read so it is supposed to run only after that existing transfer finishes.

In code that “attach” is `build_join_in_flight_branch`: take the already-existing communication activity, call `add_successor` so the new FileRead depends on it, then later `start()` the FileRead (and never start the communication again, because someone else already started it).

That sounds correct in English. The trap is timing inside SimGrid.

A communication activity has a life: not started, running, finished. Job B often arrives in the middle of that life, or even after it has already finished (the catalog might not have been re-checked yet, or the in-flight table still briefly names a Comm that has completed). Job B then does two things that are unsafe together:

1. It wires “when this Comm finishes, unlock my Read” onto a Comm that is **already running** or **already finished**.
2. It then **starts** the Read, which goes into WAITING because it believes it must wait for a predecessor signal that may never be delivered again (the “finished” event already happened, or successor wiring after start does not arm the dependency the way wiring-before-start does).

From the outside the run looks paradoxical: every FileTransfer and BackGroundFileTransfer in `events.db` shows Finished (the network work really did complete), but thousands of FileReads stay Started forever and JobExecutions wait on those reads. The deadlock dump is full of I/O and execution activities in WAITING. The data arrived; the **dependency handshake** for the late joiner did not.

So the bug is not “we forgot the inbound case in the table.” The table’s $I$ case is real. The bug is “implementing $I$ by joining a live or finished Comm with `add_successor` + start Read” is not a reliable SimGrid pattern in this codebase.

That also explains why an earlier experiment that **staged until the file was local**, then built a plain local read (no join), could finish all jobs: it never asked a late Read to wake up from a Comm it did not own from birth.

### What “fixed” must mean

For inbound ($I$), Job B must still obey the table:

- Never start a second transfer to the same destination.
- Eventually run a local file read after the file rests at the compute site.
- Leave no WAITING Read behind.

It does **not** have to mean “wire my Read as successor of someone else’s already-started Comm.” That was one implementation of $I$, not the only legal one. The first-principles doc already allows staging until local as an equivalent path for inbound.

### Proposed solution (preferred): treat $I$ like “wait outside, then $L$”

Keep the observation label $I$ in planning so we still detect inbound and refuse duplicate transfers. Change the **action** for $I$ so `execute_job` does not join.

When planning or assignment sees inbound to the compute site:

1. Do **not** dispatch into a graph that joins the foreign Comm (or, if already inside `execute_job`, soft-restage immediately).
2. Hold the job in assignment staging until observation becomes $L$ (resting replica at the compute site). Optionally also release when inbound has clearly ended and the catalog shows local.

<span style="color:red">

**Exact meaning of bullet 2 (how staging-until-$L$ is implemented with existing CGSim machinery).** “Hold in assignment staging until observation becomes $L$” is **not** a sleep inside `execute_job` and **not** a join on the inbound Comm. It is the assignment loop treating $I$ as “not yet dispatchable,” using the same staging list already used for remote-bound waits.

Concrete control flow:

- After `assignJob` sets `comp_host` / `comp_site`, the server calls `job_needs_transfer_staging(job)` before `dispatch_job_to_host`.
- That helper must call `plan_one_input` for every input. If **any** input’s `kind` is `WAIT_IN_FLIGHT_THEN_LOCAL_READ` ($I$) or `REMOTE_BOUND_WAIT` ($B$), return **true** (needs staging). Do **not** treat $I$ as dispatchable just because a Comm pointer exists.
- When true: `reserve_job_assignment(job)`, set status `"staged"`, `staging_jobs.push_back(job)`, and **skip** putting the job on the host message queue. So `receiver` / `execute_job` never run yet for that job — no `build_join_in_flight_branch`, no `add_successor` on a foreign Comm.
- On every server loop iteration (and whenever pending activities make progress), call `try_release_staged_jobs()`. For each staged job, call `job_needs_transfer_staging(job)` again.
- Release condition: `job_needs_transfer_staging` returns **false**. With the observation order $L \succ I \succ U \succ B$, that happens only when every input is either $L$ (`LOCAL_READ`, i.e. `exists(filename, comp_site)`) or a safe $U$ (remote resting, no inbound to this site). In particular, an input that was $I$ becomes $L$ only after the inbound transfer has completed **and** the catalog shows a resting replica at `comp_site`. At that moment `plan_one_input` hits the first branch (`fm->exists(filename, job->comp_site)`) and returns `LOCAL_READ`.
- On release: `dispatch_job_to_host(job)` → host MQ → `execute_job`. Planning now sees $L$ for that file and builds `build_local_read_branch` only.
- Defense in depth: if a race still presents $I$ inside `execute_job` (catalog lag), throw `SoftRestageSignal` / `return_job_to_staging` instead of joining — same “hold until $L$” policy, just re-entered from the host side by undoing dispatch into `staging_jobs`.

What “until observation becomes $L$” means in predicates: for that input, stop staging when $s \in R$ (compute site in the resting catalog), not merely when the Comm has finished in SimGrid. Finished Comm without catalog update is not enough to release; `exists(filename, comp_site)` is the gate. Finished Comm plus catalog create is what flips $I$ → $L$ on the next `plan_one_input` call from `try_release_staged_jobs`.

</span>

3. Only then call `execute_job`, which builds the ordinary local-read branch: create Read, wire Read → Exec, register, start — the HEAD path that already works for local files.
4. Pins still protect that local read from MOVE.

Intuitively: Job B sits in the waiting room until the package is on the shelf, then walks in and reads it. It does not try to clip its shopping list onto a delivery truck that already left the depot.

This matches the proven EXEC_HEADJOB behavior (stage for missing/in-flight inputs, HEAD-style local or transfer-owned graphs only) and removes the unsafe SimGrid dependency pattern.

### Why this still matches the completeness reasoning

Section 3.5 Claim 3 says $I$ must terminate. Termination can be:

- join then read, **or**
- wait until local, then read.

Both are “do not duplicate the delivery; consume after inbound lands.” The fix picks the second form because the first form is empirically unsafe here. Claims 1–2 (partition of states) stay unchanged. Claim 3’s $I$ row is refined: **action = stage until $L$, then local read**; join is deferred or abandoned until a proven-safe SimGrid pattern exists.

$B$ (remote-bound) already soft-restages; $I$ should use the same family of “wait, then re-observe” instead of join.

### Alternative (only if join must be kept later)

If a future change insists on true in-graph join, it would need a separate, carefully validated pattern, for example: only join when the Comm is known not-yet-finished and successor wiring is legal; if the Comm is already finished, skip join and build a pure local read (or restage one tick for catalog catch-up); never start a Read that is waiting on a dead predecessor. That path needs a dedicated small test before returning to the full drop-in workload. It is **not** the first fix to ship.

### Fix plan steps (order)

1. **Change $I$ semantics in the design narrative** (first principles / this report): inbound means “stage until local (or until catalog shows local after inbound completes); then $L$ branch.” Join is not the default action.
2. **Change staging policy:** `job_needs_transfer_staging` returns true for $I$ again (inbound blocks dispatch), as well as for $B$.
3. **Change `execute_job` / build:** if $I$ is still seen at plan time (race), SoftRestage instead of `build_join_in_flight_branch`. Do not call `add_successor` on foreign in-flight Comms on the hot path.
4. **Leave $U$ as owned transfer:** this job creates Comm then Read, wires before start, starts Comm then Read — HEAD-safe.
5. **Leave $L$ as local read** with pins.
6. **Rebuild and re-run** `output/dropin_test` against the Section 11 gate: FileRead Started = Finished, JobExecution Finished = 966 (or full activated set), no ker_engine WAITING dump.
7. **Only after green:** optionally research a safe join for latency; keep it behind a switch until proven.

### Success criterion

Same storage_rebalance drop-in config finishes without a deadlock dump, with FileRead Started equal Finished and all jobs completing execution. If that passes while join remains disabled, the Section 11 hypothesis is confirmed and the table stays complete with a safer $I$ action.

---

## 3.13 Post–stage-until-$L$ run: did the proposed fix work?

**Run under test.** Terminal `23` again shows `../../../../CGSim/build/cg-sim -c config.json` with `last_exit_code: 0`, wall time about `real 45.86` / `user 40.99` / `sys 1.55`, ending in a `ker_engine` WAITING dump (buffer retains roughly 81 I/O + 29 execution activities). Binary mtime **2026-07-16 16:50:27**; `output/dropin_test/events.db` mtime **16:51:21**. So this run used the **post–no-join / stage-until-$L$** build, not a stale join-enabled binary.

**Gate result.** Still failed.

| Metric | After stage-until-$L$ (this run) | Prior join-enabled L/I/U/B run (Section 11) |
|--------|----------------------------------|-----------------------------------------------|
| FileRead Started / Finished | 11456 / 7743 (3713 unfinished) | 11456 / 7774 (3682 unfinished) |
| JobExecution Started / Finished | 606 / 297 | 611 / 288 |
| FileTransfer unfinished | 0 | 0 |
| BackGroundFileTransfer unfinished | 0 | 0 |
| Peak open FileReads | ~3730 at t≈77926 | ~3713 at t≈77926 |
| Max event time | ~2.02e6 | ~8.09e6 |

Numbers are essentially the same failure class. JobExecution finished rose only slightly (288→297). Disabling join did **not** restore FileRead Started = Finished or clear the deadlock dump.

**Verdict on the Section 3.12 proposal.** The proposed solution was implemented (stage on $I$/$B$, SoftRestage instead of `add_successor` join) and this run exercised that code. **It does not work as a sufficient fix for this workload.** The Section 11 hypothesis that foreign-Comm join was *the* cause of thousands of unfinished FileReads is **not supported** by this A/B: with join removed, the stall remains.

**Why it is not (mainly) that join bug.** Evidence:

1. **Same early snowball.** All FileRead Started and Finished events occur at sim time $< 10^5$. Peak open reads still forms around $t \approx 77926$. The deadlock is not a late inbound-join race after transfers drain; the unfinished reads are already piled up early, then the sim idles for a long max time with transfers already finished.
2. **Canonical A/B counterexample still broken.** Job `4399526009`, file `125`: FileRead Started at $t=7974$, never Finished; that job has 14 FileRead Started vs 13 Finished and **no** JobExecution event. That is the same EXEC_ONLY signature from `analysis/cgsim_ab_record/CONCLUSION.md`, which appeared with the rewritten executor even when staging/pins were not the differentiator.
3. **Stuck jobs are activated L/U graphs.** Unfinished FileReads have `STATE=Started` in `events.db`, so they passed `register_job_graph` / `start_job_graph`. Under the new policy, $I$ never builds a joined Read. Those Started reads therefore come from $L$ (local read) and/or $U$ (this job owns Comm→Read). About **360** jobs have incomplete FileRead sets vs **91** with all reads finished — a systemic activation/liveness problem on the owned-graph path, not a niche inbound joiner.
4. **Transfers completing does not unblock reads.** FileTransfer and BackGroundFileTransfer again finish completely while FileReads remain Started. That pattern fit “join on already-finished Comm,” but it also fits “Read/Exec activities started in a dependency state that never becomes RUNNING for other reasons.” Removing join without restoring HEAD `execute_job` leaves the second class intact.

**Likely other reason (current best explanation).** The failure matches the earlier A/B conclusion: the **rewritten `execute_job` body / graph activation** (Plan→Build→Register→Start as currently coded for $L$ and $U$) still does not behave like the HEAD executor that finished 966/966 under EXEC_HEADJOB. Stage-until-$L$ was the right refinement for $I$ safety and should stay, but it was never sufficient alone. Remaining suspects to investigate next (not join):

- Whether multi-input $L$/$U$ wiring and start order still differ subtly from HEAD (all Reads predecessor of one Exec; start Comms then Reads then Exec then Writes).
- Whether SoftRestage or mid-build throws can leave the server/activity accounting in a bad state for *other* already-activated jobs (secondary).
- Whether something outside join (disk Io model, pin/unpin, policy MOVE under local read despite pins) stalls early Reads — less likely given EXEC_HEADJOB success with pins+staging under HEAD execute_job.

**Bottom line.** Stage-until-$L$ / no-join was a necessary safety change for $I$, but **this run shows it is not why the simulation still fails**. The dominant bug is still on the path that starts ordinary local and owned-transfer FileReads — the same layer EXEC_HEADJOB avoided by keeping HEAD’s `execute_job` implementation.

---

## 3.14 Root cause of Started-never-Finished FileReads (and proposed fix)

### Evidence that forced a sharper diagnosis

Stage-until-$L$ did not help (Section 3.13). The failure is almost entirely on **local** reads: of 3713 unfinished FileReads, **3419** belong to jobs with **zero** FileTransfer events. Join is irrelevant for those.

Canonical job `4399526009` (same counterexample as the A/B record):

- 14 local FileReads all `Started` at $t=7974$ on `AGLT2_site_27` (no transfer for this job).
- 13 finish by $t\approx 7995$ with **byte-for-byte the same finish timestamps** as the successful `ab_EXEC_HEADJOB` run.
- File `125` (~1.16 GB) `Started` at 7974 and **never** `Finished`. On HEADJOB it finishes at $t\approx 7991.16$ in the same finish order.
- After the 13 siblings finish, open FileReads on that site drop to **1** (only file `125`) for thousands of sim-seconds. A lone disk read at ~754 MB/s should finish in ~1.5 s. It does not. So that Io is **not on the disk scheduler** — it is not competing for bandwidth.
- Other jobs later read the same file `125` successfully (10 Finished / 20 Started globally). The file and disk are fine. **This specific activity instance** is broken.

So: same start order as HEADJOB, same sibling timings, one activity missing from disk progress → that activity was started (callback ran, `FileRead Started` logged) but then **lost from the live activity set / lifetime**, while siblings that remained referenced kept running exactly as on HEAD.

### Root cause

**`track_pending_activity` deduplicates by raw `Activity*` and can skip `pending_activities.push` when that address is still recorded in `tracked_pending_activities_`.** Combined with a bug in `advance_to_time`, the dedup set goes stale.

Relevant code today (`CGSim/util/job_executor.cpp`):

```cpp
void JOB_EXECUTOR::track_pending_activity(const sg4::ActivityPtr& activity)
{
  // ...
  if (tracked_pending_activities_.count(raw) > 0) {
    return;   // skips pending_activities.push
  }
  tracked_pending_activities_.insert(raw);
  pending_activities.push(activity);
}

void JOB_EXECUTOR::advance_to_time(double time)
{
  // ...
  pending_activities.wait_any_for(time - sg4::Engine::get_clock()); // return value discarded
  drain_completed_pending_activities();
}
```

`ActivitySet::wait_any_for` **removes** the completed activity from the set and returns it (SimGrid API). The current code **ignores that return value**, so it never runs `tracked_pending_activities_.erase(...)` for that completion. `drain_completed_pending_activities` only sees activities **still** in the set, so it cannot repair the miss.

Resulting sequence:

1. Some earlier activity (Mess job-transfer, Exec, Write, Comm, … — all share the `Activity*` address space) completes inside `advance_to_time` via `wait_any_for`.
2. Its raw pointer remains in `tracked_pending_activities_` after the object is freed.
3. A later `execute_job` creates a new Io/Comm/Exec that the allocator places at the **same address**.
4. `register_job_graph` → `track_pending_activity` sees the address in the set and **returns without `push`**.
5. `start_job_graph` still calls `start()`; `on_this_start` runs; `FileRead Started` is logged.
6. `execute_job` returns; `JobActivityGraph` destroys its `IoPtr`s. With no `ActivitySet` reference, refcount hits zero and the activity is **destroyed without a completion callback**.
7. Events show Started-never-Finished; the ker_engine dump shows leftover WAITING successors (Exec/Write) and any other activities entangled in the mess; open-read counts snowball because many jobs lose one or more inputs the same way.

This matches the A/B record precisely:

| Variant | Job I/O registration | Outcome |
|---------|----------------------|---------|
| `EXEC_HEADJOB` | HEAD `execute_job` uses `pending_activities.push` (**no dedup**) | 966/966, file `125` finishes |
| `EXEC_ONLY` / current | job I/O goes through `track_pending_activity` (**dedup**) | deadlock, file `125` Started forever |

Pins, MOVE, join, and staging are **not** required to explain this failure mode. They can stay as separate design pieces; they did not create this symptom.

### Why sibling finish times match HEADJOB

Destroyed file-`125` Io never holds disk share. The other 13 reads fair-share exactly as in a 13-way (not 14-way) progressive schedule that happens to align with HEADJOB’s observed finish clock for those files once `125` is omitted from the resource. That is a fingerprint of “activity missing from the resource,” not of “slow disk” or “policy MOVE.”

### Proposed solution (description only; implement next)

1. **Fix `advance_to_time` (mandatory).** Capture the result of `wait_any_for`. If it returns a completed activity, `tracked_pending_activities_.erase(act.get())` before draining. On `TimeoutException`, leave the set unchanged. Same discipline for every `wait_any` / `wait_any_for` call site (the `start_server` paths that already erase on `wait_any` are the template).

2. **Stop trusting raw-pointer dedup for liveness (mandatory).** Prefer one of:
   - **Strong fix (recommended):** `register_job_graph` / job I/O registration should `pending_activities.push` like HEAD / `EXEC_HEADJOB`, without a skip path; or
   - Keep a dedup set only if keys cannot dangle (e.g. erase in an `on_this_completion` hook on every tracked activity, never rely on wait returns alone), and never skip `push` unless the **live** `ActivitySet` already contains that activity.

3. **Keep stage-until-$L$ for inbound $I$** (Section 3.12). It is still the right semantics for join safety; it is orthogonal to this lifetime bug.

4. **Validation gate (same as Section 11).** Rebuild; rerun `output/dropin_test`. Require FileRead Started = Finished; JobExecution Finished = 966 (or full activated set); job `4399526009` file `125` Finished near $t\approx 7991$; no ker_engine WAITING dump; peak open FileReads back near the healthy ~148 regime, not thousands.

5. **Optional assert while bringing this up.** If dedup remains temporarily, log or abort when `track_pending_activity` would skip a push — that turns silent data loss into an immediate diagnostic.

### What we are not claiming

This root cause does not say the L/I/U/B observation table is wrong. It says the **implementation of registration into `pending_activities`** silently dropped activities, which the table’s completeness proof assumes never happens. Fix the lifetime/registration bug first; then the table’s $L$/$U$ actions can actually run to Finished.
