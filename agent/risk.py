"""Risk / position sizing for step04's candidates against account limits.

Risk-cap basis: account EQUITY, not `buying_power`. Confirmed with the user
on 2026-09-01 - the step05 doc's own worked example ("2% of buying power =
$2,000") only holds if the base is the $100k equity/starting-capital figure;
this account's actual `buying_power` is $400,000 (4x margin), which would
make 2% = $8,000, contradicting the doc's own number. Using the leveraged
buying_power figure as a risk-per-trade basis would itself be exactly the
kind of mechanical sizing mistake the project's docs warn against elsewhere.

Drawdown circuit-breaker threshold: 10%, confirmed with the user on
2026-09-01 (not specified anywhere in the docs).
"""
import asyncio

from agent.alpaca_clients import make_trading_client
from agent.db import get_pool
from agent.execution import _load_positions, reconcile_with_alpaca
from agent.logs import log_event

PER_TRADE_RISK_PCT = 0.02       # 2% of equity per trade (weekly mode)
MAX_CONCURRENT_POSITIONS = 3
DRAWDOWN_CEILING_PCT = 0.10     # stand down entirely below this drawdown from the tracked high-water mark

# doc/zerodte_and_watchlist.md item 1: separate, smaller cap for 0DTE - 1% of
# equity, hard-capped at $1,000 regardless of account size (same-day theta
# and gap risk warrants a tighter ceiling than the weekly 2% cap, not just a
# smaller percentage of it).
PER_TRADE_RISK_PCT_0DTE = 0.01
PER_TRADE_RISK_CAP_0DTE = 1000.0

HWM_KEY = "equity_high_water_mark"

_trading_client = make_trading_client()


async def get_account_state() -> dict:
    """Fetch and shape live account state. size_candidates() itself takes
    this as a plain dict argument (not a live call) so it stays testable
    without hitting the API.

    doc/position_overflow_and_stuck_close.md item 1: open_positions_count
    used to come from Alpaca's get_all_positions() - REAL, ACTUALLY-FILLED
    positions only. execute_order() marks a position "open" in our own
    Postgres table the moment Alpaca ACCEPTS the order, not once it fills
    (the same behavior already documented in close_logic_fix.md item 1). A
    GTC limit order priced at the mid at generation time can sit unfilled
    for a long time if price drifts away from it before it's marketable -
    confirmed directly against a real stuck account state: 6 DEBIT_SPREAD
    entries sitting at OrderStatus.NEW/filled_qty=0 for 15-40+ minutes with
    the market open the whole time, each one's limit price already stale
    against the current mid. Because the cap check only ever saw Alpaca's
    real (still-low) fill count, it kept approving new entries for
    different symbols every cycle without limit, while our own local
    bookkeeping quietly piled up "open" records for orders that were never
    real risk yet - the opposite of what a concurrency cap is for. Counting
    our OWN local "open" rows (submitted, whether filled or not) closes
    that gap: the moment we decide to open something, it counts against the
    cap immediately, exactly like a real position would.

    doc/source_of_truth_and_race_conditions.md item 2: that fix swung the
    source of truth the WRONG way - to local state - which is the actual
    root pattern behind "duplicate positions, stuck closes, mismatched
    counts". Alpaca is the permanent source of truth; local Postgres is a
    cache of it. Reconciling against Alpaca's real order status FIRST, then
    counting the now-corrected local state, gets both properties at once:
    a stale-but-not-yet-cleaned-up local record can never inflate or
    understate the count, because it's verified against Alpaca on every
    single call to this function - i.e. before every trade decision, not
    just on a periodic background pass."""
    account = await asyncio.to_thread(_trading_client.get_account)
    await reconcile_with_alpaca()
    local_positions = await _load_positions()
    open_count = sum(1 for p in local_positions if p.get("status") == "open")
    return {
        "equity": float(account.equity),
        "buying_power": float(account.buying_power),
        "open_positions_count": open_count,
    }


async def _update_high_water_mark(equity: float) -> float:
    """Atomic upsert-with-GREATEST in one round trip, instead of the old
    file's read-then-write - also closes a race the file version had if this
    were ever called concurrently (it currently isn't, but the DB gives us
    this for free)."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO account_state (key, value, updated_at) VALUES ($1, $2, now())
           ON CONFLICT (key) DO UPDATE SET
               value = GREATEST(account_state.value, EXCLUDED.value),
               updated_at = now()
           RETURNING value""",
        HWM_KEY,
        equity,
    )
    return float(row["value"])


def _reject_all(candidates: list[dict], reason: str) -> list[dict]:
    return [{**c, "contracts": 0, "passes_risk": False, "reason": reason} for c in candidates]


async def size_candidates(
    candidates: list[dict], account_state: dict, trace_id: str | None = None, expiry_mode: str = "weekly"
) -> list[dict]:
    equity = account_state["equity"]
    risk_cap_dollars = (
        round(min(equity * PER_TRADE_RISK_PCT_0DTE, PER_TRADE_RISK_CAP_0DTE), 2)
        if expiry_mode == "0dte"
        else round(equity * PER_TRADE_RISK_PCT, 2)
    )
    cap_description = (
        f"1% of ${equity:,.2f} equity, capped at ${PER_TRADE_RISK_CAP_0DTE:,.2f}"
        if expiry_mode == "0dte"
        else f"2% of ${equity:,.2f} equity"
    )

    high_water_mark = await _update_high_water_mark(equity)
    drawdown_pct = (high_water_mark - equity) / high_water_mark if high_water_mark > 0 else 0.0

    if drawdown_pct > DRAWDOWN_CEILING_PCT:
        reason = (
            f"portfolio drawdown circuit breaker: equity ${equity:,.2f} is "
            f"{drawdown_pct:.1%} below high-water mark ${high_water_mark:,.2f} "
            f"(ceiling {DRAWDOWN_CEILING_PCT:.0%}) - standing down entirely "
            f"regardless of candidate quality"
        )
        sized = _reject_all(candidates, reason)
        await log_event(
            "risk_sizing", {"candidates": candidates, "account_state": account_state}, sized, reason, trace_id=trace_id
        )
        return sized

    if account_state["open_positions_count"] >= MAX_CONCURRENT_POSITIONS:
        reason = (
            f"max concurrent positions reached "
            f"({account_state['open_positions_count']}/{MAX_CONCURRENT_POSITIONS}) - rejecting new candidates"
        )
        sized = _reject_all(candidates, reason)
        await log_event(
            "risk_sizing", {"candidates": candidates, "account_state": account_state}, sized, reason, trace_id=trace_id
        )
        return sized

    sized = []
    for c in candidates:
        max_loss_per_unit = c["max_loss"]
        contracts = int(risk_cap_dollars // max_loss_per_unit) if max_loss_per_unit > 0 else 0

        if contracts < 1:
            sized.append(
                {
                    **c,
                    "contracts": 0,
                    "passes_risk": False,
                    "reason": (
                        f"cannot size even 1 contract within the ${risk_cap_dollars:,.2f} "
                        f"per-trade cap ({cap_description}) - single contract max "
                        f"loss is ${max_loss_per_unit:,.2f}"
                    ),
                }
            )
            continue

        scaled_max_loss = round(max_loss_per_unit * contracts, 2)
        scaled_max_gain = c["max_gain"] if c["max_gain"] == "unlimited" else round(c["max_gain"] * contracts, 2)
        sized.append(
            {
                **c,
                "contracts": contracts,
                "max_loss": scaled_max_loss,
                "max_gain": scaled_max_gain,
                "passes_risk": True,
                "reason": (
                    f"sized {contracts} contract(s): ${scaled_max_loss:,.2f} max loss "
                    f"within ${risk_cap_dollars:,.2f} per-trade cap ({cap_description})"
                ),
            }
        )

    await log_event(
        "risk_sizing",
        {"candidates": candidates, "account_state": account_state},
        sized,
        f"per-trade cap ${risk_cap_dollars:,.2f} ({cap_description}), drawdown {drawdown_pct:.1%} "
        f"(ceiling {DRAWDOWN_CEILING_PCT:.0%}), "
        f"{account_state['open_positions_count']}/{MAX_CONCURRENT_POSITIONS} positions open",
        trace_id=trace_id,
    )
    return sized
