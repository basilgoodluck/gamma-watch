"""MCP server wrapping steps 02-08 as individually callable tools.

Each tool maps 1:1 to a step0X module and calls the SAME underlying function
`run_cycle` uses - no duplicated logic - so a `run_cycle` call and the
equivalent sequence of individual tool calls produce identical
logs/decisions.jsonl entries.

All tools are async (2026-09-01 async conversion) - the MCP stdio transport
already runs on asyncio, and every agent/*.py function this file calls is
itself a coroutine now.
"""
import asyncio

from alpaca.data.requests import StockLatestTradeRequest
from mcp.server.mcpserver import MCPServer

from agent.ai_review import review_candidates as _review_candidates
from agent.alpaca_clients import make_stock_data_client
from agent.config import UNDERLYING_SYMBOL
from agent.execution import execute_order as _execute_order
from agent.iv_rank import get_iv_rank as _get_iv_rank
from agent.logs import read_recent_events
from agent.monitor import check_positions as _check_positions, get_positions_with_pnl
from agent.orchestrator import run_cycle as _run_cycle, run_cycle_watchlist as _run_cycle_watchlist
from agent.regime import _fetch_bars, detect_regime as _detect_regime
from agent.risk import get_account_state, size_candidates as _size_candidates
from agent.strategy import _fetch_expiry_chain, generate_candidates as _generate_candidates

mcp = MCPServer("alpaca-options-agent")

_stock_data_client = make_stock_data_client()


@mcp.tool()
async def get_market_snapshot(symbol: str = UNDERLYING_SYMBOL) -> dict:
    """Fetch current bars, spot price, and options chain summary for the underlying."""
    trade = (
        await asyncio.to_thread(_stock_data_client.get_stock_latest_trade, StockLatestTradeRequest(symbol_or_symbols=symbol))
    )[symbol]
    bars_df = (await _fetch_bars(symbol, max_bars=50)).tail(20)
    bars = [
        {"ts": ts.isoformat(), "open": row.open, "close": row.close, "high": row.high, "low": row.low}
        for ts, row in bars_df.iterrows()
    ]

    fetched = await _fetch_expiry_chain(symbol, trade.price)
    if fetched is None:
        chain_summary = {"contracts": 0, "expiries": []}
    else:
        expiry, quotes = fetched
        quotes.pop("_widened_search", None)
        contract_count = sum(len(v) for v in quotes.values())
        chain_summary = {"contracts": contract_count, "expiries": [expiry.isoformat()]}

    return {"symbol": symbol, "spot": trade.price, "bars": bars, "chain_summary": chain_summary}


@mcp.tool()
async def classify_regime(symbol: str = UNDERLYING_SYMBOL) -> dict:
    """Runs step02: 5-state market regime classification."""
    return await _detect_regime(symbol)


@mcp.tool()
async def get_iv_rank(symbol: str = UNDERLYING_SYMBOL) -> dict:
    """Runs step03: ATM implied volatility + IV rank."""
    return await _get_iv_rank(symbol)


@mcp.tool()
async def generate_candidates(regime_result: dict, iv_result: dict) -> dict:
    """Runs step04: regime x IV-rank strategy candidate generation."""
    return {"candidates": await _generate_candidates(regime_result, iv_result)}


@mcp.tool()
async def size_candidates(candidates: list[dict], account_state: dict | None = None) -> dict:
    """Runs step05: risk sizing. account_state is fetched live from Alpaca if omitted."""
    account_state = account_state or await get_account_state()
    return {"sized_candidates": await _size_candidates(candidates, account_state)}


@mcp.tool()
async def review_candidate(
    candidates: list[dict], regime_result: dict, iv_result: dict, account_state: dict | None = None
) -> dict:
    """Runs step06: batched AI review (veto/adjust). account_state is fetched live if omitted."""
    account_state = account_state or await get_account_state()
    return await _review_candidates(candidates, regime_result, iv_result, account_state)


@mcp.tool()
async def execute_order(final_candidate: dict | None = None, stand_down_reason: str | None = None) -> dict:
    """Runs step07: multi-leg order execution, or a logged stand-down if final_candidate is None."""
    return await _execute_order(final_candidate, stand_down_reason=stand_down_reason)


@mcp.tool()
async def get_open_positions() -> dict:
    """Reads the local position store, with live current_pnl_pct for still-open positions."""
    return {"positions": await get_positions_with_pnl()}


@mcp.tool()
async def get_decision_log(limit: int = 50, since_ts: str | None = None) -> dict:
    """Reads recent entries from logs/decisions.jsonl - the live reasoning feed."""
    return {"events": await read_recent_events(limit=limit, since_ts=since_ts)}


@mcp.tool()
async def run_position_monitor() -> dict:
    """Runs step08: one poll tick over all open positions - close on trigger, log watching otherwise."""
    return {"results": await _check_positions()}


@mcp.tool()
async def run_cycle(symbol: str = UNDERLYING_SYMBOL) -> dict:
    """Chains classify_regime -> get_iv_rank -> generate_candidates -> size_candidates ->
    review_candidate -> execute_order (or a logged stand-down) for one symbol, all under a
    single trace_id. This is the only entry point that reaches step06's paid AI review -
    whatever schedules this should run it on a 5-15 minute cadence, separate from step08's
    fast, free position-monitoring loop (run_position_monitor above). Shared with the
    frontend's data-bridge API via agent/orchestrator.py - same code path, not duplicated."""
    return await _run_cycle(symbol)


@mcp.tool()
async def run_cycle_watchlist() -> dict:
    """Runs the full cycle for every symbol in config.WATCHLIST_SYMBOLS (default
    SPY, QQQ). Regime + IV rank are fetched concurrently across symbols (independent
    network I/O); candidate generation through execution then runs sequentially per
    symbol so each one's risk checks see the account state left by the ones before it.
    Same paid-AI-review caveat as run_cycle applies per symbol."""
    return {"results": await _run_cycle_watchlist()}


if __name__ == "__main__":
    mcp.run()
