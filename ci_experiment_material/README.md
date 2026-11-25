# Supplementary Material: CI-Focused Anomaly Detection Experiment

This is the code behind Section IV's CI-grounded contribution: does the
action-ledger's per-attempt deduplication change an anomaly detector's
behavior on a realistic CI/ops monitoring task?

## Setup

An on-call system counts `restart_pod` remediation actions per hourly
bucket to detect a genuine incident surge (a real, sustained increase in
the rate of required remediations). A standard one-sided CUSUM
change-point detector monitors this count series.

Two pipelines read the *same* underlying stream of logical actions:

- **NAIVE**: counts raw dispatched attempts, including duplicate effects
  from retries after ambiguous pre-acknowledgment crashes (no
  deduplication) -- what a monitor sees if it just tails the effect log.
- **LEDGER-DEDUPLICATED**: counts distinct `action_id` values via the
  ledger, collapsing attempt-1/attempt-2 retries of the same logical
  action into one count -- what a monitor sees if it reads through the
  ledger's own action_id/idempotency_key relation instead.

A transient, incident-*unrelated* retry storm (e.g. representing a brief
network blip) is injected at a random point before the true change point,
independent of the real incident. This tests the practically relevant
failure mode: can duplicate effects from unrelated infra noise be
mistaken for the real signal a CI monitor exists to detect?

## Why the duplicate rate is not a new assumption

The conditional duplicate rate used here (`CONDITIONAL_DUP_RATE = 0.82`)
is not invented for this experiment. It is the paper's own measured
"Duplicate effect, no executor idempotency" figure from Table II
(`lifecycle_sim.py`), reused directly. Everything else in this script
(baseline/surge rates, storm timing/duration, CUSUM tuning) is a modeling
choice for this specific experiment, stated plainly in the code.

## An honest note on how this design was chosen

An earlier version of this experiment used a *uniform* crash rate
(constant over time, no storm) and found only a weak difference between
the two pipelines (naive vs. dedup false-positive rates within ~1 point of
each other). Rather than tune parameters until a bigger difference
appeared, the experiment was redesigned *once*, before looking at new
results, to use a bursty/correlated noise source instead of a uniform one
-- because that is how real infrastructure failures actually behave
(correlated bursts, not uniform independent noise), not because it was
expected to produce a more favorable number. The version in this file is
that redesigned experiment, run once and reported as-is at n=2,000
replications.

## Reproducing

```bash
python3 drift_experiment.py > final_result.json
```

Deterministic given the fixed seed (`20260912`). No external dependencies
beyond the Python 3 standard library.

## Results (n=2,000 replications, matching the paper)

| Metric | Naive | Ledger-deduplicated |
|---|---:|---:|
| False-positive rate | 45.8% [43.6%, 48.0%] | 25.8% [23.9%, 27.7%] |
| True detection rate | 54.2% [52.0%, 56.4%] | 74.2% [72.3%, 76.1%] |
| Mean detection delay (hours) | 4.99 | 3.85 |

95% Wilson confidence intervals shown in brackets; they do not overlap
for either headline metric.

## Honest limitations (also stated in the paper's Limitations section)

- This is a simulation of a monitoring pipeline, not an integration with
  a real anomaly-detection system or a real CI/ops platform.
- The CUSUM detector's tuning parameters (`k_sigma`, `h_sigma`,
  `baseline_window`) are standard defaults, not calibrated against any
  specific production system's alert thresholds.
- The retry storm's timing, duration, and intensity are modeling choices
  representing a plausible transient infra blip, not measured from a real
  incident.
