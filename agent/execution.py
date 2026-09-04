"""Multi-leg options order execution via Alpaca's Trading API (paper).

SDK call shape verified directly against Alpaca's own official example
(examples/options/options-trading-mleg.ipynb in the alpaca-py GitHub repo,
fetched 2026-09-01) rather than guessed: OptionLegRequest(symbol, side,
ratio_qty[, position_intent]) + a MarketOrderRequest/LimitOrderRequest with
order_class=OrderClass.MLEG and a legs=[...] list, submitted via
trade_client.submit_order(). `position_intent` exists on OptionLegRequest but
is optional and unused in Alpaca's own example - we set it explicitly anyway
since step07's doc calls it out and every leg here is always opening a new
position (BUY_TO_OPEN / SELL_TO_OPEN), never closing one.

A 1-leg candidate (SINGLE_LONG) is submitted as a plain single-option order,
not wrapped in order_class=MLEG - Alpaca's own example never demonstrates a
1-leg mleg order, and a single option is already natively tradable without
multi-leg semantics.

Limit price sign convention (per the official example's iron-condor cell,
"limit_price=0 i.e. for a net price of 0"): positive = net debit (willing to
pay up to this much), negative = net credit (require at least this much
received).
"""
import asyncio
import json
from datetime import date, datetime, timezone

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

from agent.alpaca_clients import make_trading_client
from agent.db import get_pool
from agent.logs import log_event

_trading_client = make_trading_client()


def _parse_max_gain(value: str) -> "float | str":
    try:
        return float(value)
    except ValueError:
        return value  # "unlimited"


def _row_to_position(r) -> dict:
    return {
        "id": r["id"],
        "trace_id": r["decision_trace_id"],
        "order_id": r["id"],
        "client_order_id": r["client_order_id"],
        "symbol": r["symbol"],
        "type": r["strategy_type"],
        "legs": json.loads(r["legs"]),
        "contracts": r["contracts"],
        "entry_price": float(r["entry_price"]),
        "max_loss": float(r["max_loss"]),
        "max_gain": _parse_max_gain(r["max_gain"]),
        "expiry": r["expiry"].isoformat(),
        "expiry_mode": r["expiry_mode"],
        "order_status": r["order_status"],
        "opened_at": r["entry_ts"].isoformat(),
        "status": r["status"],
        "close_order_id": r["close_order_id"],
        "close_reason": r["close_reason"],
        "exit_price": float(r["exit_price"]) if r["exit_price"] is not None else None,
        "realized_pnl": float(r["realized_pnl"]) if r["realized_pnl"] is not None else None,
        "outcome": r["outcome"],
        "closed_at": r["exit_ts"].isoformat() if r["exit_ts"] is not None else None,
    }


async def _load_positions() -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM positions ORDER BY entry_ts")
    return [_row_to_position(r) for r in rows]


async def _upsert_position(p: dict) -> None:
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO positions (
               id, decision_trace_id, client_order_id, symbol, strategy_type, legs, contracts,
               entry_price, max_loss, max_gain, expiry, expiry_mode, order_status, status, entry_ts,
               close_order_id, close_reason, exit_price, realized_pnl, outcome, exit_ts
           ) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21)
           ON CONFLICT (id) DO UPDATE SET
               decision_trace_id = EXCLUDED.decision_trace_id,
               client_order_id = EXCLUDED.client_order_id,
               order_status = EXCLUDED.order_status,
               status = EXCLUDED.status,
               close_order_id = EXCLUDED.close_order_id,
               close_reason = EXCLUDED.close_reason,
               exit_price = EXCLUDED.exit_price,
               realized_pnl = EXCLUDED.realized_pnl,
               outcome = EXCLUDED.outcome,
               exit_ts = EXCLUDED.exit_ts""",
        p["id"],
        p.get("trace_id"),
        p.get("client_order_id"),
        p["symbol"],
        p["type"],
        json.dumps(p["legs"], default=str),
        p["contracts"],
        p["entry_price"],
        p["max_loss"],
        str(p["max_gain"]),
        date.fromisoformat(p["expiry"]) if isinstance(p["expiry"], str) else p["expiry"],
        p.get("expiry_mode", "weekly"),
        p.get("order_status"),
        p["status"],
        datetime.fromisoformat(p["opened_at"]) if isinstance(p["opened_at"], str) else p["opened_at"],
        p.get("close_order_id"),
        p.get("close_reason"),
        p.get("exit_price"),
        p.get("realized_pnl"),
        p.get("outcome"),
        datetime.fromisoformat(p["closed_at"]) if p.get("closed_at") else None,
    )


async def _save_positions(positions: list[dict]) -> None:
    """Upserts every position in the list. Kept as a whole-list call to match
    monitor.py's existing "mutate the list, save the list" pattern without
    needing to touch its call sites - each row is upserted independently
    under the hood, not a destructive replace."""
    for p in positions:
        await _upsert_position(p)


async def _append_position(record: dict) -> None:
    await _upsert_position(record)


# doc/source_of_truth_and_race_conditions.md item 2: Alpaca's real account
# state is the permanent source of truth - local Postgres is a cache/log of
# it, never the reverse. This is what position_overflow_and_stuck_close.md's
# fix got half-right: it stopped trusting Alpaca's FILLED-only position count
# (which under-counted real exposure sitting in resting orders) by switching
# to the LOCAL "open" count instead - but that swung the source of truth the
# WRONG way, to local state, which is exactly what THIS doc flags as the
# actual root pattern behind "duplicate positions, stuck closes, mismatched
# counts". The correct fix is neither "trust Alpaca's fill count" nor "trust
# local state" alone: reconcile local records against Alpaca's real order
# status first, correcting any local record Alpaca disagrees with, THEN
# count the (now-accurate) local state. Local "open" after reconciliation is
# trustworthy because it has just been verified against Alpaca, not because
# it was ever authoritative on its own.
async def reconcile_with_alpaca(trace_id: str | None = None) -> list[dict]:
    """Compares every local 'open' position's entry order against Alpaca's
    real order status and corrects any local record that disagrees. Returns
    the corrections made (empty list if local state already matched)."""
    positions = await _load_positions()
    open_positions = [p for p in positions if p.get("status") == "open"]
    corrections: list[dict] = []

    for p in open_positions:
        try:
            order = await asyncio.to_thread(_trading_client.get_order_by_id, p["id"])
        except APIError as e:
            # Can't reach Alpaca for this one right now - leave the local
            # record alone rather than guess; it'll be re-checked next pass.
            await log_event(
                "reconciliation",
                {"position_id": p["id"], "symbol": p["symbol"]},
                {"error": str(e)},
                f"could not fetch Alpaca's real order status for {p['symbol']} {p['type']} - skipping this pass",
                trace_id=trace_id,
            )
            continue

        status = str(order.status)
        filled_qty = float(order.filled_qty or 0)

        # A local "open" record whose entry order Alpaca says never filled
        # AND is in a terminal (non-recoverable) state is stale - correct it.
        if filled_qty == 0 and status in (
            "OrderStatus.CANCELED",
            "OrderStatus.EXPIRED",
            "OrderStatus.REJECTED",
            "OrderStatus.DONE_FOR_DAY",
        ):
            p["status"] = "cancelled"
            p["close_reason"] = f"reconciliation: Alpaca reports entry order {status}, never filled"
            p["realized_pnl"] = 0.0
            p["closed_at"] = datetime.now(timezone.utc).isoformat()
            await _upsert_position(p)
            correction = {
                "position_id": p["id"],
                "symbol": p["symbol"],
                "type": p["type"],
                "local_was": "open",
                "corrected_to": "cancelled",
                "alpaca_status": status,
            }
            corrections.append(correction)
            await log_event(
                "reconciliation",
                {"position_id": p["id"], "symbol": p["symbol"], "local_status": "open"},
                correction,
                f"corrected local record for {p['symbol']} {p['type']}: Alpaca shows entry order {status} "
                f"(never filled) - marked cancelled locally to match",
                trace_id=trace_id,
            )

    return corrections


async def find_orphaned_positions(trace_id: str | None = None) -> list[dict]:
    """The other half of reconcile_with_alpaca() above, which only checks
    LOCAL 'open' records against Alpaca's real order status. A real Alpaca
    position whose option symbol isn't covered by any local 'open' position's
    legs has no local governance at all - no monitor.py trigger will ever
    watch it, no risk cap will ever count it. This can only happen from a
    failure outside this process's control (e.g. a submit_order call that
    succeeded at Alpaca but whose response never reached this process), so
    the responsible move is to surface it loudly, not to guess at a
    reconstructed strategy record with made-up thresholds - we have no way
    to know the original strategy type, max_loss, or profit target for a
    position we never recorded opening."""
    real_positions = await asyncio.to_thread(_trading_client.get_all_positions)
    local_positions = await _load_positions()
    known_symbols = {
        leg["symbol"] for p in local_positions if p.get("status") == "open" for leg in p["legs"]
    }

    orphans = [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
        }
        for p in real_positions
        if p.symbol not in known_symbols
    ]

    if orphans:
        await log_event(
            "orphaned_position_detected",
            {"orphans": orphans},
            orphans,
            f"found {len(orphans)} real Alpaca position(s) with NO local record covering "
            f"them - unmonitored risk, needs manual review: {[o['symbol'] for o in orphans]}",
            trace_id=trace_id,
        )
    return orphans


def _limit_price(candidate: dict) -> float:
    if "net_debit" in candidate:
        return round(candidate["net_debit"], 2)
    return round(-candidate["net_credit"], 2)


def _build_order_request(candidate: dict) -> LimitOrderRequest:
    legs = candidate["legs"]
    limit_price = _limit_price(candidate)

    if len(legs) == 1:
        leg = legs[0]
        side = OrderSide.BUY if leg["action"] == "buy" else OrderSide.SELL
        position_intent = PositionIntent.BUY_TO_OPEN if leg["action"] == "buy" else PositionIntent.SELL_TO_OPEN
        return LimitOrderRequest(
            symbol=leg["symbol"],
            qty=candidate["contracts"],
            side=side,
            time_in_force=TimeInForce.GTC,
            limit_price=abs(limit_price),
            position_intent=position_intent,
        )

    order_legs = []
    for leg in legs:
        side = OrderSide.BUY if leg["action"] == "buy" else OrderSide.SELL
        position_intent = PositionIntent.BUY_TO_OPEN if leg["action"] == "buy" else PositionIntent.SELL_TO_OPEN
        order_legs.append(
            OptionLegRequest(symbol=leg["symbol"], side=side, ratio_qty=1, position_intent=position_intent)
        )

    return LimitOrderRequest(
        qty=candidate["contracts"],
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.GTC,
        legs=order_legs,
        limit_price=limit_price,
    )


async def execute_order(
    final_candidate: dict | None,
    stand_down_reason: str | None = None,
    trace_id: str | None = None,
    expiry_mode: str = "weekly",
) -> dict:
    if final_candidate is None:
        reason = stand_down_reason or "no approved candidate to execute"
        result = {"status": "stood_down", "reason": reason}
        await log_event("execute_order", {"final_candidate": None}, result, reason, trace_id=trace_id)
        return result

    req = _build_order_request(final_candidate)

    try:
        res = await asyncio.to_thread(_trading_client.submit_order, req)
    except APIError as e:
        result = {"status": "rejected", "error": str(e)}
        await log_event(
            "execute_order",
            {"final_candidate": final_candidate},
            result,
            f"Alpaca rejected the order: {e}",
            trace_id=trace_id,
        )
        return result

    legs_filled = [
        {"symbol": leg.symbol, "side": str(leg.side), "status": str(leg.status)} for leg in (res.legs or [])
    ] or [{"symbol": final_candidate["legs"][0]["symbol"], "side": str(res.side), "status": str(res.status)}]

    position_record = {
        "id": str(res.id),
        "trace_id": trace_id,
        "order_id": str(res.id),
        "client_order_id": res.client_order_id,
        "symbol": final_candidate["symbol"],
        "type": final_candidate["type"],
        "legs": final_candidate["legs"],
        "contracts": final_candidate["contracts"],
        "entry_price": _limit_price(final_candidate),
        "max_loss": final_candidate["max_loss"],
        "max_gain": final_candidate["max_gain"],
        "expiry": final_candidate["expiry"],
        "expiry_mode": final_candidate.get("expiry_mode", expiry_mode),
        "order_status": str(res.status),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
    }
    await _append_position(position_record)

    result = {"status": "submitted", "order_id": str(res.id), "order_status": str(res.status), "legs_filled": legs_filled}
    await log_event(
        "execute_order",
        {"final_candidate": final_candidate},
        result,
        f"submitted {final_candidate['type']} order for {final_candidate['contracts']} contract(s), "
        f"order_id={res.id}, status={res.status}",
        trace_id=trace_id,
    )
    return result
