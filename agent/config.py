"""Environment configuration for the Alpaca options regime agent."""
import os

from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
if "paper-api" not in ALPACA_BASE_URL:
    raise RuntimeError(
        f"ALPACA_BASE_URL={ALPACA_BASE_URL!r} does not contain 'paper-api' - "
        f"refusing to start against what looks like a live trading endpoint. "
        f"This is a hard safety check (audit item 8); if you genuinely intend "
        f"to trade live, that decision needs to be explicit, not a default."
    )

# doc/zerodte_and_watchlist.md item 2: expanded from SPY,QQQ to 10 liquid,
# options-heavy names - still fully overridable via the env var.
WATCHLIST_SYMBOLS = [
    s.strip()
    for s in os.environ.get(
        "WATCHLIST_SYMBOLS", "SPY,QQQ,AAPL,TSLA,NVDA,MSFT,AMZN,META,GOOGL,AMD"
    ).split(",")
    if s.strip()
]
UNDERLYING_SYMBOL = os.environ.get("UNDERLYING_SYMBOL", WATCHLIST_SYMBOLS[0])

# doc/zerodte_and_watchlist.md item 1: candidates worth even CHECKING for a
# same-day expiry - true 0DTE contracts only exist for a subset of symbols
# (mainly index-linked ones). This is a pre-filter to avoid a wasted options-
# chain lookup every cycle for names that structurally never have one (e.g.
# individual equities) - actual eligibility is still confirmed live against
# the real chain each cycle (see strategy.has_zero_dte_expiry), never assumed
# from this list alone.
ZERO_DTE_CANDIDATE_SYMBOLS = [
    s.strip() for s in os.environ.get("ZERO_DTE_SYMBOLS", "SPY,QQQ,IWM").split(",") if s.strip()
]
RISK_FREE_RATE = float(os.environ.get("RISK_FREE_RATE", "0.045"))
FEATHERLESS_API_KEY = os.environ.get("FEATHERLESS_API_KEY", "")

try:
    DATABASE_URL = os.environ["DATABASE_URL"]  # Neon Postgres connection string (audit item 11)
except KeyError:
    raise RuntimeError("DATABASE_URL is not set in .env - add your Neon connection string and retry.") from None

# Railway deployment prep: the deployed frontend lives on a Vercel domain,
# not localhost - CORS's old hardcoded `http://localhost:\d+` regex would
# silently block every request from it. Comma-separated so both a Vercel
# preview and production URL can be allowed at once if needed. Empty by
# default (local dev doesn't need it - the regex below already covers
# localhost unconditionally).
FRONTEND_ORIGINS = [s.strip() for s in os.environ.get("FRONTEND_ORIGIN", "").split(",") if s.strip()]
