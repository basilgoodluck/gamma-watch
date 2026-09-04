"""ATM implied volatility + IV rank for the underlying, independent of price
regime (see step02).

Deviation from the literal step03 spec, agreed with the user on 2026-09-01
(see step01 summary): Alpaca's options chain returns null greeks/IV for this
account because the OPRA market data agreement isn't signed - confirmed by
explicitly requesting the OPRA feed and getting "OPRA agreement is not
signed" back. Rather than depend on that entitlement, ATM IV is derived
ourselves from indicative bid/ask quotes via a Black-Scholes solver. No
dividend yield adjustment (q=0) - a deliberate simplification for a demo
project; SPY's ~1.3% yield would shift computed IV slightly but not enough
to matter for a regime x IV-rank bucket assignment.
"""
import asyncio
import math
from datetime import date, datetime, timedelta, timezone

from alpaca.data.requests import OptionBarsRequest, OptionChainRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import AssetStatus, ContractType
from alpaca.trading.requests import GetOptionContractsRequest

from agent.alpaca_clients import make_option_data_client, make_stock_data_client, make_trading_client
from agent.config import RISK_FREE_RATE
from agent.db import get_pool
from agent.logs import log_event

NEAR_ATM_STRIKES = 3        # nearest-the-money calls to average for one IV reading
MIN_DAYS_TO_EXPIRY = 5      # 1-2 DTE isn't just "0DTE-adjacent" - it's unstable too: caught a real
                            # case where a 1-day-out expiry solved to atm_iv=1.13 (113% annualized,
                            # implausible for SPY) purely from near-zero-T sensitivity to bid/ask
                            # noise, not an actual vol spike. 5 days gives enough extrinsic value
                            # cushion for the solve to be meaningful.
MAX_DAYS_TO_EXPIRY = 10     # stay in the "current" chain, not a far-dated contract
COLD_START_DAYS = 20        # trailing days needed before iv_rank is considered reliable
MAX_HISTORY_DAYS = 100      # bumped from 60 to match the 100-day backfill/chart window

_trading_client = make_trading_client()
_option_data_client = make_option_data_client()
_stock_data_client = make_stock_data_client()


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_call_price(spot: float, strike: float, t_years: float, rate: float, sigma: float) -> float:
    if sigma <= 0 or t_years <= 0:
        return max(0.0, spot - strike)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma**2) * t_years) / (sigma * math.sqrt(t_years))
    d2 = d1 - sigma * math.sqrt(t_years)
    return spot * _norm_cdf(d1) - strike * math.exp(-rate * t_years) * _norm_cdf(d2)


def _implied_vol_call(market_price: float, spot: float, strike: float, t_years: float, rate: float) -> float | None:
    """Bisection solve for sigma - call price is monotonic increasing in sigma,
    so bisection is robust even where a Newton step would blow up (near-zero
    vega for deep ITM/OTM contracts)."""
    lo, hi = 1e-4, 5.0
    price_lo, price_hi = _bs_call_price(spot, strike, t_years, rate, lo), _bs_call_price(spot, strike, t_years, rate, hi)
    if market_price <= price_lo or market_price >= price_hi:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        price_mid = _bs_call_price(spot, strike, t_years, rate, mid)
        if abs(price_mid - market_price) < 1e-6:
            return mid
        if price_mid < market_price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


async def _load_history(symbol: str) -> list[dict]:
    """Most recent MAX_HISTORY_DAYS rows, oldest first - the full table isn't
    trimmed (Postgres holds it all for free; the old file version trimmed at
    write time), only the rank-computation WINDOW is bounded, matching the
    old behavior of "trailing 60 days" rather than the entire history."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT date, atm_iv FROM iv_history WHERE symbol = $1 ORDER BY date DESC LIMIT $2", symbol, MAX_HISTORY_DAYS
    )
    history = [{"date": r["date"].isoformat(), "atm_iv": float(r["atm_iv"])} for r in rows]
    history.reverse()
    return history


async def _save_history_point(symbol: str, today: str, atm_iv: float) -> list[dict]:
    """Upsert today's reading (one point per day, not per call)."""
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO iv_history (symbol, date, atm_iv) VALUES ($1, $2, $3)
           ON CONFLICT (symbol, date) DO UPDATE SET atm_iv = EXCLUDED.atm_iv""",
        symbol,
        date.fromisoformat(today),
        atm_iv,
    )
    return await _load_history(symbol)


def _rank_from_history(atm_iv: float, history: list[dict]) -> float:
    iv_values = [h["atm_iv"] for h in history]
    iv_min, iv_max = min(iv_values), max(iv_values)
    if iv_max - iv_min < 1e-9:
        return 0.5  # no observed range yet - neutral, not 0 or 1
    return round((atm_iv - iv_min) / (iv_max - iv_min), 4)


async def get_stored_iv(symbol: str) -> dict:
    """Cheap, read-only: the last STORED daily reading + rank computed from
    the stored history. No Alpaca calls, no file writes, no log_event - safe
    to call on every frontend poll. Live recomputation (get_iv_rank below)
    should only ever be triggered by an actual decision cycle (run_cycle),
    never by polling - see audit item 1: polling this endpoint through
    get_iv_rank was firing a full options-chain fetch + Black-Scholes solve
    + file write + log line every ~5s, which is what was spamming the
    reasoning feed with iv_rank entries instead of real run_cycle events."""
    history = await _load_history(symbol)
    if not history:
        return {"symbol": symbol, "atm_iv": None, "iv_rank": None, "cold_start": True}
    latest = max(history, key=lambda h: h["date"])
    return {
        "symbol": symbol,
        "atm_iv": latest["atm_iv"],
        "iv_rank": _rank_from_history(latest["atm_iv"], history),
        "cold_start": len(history) < COLD_START_DAYS,
    }


async def get_iv_rank(symbol: str, trace_id: str | None = None) -> dict:
    underlying_trade = (
        await asyncio.to_thread(_stock_data_client.get_stock_latest_trade, StockLatestTradeRequest(symbol_or_symbols=symbol))
    )[symbol]
    spot = underlying_trade.price

    contracts_req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        type=ContractType.CALL,
        expiration_date_gte=(date.today() + timedelta(days=MIN_DAYS_TO_EXPIRY)).isoformat(),
        expiration_date_lte=(date.today() + timedelta(days=MAX_DAYS_TO_EXPIRY)).isoformat(),
        limit=1000,
    )
    contracts = (await asyncio.to_thread(_trading_client.get_option_contracts, contracts_req)).option_contracts
    if not contracts:
        result = {"symbol": symbol, "atm_iv": None, "iv_rank": None, "cold_start": True}
        await log_event(
            "iv_rank", {"symbol": symbol}, result, "no option contracts in the eligible expiry window", trace_id=trace_id
        )
        return result

    expiry = min(c.expiration_date for c in contracts)
    expiry_contracts = sorted(
        [c for c in contracts if c.expiration_date == expiry],
        key=lambda c: abs(float(c.strike_price) - spot),
    )[:NEAR_ATM_STRIKES]

    chain = await asyncio.to_thread(
        _option_data_client.get_option_chain, OptionChainRequest(underlying_symbol=symbol, expiration_date=expiry)
    )

    t_years = (
        datetime.combine(expiry, datetime.min.time(), tzinfo=timezone.utc) - datetime.now(timezone.utc)
    ).total_seconds() / (365.25 * 24 * 3600)

    ivs = []
    for c in expiry_contracts:
        snapshot = chain.get(c.symbol)
        if snapshot is None or snapshot.latest_quote is None:
            continue
        quote = snapshot.latest_quote
        if quote.bid_price <= 0 or quote.ask_price <= 0:
            continue
        mid_price = (quote.bid_price + quote.ask_price) / 2
        iv = _implied_vol_call(mid_price, spot, float(c.strike_price), t_years, RISK_FREE_RATE)
        if iv is not None:
            ivs.append(iv)

    if not ivs:
        result = {"symbol": symbol, "atm_iv": None, "iv_rank": None, "cold_start": True}
        await log_event(
            "iv_rank",
            {"symbol": symbol, "expiry": str(expiry), "candidates": len(expiry_contracts)},
            result,
            "could not solve implied vol for any near-ATM contract (bad quotes or no bid/ask)",
            trace_id=trace_id,
        )
        return result

    atm_iv = round(sum(ivs) / len(ivs), 4)
    today = date.today().isoformat()
    history = await _save_history_point(symbol, today, atm_iv)

    cold_start = len(history) < COLD_START_DAYS
    iv_rank = _rank_from_history(atm_iv, history)

    result = {"symbol": symbol, "atm_iv": atm_iv, "iv_rank": iv_rank, "cold_start": cold_start}
    await log_event(
        "iv_rank",
        {"symbol": symbol, "expiry": str(expiry), "contracts_used": len(ivs), "history_days": len(history)},
        result,
        f"iv_rank computed over trailing {len(history)} day(s) "
        f"({'cold start, need ' + str(COLD_START_DAYS) if cold_start else 'reliable'})",
        trace_id=trace_id,
    )
    return result


async def _contract_for_day(symbol: str, target_date: date, spot: float) -> "tuple | None":
    """Find a near-ATM call contract with an expiry MIN..MAX_DAYS_TO_EXPIRY out
    from target_date, as it would have looked from that day - checking both
    ACTIVE and INACTIVE (since-expired) contracts, since whether a given
    expiry has passed depends on today, not on target_date."""
    contracts = []
    for status in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
        req = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status=status,
            type=ContractType.CALL,
            expiration_date_gte=(target_date + timedelta(days=MIN_DAYS_TO_EXPIRY)).isoformat(),
            expiration_date_lte=(target_date + timedelta(days=MAX_DAYS_TO_EXPIRY)).isoformat(),
            limit=1000,
        )
        contracts.extend((await asyncio.to_thread(_trading_client.get_option_contracts, req)).option_contracts)

    if not contracts:
        return None
    expiry = min(c.expiration_date for c in contracts)
    same_expiry = [c for c in contracts if c.expiration_date == expiry]
    nearest = min(same_expiry, key=lambda c: abs(float(c.strike_price) - spot))
    return nearest.symbol, expiry, float(nearest.strike_price)


async def backfill_history(symbol: str, days: int = 45) -> dict:
    """One-shot backfill of `days` calendar days of ATM IV, so iv_rank clears
    cold_start immediately instead of waiting for real days to elapse one at
    a time (audit item 2). No historical bid/ask quotes are available via
    this API tier (OptionHistoricalDataClient has no get_option_quotes) -
    uses each day's option daily-bar CLOSE price as the market-price input
    to the same Black-Scholes solver the live path uses, which is the best
    available proxy, not a live bid/ask mid. Skips (does not fabricate) any
    day where no contract/bar/solve is available - weekends, holidays, and
    thin-liquidity gaps are expected and reported, not silently papered over.
    """
    underlying_bars = (
        await asyncio.to_thread(
            _stock_data_client.get_stock_bars,
            StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=datetime.now(timezone.utc) - timedelta(days=days + 5),
            ),
        )
    ).df.loc[symbol]

    results = {"seeded": 0, "skipped": [], "days_requested": days}
    trading_days = [ts.date() for ts in underlying_bars.index][-days:]

    for target_date in trading_days:
        if target_date == date.today():
            continue  # today's real reading comes from the live path, not backfill

        spot = float(underlying_bars.loc[underlying_bars.index.date == target_date, "close"].iloc[0])

        found = await _contract_for_day(symbol, target_date, spot)
        if found is None:
            results["skipped"].append({"date": target_date.isoformat(), "reason": "no contract in expiry window"})
            continue
        contract_symbol, expiry, strike = found

        bars = await asyncio.to_thread(
            _option_data_client.get_option_bars,
            OptionBarsRequest(
                symbol_or_symbols=contract_symbol,
                timeframe=TimeFrame.Day,
                start=datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc),
                end=datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1),
            ),
        )
        if contract_symbol not in bars.data or not bars.data[contract_symbol]:
            results["skipped"].append({"date": target_date.isoformat(), "reason": f"no daily bar for {contract_symbol}"})
            continue
        close_price = bars.data[contract_symbol][0].close

        t_years = (
            datetime.combine(expiry, datetime.min.time(), tzinfo=timezone.utc)
            - datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
        ).total_seconds() / (365.25 * 24 * 3600)

        iv = _implied_vol_call(close_price, spot, strike, t_years, RISK_FREE_RATE)
        if iv is None:
            results["skipped"].append({"date": target_date.isoformat(), "reason": "IV solve failed (bad price bounds)"})
            continue

        await _save_history_point(symbol, target_date.isoformat(), round(iv, 4))
        results["seeded"] += 1

    await log_event(
        "iv_backfill",
        {"symbol": symbol, "days_requested": days},
        results,
        f"backfilled {results['seeded']} day(s), skipped {len(results['skipped'])}",
    )
    return results
