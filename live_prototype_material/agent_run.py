"""
Runs ONE agent attempt, for ONE harness (langgraph or single-agent loop),
and hard-crashes at the requested lifecycle boundary via a genuine
SIGKILL delivered to its own process (os.kill(os.getpid(), signal.SIGKILL))
-- not os._exit() and not a Python exception -- so no try/finally,
atexit, or interpreter cleanup runs. This is what makes the crash real
rather than a controlled exception the surrounding code could still react to.

Usage:
  python3 agent_run.py --harness langgraph --boundary B2 --ledger 1 \
      --action-id restart-incident-4471 --attempt-id attempt-01 \
      --idempotency-key incident-4471-restart --idempotent-executor 0
"""
import argparse
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(__file__))
import mock_executor
import ledger as ledger_mod


def run_single_agent(args):
    """Minimal single-agent tool-calling loop: no framework checkpointing at
    all -- whatever durability exists comes only from the ledger, if enabled."""
    tool_call_id = "call_" + args.attempt_id

    if args.ledger:
        ledger_mod.write_state(args.action_id, args.attempt_id, tool_call_id, args.idempotency_key, "PREPARED")

    if args.boundary == "B1":
        os.kill(os.getpid(), signal.SIGKILL)  # crash before any transmission attempt

    # "transmission": the tool is actually called
    result = mock_executor.apply_effect("restart_pod", args.idempotency_key, bool(args.idempotent_executor))

    if args.boundary == "B2":
        os.kill(os.getpid(), signal.SIGKILL)  # crash after the call, before SENT is durably recorded

    if args.ledger:
        ledger_mod.write_state(args.action_id, args.attempt_id, tool_call_id, args.idempotency_key, "SENT")

    if args.boundary == "B3":
        os.kill(os.getpid(), signal.SIGKILL)  # crash after SENT, before ACKNOWLEDGED

    if args.ledger:
        ledger_mod.write_state(args.action_id, args.attempt_id, tool_call_id, args.idempotency_key, "ACKNOWLEDGED")

    if args.boundary == "B5":
        os.kill(os.getpid(), signal.SIGKILL)  # crash after effect + ack, before EFFECT_VERIFIED / result persistence

    if args.ledger:
        ledger_mod.write_state(args.action_id, args.attempt_id, tool_call_id, args.idempotency_key, "EFFECT_VERIFIED")

    print("single-agent: completed normally")


def run_langgraph(args):
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.sqlite import SqliteSaver
    import sqlite3
    from typing import TypedDict

    class S(TypedDict):
        step: str

    tool_call_id = "call_" + args.attempt_id

    def remediation_node(state):
        if args.ledger:
            ledger_mod.write_state(args.action_id, args.attempt_id, tool_call_id, args.idempotency_key, "PREPARED")

        if args.boundary == "B1":
            os.kill(os.getpid(), signal.SIGKILL)

        mock_executor.apply_effect("restart_pod", args.idempotency_key, bool(args.idempotent_executor))

        if args.boundary == "B2":
            os.kill(os.getpid(), signal.SIGKILL)

        if args.ledger:
            ledger_mod.write_state(args.action_id, args.attempt_id, tool_call_id, args.idempotency_key, "SENT")

        if args.boundary == "B3":
            os.kill(os.getpid(), signal.SIGKILL)

        if args.ledger:
            ledger_mod.write_state(args.action_id, args.attempt_id, tool_call_id, args.idempotency_key, "ACKNOWLEDGED")

        if args.boundary == "B5":
            os.kill(os.getpid(), signal.SIGKILL)

        if args.ledger:
            ledger_mod.write_state(args.action_id, args.attempt_id, tool_call_id, args.idempotency_key, "EFFECT_VERIFIED")

        return {"step": "done"}

    conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "langgraph_checkpoints.db"), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    g = StateGraph(S)
    g.add_node("remediate", remediation_node)
    g.set_entry_point("remediate")
    g.add_edge("remediate", END)
    graph = g.compile(checkpointer=saver)
    thread = {"configurable": {"thread_id": args.action_id}}
    graph.invoke({"step": "start"}, config=thread)
    print("langgraph: completed normally")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--harness", choices=["langgraph", "single"], required=True)
    p.add_argument("--boundary", choices=["B1", "B2", "B3", "B5", "NONE"], required=True)
    p.add_argument("--ledger", type=int, choices=[0, 1], required=True)
    p.add_argument("--idempotent-executor", type=int, choices=[0, 1], required=True)
    p.add_argument("--action-id", required=True)
    p.add_argument("--attempt-id", required=True)
    p.add_argument("--idempotency-key", required=True)
    args = p.parse_args()

    if args.harness == "single":
        run_single_agent(args)
    else:
        run_langgraph(args)
