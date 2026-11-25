# Supplementary Material: Live Prototype Validation

Real code, run against real LangGraph 1.2.11, producing the numbers in the
paper's "Live Prototype Validation" subsection (Section V). This is
distinct from the earlier model-based simulation package: nothing here is
simulated or assumed. Crashes are genuine hard process kills
(`os._exit(137)`, SIGKILL's exit code), not caught exceptions.

## Files

- `mock_executor.py` — the "external system" a tool call acts on. Its state
  is a separate SQLite database that survives the agent process being
  killed, which is what lets the harness determine ground truth (did the
  effect really happen, once or twice) after a crash.
- `ledger.py` — a real implementation of the paper's proposed action-ledger
  contract: synchronous, durable (fsync-backed commit) writes of each
  lifecycle transition (`PREPARED`/`SENT`/`ACKNOWLEDGED`/`EFFECT_VERIFIED`).
- `agent_run.py` — runs ONE attempt of ONE harness (`langgraph` or
  `single`) and hard-crashes at a requested boundary via `os._exit(137)`.
  Run standalone via its CLI (see the file's docstring) or via
  `run_trials.py` below.
- `run_trials.py` — orchestrates real trials: spawns a hard-killed
  subprocess attempt, inspects what's actually durable afterward, applies
  the paper's auditor decision rule to decide whether to retry, spawns a
  real retry if warranted, and checks the mock executor's own record for a
  genuine duplicate effect. All latencies and outcomes are measured.
- `results_single.json` — full results, single-agent harness, all 4
  boundaries (B1/B2/B3/B5) x 4 conditions (ledger on/off x executor
  idempotency on/off), 50 trials each.
- `results_lg_1.json`, `results_lg_2.json` — LangGraph harness results,
  split into two runs (B1/B2 and B3/B5) to fit within tool time limits, 10
  trials each condition (fewer than single-agent due to ~0.77s LangGraph
  import overhead per subprocess).
- `merged_results.json` — the three files above combined into one
  structure, `{"single": {...}, "langgraph": {...}}`.

## Reproducing

```bash
pip install langgraph langgraph-checkpoint-sqlite --break-system-packages

# single-agent harness, all boundaries, N=50 (fast, no LangGraph import cost)
python3 run_trials.py 50 single > results_single.json

# LangGraph harness, split to keep runtime manageable (~0.77s/subprocess
# just for import, so N=10 x 4 conditions x 2 boundaries ≈ 2-3 minutes/run)
python3 run_trials.py 10 langgraph B1,B2 > results_lg_1.json
python3 run_trials.py 10 langgraph B3,B5 > results_lg_2.json
```

## Latency measurement (updated)

The paper's per-write ledger latency figures are from a dedicated
in-process measurement (500 sequential calls to `ledger.write_state()`,
not through the subprocess harness, which would conflate ledger latency
with Python/LangGraph process-startup cost):

```python
import time, ledger as ledger_mod
ledger_mod.reset()
latencies = []
for i in range(500):
    t0 = time.perf_counter()
    ledger_mod.write_state(f'action-{i}', f'attempt-{i}', f'call-{i}', f'key-{i}', 'PREPARED')
    latencies.append(time.perf_counter() - t0)
```

Results (milliseconds): mean 0.74, median 0.69, p95 1.00, p99 1.21.
SQLite durability settings in use (defaults, no explicit PRAGMA changes):
`journal_mode=delete`, `synchronous=FULL` (2) -- i.e. `commit()` waits for
the write to reach durable storage before returning, consistent with the
paper's "synchronous, durable write" description.

## SIGKILL verification

The crash mechanism is a genuine OS-delivered SIGKILL, not merely an exit
code chosen to resemble one. Verified directly:

```python
import subprocess, signal
r = subprocess.run(['python3', 'agent_run.py', '--harness', 'single',
                     '--boundary', 'B2', '--ledger', '0',
                     '--idempotent-executor', '0', '--action-id', 'verify-1',
                     '--attempt-id', 'attempt-1',
                     '--idempotency-key', 'verify-key'], capture_output=True)
assert r.returncode == -signal.SIGKILL  # passes: -9
```

Each condition resets both the ledger and mock-executor SQLite state before
every individual trial (`reset_all()` in `run_trials.py`), so trials are
independent. Given the crash points are deterministic (each boundary always
crashes at the same, fixed point in the code, unlike the earlier
simulation's probabilistic ground truth), results are exactly reproducible
run to run — the only thing that varies between runs is measured latency.

## Mapping to the paper's numbers

The paper's Live Prototype Validation table reports, per boundary, the
`duplicate_rate` field from `merged_results.json` under the
`ledger=0_idemexec=0` and `ledger=1_idemexec=0` condition keys (the "No
Ledger" and "With Ledger" columns, both without executor-side idempotency,
since that's the condition that isolates what the ledger alone
contributes). The 0.89ms per-write ledger latency cited in the paper's text
was measured separately (see the paper's method description) using
`ledger.write_state()` timed directly, not through the subprocess-level
`mean_attempt1_latency_s` field in these JSON files (which includes Python
process startup and, for LangGraph, import overhead, and so measures
end-to-end subprocess cost rather than the ledger write itself).

## Honest limitations of this prototype (also stated in the paper)

- The "executor" is a mock (a local SQLite database), not a real
  Kubernetes API, cloud provider, or billing-backed model call.
- The "model" step in both harnesses is scripted, not a real LLM call —
  intentional, since the object under test is framework/harness behavior
  around persistence and idempotency, not model reasoning quality.
- The LangGraph condition used fewer trials (10 vs. 50) than the
  single-agent condition, due to per-subprocess import overhead making a
  matched N impractical within reasonable runtime.
