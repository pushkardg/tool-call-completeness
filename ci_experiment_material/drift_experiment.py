"""
CI-focused experiment: does the action-ledger's per-attempt deduplication
change an anomaly detector's behavior on a realistic CI/ops monitoring task?

Setup, directly tied to the paper's own incident-remediation workload
(Section IV): an on-call system counts "restart_pod" remediation actions
per hourly bucket to detect an incident surge (a real change in the rate
of required remediations, e.g. a genuine cluster-wide problem). Two
pipelines consume the SAME underlying stream of actions:

  NAIVE:              counts raw dispatched attempts, including duplicates
                       from retries after ambiguous crashes (no
                       deduplication).
  LEDGER-DEDUPLICATED: counts distinct action_id values via the ledger,
                       collapsing attempt-1/attempt-2 retries of the same
                       logical action into one count, exactly as the
                       paper's lifecycle model intends.

This does NOT re-run the crash-injection harness (that would be far too
slow at this scale via subprocess spawns). Instead it reuses the ALREADY
MEASURED, already-reported conditional duplicate rate from
lifecycle_sim.py / Table II (~82% duplicate rate at a crash before
ACKNOWLEDGED, without executor-side idempotency) as the parameter driving
how often a crashed-and-retried action produces two observed effects
instead of one. Nothing here is a new, unjustified assumption; it is the
paper's own already-verified number applied to a new question: does this
duplication corrupt a downstream monitoring decision, and does the
ledger's deduplication capability fix it?

Detector: a simple, standard one-sided CUSUM (cumulative sum) change-point
detector over hourly action counts, comparing each pipeline's observed
counts against a baseline rate estimated from the first quarter of the
stream.
"""
import random
import json
import math

random.seed(20260912)

N_BUCKETS = 200            # hourly buckets
CHANGE_POINT = 100         # true incident surge begins here
BASELINE_RATE = 5.0        # true mean remediation actions/hour, baseline
SURGE_RATE = 9.0           # true mean remediation actions/hour, during incident
CRASH_RATE = 0.12          # fraction of actions that hit an ambiguous pre-ACK crash, normal conditions
STORM_CRASH_RATE = 0.55    # fraction during a transient retry storm (e.g. a brief network blip)
STORM_DURATION = 8         # hours the storm lasts
CONDITIONAL_DUP_RATE = 0.82  # reused directly from Table II / lifecycle_sim.py:
                              # P(duplicate effect | crash before ACK, no exec idempotency)
N_TRIALS = 2000              # independent stream replications


def simulate_stream():
    """
    Returns (true_counts, naive_counts, dedup_counts) per bucket, all
    length N_BUCKETS. true_counts is the ground truth (what a perfect
    monitor would see). naive_counts includes duplicate effects from
    retried, crashed actions. dedup_counts is what a ledger-aware
    consumer sees after collapsing attempts sharing one action_id.

    A transient "retry storm" -- an unrelated, temporary spike in crash
    rate (e.g. a brief network blip), independent of any real incident --
    is injected at a random point before CHANGE_POINT, to test whether
    duplicate effects from unrelated infra noise can be mistaken for a
    genuine incident surge.
    """
    storm_start = random.randint(10, CHANGE_POINT - STORM_DURATION - 5)
    true_counts = []
    naive_counts = []
    dedup_counts = []
    for t in range(N_BUCKETS):
        rate = SURGE_RATE if t >= CHANGE_POINT else BASELINE_RATE
        in_storm = storm_start <= t < storm_start + STORM_DURATION
        crash_rate = STORM_CRASH_RATE if in_storm else CRASH_RATE
        # true number of logical remediation actions genuinely required this hour
        n_true_actions = poisson(rate)
        naive = 0
        dedup = 0
        for _ in range(n_true_actions):
            dedup += 1  # ledger always counts the logical action exactly once
            attempts = 1
            if random.random() < crash_rate:
                # ambiguous crash before ACK -> a retry is issued (attempt-2)
                if random.random() < CONDITIONAL_DUP_RATE:
                    attempts = 2  # both attempts' effects land in the raw store
            naive += attempts
        true_counts.append(n_true_actions)
        naive_counts.append(naive)
        dedup_counts.append(dedup)
    return true_counts, naive_counts, dedup_counts


def poisson(lam):
    # Knuth's algorithm; avoids a numpy dependency
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= random.random()
        if p <= L:
            return k - 1


def cusum_detect(counts, baseline_window=25, k_sigma=0.5, h_sigma=5.0):
    """
    One-sided CUSUM over a count series. Baseline mean/std estimated from
    the first `baseline_window` buckets (representative of the pre-surge
    period in both the true stream and any realistic deployment, where a
    monitor calibrates against recent quiet history). Returns the first
    bucket index where the cumulative sum exceeds the alarm threshold, or
    None if it never fires.
    """
    baseline = counts[:baseline_window]
    mu = sum(baseline) / len(baseline)
    var = sum((x - mu) ** 2 for x in baseline) / max(1, len(baseline) - 1)
    sigma = math.sqrt(var) if var > 0 else 1.0
    k = k_sigma * sigma
    h = h_sigma * sigma
    s = 0.0
    for i, x in enumerate(counts):
        s = max(0.0, s + (x - mu - k))
        if s > h:
            return i
    return None


def run():
    naive_alarms = []
    dedup_alarms = []
    naive_false_positive = 0
    dedup_false_positive = 0
    naive_detected = 0
    dedup_detected = 0
    naive_delays = []
    dedup_delays = []

    for _ in range(N_TRIALS):
        true_c, naive_c, dedup_c = simulate_stream()
        naive_alarm = cusum_detect(naive_c)
        dedup_alarm = cusum_detect(dedup_c)

        if naive_alarm is not None and naive_alarm < CHANGE_POINT:
            naive_false_positive += 1
        if dedup_alarm is not None and dedup_alarm < CHANGE_POINT:
            dedup_false_positive += 1

        if naive_alarm is not None and naive_alarm >= CHANGE_POINT:
            naive_detected += 1
            naive_delays.append(naive_alarm - CHANGE_POINT)
        if dedup_alarm is not None and dedup_alarm >= CHANGE_POINT:
            dedup_detected += 1
            dedup_delays.append(dedup_alarm - CHANGE_POINT)

        naive_alarms.append(naive_alarm)
        dedup_alarms.append(dedup_alarm)

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    result = {
        "n_trials": N_TRIALS,
        "params": {
            "n_buckets": N_BUCKETS, "change_point": CHANGE_POINT,
            "baseline_rate": BASELINE_RATE, "surge_rate": SURGE_RATE,
            "crash_rate": CRASH_RATE, "storm_crash_rate": STORM_CRASH_RATE,
            "storm_duration_hours": STORM_DURATION,
            "conditional_dup_rate_source": "Table II / lifecycle_sim.py (measured, not assumed here)",
            "conditional_dup_rate": CONDITIONAL_DUP_RATE,
        },
        "naive_pipeline": {
            "false_positive_rate": round(naive_false_positive / N_TRIALS, 4),
            "true_detection_rate": round(naive_detected / N_TRIALS, 4),
            "mean_detection_delay_hours": round(mean(naive_delays), 2) if naive_delays else None,
        },
        "ledger_deduplicated_pipeline": {
            "false_positive_rate": round(dedup_false_positive / N_TRIALS, 4),
            "true_detection_rate": round(dedup_detected / N_TRIALS, 4),
            "mean_detection_delay_hours": round(mean(dedup_delays), 2) if dedup_delays else None,
        },
    }
    return result


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
