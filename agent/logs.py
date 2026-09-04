"""Decision log, backed by Neon Postgres (audit item 11's `decisions` table -
replaces the old append-only logs/decisions.jsonl file). asyncpg doesn't
auto-decode jsonb, so input/output are explicitly json.dumps()'d on write
and json.loads()'d on read."""
import json
import uuid
from datetime import datetime, timezone

from agent.db import get_pool


def new_trace_id() -> str:
    """One trace_id per full pipeline cycle (step09's run_cycle generates one
    and threads it through every step's log_event call), so step07 can record
    which decision chain produced a given position, and step08 can find it."""
    return uuid.uuid4().hex


async def log_event(step: str, input: dict, output, reason: str = "", trace_id: str | None = None) -> dict:
    """Insert one decision row and return it in the same shape the old
    decisions.jsonl line had."""
    ts = datetime.now(timezone.utc)
    symbol = input.get("symbol") if isinstance(input, dict) else None

    pool = await get_pool()
    await pool.execute(
        """INSERT INTO decisions (ts, cycle_id, step, symbol, input, output, reason)
           VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7)""",
        ts,
        trace_id,
        step,
        symbol,
        json.dumps(input, default=str),
        json.dumps(output, default=str),
        reason,
    )
    return {"ts": ts.isoformat(), "trace_id": trace_id, "step": step, "input": input, "output": output, "reason": reason}


async def read_recent_events(limit: int = 50, since_ts: str | None = None) -> list[dict]:
    """Shared by the MCP server's get_decision_log tool and the frontend's
    data-bridge API - one read path, not duplicated. Returns events oldest
    -> newest within the returned page, matching the old JSONL tail-read
    behavior (events[-limit:])."""
    pool = await get_pool()
    if since_ts is None:
        rows = await pool.fetch(
            "SELECT ts, cycle_id, step, input, output, reason FROM decisions ORDER BY ts DESC LIMIT $1", limit
        )
    else:
        rows = await pool.fetch(
            """SELECT ts, cycle_id, step, input, output, reason FROM decisions
               WHERE ts > $1 ORDER BY ts DESC LIMIT $2""",
            datetime.fromisoformat(since_ts),
            limit,
        )
    events = [
        {
            "ts": r["ts"].isoformat(),
            "trace_id": r["cycle_id"],
            "step": r["step"],
            "input": json.loads(r["input"]),
            "output": json.loads(r["output"]),
            "reason": r["reason"],
        }
        for r in rows
    ]
    events.reverse()  # DESC query for LIMIT semantics, then back to chronological order
    return events
