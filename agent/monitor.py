"""Poll open positions, close on trigger, and label outcomes.

Reuse pattern (shape only) from the old crypto agent's monitor.py +
outcome_tracker.py: periodic poll of pending items -> compare against close
conditions -> close and label -> resolve, logging a "watching" event every
tick even when nothing closes.

What's different for options (per step08's doc):
- No simple SL/TP price check on the underlying - the position is
  mark-to-market from its own legs' current bid/ask, not the underlying's
  price.
- Three trigger types instead of two: max loss hit, profit target hit (the
  target differs for credit vs. debit-style strategies), and expiry
  management. The last one only applies to strategies carrying a short leg
  (DEBIT_SPREAD, IRON_CONDOR) - a long-only position (SINGLE_LONG,
  STRADDLE_STRANGLE) has no assignment/pin risk and can safely ride to
  expiry, so it isn't force-closed on a countdown the way short premium is.
- Theta decay is logged on every watching tick for credit strategies (part
  of "the position is behaving as expected" being its own loggable event),
  not just reported at close.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

from alpaca.common.exceptions import APIError
from alpaca.data.requests import OptionLatestQuoteRequest
from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

from agent.alpaca_clients import make_option_data_client
from agent.execution import _load_positions, _save_positions, _trading_client
from agent.logs import log_event

# doc/close_logic_fix.md item 2: per-strategy-type thresholds - IRON_CONDOR
# and DEBIT_SPREAD used to share one rule (branching on max_gain=="unlimited"
# rather than on the actual strategy type), which also misclassified a
# bearish SINGLE_LONG put (numeric max_gain) into the DEBIT_SPREAD/IRON_CONDOR
# bucket instead of matching its own bullish counterpart.
PROFIT_TARGET_IRON_CONDOR = 0.5      # % of max_gain
LOSS_TRIGGER_IRON_CONDOR = 0.9       # % of max_loss
PROFIT_TARGET_DEBIT_SPREAD = 0.65    # % of max_gain
LOSS_TRIGGER_DEBIT_SPREAD = 0.5      # % of max_loss
PROFIT_TARGET_LONG_PREMIUM = 1.0     # % return on premium paid - SINGLE_LONG (both directions) + STRADDLE_STRANGLE
LOSS_TRIGGER_LONG_PREMIUM = 0.5      # % of premium paid - same group

# doc/zerodte_and_watchlist.md item 1: ONE pair of thresholds for 0DTE,
# applied regardless of strategy type - tighter than any weekly threshold
# (25-30% profit / 40-50% loss vs. weekly's per-type 50-90%/50-65%), since a
# same-day position has no time left to recover from an adverse move the way
# a multi-day one might.
PROFIT_TARGET_0DTE = 0.28            # midpoint of the 25-30% range asked for
LOSS_TRIGGER_0DTE = 0.45             # midpoint of the 40-50% range asked for

# Hard close-by time - 45 min before market close (midpoint of the 30-60 min
# range asked for), regardless of P&L. Checked against Alpaca's own clock
# (real market hours/holidays), not a naive fixed wall-clock guess.
ZERO_DTE_CUTOFF_MINUTES_BEFORE_CLOSE = 45

# doc/position_overflow_and_stuck_close.md item 1: how long a fresh entry
# order gets to fill normally before it's treated as stale. A flat, never-
# filled entry never crosses any PnL trigger (there's no real PnL to
# compute against nothing), so without this check it can sit "open" in our
# own bookkeeping forever - confirmed directly against 6 real GTC limit
# orders left resting for 15-40+ minutes while the market stayed open the
# whole time, their limit prices already stale against the current mid.
ENTRY_FILL_GRACE_PERIOD_MINUTES = 5

EXPIRY_MANAGEMENT_DAYS = 2                # force-close short-leg WEEKLY strategies within this many days of expiry
                                           # (0DTE positions expire today by definition - see the hard cutoff above,
                                           # which supersedes this day-granularity check for them entirely)
OUTCOME_NEUTRAL_BAND_PCT = 0.05           # WIN/LOSS/NEUTRAL band, as a fraction of max_loss - adapted from the old
                                           # codebase's +/-0.3% raw-price band; 0.3% of max defined risk would be an
                                           # almost-always-NEUTRAL band here, so the band is rescaled to this metric's
                                           # actual typical swing size rather than reusing the number verbatim.

_option_data_client = make_option_data_client()


def _has_short_leg(position: dict) -> bool:
    return any(leg["action"] == "sell" for leg in position["legs"])


def _raw_value(legs: list[dict], prices: dict[str, float]) -> float:
    """buy legs contribute +price, sell legs contribute -price. This is the
    same sign convention execution.py's _limit_price used to build the
    original entry order, which is why it lines up directly with the stored
    entry_price for a PnL diff regardless of strategy type (verified by hand
    for both a debit spread and an iron condor before relying on it)."""
    return sum(prices[leg["symbol"]] * (1 if leg["action"] == "buy" else -1) for leg in legs)


async def _current_pnl(position: dict) -> tuple[float, float]:
    """Returns (unrealized_pnl_dollars, pnl_pct_of_max_loss) using current mid prices."""
    symbols = [leg["symbol"] for leg in position["legs"]]
    quotes = await asyncio.to_thread(
        _option_data_client.get_option_latest_quote, OptionLatestQuoteRequest(symbol_or_symbols=symbols)
    )
    mids = {s: (q.bid_price + q.ask_price) / 2 for s, q in quotes.items()}

    current_value = _raw_value(position["legs"], mids)
    unrealized_pnl = (current_value - position["entry_price"]) * position["contracts"] * 100
    pnl_pct_of_max_loss = unrealized_pnl / position["max_loss"] if position["max_loss"] else 0.0
    return unrealized_pnl, pnl_pct_of_max_loss


async def get_positions_with_pnl() -> list[dict]:
    """Local position store enriched with live current_pnl_pct for open
    positions (realized_pnl-based for closed ones). Shared by the MCP
    server's get_open_positions tool and the frontend's data-bridge API."""
    positions = await _load_positions()
    enriched = []
    for p in positions:
        p = dict(p)
        if p.get("status") == "open":
            try:
                unrealized_pnl, pnl_pct = await _current_pnl(p)
                p["current_pnl_pct"] = round(pnl_pct, 4)
                target = _profit_target_dollars(p)
                p["profit_target_pct"] = round(unrealized_pnl / target, 4) if target else None
            except Exception:
                p["current_pnl_pct"] = None
                p["profit_target_pct"] = None
        else:
            p["current_pnl_pct"] = round(p.get("realized_pnl", 0) / p["max_loss"], 4) if p.get("max_loss") else None
            p["profit_target_pct"] = None
        enriched.append(p)
    return enriched


def _thresholds_for(strategy_type: str, expiry_mode: str = "weekly") -> tuple[float, float]:
    """Returns (loss_fraction, profit_fraction). Both are expressed as a
    fraction of max_loss / max_gain respectively, EXCEPT for the long-premium
    group (SINGLE_LONG, STRADDLE_STRANGLE), where max_loss already equals the
    full premium paid by construction (see strategy.py's _build_single_long /
    _build_straddle_strangle) - so "loss_fraction of max_loss" and
    "loss_fraction of premium paid" are the same number for that group; no
    special-casing needed on the loss side.

    0DTE uses ONE pair of thresholds regardless of strategy type - doc/
    zerodte_and_watchlist.md item 1 gives a single profit/loss range for
    same-day positions, not per-strategy ranges the way weekly does."""
    if expiry_mode == "0dte":
        return LOSS_TRIGGER_0DTE, PROFIT_TARGET_0DTE
    if strategy_type == "IRON_CONDOR":
        return LOSS_TRIGGER_IRON_CONDOR, PROFIT_TARGET_IRON_CONDOR
    if strategy_type == "DEBIT_SPREAD":
        return LOSS_TRIGGER_DEBIT_SPREAD, PROFIT_TARGET_DEBIT_SPREAD
    if strategy_type in ("SINGLE_LONG", "STRADDLE_STRANGLE"):
        return LOSS_TRIGGER_LONG_PREMIUM, PROFIT_TARGET_LONG_PREMIUM
    raise ValueError(f"no close-trigger thresholds defined for strategy type {strategy_type!r}")


def _profit_target_dollars(position: dict) -> float:
    """The actual dollar amount _check_trigger compares unrealized_pnl
    against for a profit-target close - extracted so the frontend can show
    real progress toward THIS number instead of the doc/
    source_of_truth_and_race_conditions.md item 1 bug: the UI showed
    unrealized_pnl as a % of max_loss right next to a "65% target" that is
    actually 65% of max_gain - two different denominators that look like
    the same number. For a DEBIT_SPREAD, max_gain is routinely 3-4x
    max_loss, so 100%+ of max_loss can still be well under the real
    target - confirmed for real against a live account: $1,927 profit read
    as "106%" while the real target was $4,798 (65% of a $7,381 max_gain),
    and the position was never actually close to triggering."""
    strategy_type = position["type"]
    _, profit_fraction = _thresholds_for(strategy_type, position.get("expiry_mode", "weekly"))
    if strategy_type in ("SINGLE_LONG", "STRADDLE_STRANGLE"):
        premium_paid = abs(position["entry_price"]) * position["contracts"] * 100
        return profit_fraction * premium_paid
    return profit_fraction * position["max_gain"]


def _check_trigger(position: dict, unrealized_pnl: float, pnl_pct_of_max_loss: float, clock=None) -> str | None:
    strategy_type = position["type"]
    expiry_mode = position.get("expiry_mode", "weekly")
    loss_fraction, profit_fraction = _thresholds_for(strategy_type, expiry_mode)

    if pnl_pct_of_max_loss <= -loss_fraction:
        return (
            f"max loss trigger: {-pnl_pct_of_max_loss:.0%} of defined max loss realized "
            f"({loss_fraction:.0%} threshold, {strategy_type})"
        )

    target = _profit_target_dollars(position)
    if unrealized_pnl >= target:
        basis = "return on premium paid" if strategy_type in ("SINGLE_LONG", "STRADDLE_STRANGLE") else "of max gain"
        return (
            f"profit target trigger: unrealized gain ${unrealized_pnl:,.2f} >= "
            f"{profit_fraction:.0%} {basis} ({strategy_type})"
        )

    if expiry_mode == "0dte":
        # doc/zerodte_and_watchlist.md item 1: a fixed clock-time cutoff,
        # IN ADDITION to the profit/loss triggers above, regardless of P&L.
        # This replaces (not stacks with) the day-granularity expiry-
        # management check below, which doesn't mean anything at same-day
        # timescale - checked against Alpaca's real clock (actual market
        # hours/holidays), not a naive fixed wall-clock guess.
        if clock is not None:
            cutoff = clock.next_close - timedelta(minutes=ZERO_DTE_CUTOFF_MINUTES_BEFORE_CLOSE)
            if clock.timestamp >= cutoff:
                return (
                    f"0DTE hard time cutoff: within {ZERO_DTE_CUTOFF_MINUTES_BEFORE_CLOSE} min of market close - "
                    f"closing regardless of P&L status"
                )
    elif _has_short_leg(position):
        days_to_expiry = (date.fromisoformat(position["expiry"]) - date.today()).days
        if days_to_expiry <= EXPIRY_MANAGEMENT_DAYS:
            return (
                f"expiry management trigger: {days_to_expiry} day(s) to expiry with a short leg open - "
                f"not letting short premium ride unmanaged"
            )

    return None


async def _real_positions_by_symbol() -> dict[str, object]:
    """Alpaca's own actual account-level positions, keyed by option symbol.
    doc/close_logic_fix.md item 1: the real bug behind the "position intent
    mismatch, inferred: sell_to_open, specified: sell_to_close" rejections
    wasn't the buy/sell-to-close mapping itself (that was already the correct
    opposite-of-open direction) - it's that the affected position's entry
    order had NEVER ACTUALLY FILLED (confirmed directly against Alpaca:
    order.status == "NEW", filled_qty=0 on every leg). execute_order() records
    a position as "open" as soon as Alpaca ACCEPTS the order, not once it
    FILLS, so monitor.py was computing fake P&L against a position that never
    existed and then trying to "close" a leg Alpaca has no record of ever
    opening - which is exactly what that rejection means. Querying the real
    account state here (not the original candidate's pre-trade action,
    "assumed") is what the fix needs to check against."""
    positions = await asyncio.to_thread(_trading_client.get_all_positions)
    return {p.symbol: p for p in positions}


def _build_closing_order(position: dict, quotes: dict, real_positions: dict[str, object]) -> LimitOrderRequest | None:
    """Mirrors execution.py's _build_order_request, reversed: each leg's side
    flips, and pricing uses the bid (for legs being sold to close) / ask (for
    legs being bought back to close) - not mid - so the close is priced to
    actually fill against current resting liquidity rather than sit passively.

    Side/position_intent are derived from Alpaca's REAL current position for
    each symbol, not the original candidate's pre-trade leg action - see
    _real_positions_by_symbol(). Returns None if any leg has no real open
    position at Alpaca (nothing to close - the entry never filled)."""
    legs = position["legs"]

    leg_sides: dict[str, tuple[OrderSide, PositionIntent]] = {}
    for leg in legs:
        real_pos = real_positions.get(leg["symbol"])
        if real_pos is None or float(real_pos.qty) == 0:
            return None
        is_long = float(real_pos.qty) > 0
        leg_sides[leg["symbol"]] = (
            (OrderSide.SELL, PositionIntent.SELL_TO_CLOSE) if is_long else (OrderSide.BUY, PositionIntent.BUY_TO_CLOSE)
        )

    closing_prices = {
        leg["symbol"]: (
            quotes[leg["symbol"]].bid_price
            if leg_sides[leg["symbol"]][0] == OrderSide.SELL
            else quotes[leg["symbol"]].ask_price
        )
        for leg in legs
    }
    closing_limit_price = round(
        sum(
            closing_prices[leg["symbol"]] * (1 if leg_sides[leg["symbol"]][0] == OrderSide.SELL else -1)
            for leg in legs
        ),
        2,
    )

    if len(legs) == 1:
        leg = legs[0]
        side, position_intent = leg_sides[leg["symbol"]]
        return LimitOrderRequest(
            symbol=leg["symbol"],
            qty=position["contracts"],
            side=side,
            time_in_force=TimeInForce.DAY,
            limit_price=abs(closing_limit_price),
            position_intent=position_intent,
        )

    order_legs = []
    for leg in legs:
        side, position_intent = leg_sides[leg["symbol"]]
        order_legs.append(
            OptionLegRequest(symbol=leg["symbol"], side=side, ratio_qty=1, position_intent=position_intent)
        )

    return LimitOrderRequest(
        qty=position["contracts"],
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        legs=order_legs,
        limit_price=closing_limit_price,
    )


async def close_position(position: dict, reason: str, trace_id: str | None = None) -> dict:
    symbols = [leg["symbol"] for leg in position["legs"]]
    real_positions = await _real_positions_by_symbol()

    # doc/close_logic_fix.md item 1: if the entry order never filled, there is
    # genuinely nothing to close - cancel the stale resting order instead of
    # submitting a trade Alpaca will reject (or, worse, one that would open a
    # brand-new unintended position from scratch).
    if any(s not in real_positions or float(real_positions[s].qty) == 0 for s in symbols):
        try:
            await asyncio.to_thread(_trading_client.cancel_order_by_id, position["order_id"])
        except APIError as e:
            result = {"status": "cancel_failed", "error": str(e)}
            await log_event(
                "close_position",
                {"position": position, "reason": reason},
                result,
                f"entry order for {position['symbol']} {position['type']} never filled and cancel failed: {e}",
                trace_id=trace_id,
            )
            return result
        result = {"status": "cancelled_unfilled"}
        await log_event(
            "close_position",
            {"position": position, "reason": reason},
            result,
            f"entry order for {position['symbol']} {position['type']} never filled - cancelled the stale "
            "resting order instead of attempting a close",
            trace_id=trace_id,
        )
        return result

    quotes = await asyncio.to_thread(
        _option_data_client.get_option_latest_quote, OptionLatestQuoteRequest(symbol_or_symbols=symbols)
    )
    req = _build_closing_order(position, quotes, real_positions)

    try:
        res = await asyncio.to_thread(_trading_client.submit_order, req)
    except APIError as e:
        result = {"status": "close_rejected", "error": str(e)}
        await log_event(
            "close_position",
            {"position": position, "reason": reason},
            result,
            f"Alpaca rejected the close: {e}",
            trace_id=trace_id,
        )
        return result

    result = {
        "status": "close_submitted",
        "order_id": str(res.id),
        "order_status": str(res.status),
        "exit_price": req.limit_price,
    }
    await log_event(
        "close_position",
        {"position": position, "reason": reason},
        result,
        f"submitted closing order for {position['symbol']} {position['type']}: {reason}",
        trace_id=trace_id,
    )
    return result


def label_outcome(entry_state: dict, exit_state: dict) -> str:
    max_loss = entry_state["max_loss"]
    pnl_pct = (exit_state["exit_price"] - entry_state["entry_price"]) * entry_state["contracts"] * 100 / max_loss if max_loss else 0.0
    if pnl_pct > OUTCOME_NEUTRAL_BAND_PCT:
        return "WIN"
    if pnl_pct < -OUTCOME_NEUTRAL_BAND_PCT:
        return "LOSS"
    return "NEUTRAL"


async def check_positions(trace_id: str | None = None, expiry_mode: str | None = None) -> list[dict]:
    """One poll tick over every open position. Closes and labels any that
    hit a trigger; logs a 'watching' event for the rest so the frontend feed
    stays alive between trades.

    expiry_mode filters which positions this tick processes ("weekly" |
    "0dte" | None for all) - doc/zerodte_and_watchlist.md item 1 wants 0DTE
    positions monitored on a tighter cadence WITHOUT slowing down weekly's
    existing one, which server.py achieves by running two separate loops
    against two disjoint filters rather than one loop doing double the work.
    Mutually exclusive filtering also means a position is never processed by
    both loops in the same tick - no risk of the double-processing/duplicate-
    event bug class doc/chart_scroll_and_pnl_flap.md hit with duplicate
    backend processes."""
    positions = await _load_positions()
    results = []

    # Only spend the extra Alpaca call on the clock if there's actually a
    # 0DTE position in scope to check it against.
    relevant = [
        p
        for p in positions
        if p.get("status") == "open" and (expiry_mode is None or p.get("expiry_mode", "weekly") == expiry_mode)
    ]
    clock = None
    if any(p.get("expiry_mode") == "0dte" for p in relevant):
        clock = await asyncio.to_thread(_trading_client.get_clock)

    # doc/position_overflow_and_stuck_close.md item 1: fetched once per tick
    # (not per-position) - the same real-account query close_position()
    # already uses internally to detect a never-filled entry, hoisted up
    # here so an unfilled position can be caught BEFORE wasting a PnL
    # computation on it and, more importantly, before it can sit "open"
    # forever with nothing ever triggering on it.
    real_positions = await _real_positions_by_symbol()

    for position in positions:
        if position.get("status") != "open":
            continue
        if expiry_mode is not None and position.get("expiry_mode", "weekly") != expiry_mode:
            continue

        leg_symbols = [leg["symbol"] for leg in position["legs"]]
        is_filled = all(s in real_positions and float(real_positions[s].qty) != 0 for s in leg_symbols)

        if not is_filled:
            age_minutes = (
                datetime.now(timezone.utc) - datetime.fromisoformat(position["opened_at"])
            ).total_seconds() / 60
            if age_minutes < ENTRY_FILL_GRACE_PERIOD_MINUTES:
                continue  # still within the normal fill window - nothing to evaluate yet
            trigger_reason = (
                f"entry order still unfilled after {age_minutes:.0f} min (grace period "
                f"{ENTRY_FILL_GRACE_PERIOD_MINUTES} min) - cancelling rather than let it "
                f"count as open risk indefinitely"
            )
        else:
            unrealized_pnl, pnl_pct_of_max_loss = await _current_pnl(position)
            trigger_reason = _check_trigger(position, unrealized_pnl, pnl_pct_of_max_loss, clock)

            if trigger_reason is None:
                # Credit strategy = IRON_CONDOR specifically (entry_price stored negative,
                # i.e. net credit received) - NOT "has a short leg", which is also true of
                # DEBIT_SPREAD (a net-debit strategy that happens to include a short leg).
                is_credit_strategy = position["entry_price"] < 0
                decay_note = (
                    f"theta decay: {pnl_pct_of_max_loss:+.1%} of max risk captured toward the {position['type']} credit"
                    if is_credit_strategy
                    else f"unrealized P&L tracking toward the {position['type']} profit target"
                )
                watching_result = {
                    "position_id": position["id"],
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "pnl_pct_of_max_loss": round(pnl_pct_of_max_loss, 4),
                }
                await log_event(
                    "watching",
                    {"position_id": position["id"], "symbol": position["symbol"], "type": position["type"]},
                    watching_result,
                    f"unrealized P&L ${unrealized_pnl:,.2f} ({pnl_pct_of_max_loss:+.1%} of max risk) - {decay_note}",
                    trace_id=position.get("trace_id"),
                )
                results.append({"position_id": position["id"], "action": "watching", **watching_result})
                continue

        close_result = await close_position(position, trigger_reason, trace_id=position.get("trace_id"))

        if close_result["status"] == "cancelled_unfilled":
            # doc/close_logic_fix.md: the entry order never filled - nothing
            # was ever actually at risk, so this is neither a WIN/LOSS/NEUTRAL
            # trade outcome nor a "close_failed" retry candidate. Mark it
            # cancelled and drop it out of the open-position loop for good.
            position.update(
                {
                    "status": "cancelled",
                    "close_reason": trigger_reason,
                    "realized_pnl": 0.0,
                    "closed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await _save_positions(positions)
            results.append({"position_id": position["id"], "action": "cancelled_unfilled"})
            continue

        if close_result["status"] != "close_submitted":
            results.append({"position_id": position["id"], "action": "close_failed", **close_result})
            continue

        exit_state = {"exit_price": close_result["exit_price"]}
        outcome = label_outcome(position, exit_state)
        realized_pnl = (exit_state["exit_price"] - position["entry_price"]) * position["contracts"] * 100

        position.update(
            {
                "status": "closed",
                "close_order_id": close_result["order_id"],
                "close_reason": trigger_reason,
                "exit_price": exit_state["exit_price"],
                "realized_pnl": round(realized_pnl, 2),
                "outcome": outcome,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await _save_positions(positions)

        await log_event(
            "outcome_labeled",
            {"position_id": position["id"], "trace_id": position.get("trace_id")},
            {"outcome": outcome, "realized_pnl": round(realized_pnl, 2)},
            f"{position['symbol']} {position['type']} closed {outcome} (${realized_pnl:,.2f}): {trigger_reason}",
            trace_id=position.get("trace_id"),
        )
        results.append(
            {"position_id": position["id"], "action": "closed", "outcome": outcome, "realized_pnl": round(realized_pnl, 2)}
        )

    return results
