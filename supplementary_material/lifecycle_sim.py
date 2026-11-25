"""
Controlled simulation of the tool-call action lifecycle with fault injection.

This is NOT a test against real LangGraph/OpenAI infrastructure. It is a
discrete-event simulation of the lifecycle state machine described in the
paper (PROPOSED -> AUTHORIZED -> PREPARED -> SENT -> ACKNOWLEDGED ->
EFFECT_VERIFIED), used to compare three observability strategies under
injected crashes at the five boundaries a reviewer requested:

  B1: before dispatch (crash before PREPARED is written)
  B2: after dispatch, before the SENT write (transmission attempted;
      whether it actually reached the executor before the crash is
      itself uncertain and is resolved randomly per trial as ground truth)
  B3: after SENT, before ACKNOWLEDGED (transmission confirmed; executor
      applies the effect with high probability even though the process
      crashes before acknowledgment is recorded)
  B4: during retry (a second attempt is issued and itself crashes at a
      randomly chosen sub-boundary among B1-B3)
  B5: after effect execution, before result persistence (the effect is
      certain to have happened; only the terminal record is missing)

Three observability strategies are compared:
  LOGS       - ordinary application logging: each lifecycle event is
               "logged", but log lines are only durably flushed with
               probability p_flush before a crash (modeling ordinary,
               non-synchronous application logs).
  CHECKPOINT - framework-style checkpointing (e.g., LangGraph super-steps):
               only records whether the whole super-step completed, not
               which lifecycle sub-boundary was reached.
  LEDGER     - the paper's proposed action ledger: each lifecycle
               transition is written synchronously and durably before the
               corresponding step is taken.

For each strategy we compute, over many independent trials per boundary:
  - false_safe_rate: how often the strategy's evidence would let an
    auditor *incorrectly* conclude "no retry needed / safe to retry
    blindly" when the ground truth says otherwise (the dangerous failure
    mode a provenance mechanism should minimize)
  - ambiguous_correctly_flagged: how often a genuinely ambiguous case is
    correctly reported as ambiguous rather than misclassified
  - duplicate_effect_rate: of the trials where the system retries based on
    that strategy's evidence, how often a real duplicate external effect
    results, with and without executor-side idempotency support
  - durable_writes_per_attempt / bytes_per_attempt: a simple overhead proxy
"""
import random
import json
import statistics

random.seed(20260912)

N_TRIALS = 2000
P_FLUSH = 0.5          # probability an ordinary log line is durably flushed before a crash
P_EFFECT_BY_SENT = 0.95  # once SENT is durably recorded, probability the executor already applied the effect

BOUNDARIES = ["B1_before_dispatch", "B2_dispatch_before_sent", "B3_sent_before_ack",
              "B4_during_retry", "B5_effect_before_result_persist"]

LEDGER_RECORD_BYTES = 96   # UNMEASURED GUESS, not a benchmark: approx bytes for one structured ledger line (id/state/key/timestamp)
LOG_LINE_BYTES = 40        # UNMEASURED GUESS, not a benchmark: approx bytes for one unstructured log line
CHECKPOINT_BYTES = 512     # UNMEASURED GUESS, not a benchmark: approx bytes for one full StateSnapshot write


def ground_truth_for_boundary(boundary):
    """Return (effect_happened, ambiguous_in_reality) for one trial at this boundary."""
    if boundary == "B1_before_dispatch":
        return False, False  # nothing was sent; not actually ambiguous, safe to retry
    if boundary == "B2_dispatch_before_sent":
        # 50/50 whether the request actually reached the executor before the crash
        return (random.random() < 0.5), True
    if boundary == "B3_sent_before_ack":
        return (random.random() < P_EFFECT_BY_SENT), True
    if boundary == "B5_effect_before_result_persist":
        return True, False  # effect is certain; only the record is missing
    raise ValueError(boundary)


def simulate_attempt(boundary, strategy):
    """
    Simulate one attempt reaching `boundary` before a crash, observed
    through `strategy`. Returns a dict describing what the strategy would
    let an external auditor conclude, plus the ground truth.
    """
    effect_happened, really_ambiguous = ground_truth_for_boundary(boundary)

    # What lifecycle states were reached before the crash (ground truth process state)
    order = ["PROPOSED", "AUTHORIZED", "PREPARED", "SENT", "ACKNOWLEDGED", "EFFECT_VERIFIED"]
    if boundary == "B1_before_dispatch":
        reached = order[:2]  # PROPOSED, AUTHORIZED only
    elif boundary == "B2_dispatch_before_sent":
        reached = order[:3]  # ... PREPARED (SENT not yet durably written)
    elif boundary == "B3_sent_before_ack":
        reached = order[:4]  # ... SENT (ACK not yet recorded)
    elif boundary == "B5_effect_before_result_persist":
        reached = order[:4]  # effect happened externally, but only SENT is durable; ACK/EFFECT_VERIFIED never written
    else:
        raise ValueError(boundary)

    # --- What each strategy actually has durably available after the crash ---
    if strategy == "LOGS":
        # each reached state is logged, but only durably flushed with prob P_FLUSH, independently
        durable = [s for s in reached if random.random() < P_FLUSH]
        highest = durable[-1] if durable else None
    elif strategy == "CHECKPOINT":
        # only knows whether the super-step (whole node) completed; a crash mid-node means
        # the checkpoint shows the PRE-node state only, with no sub-boundary detail
        highest = None  # node did not complete -> no finer-grained evidence than "did not finish"
    elif strategy == "LEDGER":
        # every reached state is written synchronously and durably before proceeding
        highest = reached[-1]
    else:
        raise ValueError(strategy)

    # --- Auditor decision rule applied to what's durably available ---
    # "false_safe": auditor concludes no action needed / safe to blindly retry, but that's wrong
    # "ambiguous_flagged": auditor correctly reports "cannot determine, do not blindly retry"
    if highest in (None,):
        # no evidence beyond "the step did not finish" -> the safest correct behavior is to
        # flag ambiguous UNLESS boundary is B1, where "not started" really is safe
        if boundary == "B1_before_dispatch":
            auditor_says_ambiguous = False
            auditor_false_safe = False  # concluding "safe to retry" here is actually correct
        else:
            auditor_says_ambiguous = True
            auditor_false_safe = False
    elif highest in ("PROPOSED", "AUTHORIZED"):
        auditor_says_ambiguous = False
        auditor_false_safe = False  # correctly concludes nothing was dispatched
    elif highest == "PREPARED":
        # LEDGER's own documented rule: PREPARED w/o SENT is ambiguous, not safe
        auditor_says_ambiguous = True
        auditor_false_safe = False
    elif highest == "SENT":
        auditor_says_ambiguous = True
        auditor_false_safe = False
    elif highest in ("ACKNOWLEDGED", "EFFECT_VERIFIED"):
        auditor_says_ambiguous = False
        auditor_false_safe = True if not effect_happened else False
    else:
        raise ValueError(highest)

    return {
        "effect_happened": effect_happened,
        "really_ambiguous": really_ambiguous,
        "highest_durable_state": highest,
        "auditor_says_ambiguous": auditor_says_ambiguous,
        "auditor_false_safe": auditor_false_safe,
    }


def simulate_retry_duplicate(boundary, strategy, executor_idempotent):
    """
    Given the auditor's evidence, decide whether the system retries, and
    if so, whether that retry produces a real duplicate external effect.
    Policy: retry unless the auditor has definitive evidence the action
    already completed (ACKNOWLEDGED/EFFECT_VERIFIED with a real effect).
    """
    r = simulate_attempt(boundary, strategy)
    if r["auditor_says_ambiguous"] or r["highest_durable_state"] in (None, "PROPOSED", "AUTHORIZED", "PREPARED", "SENT"):
        will_retry = True
    else:
        will_retry = not r["effect_happened"]

    duplicate = False
    if will_retry and r["effect_happened"]:
        # a retry when the original effect already happened is a duplicate,
        # UNLESS the executor itself deduplicates on the idempotency key
        duplicate = not executor_idempotent
    return r, will_retry, duplicate


def run():
    results = {}
    for boundary in ["B1_before_dispatch", "B2_dispatch_before_sent", "B3_sent_before_ack", "B5_effect_before_result_persist"]:
        results[boundary] = {}
        for strategy in ["LOGS", "CHECKPOINT", "LEDGER"]:
            false_safe = 0
            ambiguous_correct = 0
            really_ambiguous_count = 0
            dup_no_exec = 0
            dup_with_exec = 0
            retried_count = 0
            for _ in range(N_TRIALS):
                r, will_retry, dup = simulate_retry_duplicate(boundary, strategy, executor_idempotent=False)
                if r["auditor_false_safe"]:
                    false_safe += 1
                if r["really_ambiguous"]:
                    really_ambiguous_count += 1
                    if r["auditor_says_ambiguous"]:
                        ambiguous_correct += 1
                if will_retry:
                    retried_count += 1
                if dup:
                    dup_no_exec += 1
                _, will_retry2, dup2 = simulate_retry_duplicate(boundary, strategy, executor_idempotent=True)
                if dup2:
                    dup_with_exec += 1

            results[boundary][strategy] = {
                "false_safe_rate": round(false_safe / N_TRIALS, 4),
                "ambiguous_correctly_flagged_rate": (round(ambiguous_correct / really_ambiguous_count, 4)
                                                      if really_ambiguous_count else None),
                "retry_rate": round(retried_count / N_TRIALS, 4),
                "duplicate_effect_rate_no_exec_idem": round(dup_no_exec / N_TRIALS, 4),
                "duplicate_effect_rate_with_exec_idem": round(dup_with_exec / N_TRIALS, 4),
            }

    # B4: during retry -- attempt-1 crashes at a random sub-boundary (B1-B3), then attempt-2
    # (linked by the same idempotency_key under LEDGER; NOT linked under LOGS/CHECKPOINT,
    # which have no cross-attempt identifier by construction) also crashes at a random sub-boundary.
    b4 = {}
    sub_boundaries = ["B1_before_dispatch", "B2_dispatch_before_sent", "B3_sent_before_ack"]
    for strategy in ["LOGS", "CHECKPOINT", "LEDGER"]:
        dup_no_exec = 0
        dup_with_exec = 0
        linked_count = 0
        for _ in range(N_TRIALS):
            b1 = random.choice(sub_boundaries)
            b2 = random.choice(sub_boundaries)
            r1 = simulate_attempt(b1, strategy)
            r2 = simulate_attempt(b2, strategy)
            # cross-attempt linkage: only the LEDGER schema carries action_id/idempotency_key
            # that ties attempt-1 and attempt-2 together for an auditor
            linked = (strategy == "LEDGER")
            if linked:
                linked_count += 1
            either_effect = r1["effect_happened"] or r2["effect_happened"]
            both_effect = r1["effect_happened"] and r2["effect_happened"]
            # without executor idempotency: if either attempt's request actually reached the
            # executor and a second attempt is also dispatched, a real duplicate can occur
            dup_no_exec += 1 if both_effect else 0
            # with executor idempotency: duplicates are suppressed regardless of linkage
            dup_with_exec += 0
        b4[strategy] = {
            "cross_attempt_linkage_rate": round(linked_count / N_TRIALS, 4),
            "duplicate_effect_rate_no_exec_idem": round(dup_no_exec / N_TRIALS, 4),
            "duplicate_effect_rate_with_exec_idem": round(dup_with_exec / N_TRIALS, 4),
        }
    results["B4_during_retry"] = b4

    # Overhead proxy: durable writes and bytes per attempt (no crash, happy path, one attempt)
    overhead = {
        "LOGS": {"durable_writes_per_attempt": 4, "bytes_per_attempt_UNVERIFIED_ESTIMATE": 4 * LOG_LINE_BYTES},
        "CHECKPOINT": {"durable_writes_per_attempt": 1, "bytes_per_attempt_UNVERIFIED_ESTIMATE": 1 * CHECKPOINT_BYTES},
        "LEDGER": {"durable_writes_per_attempt": 6, "bytes_per_attempt_UNVERIFIED_ESTIMATE": 6 * LEDGER_RECORD_BYTES},
    }
    # NOTE: the "durable_writes_per_attempt" counts are a design-level assumption
    # (how many synchronous writes each strategy's description implies), used in
    # the paper's Table II labeled explicitly as "Assumed durable writes per
    # attempt." The "bytes_per_attempt_UNVERIFIED_ESTIMATE" fields below are NOT
    # used anywhere in the paper -- they are illustrative arithmetic on the three
    # *_BYTES constants above, which are themselves this author's unmeasured
    # guesses at plausible record sizes, not a benchmark of any real serialization
    # format. Do not cite these byte figures as if they were measured.

    return results, overhead


if __name__ == "__main__":
    results, overhead = run()
    print(json.dumps({"per_boundary": results, "overhead": overhead}, indent=2))
