"""Lightweight HTTP bridge for the frontend (step10).

The MCP server (step09) only speaks the MCP protocol over stdio - not
directly fetchable from a browser. Per step10's doc ("a lightweight polling
endpoint wrapping it" is an explicit allowed alternative), this exposes
plain REST/JSON endpoints that call the SAME underlying agent/*.py functions
the MCP tools call - no separate logic path, no duplicated business logic.

All routes are async def (2026-09-01 async conversion) - every agent/*.py
function this file calls is a coroutine now, and FastAPI runs async def
handlers natively on its own event loop without needing a threadpool hop.

python -m uvicorn web_api.server:app --reload --port 8000
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import asyncio
import time

from alpaca.data.requests import StockLatestTradeRequest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from agent.alpaca_clients import make_stock_data_client
from agent.config import FRONTEND_ORIGINS, UNDERLYING_SYMBOL, WATCHLIST_SYMBOLS
from agent.iv_rank import COLD_START_DAYS, _load_history as _load_iv_history, backfill_history, get_stored_iv
from agent.logs import read_recent_events
from agent.db import close_pool
from agent.execution import find_orphaned_positions, reconcile_with_alpaca
from agent.monitor import check_positions, get_positions_with_pnl
from agent.orchestrator import run_cycle, run_cycle_watchlist, run_cycle_watchlist_0dte
from agent.price_stream import hub as price_stream_hub
from agent.regime import _fetch_bars, classify_history
from agent.strategy import _fetch_expiry_chain

_stock_data_client = make_stock_data_client()

# --- Autonomous decision loop + kill switch (doc/autonomous_loop_fix.md) ---
# This is an autonomous agent: it runs itself on a schedule from the moment
# the backend process starts, not on a frontend button click. The kill switch
# is a SAFETY control (pause new cycles), never how the agent gets its work
# done. Position monitoring (step08) is a SEPARATE loop that is never gated
# by this switch - stopping new decision cycles must never abandon a
# position already being watched.
DECISION_LOOP_INTERVAL_SECONDS = 600  # 10 min - within the "5-15 minute" range asked for
MONITOR_LOOP_INTERVAL_SECONDS = 30    # step08 monitoring is frequent and free, independent of the switch
# doc/zerodte_and_watchlist.md item 1: 0DTE positions get a SEPARATE, tighter
# monitor loop - conditions shift fast intraday - while weekly positions stay
# on the cadence above, untouched. check_positions()'s expiry_mode filter
# keeps the two loops' position sets disjoint, so a position is never
# double-processed by both.
MONITOR_LOOP_INTERVAL_SECONDS_0DTE = 10
# doc/source_of_truth_and_race_conditions.md item 2: standing reconciliation
# cadence, independent of trade decisions (which already reconcile on every
# call to get_account_state()) - catches local/Alpaca drift during any
# stretch with no new cycles.
RECONCILIATION_LOOP_INTERVAL_SECONDS = 60
LOOP_POLL_SECONDS = 2                 # how promptly a stop/resume takes effect, and how often the "stopped" state is checked

# doc/fix_dead_loop.md: a single hung asyncpg query inside run_cycle_watchlist
# once froze the whole decision loop forever with no error, no log line, and
# /api/loop/status still lying "running": true. 180s is comfortably above the
# worst real cycle time observed in production (~90s, two watchlist symbols
# both reaching AI review) while still being far under the 600s interval, so
# a genuinely hung cycle is caught and released within the SAME scheduled
# slot instead of needing minutes/hours to notice.
#
# Bumped from 180s -> 240s for doc/zerodte_and_watchlist.md: the watchlist
# grew from 2 to up to 10 symbols AND every cycle now also runs a second,
# sequential 0DTE pass (see _run_all_cycles) - both add real wall-clock time
# to a single decision-loop tick, still comfortably under the 600s interval.
DECISION_CYCLE_TIMEOUT_SECONDS = 240
WATCHDOG_POLL_SECONDS = 30            # how often the watchdog re-checks liveness
WATCHDOG_REWARN_SECONDS = 300         # re-log "still stuck" at most this often, not every poll tick
STUCK_AFTER_SECONDS = 2 * DECISION_LOOP_INTERVAL_SECONDS  # "hasn't fired within 2x the interval"

_loop_state: dict = {"running": True, "next_fire_at": None, "last_cycle_at": None}
_loop_started_at = datetime.now(timezone.utc)
_watchdog_state: dict = {"warned": False, "last_warned_at": None}
_background_tasks: list[asyncio.Task] = []


def _is_loop_alive() -> bool:
    """Real liveness, not the static running flag: while running, a cycle
    must have actually completed (successfully, with an error, or via
    timeout - any of those prove the loop is still turning) within
    STUCK_AFTER_SECONDS. Stopped-by-request is reported alive (idle by
    design), not stuck."""
    if not _loop_state["running"]:
        return True
    reference = _loop_state["last_cycle_at"] or _loop_started_at.isoformat()
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(reference)).total_seconds()
    return elapsed <= STUCK_AFTER_SECONDS


async def _run_all_cycles() -> None:
    """Weekly, then 0DTE - SEQUENTIALLY, never concurrently. Both mutate the
    SAME shared account state (open_positions_count, the drawdown high-water
    mark); run_cycle_watchlist's own per-symbol pipeline is already
    sequential for exactly this reason (see orchestrator.py's module
    docstring) - running the two cycles concurrently with each other would
    reintroduce the identical race."""
    await run_cycle_watchlist()
    await run_cycle_watchlist_0dte()


async def _decision_loop() -> None:
    while True:
        if _loop_state["running"]:
            _loop_state["next_fire_at"] = None
            try:
                await asyncio.wait_for(_run_all_cycles(), timeout=DECISION_CYCLE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                print(
                    f"[decision loop] cycle TIMED OUT after {DECISION_CYCLE_TIMEOUT_SECONDS}s - "
                    "cancelling this cycle, continuing to next scheduled fire"
                )
            except Exception as e:
                print(f"[decision loop] cycle failed: {e}")
            # Stamped after every attempt regardless of outcome - this, not
            # the running flag, is what proves the loop is still alive.
            _loop_state["last_cycle_at"] = datetime.now(timezone.utc).isoformat()
            if _loop_state["running"]:
                next_fire = datetime.now(timezone.utc) + timedelta(seconds=DECISION_LOOP_INTERVAL_SECONDS)
                _loop_state["next_fire_at"] = next_fire.isoformat()
            # Sleep in short ticks (not one long sleep) so a stop takes effect
            # within ~LOOP_POLL_SECONDS instead of waiting out the full interval.
            slept = 0
            while slept < DECISION_LOOP_INTERVAL_SECONDS and _loop_state["running"]:
                await asyncio.sleep(LOOP_POLL_SECONDS)
                slept += LOOP_POLL_SECONDS
        else:
            _loop_state["next_fire_at"] = None
            await asyncio.sleep(LOOP_POLL_SECONDS)


async def _monitor_loop() -> None:
    """Always runs, regardless of the decision loop's kill switch - an open
    position must keep being watched/managed even while new cycles are
    paused. Weekly positions only (doc/zerodte_and_watchlist.md item 1) -
    0DTE positions get their own, tighter loop below so this one's existing
    cadence never slows down for them."""
    while True:
        try:
            await check_positions(expiry_mode="weekly")
        except Exception as e:
            print(f"[monitor loop] tick failed: {e}")
        await asyncio.sleep(MONITOR_LOOP_INTERVAL_SECONDS)


async def _monitor_loop_0dte() -> None:
    """Same as _monitor_loop above, but 0DTE positions only, on a tighter
    cadence - conditions shift fast intraday for a same-day position, and
    this is also where the hard time-cutoff trigger gets checked."""
    while True:
        try:
            await check_positions(expiry_mode="0dte")
        except Exception as e:
            print(f"[monitor loop 0dte] tick failed: {e}")
        await asyncio.sleep(MONITOR_LOOP_INTERVAL_SECONDS_0DTE)


async def _reconciliation_loop() -> None:
    """doc/source_of_truth_and_race_conditions.md item 2: reconcile local
    Postgres position records against Alpaca's real order status on a
    standing schedule, independent of whether any trade decision is being
    made right now - get_account_state() already reconciles before every
    cap check, but this catches drift even during a stretch with no new
    cycles (e.g. the decision loop paused, or between 0DTE-eligible
    windows)."""
    while True:
        try:
            corrections = await reconcile_with_alpaca()
            if corrections:
                print(f"[reconciliation] corrected {len(corrections)} local record(s): {corrections}")
            orphans = await find_orphaned_positions()
            if orphans:
                print(f"[reconciliation] WARNING: {len(orphans)} orphaned Alpaca position(s) with no local record: {orphans}")
        except Exception as e:
            print(f"[reconciliation loop] tick failed: {e}")
        await asyncio.sleep(RECONCILIATION_LOOP_INTERVAL_SECONDS)


async def _watchdog_loop() -> None:
    """The decision loop's own timeout (above) is the real fix - this is the
    belt-and-suspenders visibility layer doc/fix_dead_loop.md item 5 asked
    for: even if that somehow still doesn't catch a future failure mode,
    this makes a stuck loop loud in the server log instead of silent, so it
    shows up without anyone having to think to poll /api/loop/status."""
    while True:
        await asyncio.sleep(WATCHDOG_POLL_SECONDS)
        if _loop_state["running"] and not _is_loop_alive():
            now = datetime.now(timezone.utc)
            last_warned = _watchdog_state["last_warned_at"]
            due_to_rewarn = last_warned is None or (now - datetime.fromisoformat(last_warned)).total_seconds() >= WATCHDOG_REWARN_SECONDS
            if not _watchdog_state["warned"] or due_to_rewarn:
                print(
                    f"[watchdog] loop appears STUCK - no decision cycle has completed in over "
                    f"{STUCK_AFTER_SECONDS}s (last_cycle_at={_loop_state['last_cycle_at']}, "
                    f"expected every {DECISION_LOOP_INTERVAL_SECONDS}s)"
                )
                _watchdog_state["warned"] = True
                _watchdog_state["last_warned_at"] = now.isoformat()
        else:
            _watchdog_state["warned"] = False
            _watchdog_state["last_warned_at"] = None


async def _seed_iv_history() -> None:
    """Auto-backfill on startup, so pointing at a fresh DATABASE_URL (e.g. a
    clean database for a submission) seeds itself instead of sitting in
    cold_start for 20+ real trading days. Only backfills symbols that don't
    already have enough history - an already-seeded DB just no-ops here on
    every restart. Runs as a one-shot background task (not awaited before
    yield) so it never delays startup/health checks; each symbol is a
    sequential real Alpaca lookup and the whole watchlist can take minutes."""
    for symbol in WATCHLIST_SYMBOLS:
        try:
            history = await _load_iv_history(symbol)
            if len(history) >= COLD_START_DAYS:
                continue
            result = await backfill_history(symbol, days=DEFAULT_CHART_DAYS)
            print(f"[iv_backfill] {symbol}: seeded={result['seeded']} skipped={len(result['skipped'])}")
        except Exception as e:
            print(f"[iv_backfill] {symbol}: ERROR {e}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    _background_tasks.append(asyncio.create_task(_decision_loop()))
    _background_tasks.append(asyncio.create_task(_monitor_loop()))
    _background_tasks.append(asyncio.create_task(_monitor_loop_0dte()))
    _background_tasks.append(asyncio.create_task(_reconciliation_loop()))
    _background_tasks.append(asyncio.create_task(_watchdog_loop()))
    _background_tasks.append(asyncio.create_task(_seed_iv_history()))
    price_stream_hub.start()
    yield
    for task in _background_tasks:
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)
    await close_pool()


app = FastAPI(title="Alpaca Options Agent - Frontend Bridge", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # Next.js dev falls back to the next free port (3001, 3002, ...) whenever
    # 3000 is already taken by something else on the machine - matched here
    # via regex rather than a fixed origin so the frontend isn't silently
    # CORS-blocked depending on what else happens to be running locally.
    # allow_origins additionally allowlists the deployed Vercel frontend's
    # real origin(s) via FRONTEND_ORIGIN - the regex alone only ever matches
    # localhost, which the production frontend isn't.
    allow_origin_regex=r"http://localhost:\d+",
    allow_origins=FRONTEND_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Two genuinely separate needs, two separate constants (chart_and_watchlist_fix.md
# item 1) - confirmed the prior bug empirically: at the old hours=24 default only
# 64 bars (~1 session) came back, while the SAME underlying fetch had 640 bars
# (14 days) available. The actual root cause wasn't the chart sharing the regime
# engine's small internal feature-computation window (that's fully internal to
# agent/regime.py and never touched this layer) - it was this layer applying one
# WALL-CLOCK-hours post-fetch trim to both endpoints, which (since markets are
# closed nights/weekends) can cut off most of the visible range depending on what
# time of day/week it's viewed. Fixed by trimming on calendar DAYS instead, and by
# giving the chart's display range its own default, independent of how much
# history the regime engine fetches to compute reliable percentile stats.
DEFAULT_CHART_DAYS = 100           # chart/regime-overlay DISPLAY window - bumped from 14 to 100 per request
# Must be >= DEFAULT_CHART_DAYS - the display filter below only ever shows
# what this compute window actually produced; a smaller compute window than
# the display window silently caps the display at the smaller number.
REGIME_COMPUTE_LOOKBACK_DAYS = 100

# doc/loop_and_ui_fixes.md: classify_history() is a genuinely expensive,
# synchronous, CPU-bound computation (~20s measured directly, redundant
# sliding-window recomputation across ~30 days of history) - with several
# concurrent browser polls hitting this endpoint every 5s, it was
# monopolizing the single-threaded event loop badly enough to starve the
# decision loop's own scheduling for extended stretches, which is what
# actually looked like "the loop is stuck". The underlying data (15-min
# bars) can't meaningfully change faster than this TTL anyway.
#
# A plain TTL cache alone isn't enough: when it expires, every concurrent
# poller (confirmed via the server log: 6+ simultaneous client connections
# for the same symbol) sees a miss AT THE SAME TIME and each independently
# starts its own redundant ~20s computation - since these all run on the
# same single thread, that's N x 20s of serialized blocking from one cache
# expiry, not one. The lock below makes concurrent requests for the same
# symbol wait on and share a single in-flight computation instead.
REGIME_HISTORY_CACHE_TTL_SECONDS = 60
_regime_history_cache: dict[str, tuple[float, list]] = {}
_regime_history_locks: dict[str, asyncio.Lock] = {}


async def _get_regime_history_cached(symbol: str) -> list:
    now = time.monotonic()
    cached = _regime_history_cache.get(symbol)
    if cached is not None and now - cached[0] < REGIME_HISTORY_CACHE_TTL_SECONDS:
        return cached[1]

    lock = _regime_history_locks.setdefault(symbol, asyncio.Lock())
    async with lock:
        # Re-check after acquiring the lock - another request may have
        # already computed and cached a fresh result while we were waiting.
        now = time.monotonic()
        cached = _regime_history_cache.get(symbol)
        if cached is not None and now - cached[0] < REGIME_HISTORY_CACHE_TTL_SECONDS:
            return cached[1]
        sequence = await classify_history(symbol, lookback_days=REGIME_COMPUTE_LOOKBACK_DAYS)
        _regime_history_cache[symbol] = (time.monotonic(), sequence)
        return sequence


def _since_days(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/price_history")
async def price_history(symbol: str = UNDERLYING_SYMBOL, days: int = DEFAULT_CHART_DAYS) -> dict:
    """Fetches its OWN display-appropriate range directly - not shared with
    regime_history's computation lookback below."""
    df = await _fetch_bars(symbol, max_bars=100_000, lookback_days=days)
    bars = [
        {"ts": ts.isoformat(), "open": row.open, "close": row.close, "high": row.high, "low": row.low}
        for ts, row in df.iterrows()
    ]
    return {"symbol": symbol, "bars": bars}


@app.websocket("/ws/price")
async def ws_price(websocket: WebSocket, symbol: str = UNDERLYING_SYMBOL, days: int = DEFAULT_CHART_DAYS) -> None:
    """doc/zerodte_and_watchlist.md item 3 / doc/source_of_truth_and_race_
    conditions.md: replaces price-history POLLING for the chart - one push
    path, no interval on either side. Protocol: on connect, one "history"
    message with the same bars /api/price_history would return; after that,
    one "bar" message per live bar Alpaca's stream delivers for this symbol,
    pushed immediately - not batched, not polled.

    Symbol switching is a plain reconnect: the frontend closes this socket
    and opens a new one with the new symbol as a query param, rather than an
    in-band "change symbol" message - the hub's subscribe/unsubscribe calls
    below then correctly drop the old symbol's Alpaca subscription (if this
    was its last local subscriber) and add the new one."""
    await websocket.accept()
    queue = await price_stream_hub.subscribe(symbol)
    try:
        df = await _fetch_bars(symbol, max_bars=100_000, lookback_days=days)
        bars = [
            {"ts": ts.isoformat(), "open": row.open, "close": row.close, "high": row.high, "low": row.low}
            for ts, row in df.iterrows()
        ]
        await websocket.send_json({"type": "history", "symbol": symbol, "bars": bars})

        while True:
            bar = await queue.get()
            await websocket.send_json({"type": "bar", "symbol": symbol, "bar": bar})
    except WebSocketDisconnect:
        pass
    finally:
        await price_stream_hub.unsubscribe(symbol, queue)


@app.get("/api/regime_history")
async def regime_history(symbol: str = UNDERLYING_SYMBOL, days: int = DEFAULT_CHART_DAYS) -> dict:
    """Regime classification needs its own longer, FIXED lookback
    (REGIME_COMPUTE_LOOKBACK_DAYS) to compute reliable percentile stats -
    independent of how much of the resulting sequence is actually returned
    for display (`days`, matching the chart's own range)."""
    sequence = await _get_regime_history_cached(symbol)
    cutoff = _since_days(days)
    return {"symbol": symbol, "points": [p for p in sequence if p["ts"] >= cutoff]}


@app.get("/api/iv_history")
async def iv_history(symbol: str = UNDERLYING_SYMBOL) -> dict:
    """Daily ATM IV history (step03's rolling store) + the last STORED reading.
    Read-only, cheap - safe for frontend polling. Does NOT trigger a live
    recompute (that only happens inside run_cycle) - see audit item 1."""
    history = await _load_iv_history(symbol)
    current = await get_stored_iv(symbol)
    return {"symbol": symbol, "history": history, "current": current}


@app.get("/api/decision_log")
async def decision_log(limit: int = 50, since_ts: str | None = None) -> dict:
    return {"events": await read_recent_events(limit=limit, since_ts=since_ts)}


@app.get("/api/open_positions")
async def open_positions() -> dict:
    return {"positions": await get_positions_with_pnl()}


@app.post("/api/run_cycle")
async def trigger_run_cycle(symbol: str = UNDERLYING_SYMBOL) -> dict:
    """Manual single-symbol trigger, kept for debugging/MCP use - the
    frontend no longer exposes a manual button (doc/autonomous_loop_fix.md):
    the agent runs itself via the autonomous decision loop below. This reaches
    step06's paid AI review - don't poll this endpoint on a tight interval."""
    return await run_cycle(symbol)


def _loop_status_payload() -> dict:
    """doc/fix_dead_loop.md item 3: `running` alone is just the kill-switch
    INTENT, set once by whoever last called start/stop - it stayed "true"
    for hours after the loop actually died silently. `alive` is computed
    fresh on every call from when a cycle last really completed."""
    return {**_loop_state, "alive": _is_loop_alive()}


@app.get("/api/loop/status")
async def loop_status() -> dict:
    return _loop_status_payload()


@app.post("/api/loop/start")
async def loop_start() -> dict:
    """Resume the autonomous decision loop (safety control only - see
    doc/autonomous_loop_fix.md). Takes effect within ~LOOP_POLL_SECONDS."""
    _loop_state["running"] = True
    return _loop_status_payload()


@app.post("/api/loop/stop")
async def loop_stop() -> dict:
    """Pause the autonomous decision loop - no NEW cycles fire. step08
    position monitoring keeps running regardless; nothing already open gets
    abandoned. Takes effect within ~LOOP_POLL_SECONDS, not the full interval."""
    _loop_state["running"] = False
    return _loop_status_payload()


@app.get("/api/watchlist")
async def watchlist() -> dict:
    return {"symbols": WATCHLIST_SYMBOLS}


@app.post("/api/run_cycle_watchlist")
async def trigger_run_cycle_watchlist() -> dict:
    """Manual trigger to run the decision cycle for every symbol in
    WATCHLIST_SYMBOLS (audit item 4). Reaches step06's paid AI review once
    per symbol that clears the pre-filter - don't poll this on a tight
    interval from the frontend."""
    return {"results": await run_cycle_watchlist()}


@app.post("/api/run_monitor")
async def trigger_run_monitor() -> dict:
    """Manual trigger for one step08 position-monitor poll tick. Free (no paid
    inference) - safe to call more often than run_cycle."""
    return {"results": await check_positions()}


@app.get("/api/market_snapshot")
async def market_snapshot(symbol: str = UNDERLYING_SYMBOL) -> dict:
    trade = (
        await asyncio.to_thread(_stock_data_client.get_stock_latest_trade, StockLatestTradeRequest(symbol_or_symbols=symbol))
    )[symbol]

    fetched = await _fetch_expiry_chain(symbol, trade.price)
    if fetched is None:
        chain_summary = {"contracts": 0, "expiries": []}
    else:
        expiry, quotes = fetched
        quotes.pop("_widened_search", None)
        chain_summary = {"contracts": sum(len(v) for v in quotes.values()), "expiries": [expiry.isoformat()]}

    return {"symbol": symbol, "spot": trade.price, "chain_summary": chain_summary}
