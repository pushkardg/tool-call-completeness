# Supplementary Material: Tool-Call Completeness Boundaries in LLM Agent Frameworks

This package contains the fault-injection simulator and sensitivity-sweep
script used in Section V ("Model-Based Fault-Injection Analysis") of the
paper, along with their raw output.

**Scope note:** both scripts are a controlled discrete-event simulation of
the six-state lifecycle model described in Section III. They are NOT a test
against live LangGraph, OpenAI, or AG2 infrastructure. Results illustrate
the detection behavior implied by the lifecycle model, not measured
production framework behavior.

## Files

- `lifecycle_sim.py` — main simulator. Produces the per-boundary detection
  rates, cross-attempt linkage rates, and duplicate-effect rates reported
  in Table II.
- `sensitivity.py` — sensitivity-sweep script. Produces the `p_flush` sweep
  and the PREPARED/SENT x SENT/ACKNOWLEDGED effect-probability sweep
  referenced in the paragraph following Table II.
- `table2_output.json` — raw JSON output of `lifecycle_sim.py`, from which
  Table II's rounded values are taken directly.
- `sweep_output.json` — raw JSON output of `sensitivity.py`, from which the
  sweep sentence's rounded values are taken directly.

## Parameters (fixed in both scripts, matching the paper)

- Random seed: `20260912`
- Trials per boundary/condition: `N_TRIALS = 2000`
- Default log-flush probability: `p_flush = 0.5`
- Default effect-already-occurred probability at the PREPARED/SENT
  boundary (B2): `0.5`
- Default effect-already-occurred probability at the SENT/ACKNOWLEDGED
  boundary (B3): `0.95`
- `p_flush` sweep range: `{0.25, 0.5, 0.75, 1.0}`
- Effect-probability sweep range: `p2 in {0.3, 0.5, 0.7}`, `p3 in {0.8, 0.95, 0.99}`

## Reproducing the paper's numbers

```bash
# Table II (detection rates, cross-attempt linkage, duplicate-effect rates,
# assumed durable-writes-per-attempt overhead)
python3 lifecycle_sim.py > table2_output.json

# Sensitivity sweeps (p_flush sweep and effect-probability sweep)
python3 sensitivity.py > sweep_output.json
```

Both scripts seed Python's `random` module with `20260912` at import time,
so repeated runs on the same Python version reproduce identical output.
No external dependencies are required beyond the Python 3 standard library.

## Mapping paper numbers to output fields

- Table II "Ambiguity correctly flagged (avg., B2/B3)" = average of
  `per_boundary.B2_dispatch_before_sent.<STRATEGY>.ambiguous_correctly_flagged_rate`
  and `per_boundary.B3_sent_before_ack.<STRATEGY>.ambiguous_correctly_flagged_rate`
  in `table2_output.json`.
- Table II "Cross-attempt linkage on retry (B4)" =
  `per_boundary.B4_during_retry.<STRATEGY>.cross_attempt_linkage_rate`.
- Table II "Duplicate effect" rows = averages of
  `duplicate_effect_rate_no_exec_idem` / `duplicate_effect_rate_with_exec_idem`
  across B2, B3, and B5 in `table2_output.json`.
- Table II "Assumed durable writes per attempt" = `overhead.<STRATEGY>.durable_writes_per_attempt`
  in `table2_output.json` (an assumption stated in code, not a measurement).

## Numbers NOT used in the paper (disclosure)

`table2_output.json`'s `overhead` section also contains a
`bytes_per_attempt_UNVERIFIED_ESTIMATE` field for each strategy. These are
**not cited anywhere in the paper** and should not be treated as
measurements: they are simple arithmetic on three hardcoded byte-size
constants (`LEDGER_RECORD_BYTES = 96`, `LOG_LINE_BYTES = 40`,
`CHECKPOINT_BYTES = 512` in `lifecycle_sim.py`) that are this author's own
unmeasured guesses at plausible record sizes, not a benchmark of any real
serialization format. They are left in the output, now clearly labeled,
rather than silently removed, so anyone inspecting the raw JSON isn't
misled into thinking they were measured.
- The `p_flush` sweep sentence (70.6%, 71.3%, 84.9%, 100.0%) =
  `p_flush_sweep.<VALUE>.avg` in `sweep_output.json`, rounded to one decimal.
  The `p_flush = 0.5` entry is intentionally *not* an independent recomputation:
  `sensitivity.py` reuses the exact value already computed by `lifecycle_sim.py`
  for Table II's "Ambiguity correctly flagged" row (see the
  `table2_p_flush_05_value()` function and the `source` field in
  `sweep_output.json`), so the two numbers match by construction rather than
  by coincidence of random-number-generator alignment across the two scripts.
- The effect-probability sweep range (56%-84%) = the min/max across all
  `effect_prob_sweep.*` entries in `sweep_output.json`.
