"""Shared Alpaca SDK client construction (doc/loop_and_ui_fixes.md priority 1).

Root cause of the decision loop repeatedly freezing for 20-30+ minutes at a
time: alpaca-py builds every request on a plain requests.Session and never
sets a `timeout` (alpaca/common/rest.py's _one_request does
`self._session.request(method, url, **opts)` with no `timeout` in opts), so
a genuinely stalled TCP connection to Alpaca can hang the calling OS thread
forever. Every one of these calls runs inside asyncio.to_thread(), and a
blocking syscall already executing in a worker thread has no cancellation
hook - so asyncio.wait_for() around the *caller* (the fix applied in
doc/fix_dead_loop.md) can never actually interrupt it; the timeout just
never fires; the wrapping await sits there forever waiting for the thread to
return, which it won't. That fix was still worth having (it does correctly
handle real, raised exceptions - e.g. the earlier Postgres/PgBouncer
protocol errors - and asyncpg's queries hang correctly on real asyncio
sockets), but it could never fix THIS failure mode, since it's a raw
blocking call with no asyncio involvement at all.

The actual fix has to happen at the source: every Alpaca client used
anywhere in this codebase must be constructed through this module, so every
request always has a bounded timeout and can never hang past it.
"""
import functools

from requests import Session

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.live.stock import StockDataStream
from alpaca.trading.client import TradingClient

from agent.config import ALPACA_API_KEY, ALPACA_SECRET_KEY

REQUEST_TIMEOUT_SECONDS = 20  # generous for a single paper-trading REST call, tiny next to the 600s cycle interval


def _patch_timeout(session: Session, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
    original_request = session.request

    @functools.wraps(original_request)
    def _request_with_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return original_request(*args, **kwargs)

    session.request = _request_with_timeout


def make_trading_client() -> TradingClient:
    client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
    _patch_timeout(client._session)
    return client


def make_stock_data_client() -> StockHistoricalDataClient:
    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    _patch_timeout(client._session)
    return client


def make_option_data_client() -> OptionHistoricalDataClient:
    client = OptionHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    _patch_timeout(client._session)
    return client


def make_stock_data_stream() -> StockDataStream:
    """doc/zerodte_and_watchlist.md item 3: the live bar/trade WebSocket
    client, not a REST client - built on `websockets`, not `requests.Session`,
    so the timeout patch above doesn't apply here (and isn't needed: a
    dropped stream connection surfaces as a real exception/reconnect inside
    the library's own _run_forever loop, not a silent hang)."""
    return StockDataStream(ALPACA_API_KEY, ALPACA_SECRET_KEY)
