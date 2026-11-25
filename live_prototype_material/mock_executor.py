"""
Mock external executor: stands in for the real target system (e.g. a
Kubernetes API) that a tool call actually acts on. It is intentionally a
SEPARATE persistent store from the agent process, so that when the agent
process is hard-killed mid-execution, the executor's own record of what it
actually did survives independently -- this is what lets us determine
ground truth (did the effect really happen, once or twice?) after a crash,
the same way a real Kubernetes API's own state would survive an agent crash.
"""
import sqlite3
import time
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "executor_state.db")


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS effects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT,
            action TEXT,
            applied_at REAL
        )
    """)
    conn.commit()
    return conn


def reset():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    _connect().close()


def apply_effect(action: str, idempotency_key: str, idempotent: bool) -> dict:
    """
    Apply an effect. If idempotent=True, checks for a prior effect with the
    same idempotency_key and no-ops if found (this is the "idempotency-aware
    executor" condition). If idempotent=False, always applies (this is the
    "no executor cooperation" condition, matching the paper's assumption).
    Returns {"applied": bool, "duplicate": bool}.
    """
    conn = _connect()
    try:
        if idempotent:
            cur = conn.execute(
                "SELECT COUNT(*) FROM effects WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            already = cur.fetchone()[0] > 0
            if already:
                return {"applied": False, "duplicate": False}
        cur = conn.execute(
            "SELECT COUNT(*) FROM effects WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        prior_count = cur.fetchone()[0]
        conn.execute(
            "INSERT INTO effects (idempotency_key, action, applied_at) VALUES (?, ?, ?)",
            (idempotency_key, action, time.time()),
        )
        conn.commit()
        return {"applied": True, "duplicate": prior_count > 0}
    finally:
        conn.close()


def effect_count(idempotency_key: str) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM effects WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()
