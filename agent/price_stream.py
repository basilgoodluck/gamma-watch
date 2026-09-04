"""Live price bar streaming (doc/zerodte_and_watchlist.md item 3).

Replaces frontend polling of /api/price_history with a real push path:
Alpaca's streaming market-data WebSocket -> this hub -> our own FastAPI
WebSocket endpoint -> the browser. No interval polls anywhere in this path.

Threading model: alpaca-py's StockDataStream.run() calls asyncio.run(...)
internally - it OWNS an event loop, it can't be awaited inside one that's
already running (FastAPI's). So it runs in its own background thread with
its own loop, exactly as alpaca-py's own docs/examples do. The bar handler
therefore executes on the STREAM thread's loop, not FastAPI's - handing a bar
off to a websocket connection living on FastAPI's loop needs
asyncio.run_coroutine_threadsafe in that direction. subscribe_bars() /
unsubscribe_bars() do the same crossing in reverse internally (confirmed by
reading alpaca-py's own source: DataStream._subscribe calls
run_coroutine_threadsafe(..., self._loop).result() when the stream is already
running) - which is a BLOCKING call, so this module always calls them via
asyncio.to_thread() from the FastAPI side rather than directly, so a
subscribe/unsubscribe from a websocket handler never blocks the main loop.
"""
import asyncio
import threading
from datetime import datetime, timezone

from agent.alpaca_clients import make_stock_data_stream

# doc's own reconnection story: alpaca-py's stream ALREADY auto-reconnects
# internally (confirmed in its _run_forever - a retry loop with backoff) and
# resubscribes using the same handlers dict every time, so a dropped Alpaca
# connection self-heals without any code here noticing. What this module adds
# is the browser<->our-backend leg (handled in web_api/server.py's websocket
# endpoint) and keeping our own subscriber bookkeeping correct.


def _bar_to_dict(bar) -> dict:
    ts = bar.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return {
        "ts": ts.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
    }


class PriceStreamHub:
    """One shared Alpaca stream connection, fanned out to any number of
    per-symbol asyncio.Queue subscribers living on the MAIN (FastAPI) loop."""

    def __init__(self) -> None:
        self._stream = make_stock_data_stream()
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._stream_subscribed: set[str] = set()

    def start(self) -> None:
        """Call once, from FastAPI's lifespan startup, on the main loop."""
        self._main_loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._stream.run, daemon=True, name="alpaca-price-stream")
        self._thread.start()

    async def _on_bar(self, bar) -> None:
        """Runs on the STREAM thread's own loop (alpaca-py awaits this
        handler directly) - hop back onto the main loop to touch the
        subscriber queues, which belong to it."""
        payload = _bar_to_dict(bar)
        if self._main_loop is not None:
            asyncio.run_coroutine_threadsafe(self._deliver(bar.symbol, payload), self._main_loop)

    async def _deliver(self, symbol: str, payload: dict) -> None:
        for q in list(self._subscribers.get(symbol, ())):
            q.put_nowait(payload)

    async def subscribe(self, symbol: str) -> asyncio.Queue:
        """Called from a websocket handler on the MAIN loop."""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(symbol, set()).add(q)
        if symbol not in self._stream_subscribed:
            self._stream_subscribed.add(symbol)
            # subscribe_bars() is a plain sync method that can block on a
            # cross-thread .result() wait when the stream is already running -
            # to_thread() so that wait never blocks FastAPI's own loop.
            await asyncio.to_thread(self._stream.subscribe_bars, self._on_bar, symbol)
        return q

    async def unsubscribe(self, symbol: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(symbol)
        if subs is None:
            return
        subs.discard(q)
        if not subs:
            del self._subscribers[symbol]
            self._stream_subscribed.discard(symbol)
            await asyncio.to_thread(self._stream.unsubscribe_bars, symbol)


hub = PriceStreamHub()
