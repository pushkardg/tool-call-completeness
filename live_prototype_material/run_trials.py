"""
Runs real trials: for each (harness, boundary, ledger on/off, executor
idempotent on/off) condition, spawns a genuinely hard-killed subprocess
attempt, inspects what's actually recoverable afterward, decides whether to
retry using the SAME auditor rule the paper's simulation uses, spawns a
real retry attempt if warranted, and checks the mock executor's own record
to see whether a real duplicate effect resulted. All latencies and outcomes
are measured, not assumed.
"""
import subprocess
import signal
import sys
import os
import time
import json
import sqlite3
import statistics

sys.path.insert(0, os.path.dirname(__file__))
import mock_executor
import ledger as ledger_mod

HERE = os.path.dirname(__file__)
AGENT_RUN = os.path.join(HERE, "agent_run.py")
LANGGRAPH_DB = os.path.join(HERE, "langgraph_checkpoints.db")


def reset_all():
    mock_executor.reset()
    ledger_mod.reset()
    if os.path.exists(LANGGRAPH_DB):
        os.remove(LANGGRAPH_DB)


def spawn_attempt(harness, boundary, use_ledger, idempotent_executor, action_id, attempt_id, idempotency_key):
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, AGENT_RUN,
         "--harness", harness, "--boundary", boundary,
         "--ledger", str(use_ledger), "--idempotent-executor", str(idempotent_executor),
         "--action-id", action_id, "--attempt-id", attempt_id,
         "--idempotency-key", idempotency_key],
        capture_output=True, text=True, timeout=30,
    )
    elapsed = time.perf_counter() - t0
    crashed = (proc.returncode == -signal.SIGKILL)
    return {"elapsed": elapsed, "crashed": crashed, "returncode": proc.returncode, "stderr": proc.stderr}


def auditor_decision(harness, use_ledger, action_id):
    """
    What can an external auditor actually determine after the crash, using
    only what's durably on disk? Mirrors the paper's decision rule:
    ambiguous/unknown states => retry; ACKNOWLEDGED or EFFECT_VERIFIED => don't.
    Returns (observed_state_description, will_retry: bool)
    """
    if use_ledger:
        state = ledger_mod.highest_state_for_action(action_id)
        if state in (None, "PREPARED", "SENT"):
            return state, True
        if state in ("ACKNOWLEDGED", "EFFECT_VERIFIED"):
            return state, False
        return state, True

    if harness == "langgraph":
        # No ledger: the only durable evidence is LangGraph's own checkpoint.
        # Check whether the checkpoint shows the node's super-step completed.
        if not os.path.exists(LANGGRAPH_DB):
            return "no_checkpoint", True
        conn = sqlite3.connect(LANGGRAPH_DB)
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (action_id,)
            )
            n = cur.fetchone()[0]
        finally:
            conn.close()
        # A completed run produces more checkpoint rows than a crashed mid-node run;
        # but critically, this tells us nothing about the tool call itself, only
        # that "some checkpoint exists" -- so treat any checkpoint presence as
        # ambiguous (node may or may not have finished the remediation node).
        return f"checkpoint_rows={n}", True

    # single-agent, no ledger: nothing durable at all
    return "no_evidence", True


def run_condition(harness, boundary, use_ledger, idempotent_executor, n_trials, action_prefix):
    results = []
    for i in range(n_trials):
        action_id = f"{action_prefix}-{harness}-{boundary}-L{use_ledger}-I{idempotent_executor}-{i}"
        idem_key = action_id + "-key"
        reset_all()

        r1 = spawn_attempt(harness, boundary, use_ledger, idempotent_executor,
                            action_id, "attempt-01", idem_key)

        ground_truth_effects_after_attempt1 = mock_executor.effect_count(idem_key)

        obs_state, will_retry = auditor_decision(harness, use_ledger, action_id)

        r2 = None
        if will_retry:
            r2 = spawn_attempt(harness, "NONE", use_ledger, idempotent_executor,
                                action_id, "attempt-02", idem_key)

        final_effects = mock_executor.effect_count(idem_key)
        duplicate = final_effects >= 2

        results.append({
            "ground_truth_effects_after_attempt1": ground_truth_effects_after_attempt1,
            "observed_state": obs_state,
            "retried": will_retry,
            "final_effect_count": final_effects,
            "duplicate": duplicate,
            "attempt1_elapsed_s": r1["elapsed"],
            "attempt2_elapsed_s": (r2["elapsed"] if r2 else None),
        })
    return results


def summarize(results):
    dup_rate = sum(1 for r in results if r["duplicate"]) / len(results)
    retry_rate = sum(1 for r in results if r["retried"]) / len(results)
    lat1 = statistics.mean(r["attempt1_elapsed_s"] for r in results)
    return {"duplicate_rate": dup_rate, "retry_rate": retry_rate, "mean_attempt1_latency_s": round(lat1, 4), "n": len(results)}


if __name__ == "__main__":
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    harnesses = sys.argv[2].split(",") if len(sys.argv) > 2 else ["langgraph", "single"]
    boundaries = sys.argv[3].split(",") if len(sys.argv) > 3 else ["B1", "B2", "B3", "B5"]
    out = {}
    for harness in harnesses:
        out[harness] = {}
        for boundary in boundaries:
            out[harness][boundary] = {}
            for use_ledger in [0, 1]:
                for idem_exec in [0, 1]:
                    key = f"ledger={use_ledger}_idemexec={idem_exec}"
                    print(f"Running {harness}/{boundary}/{key} x{N} ...", file=sys.stderr)
                    res = run_condition(harness, boundary, use_ledger, idem_exec, N, "trial")
                    out[harness][boundary][key] = summarize(res)
                    # flush progress incrementally so a timeout doesn't lose everything
                    fname = os.path.join(HERE, f"partial_{harness}_{boundary}.json")
                    with open(fname, "w") as f:
                        json.dump(out[harness][boundary], f, indent=2)
    print(json.dumps(out, indent=2))
