"""Regime x IV-rank strategy candidate generation (the matrix in step04).

Each (regime, IV bucket) cell maps to exactly one of four strategy shapes -
directional debit spread, directional single-leg long, iron condor/credit
spread, or long straddle/strangle - or to a stand-down. This does not
generate multiple strike/width variants per cell; that's out of scope for
what step04 asks for.

Strike selection is offset-from-spot by a fixed percentage, not delta-based -
we have no reliable per-contract greeks (see the OPRA gap noted in step01/
step03), so a delta-targeted strike pick isn't available. Percentage-of-spot
offsets are a defensible demo-level substitute; documented here rather than
implied.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

from alpaca.data.requests import OptionChainRequest, StockLatestTradeRequest
from alpaca.trading.enums import AssetStatus, ContractType
from alpaca.trading.requests import GetOptionContractsRequest

from agent.alpaca_clients import make_option_data_client, make_stock_data_client, make_trading_client
from agent.logs import log_event

IV_RANK_HIGH_THRESHOLD = 0.35
LOW_CONFIDENCE_THRESHOLD = 0.4

EXPIRY_TARGET_MIN_DAYS = 7
EXPIRY_TARGET_MAX_DAYS = 14
EXPIRY_FALLBACK_MAX_DAYS = 30  # widen the search if nothing in the preferred window

OTM_PCT_NEAR = 0.02    # short leg of a debit spread
OTM_PCT_FAR = 0.04     # sell strike of an iron condor
OTM_PCT_WING = 0.06    # protective wing of an iron condor
STRANGLE_OTM_PCT = 0.015
RICH_STRADDLE_PCT_OF_SPOT = 0.03  # if ATM straddle costs more than this % of spot, use a strangle instead

CONTRACT_MULTIPLIER = 100

_trading_client = make_trading_client()
_option_data_client = make_option_data_client()
_stock_data_client = make_stock_data_client()


def _iv_bucket(iv_rank: float) -> str:
    return "HIGH" if iv_rank >= IV_RANK_HIGH_THRESHOLD else "LOW"


# (regime, iv_bucket) -> strategy type. STAND_DOWN is the only non-executable outcome.
_MATRIX = {
    ("TREND_CALM", "LOW"): "DEBIT_SPREAD",
    ("TREND_CALM", "HIGH"): "DEBIT_SPREAD",  # sized smaller in step05
    ("TREND_VOLATILE", "LOW"): "SINGLE_LONG",
    ("TREND_VOLATILE", "HIGH"): "DEBIT_SPREAD",
    ("RANGE_CALM", "LOW"): "IRON_CONDOR",
    ("RANGE_CALM", "HIGH"): "IRON_CONDOR",
    ("RANGE_VOLATILE", "LOW"): "STRADDLE_STRANGLE",
    ("RANGE_VOLATILE", "HIGH"): "STAND_DOWN",
    ("BREAKOUT", "LOW"): "STRADDLE_STRANGLE",
    ("BREAKOUT", "HIGH"): "SINGLE_LONG",  # post-confirmation only, sized smaller
}

# doc/zerodte_and_watchlist.md item 1: a SEPARATE matrix for 0DTE, not a
# variant of the weekly one - DEBIT_SPREAD is dropped entirely (needs more
# time to develop than same-day allows, straight from the doc). STRADDLE_
# STRANGLE is dropped too, on my own judgment rather than guessing it's
# fine: it's a long-premium strategy, and 0DTE contracts bleed nearly all
# of their extrinsic value within hours, not days - the exact same "needs
# more time" problem the doc names for DEBIT_SPREAD applies at least as
# hard here. Flagging this explicitly rather than silently including it.
# Both of its cells map to SINGLE_LONG instead (still a defined-risk,
# directional bet on a big move, without paying for two legs of decay).
# RANGE_VOLATILE+LOW keeps IRON_CONDOR (still fundamentally range-bound,
# credit collected decays in our favor within the day); RANGE_VOLATILE+HIGH
# stays STAND_DOWN for the same whipsaw/rich-premium reasoning as weekly.
_MATRIX_0DTE = {
    ("TREND_CALM", "LOW"): "SINGLE_LONG",
    ("TREND_CALM", "HIGH"): "SINGLE_LONG",
    ("TREND_VOLATILE", "LOW"): "SINGLE_LONG",
    ("TREND_VOLATILE", "HIGH"): "SINGLE_LONG",
    ("RANGE_CALM", "LOW"): "IRON_CONDOR",
    ("RANGE_CALM", "HIGH"): "IRON_CONDOR",
    ("RANGE_VOLATILE", "LOW"): "IRON_CONDOR",
    ("RANGE_VOLATILE", "HIGH"): "STAND_DOWN",
    ("BREAKOUT", "LOW"): "SINGLE_LONG",
    ("BREAKOUT", "HIGH"): "SINGLE_LONG",
}


async def _discover_expiries(symbol: str, days_ahead_gte: int, days_ahead_lte: int) -> list[date]:
    """A gte/lte date-range query pages at 1000 contracts and does NOT
    auto-paginate - with many strikes x expiries x {call,put} in range, a
    single page can be strike-truncated for whichever expiries sort last,
    missing the far side of the chain entirely (verified: request for a
    7-14 day window came back with all 4 expiries present, but the last one
    truncated to strikes 500-545 only, nowhere near a $762 spot). Good
    enough for just collecting which expiry dates exist, though - use exact
    per-expiry queries afterwards to get the full strike set for the one we
    actually pick."""
    req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=(date.today() + timedelta(days=days_ahead_gte)).isoformat(),
        expiration_date_lte=(date.today() + timedelta(days=days_ahead_lte)).isoformat(),
        limit=1000,
    )
    contracts = (await asyncio.to_thread(_trading_client.get_option_contracts, req)).option_contracts
    return sorted({c.expiration_date for c in contracts})


async def has_zero_dte_expiry(symbol: str) -> bool:
    """The correctness check doc/zerodte_and_watchlist.md item 1 demands
    before running 0DTE mode on any symbol: true same-day expiring
    contracts only exist for a subset of symbols (mainly index-linked
    ones). This checks the REAL, live chain for today's date - never
    assumed from a static list - so a symbol without one falls back to
    weekly instead of forcing a trade against the nearest available
    expiry."""
    expiries = await _discover_expiries(symbol, 0, 0)
    return date.today() in expiries


async def _fetch_expiry_chain(symbol: str, spot: float, zero_dte: bool = False) -> tuple[date, dict] | None:
    """Returns (expiry, quotes) where quotes maps strike (float) -> {"call":
    {...}, "put": {...}}, each leaf a dict with symbol/bid/ask/mid. None if no
    usable expiry/contracts are found at all.

    zero_dte=True targets EXACTLY today's date, with no widening fallback -
    doc/zerodte_and_watchlist.md item 1 is explicit that a same-day trade
    must never be forced onto the nearest available expiry when today
    itself doesn't have one; the caller (generate_candidates) is expected to
    have already confirmed via has_zero_dte_expiry() before calling with
    zero_dte=True, so reaching "no expiries" here means standing down for
    this cycle, not searching further out."""
    if zero_dte:
        expiries = await _discover_expiries(symbol, 0, 0)
        if date.today() not in expiries:
            return None
        expiry = date.today()
        widened = False
    else:
        expiries = await _discover_expiries(symbol, EXPIRY_TARGET_MIN_DAYS, EXPIRY_TARGET_MAX_DAYS)

        widened = False
        if not expiries:
            widened = True
            expiries = await _discover_expiries(symbol, 0, EXPIRY_FALLBACK_MAX_DAYS)

        if not expiries:
            return None

        target_days = (EXPIRY_TARGET_MIN_DAYS + EXPIRY_TARGET_MAX_DAYS) / 2
        expiry = min(expiries, key=lambda e: abs((e - date.today()).days - target_days))

    # Exact expiration_date (not a range) returns the full strike set for
    # this one day, unpaginated (verified: 390 contracts, no next_page_token).
    contracts_req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        expiration_date=expiry.isoformat(),
        limit=1000,
    )
    contracts = (await asyncio.to_thread(_trading_client.get_option_contracts, contracts_req)).option_contracts

    chain = await asyncio.to_thread(
        _option_data_client.get_option_chain, OptionChainRequest(underlying_symbol=symbol, expiration_date=expiry)
    )

    quotes: dict[float, dict] = {}
    for c in contracts:
        snapshot = chain.get(c.symbol)
        if snapshot is None or snapshot.latest_quote is None:
            continue
        q = snapshot.latest_quote
        if q.bid_price <= 0 or q.ask_price <= 0:
            continue
        strike = float(c.strike_price)
        side = "call" if c.type == ContractType.CALL else "put"
        quotes.setdefault(strike, {})[side] = {
            "symbol": c.symbol,
            "bid": q.bid_price,
            "ask": q.ask_price,
            "mid": round((q.bid_price + q.ask_price) / 2, 4),
        }

    if widened:
        quotes["_widened_search"] = True  # signal for the caller's log, stripped before use
    return expiry, quotes


def _nearest_strike(quotes: dict, target: float, side: str) -> float | None:
    candidates = [k for k, v in quotes.items() if isinstance(k, float) and side in v]
    if not candidates:
        return None
    return min(candidates, key=lambda k: abs(k - target))


def _leg(quotes: dict, strike: float, side: str, action: str) -> dict:
    q = quotes[strike][side]
    return {
        "action": action,  # "buy" or "sell"
        "right": side,      # "call" or "put"
        "strike": strike,
        "symbol": q["symbol"],
        "mid_price": q["mid"],
    }


def _build_debit_spread(quotes: dict, spot: float, direction: str) -> dict | None:
    side = "call" if direction == "bullish" else "put"
    long_strike = _nearest_strike(quotes, spot, side)
    far_target = spot * (1 + OTM_PCT_FAR) if direction == "bullish" else spot * (1 - OTM_PCT_FAR)
    short_strike = _nearest_strike(quotes, far_target, side)
    if long_strike is None or short_strike is None or long_strike == short_strike:
        return None

    long_leg = _leg(quotes, long_strike, side, "buy")
    short_leg = _leg(quotes, short_strike, side, "sell")
    net_debit = round(long_leg["mid_price"] - short_leg["mid_price"], 4)
    width = abs(short_strike - long_strike)

    return {
        "type": "DEBIT_SPREAD",
        "direction": direction,
        "legs": [long_leg, short_leg],
        "net_debit": net_debit,
        "max_loss": round(net_debit * CONTRACT_MULTIPLIER, 2),
        "max_gain": round((width - net_debit) * CONTRACT_MULTIPLIER, 2),
    }


def _build_single_long(quotes: dict, spot: float, direction: str) -> dict | None:
    side = "call" if direction == "bullish" else "put"
    strike = _nearest_strike(quotes, spot, side)
    if strike is None:
        return None

    leg = _leg(quotes, strike, side, "buy")
    max_loss = round(leg["mid_price"] * CONTRACT_MULTIPLIER, 2)
    max_gain = "unlimited" if side == "call" else round((strike - leg["mid_price"]) * CONTRACT_MULTIPLIER, 2)

    return {
        "type": "SINGLE_LONG",
        "direction": direction,
        "legs": [leg],
        "net_debit": leg["mid_price"],
        "max_loss": max_loss,
        "max_gain": max_gain,
    }


def _build_iron_condor(quotes: dict, spot: float) -> dict | None:
    short_call_strike = _nearest_strike(quotes, spot * (1 + OTM_PCT_FAR), "call")
    long_call_strike = _nearest_strike(quotes, spot * (1 + OTM_PCT_WING), "call")
    short_put_strike = _nearest_strike(quotes, spot * (1 - OTM_PCT_FAR), "put")
    long_put_strike = _nearest_strike(quotes, spot * (1 - OTM_PCT_WING), "put")
    strikes = [short_call_strike, long_call_strike, short_put_strike, long_put_strike]
    if any(s is None for s in strikes) or len(set(strikes)) < 4:
        return None

    short_call = _leg(quotes, short_call_strike, "call", "sell")
    long_call = _leg(quotes, long_call_strike, "call", "buy")
    short_put = _leg(quotes, short_put_strike, "put", "sell")
    long_put = _leg(quotes, long_put_strike, "put", "buy")

    net_credit = round(
        (short_call["mid_price"] - long_call["mid_price"]) + (short_put["mid_price"] - long_put["mid_price"]), 4
    )
    call_wing_width = abs(long_call_strike - short_call_strike)
    put_wing_width = abs(short_put_strike - long_put_strike)
    max_wing_width = max(call_wing_width, put_wing_width)

    return {
        "type": "IRON_CONDOR",
        "direction": "neutral",
        "legs": [short_call, long_call, short_put, long_put],
        "net_credit": net_credit,
        "max_gain": round(net_credit * CONTRACT_MULTIPLIER, 2),
        "max_loss": round((max_wing_width - net_credit) * CONTRACT_MULTIPLIER, 2),
    }


def _build_straddle_strangle(quotes: dict, spot: float) -> dict | None:
    atm_call_strike = _nearest_strike(quotes, spot, "call")
    atm_put_strike = _nearest_strike(quotes, spot, "put")
    if atm_call_strike is None or atm_put_strike is None:
        return None

    straddle_cost = quotes[atm_call_strike]["call"]["mid"] + quotes[atm_put_strike]["put"]["mid"]
    if straddle_cost > spot * RICH_STRADDLE_PCT_OF_SPOT:
        call_strike = _nearest_strike(quotes, spot * (1 + STRANGLE_OTM_PCT), "call")
        put_strike = _nearest_strike(quotes, spot * (1 - STRANGLE_OTM_PCT), "put")
        shape = "STRANGLE"
    else:
        call_strike, put_strike = atm_call_strike, atm_put_strike
        shape = "STRADDLE"
    if call_strike is None or put_strike is None:
        return None

    call_leg = _leg(quotes, call_strike, "call", "buy")
    put_leg = _leg(quotes, put_strike, "put", "buy")
    net_debit = round(call_leg["mid_price"] + put_leg["mid_price"], 4)

    return {
        "type": "STRADDLE_STRANGLE",
        "shape": shape,
        "direction": "neutral",
        "legs": [call_leg, put_leg],
        "net_debit": net_debit,
        "max_loss": round(net_debit * CONTRACT_MULTIPLIER, 2),
        "max_gain": "unlimited",
    }


async def generate_candidates(
    regime_result: dict, iv_result: dict, trace_id: str | None = None, expiry_mode: str = "weekly"
) -> list[dict]:
    symbol = regime_result["symbol"]

    if not regime_result.get("ready", False):
        await log_event(
            "strategy_generate",
            {"regime": regime_result, "iv": iv_result},
            [],
            "regime not ready - refusing to map an unreliable regime read to a trade",
            trace_id=trace_id,
        )
        return []

    if iv_result.get("atm_iv") is None:
        await log_event(
            "strategy_generate",
            {"regime": regime_result, "iv": iv_result},
            [],
            "no usable IV reading - cannot select a matrix cell without it",
            trace_id=trace_id,
        )
        return []

    regime = regime_result["regime"]
    iv_bucket = _iv_bucket(iv_result["iv_rank"])
    matrix = _MATRIX_0DTE if expiry_mode == "0dte" else _MATRIX
    strategy_type = matrix[(regime, iv_bucket)]

    if strategy_type == "STAND_DOWN":
        await log_event(
            "strategy_generate",
            {"regime": regime, "iv_bucket": iv_bucket},
            [],
            "RANGE_VOLATILE + high IV: whipsaw and rich premium disadvantage both "
            "directional and premium-selling approaches structurally - standing down "
            "rather than forcing a trade",
            trace_id=trace_id,
        )
        return []

    underlying_trade = (
        await asyncio.to_thread(_stock_data_client.get_stock_latest_trade, StockLatestTradeRequest(symbol_or_symbols=symbol))
    )[symbol]
    spot = underlying_trade.price

    fetched = await _fetch_expiry_chain(symbol, spot, zero_dte=(expiry_mode == "0dte"))
    if fetched is None:
        await log_event(
            "strategy_generate",
            {"regime": regime, "iv_bucket": iv_bucket, "spot": spot, "expiry_mode": expiry_mode},
            [],
            "no same-day expiry available - standing down this 0DTE cycle rather than forcing a later expiry"
            if expiry_mode == "0dte"
            else "no option contracts available to build legs from",
            trace_id=trace_id,
        )
        return []
    expiry, quotes = fetched
    widened_search = quotes.pop("_widened_search", False)

    direction = regime_result.get("trend_direction")
    builders = {
        "DEBIT_SPREAD": lambda: _build_debit_spread(quotes, spot, direction),
        "SINGLE_LONG": lambda: _build_single_long(quotes, spot, direction),
        "IRON_CONDOR": lambda: _build_iron_condor(quotes, spot),
        "STRADDLE_STRANGLE": lambda: _build_straddle_strangle(quotes, spot),
    }
    candidate = builders[strategy_type]()

    if candidate is None:
        await log_event(
            "strategy_generate",
            {"regime": regime, "iv_bucket": iv_bucket, "strategy_type": strategy_type, "spot": spot},
            [],
            "matrix selected a strategy but the live chain didn't have usable strikes/quotes to build it",
            trace_id=trace_id,
        )
        return []

    low_confidence = regime_result.get("confidence", 0) < LOW_CONFIDENCE_THRESHOLD or iv_result.get(
        "cold_start", False
    )

    candidate.update(
        {
            "symbol": symbol,
            "expiry": expiry.isoformat(),
            "expiry_mode": expiry_mode,
            "spot_at_generation": spot,
            "low_confidence": low_confidence,
            "regime": regime,
            "iv_rank": iv_result["iv_rank"],
            "iv_bucket": iv_bucket,
        }
    )
    candidates = [candidate]

    await log_event(
        "strategy_generate",
        {"regime": regime_result, "iv": iv_result, "expiry_mode": expiry_mode},
        candidates,
        f"matrix cell ({regime}, {iv_bucket}) -> {strategy_type}"
        + (f" [{expiry_mode}]" if expiry_mode == "0dte" else "")
        + (" [widened expiry search - nothing in the 7-14d window]" if widened_search else "")
        + (" [low_confidence]" if low_confidence else ""),
        trace_id=trace_id,
    )
    return candidates
