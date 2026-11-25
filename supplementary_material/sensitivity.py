import random
import json
import os

random.seed(20260912)

N_TRIALS = 2000

def ground_truth_for_boundary(boundary, p_effect_b2, p_effect_b3):
    if boundary == "B2":
        return (random.random() < p_effect_b2), True
    if boundary == "B3":
        return (random.random() < p_effect_b3), True
    raise ValueError(boundary)

def simulate_attempt(boundary, strategy, p_flush, p_effect_b2, p_effect_b3):
    effect_happened, really_ambiguous = ground_truth_for_boundary(boundary, p_effect_b2, p_effect_b3)
    order = ["PROPOSED", "AUTHORIZED", "PREPARED", "SENT"]
    reached = order[:3] if boundary == "B2" else order[:4]

    if strategy == "LOGS":
        durable = [s for s in reached if random.random() < p_flush]
        highest = durable[-1] if durable else None
    elif strategy == "CHECKPOINT":
        highest = None
    elif strategy == "LEDGER":
        highest = reached[-1]
    else:
        raise ValueError(strategy)

    if highest is None:
        auditor_says_ambiguous = True
    elif highest in ("PROPOSED", "AUTHORIZED"):
        auditor_says_ambiguous = False
    else:  # PREPARED or SENT
        auditor_says_ambiguous = True

    return effect_happened, really_ambiguous, auditor_says_ambiguous


def table2_p_flush_05_value():
    """
    The p_flush = 0.5 point in the sweep is the SAME quantity Table II
    reports (LOGS's B2/B3 average ambiguity-flagging rate at the paper's
    default p_flush = 0.5). Rather than recompute it here with a different
    call sequence -- which, even under the same seed, would consume the
    random stream differently than lifecycle_sim.py's own run and could
    legitimately land on a different (but equally valid) Monte Carlo
    estimate -- this reuses lifecycle_sim.py's own output directly, so the
    number reported in the paper's sensitivity sentence and in Table II are
    identical by construction, not by coincidence of RNG alignment.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "table2_output.json")
    with open(path) as f:
        d = json.load(f)
    pb = d["per_boundary"]
    b2 = pb["B2_dispatch_before_sent"]["LOGS"]["ambiguous_correctly_flagged_rate"]
    b3 = pb["B3_sent_before_ack"]["LOGS"]["ambiguous_correctly_flagged_rate"]
    return b2, b3, (b2 + b3) / 2


def run_sensitivity():
    out = {}
    # Sweep p_flush for LOGS ambiguity-flagging rate (B2/B3 average), holding effect probs fixed
    out["p_flush_sweep"] = {}
    for p_flush in [0.25, 0.5, 0.75, 1.0]:
        if p_flush == 0.5:
            # Reuse Table II's own run instead of an independent recomputation
            # (see table2_p_flush_05_value docstring) so the two reported
            # numbers match exactly rather than differing by Monte Carlo noise.
            b2r, b3r, avg = table2_p_flush_05_value()
            out["p_flush_sweep"][str(p_flush)] = {
                "B2_ambiguous_flagged": round(b2r, 4),
                "B3_ambiguous_flagged": round(b3r, 4),
                "avg": round(avg, 4),
                "source": "reused from table2_output.json (lifecycle_sim.py), not independently recomputed",
            }
            continue
        rates = []
        for boundary in ["B2", "B3"]:
            correct = 0
            total = 0
            for _ in range(N_TRIALS):
                eff, amb, flagged = simulate_attempt(boundary, "LOGS", p_flush, 0.5, 0.95)
                if amb:
                    total += 1
                    if flagged:
                        correct += 1
            rates.append(correct / total if total else None)
        out["p_flush_sweep"][str(p_flush)] = {
            "B2_ambiguous_flagged": round(rates[0], 4),
            "B3_ambiguous_flagged": round(rates[1], 4),
            "avg": round(sum(rates) / 2, 4),
            "source": "independently computed in this script",
        }

    # Sweep B2/B3 effect probabilities' impact on duplicate-effect rate without executor idempotency,
    # for LEDGER (representative; CHECKPOINT/LOGS behave identically on this metric since none of
    # the three strategies alone change whether a retry is issued or whether the effect occurred)
    out["effect_prob_sweep"] = {}
    for p2 in [0.3, 0.5, 0.7]:
        for p3 in [0.8, 0.95, 0.99]:
            dup = 0
            trials = 0
            for boundary, p_eff in [("B2", p2), ("B3", p3)]:
                for _ in range(N_TRIALS):
                    eff, amb, flagged = simulate_attempt(boundary, "LEDGER", 0.5, p2, p3)
                    trials += 1
                    # policy: retry whenever ambiguous; duplicate iff effect already happened and we retry
                    if flagged and eff:
                        dup += 1
            out["effect_prob_sweep"][f"p2={p2}_p3={p3}"] = round(dup / trials, 4)

    return out


if __name__ == "__main__":
    print(json.dumps(run_sensitivity(), indent=2))
