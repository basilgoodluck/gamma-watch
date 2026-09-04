"""Shared run_cycle implementation, called identically by the MCP server
(step09) and the frontend's data-bridge API (step10) - one code path, so
every caller produces identical logs/decisions.jsonl entries.

Concurrency design (async conversion, 2026-09-01): run_cycle_watchlist does
NOT wrap the entire per-symbol pipeline in asyncio.gather. Candidate sizing
and execution read and mutate SHARED account state (open_positions_count,
the drawdown high-water mark) - if two symbols' full pipelines ran fully
concurrently, both could fetch account_state before either had opened a
position and each independently conclude there's room under
MAX_CONCURRENT_POSITIONS, together exceeding it. That's a real risk-control
correctness regression, not just a performance nitpick, so it's not
acceptable to trade away for concurrency.

Instead: the actual independent, slow, per-symbol network I/O - regime
classification and IV rank, which share no state across symbols - runs
CONCURRENTLY via asyncio.gather. Candidate generation -> sizing -> AI
review -> execution runs SEQUENTIALLY per symbol afterwards, so each
symbol's risk checks correctly see what the symbols before it just did
(the property audit item 4 verified). This is still the real payoff the
conversion is for: the two live-market round trips no longer serialize.
"""
import asyncio

from agent.ai_review import review_candidates
from agent.config import UNDERLYING_SYMBOL, WATCHLIST_SYMBOLS, ZERO_DTE_CANDIDATE_SYMBOLS
from agent.execution import execute_order
from agent.iv_rank import get_iv_rank
from agent.logs import new_trace_id
from agent.regime import detect_regime
from agent.risk import get_account_state, size_candidates
from agent.strategy import generate_candidates, has_zero_dte_expiry


async def _decide_and_execute(
    symbol: str, regime_result: dict, iv_result: dict, trace_id: str, expiry_mode: str = "weekly"
) -> dict:
    candidates = await generate_candidates(regime_result, iv_result, trace_id=trace_id, expiry_mode=expiry_mode)

    account_state = await get_account_state()
    sized = await size_candidates(candidates, account_state, trace_id=trace_id, expiry_mode=expiry_mode)
    review = await review_candidates(sized, regime_result, iv_result, account_state, trace_id=trace_id)

    final_candidate = None
    stand_down_reason = None

    if review["skipped"]:
        stand_down_reason = review["skip_reason"]
    else:
        approved = [v for v in review["verdicts"] if v["approve"]]
        if not approved:
            stand_down_reason = "all candidates vetoed by AI review"
        else:
            by_id = {f"{c['type']}_{i}": c for i, c in enumerate(sized)}
            best_verdict = max(approved, key=lambda v: v["confidence_adjustment"])
            final_candidate = by_id.get(best_verdict["candidate_id"])
            if final_candidate is None or not final_candidate.get("passes_risk"):
                final_candidate = None
                stand_down_reason = "approved candidate did not pass risk sizing"

    execution_result = await execute_order(
        final_candidate, stand_down_reason=stand_down_reason, trace_id=trace_id, expiry_mode=expiry_mode
    )

    return {
        "trace_id": trace_id,
        "symbol": symbol,
        "regime_result": regime_result,
        "iv_result": iv_result,
        "candidates": candidates,
        "sized_candidates": sized,
        "review": review,
        "execution_result": execution_result,
    }


async def run_cycle(symbol: str = UNDERLYING_SYMBOL) -> dict:
    """Chains regime -> IV rank -> candidates -> sizing -> AI review ->
    execution (or a logged stand-down) for one symbol, all under a single
    trace_id. The only entry point that reaches step06's paid AI review -
    schedule this on a 5-15 minute cadence, separate from step08's fast,
    free position-monitoring loop."""
    trace_id = new_trace_id()
    regime_result = await detect_regime(symbol, trace_id=trace_id)
    iv_result = await get_iv_rank(symbol, trace_id=trace_id)
    return await _decide_and_execute(symbol, regime_result, iv_result, trace_id)


async def run_cycle_watchlist(symbols: list[str] | None = None) -> list[dict]:
    """Runs the full cycle for every symbol in the watchlist (default:
    config.WATCHLIST_SYMBOLS). Regime + IV rank are fetched for all symbols
    CONCURRENTLY (independent per-symbol network I/O, no shared state) - the
    real concurrency payoff of the async conversion. Candidate generation
    through execution then runs sequentially per symbol, so each one's risk
    checks see the account state left by the ones before it (audit item 4)."""
    symbols = symbols or WATCHLIST_SYMBOLS
    trace_ids = [new_trace_id() for _ in symbols]

    regime_results, iv_results = await asyncio.gather(
        asyncio.gather(*(detect_regime(s, trace_id=t) for s, t in zip(symbols, trace_ids))),
        asyncio.gather(*(get_iv_rank(s, trace_id=t) for s, t in zip(symbols, trace_ids))),
    )

    results = []
    for symbol, regime_result, iv_result, trace_id in zip(symbols, regime_results, iv_results, trace_ids):
        results.append(await _decide_and_execute(symbol, regime_result, iv_result, trace_id))
    return results


async def run_cycle_watchlist_0dte(symbols: list[str] | None = None) -> list[dict]:
    """doc/zerodte_and_watchlist.md item 1: an ADDITIONAL, parallel cycle for
    0DTE - built alongside run_cycle_watchlist above, not a modification of
    it. A symbol eligible for 0DTE can also carry an open WEEKLY position at
    the same time; this is a second, independent decision pass, not a
    replacement for the weekly one.

    Eligibility is checked live, per symbol, per call - never assumed from
    ZERO_DTE_CANDIDATE_SYMBOLS alone (that list is only a pre-filter to skip
    the chain lookup for symbols that structurally never have one). A symbol
    without a real same-day expiry today is silently excluded from this
    cycle entirely - it simply keeps running weekly-only, exactly as if this
    function didn't exist for it today.

    Runs sequentially with (never concurrently to) run_cycle_watchlist in the
    caller - both mutate the SAME shared account state (open_positions_count,
    the drawdown high-water mark), the same correctness reason
    run_cycle_watchlist's own per-symbol pipeline isn't parallelized either
    (see the module docstring)."""
    candidates_pool = [s for s in (symbols or ZERO_DTE_CANDIDATE_SYMBOLS) if s in WATCHLIST_SYMBOLS]
    if not candidates_pool:
        return []

    eligible_flags = await asyncio.gather(*(has_zero_dte_expiry(s) for s in candidates_pool))
    eligible = [s for s, ok in zip(candidates_pool, eligible_flags) if ok]
    if not eligible:
        return []

    trace_ids = [new_trace_id() for _ in eligible]
    regime_results, iv_results = await asyncio.gather(
        asyncio.gather(*(detect_regime(s, trace_id=t) for s, t in zip(eligible, trace_ids))),
        asyncio.gather(*(get_iv_rank(s, trace_id=t) for s, t in zip(eligible, trace_ids))),
    )

    results = []
    for symbol, regime_result, iv_result, trace_id in zip(eligible, regime_results, iv_results, trace_ids):
        results.append(await _decide_and_execute(symbol, regime_result, iv_result, trace_id, expiry_mode="0dte"))
    return results
