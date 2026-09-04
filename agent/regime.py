"""5-state market regime classifier: {TREND, RANGE} x {CALM, VOLATILE} + BREAKOUT.

Pattern reused (shape only, not code) from the old crypto agent's regime.py:
rolling window -> slope/consistency features -> weighted scores -> normalized
probabilities -> labeled regime with a confidence threshold. Perp-only
signals (funding, OI, liquidations, CVD) have no options equivalent and are
dropped in favor of price-only features (return slope, autocorrelation,
realized vol, high-low range).

Unlike the old 3-state model, trend/range and calm/volatile are scored as
two independent axes and combined into 4 states, with BREAKOUT as a 5th,
separately-detected state (a sharp volatility spike coinciding with a
low-to-high trend transition within the window) rather than a byproduct of
the other four.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from agent.alpaca_clients import make_stock_data_client
from agent.logs import log_event

BAR_MINUTES = 15
WINDOW = 32               # bars per detection window (~8 trading hours at 15min)
STEP = 8                  # slide step for building the historical distribution
HISTORY_BARS = 1000       # bars to fetch for percentile history (~a few weeks)
MIN_HISTORY_WINDOWS = 5   # minimum sliding windows (incl. current) to trust percentiles
EMA_ALPHA = 0.1           # smoothing factor for regime scores across successive calls
BREAKOUT_TRANSITION_THRESHOLD = 0.4  # min low->high trend-percentile jump to even consider BREAKOUT
BREAKOUT_VOL_THRESHOLD = 0.8         # min volatility percentile ("sharp spike") to even consider BREAKOUT

STATES = ["TREND_CALM", "TREND_VOLATILE", "RANGE_CALM", "RANGE_VOLATILE", "BREAKOUT"]

_data_client = make_stock_data_client()

# The trend/vol percentiles are noisy on any single ~8h window (small-sample
# slope estimates), which without smoothing flips the TREND/RANGE argmax on
# nearly every call - those regimes are supposed to persist for hours/days,
# not toggle every 2 hours. EMA-smooth the two continuous axis signals across
# successive calls per symbol (the same way a continuously-polling process
# would naturally carry state forward). BREAKOUT is deliberately NOT run
# through this smoothing: it's a brief, sudden spike by definition, already
# protected from noise by its own hard gate below - smoothing it the same way
# as the other axes was found in backtesting to nearly erase it entirely
# (any real spike gets diluted by 80% of the pre-spike, near-zero EMA state).
_smoothed_signals: dict[str, dict[str, float]] = {}


def reset_state(symbol: str | None = None) -> None:
    """Clear EMA state - used by the backtest script to start each run clean,
    and by tests. Not needed in normal live operation."""
    if symbol is None:
        _smoothed_signals.clear()
    else:
        _smoothed_signals.pop(symbol, None)


async def _fetch_bars(symbol: str, max_bars: int, lookback_days: int = 30) -> pd.DataFrame:
    # `limit` truncates from the OLDEST end of the range, not the newest - so
    # combining it with `start` would silently cut off the most recent bars
    # (verified: start=30d-ago + limit=1000 returned Aug3-Aug24, not the last
    # week). Fetch the full start->now range instead and tail() in Python to
    # guarantee the most recent bars are what we actually score.
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(BAR_MINUTES, TimeFrameUnit.Minute),
        start=datetime.now(timezone.utc) - timedelta(days=lookback_days),
    )
    # alpaca-py is synchronous under the hood (requests.Session, no async
    # client) - asyncio.to_thread() runs the blocking call in a worker thread
    # so it doesn't block the event loop, which is what actually makes
    # concurrent multi-symbol fetches (run_cycle_watchlist) real rather than
    # async syntax around calls that still serialize on the loop.
    bars = await asyncio.to_thread(_data_client.get_stock_bars, req)
    if symbol not in bars.data or not bars.data[symbol]:
        return pd.DataFrame(columns=["open", "close", "high", "low"])
    df = bars.df.loc[symbol].sort_index()
    # "open" is unused by the regime math below (close/high/low only) but is
    # kept here so callers needing real OHLC (step10's candlestick chart)
    # don't need a second, separate bars fetch.
    return df[["open", "close", "high", "low"]].tail(max_bars)


def _slope(series: np.ndarray) -> float:
    x = np.arange(len(series))
    return float(np.polyfit(x, series, 1)[0])


def _autocorr_lag1(returns: np.ndarray) -> float:
    if len(returns) < 3:
        return 0.0
    a, b = returns[:-1], returns[1:]
    if np.std(a) < 1e-12 or np.std(b) < 1e-12:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _window_metrics(sub: pd.DataFrame) -> dict:
    """Price-only features for one window: a raw trend-strength score and a
    raw volatility level. Both are ranked as percentiles against a rolling
    history in _classify rather than squashed against a fixed constant -
    "how trend-like / volatile is this window compared to this symbol's own
    recent behavior" self-calibrates per symbol and timeframe, where a fixed
    threshold (e.g. tanh(x/K)) does not and was found to saturate near 1.0
    for nearly all windows during backtesting."""
    log_close = np.log(sub["close"].to_numpy())
    log_returns = np.diff(log_close)
    n = len(log_close)

    slope = _slope(log_close)
    realized_vol = float(np.std(log_returns)) if len(log_returns) > 1 else 0.0
    autocorr = _autocorr_lag1(log_returns)
    hl_ratio = float(np.mean((sub["high"] - sub["low"]) / sub["close"]))

    slope_tstat = slope * n / (realized_vol + 1e-9)
    trend_raw = abs(slope_tstat) * 0.6 + abs(autocorr) * 0.4
    volatility_level = 0.6 * realized_vol + 0.4 * hl_ratio

    return {
        "trend_raw": trend_raw,
        "volatility_level": volatility_level,
        "signed_slope": slope,
    }


def _percentile_rank(value: float, population: list[float]) -> float:
    if not population:
        return 0.5
    return float(np.mean([v <= value for v in population]))


async def _classify(
    symbol: str,
    df: pd.DataFrame,
    log: bool = True,
    trace_id: str | None = None,
    state: dict | None = None,
) -> dict:
    """Classify one point in time from a bars dataframe already truncated to
    'now' (or to a historical instant, for backtesting - callers must ensure
    df contains no bars beyond the point being classified).

    `state`: EMA smoothing state store to read/write. Defaults to the shared
    module-level _smoothed_signals (live detect_regime usage). Pass a
    caller-owned dict to isolate a sequence of calls from live state - e.g.
    classify_history() below runs a fresh local dict per call so charting a
    symbol's history never perturbs that same symbol's live EMA continuity,
    and vice versa, even though both run in the same long-lived server
    process."""
    n_windows = (len(df) - WINDOW) // STEP + 1 if len(df) >= WINDOW else 0

    if n_windows < MIN_HISTORY_WINDOWS:
        result = {
            "symbol": symbol,
            "regime": "RANGE_CALM",
            "probabilities": {s: (1.0 if s == "RANGE_CALM" else 0.0) for s in STATES},
            "confidence": 0.0,
            "trend_direction": "unknown",
            "ready": False,
        }
        if log:
            await log_event(
                "regime_classify",
                {"symbol": symbol, "bars_fetched": len(df)},
                result,
                f"not enough bar history for a reliable read (need >= {MIN_HISTORY_WINDOWS} "
                f"windows of {WINDOW} bars, got {n_windows})",
                trace_id=trace_id,
            )
        return result

    starts = list(range(0, len(df) - WINDOW + 1, STEP))
    windows = [_window_metrics(df.iloc[s : s + WINDOW]) for s in starts]
    current = windows[-1]

    vol_history = [w["volatility_level"] for w in windows]
    trend_history = [w["trend_raw"] for w in windows]
    vol_signal_raw = _percentile_rank(current["volatility_level"], vol_history)
    trend_signal_raw = _percentile_rank(current["trend_raw"], trend_history)

    # Compare the current window's trend percentile to the immediately
    # preceding, non-overlapping window's (not a bisection of the current
    # window, which at 16 bars / 4h was too noisy and fired on every step).
    # Uses the raw (pre-smoothing) signal - a transition needs to be visible
    # in the instant it happens, not lagged behind the EMA below.
    prior = _window_metrics(df.iloc[-2 * WINDOW : -WINDOW])
    prior_trend_signal = _percentile_rank(prior["trend_raw"], trend_history)
    trend_transition = max(0.0, trend_signal_raw - prior_trend_signal)

    # BREAKOUT is gated, not just a scaled product of transition*vol: with
    # both signals being roughly-uniform percentiles, a plain product has
    # too much probability mass at moderate values to stay a rare, distinct
    # event (verified in backtesting: an ungated product made BREAKOUT ~30-40%
    # of all labels). Require both to clear a high bar first; scale only the
    # excess above threshold so it still fades in smoothly at the margin.
    if trend_transition > BREAKOUT_TRANSITION_THRESHOLD and vol_signal_raw > BREAKOUT_VOL_THRESHOLD:
        excess_transition = (trend_transition - BREAKOUT_TRANSITION_THRESHOLD) / (1 - BREAKOUT_TRANSITION_THRESHOLD)
        excess_vol = (vol_signal_raw - BREAKOUT_VOL_THRESHOLD) / (1 - BREAKOUT_VOL_THRESHOLD)
        breakout_raw = 3.0 * excess_transition * excess_vol
    else:
        breakout_raw = 0.0

    state_store = state if state is not None else _smoothed_signals
    prev = state_store.get(symbol)
    if prev is None:
        trend_signal, vol_signal = trend_signal_raw, vol_signal_raw
    else:
        trend_signal = EMA_ALPHA * trend_signal_raw + (1 - EMA_ALPHA) * prev["trend"]
        vol_signal = EMA_ALPHA * vol_signal_raw + (1 - EMA_ALPHA) * prev["vol"]
    state_store[symbol] = {"trend": trend_signal, "vol": vol_signal}

    raw_scores = {
        "TREND_CALM": trend_signal * (1 - vol_signal),
        "TREND_VOLATILE": trend_signal * vol_signal,
        "RANGE_CALM": (1 - trend_signal) * (1 - vol_signal),
        "RANGE_VOLATILE": (1 - trend_signal) * vol_signal,
        "BREAKOUT": breakout_raw,
    }
    total = sum(raw_scores.values()) + 1e-9
    probabilities = {s: round(v / total, 4) for s, v in raw_scores.items()}

    regime = max(probabilities, key=probabilities.get)
    confidence = probabilities[regime]
    trend_direction = "bullish" if current["signed_slope"] > 0 else "bearish"

    result = {
        "symbol": symbol,
        "regime": regime,
        "probabilities": probabilities,
        "confidence": confidence,
        "trend_direction": trend_direction,
        "ready": True,
    }
    if log:
        await log_event(
            "regime_classify",
            {"symbol": symbol, "bars_fetched": len(df), "windows_in_history": n_windows},
            result,
            f"trend_strength={trend_signal:.4f} vol_percentile={vol_signal:.4f} "
            f"trend_transition={trend_transition:.4f}",
            trace_id=trace_id,
        )
    return result


async def detect_regime(symbol: str, trace_id: str | None = None) -> dict:
    df = await _fetch_bars(symbol, HISTORY_BARS)
    return await _classify(symbol, df, log=True, trace_id=trace_id)


async def classify_history(symbol: str, lookback_days: int = 30, eval_step: int = STEP) -> list[dict]:
    """Regime sequence over recent history, for charting (step10's regime
    shading). No lookahead - each point only sees bars up to itself. Uses an
    isolated, per-call EMA state so this never perturbs (or is perturbed by)
    the live detect_regime() state for the same symbol."""
    df = await _fetch_bars(symbol, max_bars=100_000, lookback_days=lookback_days)
    min_needed = WINDOW + (MIN_HISTORY_WINDOWS - 1) * STEP
    if len(df) < min_needed:
        return []

    local_state: dict = {}
    sequence = []
    for i, end in enumerate(range(min_needed, len(df) + 1, eval_step)):
        window_df = df.iloc[max(0, end - HISTORY_BARS) : end]
        # Sequential, not gathered: each step's EMA smoothing depends on the
        # previous step's local_state, an inherent data dependency (unlike
        # run_cycle_watchlist's per-symbol fetches, which are independent).
        result = await _classify(symbol, window_df, log=False, state=local_state)
        result["ts"] = df.index[end - 1].isoformat()
        sequence.append(result)
        # doc/loop_and_ui_fixes.md: each _classify call here recomputes a full
        # ~HISTORY_BARS-bar sliding-window feature set from scratch (an
        # O(N^2/STEP^2) blowup across this whole loop), and since it's pure
        # synchronous numpy with no await inside, a long uninterrupted run of
        # this loop can pin the single-threaded event loop for minutes -
        # starving every other coroutine, including the decision loop's own
        # scheduling, of any chance to run at all. This doesn't fix the
        # underlying redundant computation (a real but separate optimization),
        # but yielding periodically at least lets the event loop interleave
        # other pending work between chunks instead of blocking solid.
        if i % 10 == 0:
            await asyncio.sleep(0)
    return sequence
