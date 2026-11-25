"""
Real implementation of the paper's proposed action-ledger contract.
Each state transition is written synchronously (INSERT + commit, which
forces an fsync-backed durable write in SQLite's default journal mode)
before the corresponding step is taken, matching Section V's description.
"""
import sqlite3
import time
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "ledger_state.db")


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id TEXT,
            attempt_id TEXT,
            tool_call_id TEXT,
            idempotency_key TEXT,
            state TEXT,
            written_at REAL
        )
    """)
    conn.commit()
    return conn


def reset():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    _connect().close()


def write_state(action_id, attempt_id, tool_call_id, idempotency_key, state):
    """Durable, synchronous write of one lifecycle transition. Returns elapsed seconds."""
    t0 = time.perf_counter()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO ledger (action_id, attempt_id, tool_call_id, idempotency_key, state, written_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (action_id, attempt_id, tool_call_id, idempotency_key, state, time.time()),
        )
        conn.commit()  # forces durable write
    finally:
        conn.close()
    return time.perf_counter() - t0


def highest_state_for_action(action_id):
    """What an auditor sees after the fact: the furthest state reached, across all attempts of this action."""
    order = ["PREPARED", "SENT", "ACKNOWLEDGED", "EFFECT_VERIFIED"]
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT state FROM ledger WHERE action_id = ?", (action_id,)
        )
        states = [r[0] for r in cur.fetchall()]
        if not states:
            return None
        return max(states, key=lambda s: order.index(s) if s in order else -1)
    finally:
        conn.close()


def attempts_for_action(action_id):
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT DISTINCT attempt_id FROM ledger WHERE action_id = ?", (action_id,)
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def record_size_bytes():
    """Real on-disk size of one ledger row, measured, not assumed."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO ledger (action_id, attempt_id, tool_call_id, idempotency_key, state, written_at) "
            "VALUES ('sizeprobe','sizeprobe','sizeprobe','sizeprobe','PREPARED', 0)"
        )
        conn.commit()
        before = os.path.getsize(DB_PATH)
        conn.execute(
            "INSERT INTO ledger (action_id, attempt_id, tool_call_id, idempotency_key, state, written_at) "
            "VALUES ('sizeprobe','sizeprobe','sizeprobe','sizeprobe','PREPARED', 0)"
        )
        conn.commit()
        after_size = os.path.getsize(DB_PATH)
        conn.execute("DELETE FROM ledger WHERE action_id = 'sizeprobe'")
        conn.commit()
        return before, after_size
    finally:
        conn.close()
