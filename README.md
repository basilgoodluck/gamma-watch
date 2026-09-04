# Gamma Watch

An autonomous options trading agent for Alpaca (paper trading), with a full
decision pipeline, a live dashboard, and a complete audit trail of every
trade it does or does not make.

## What this is

Gamma Watch watches a symbol watchlist, classifies the current market regime
and implied volatility rank for each one, maps that combination to a specific
options strategy, sizes the trade against account risk limits, gets a cheap
AI second opinion, executes on Alpaca, and then monitors the resulting
position until it hits a profit target, a loss limit, or an expiry deadline.
Every step of that chain is logged with its reasoning, so any trade (or any
decision not to trade) can be traced back to exactly why it happened.

It runs on its own, on a schedule, not from a button click. A person can
watch it, pause it, and review its reasoning, but the agent decides.

## Business value

Systematic options trading is hard to do consistently by hand for three
reasons: it requires constant awareness of market regime and volatility
context, it requires strict and repeatable position sizing, and it requires
exiting positions on a plan rather than a feeling. Most retail and small-desk
losses trace back to one of those three breaking down under pressure, not to
a bad strategy idea.

Gamma Watch encodes that discipline directly into the system instead of
relying on a person to apply it every time:

- Every trade is sized as a fixed percentage of account equity, never off
  leveraged buying power, and a hard drawdown circuit breaker stands the
  whole system down if the account falls too far from its high water mark.
- Every candidate strategy is chosen from an explicit regime times IV rank
  matrix, so the strategy shape always matches the stated market condition
  instead of drifting toward whatever looks appealing in the moment.
- Every position is closed on a predefined trigger for its specific strategy
  type, not a gut call, and a stale or never filled order is cleaned up
  automatically instead of sitting as forgotten risk.
- Every decision, approval, veto, and close carries a plain English reason in
  a queryable log, which is what a real trading desk needs for review and
  compliance and what most hobby bots never build.

The result is a demonstration of how much of a trading desk's actual
decision process, not just order placement, can be automated end to end
while staying fully explainable.

## How it works

One pass through the pipeline, per symbol, per cycle:

1. **Regime detection.** Recent price bars are scored on two independent
   axes, trend strength and volatility level, each ranked as a percentile
   against that symbol's own recent history. A separate, more strictly
   gated check looks for a breakout: a sharp volatility spike happening at
   the same time as a trend reversal.
2. **IV rank.** Near the money option quotes are solved for implied
   volatility with a Black-Scholes solver, then ranked against that symbol's
   own trailing IV history to get a 0 to 1 rank, not just a raw number.
3. **Strategy selection.** The (regime, IV bucket) pair maps to exactly one
   strategy shape: a directional debit spread, a directional single leg
   long, an iron condor, a long straddle or strangle, or a decision to stand
   down entirely.
4. **Risk sizing.** The candidate is sized against a per-trade percentage of
   account equity, checked against a portfolio drawdown ceiling and a
   concurrent position cap, and rejected outright if it fails any of them.
5. **AI review.** A cheap, budget-capped LLM call reviews the sized
   candidate for internal inconsistency (for example, an iron condor picked
   while IV rank is actually low) and can veto or adjust confidence. If the
   review is skipped or fails, the system falls back to its own risk and
   regime checks rather than blocking on a third party API.
6. **Execution.** The approved candidate is submitted to Alpaca as a real
   (paper) multi leg or single leg order, and recorded locally the moment
   Alpaca accepts it.
7. **Monitoring.** A separate, fast loop marks every open position to market
   from its own legs' live bid and ask, checks it against a profit target,
   loss limit, or expiry deadline specific to its strategy type, and closes
   it when a trigger fires. A same day (0DTE) variant runs on a tighter
   schedule with its own smaller risk budget and a hard time cutoff before
   market close.
8. **Reconciliation.** On a standing schedule, and before every trade
   decision, local position records are checked against Alpaca's real order
   status and corrected if they disagree, so a stuck or unfilled order can
   never be miscounted as live risk.

## Tech stack

**Backend**
- Python, FastAPI, asyncio for the decision loop and background tasks
- Alpaca Markets API (paper trading) via the `alpaca-py` SDK, for price
  data, option chains, and order execution
- Postgres (Neon) via `asyncpg`, as the single source of record for
  decisions, positions, IV history, and spend tracking
- Featherless AI (OpenAI compatible), running Qwen3.8-Flash-Next, for the
  AI review step

**Frontend**
- Next.js and TypeScript
- Tailwind CSS
- Native WebSockets for live price streaming to the chart

**Interfaces**
- An MCP server exposing the same pipeline as tools for an MCP client
- A FastAPI REST and WebSocket bridge for the browser dashboard

**Deployment**
- Backend: Docker container on Railway
- Frontend: Vercel

## Notable engineering decisions

A few of the harder problems this project actually had to solve, beyond the
basic pipeline:

- **Source of truth discipline.** Alpaca is the permanent source of truth
  for account and order state. Local Postgres is treated strictly as a
  cache of it, reconciled against the real broker state before every trade
  decision and on its own standing loop, rather than trusted on its own.
- **Race safe concurrency.** Regime and IV data for every watchlist symbol
  are fetched concurrently, since that work is independent and read only.
  The part that actually opens and sizes trades runs sequentially per
  symbol on purpose, because two symbols reading shared account state at
  the same time could both see room under the position cap and together
  exceed it.
- **Self calibrating regime detection.** Trend and volatility signals are
  ranked as percentiles against each symbol's own rolling history instead
  of a fixed global threshold, smoothed to avoid flapping between labels
  every couple of hours, with breakout detection deliberately excluded from
  that smoothing so a real spike is not diluted away.
- **Fail open AI review with a real budget ceiling.** The AI review step
  batches every candidate into a single call, skips the call entirely when
  a deterministic pre-filter already says no, tracks cumulative spend
  against a hard ceiling, and degrades to the system's own risk and regime
  checks rather than blocking the pipeline if the provider is slow, over
  budget, or unavailable.
- **Stale order cleanup.** A resting entry order that never fills at Alpaca
  used to sit as "open" risk forever, since it never crosses any profit or
  loss trigger. A grace period plus a live check against Alpaca's real
  positions now cancels it automatically instead of leaving it as forgotten
  exposure.
- **Live price streaming.** Alpaca's Python streaming client runs its own
  event loop internally, so it runs on a dedicated background thread and
  bridges results back into FastAPI's event loop, letting the dashboard
  receive live bars over WebSocket instead of polling.

See `feedback.md` for an honest assessment of where this design is
strong and where it would need more work before running with real capital.

## Project structure

```
agent/            Core pipeline: regime, IV rank, strategy, risk, AI review,
                   execution, monitoring, and the shared orchestrator
web_api/           FastAPI bridge: REST and WebSocket endpoints for the
                   frontend, plus the background decision and monitor loops
mcp_server/        MCP server exposing the same pipeline as tools
frontend/          Next.js dashboard
db/                Postgres schema
scripts/           One off operational scripts (connection check, backfill,
                   backtest)
Dockerfile         Backend container image for Railway
```

## Running locally

Backend:

```
pip install -r requirements.txt
cp .env.example .env      # fill in ALPACA_API_KEY, ALPACA_SECRET_KEY, DATABASE_URL
psql "$DATABASE_URL" -f db/schema.sql   # once, against a fresh database
python -m uvicorn web_api.server:app --reload --host 127.0.0.1 --port 8000
```

Frontend:

```
cd frontend
npm install
npm run dev
```

Set `NEXT_PUBLIC_API_BASE` in the frontend environment to point at the
backend (defaults to `http://localhost:8000` for local development). The
frontend derives its WebSocket URL from the same variable, so only one value
needs to change between environments.

MCP server, for an MCP client such as Claude Code, run separately from the
two processes above:

```
python mcp_server/server.py
```

## Deployment

The backend ships as a Docker image (see `Dockerfile`) built for Railway:

- Binds to `0.0.0.0` on the `$PORT` Railway provides at runtime.
- Only `agent/` and `web_api/` are copied into the image. The MCP server is
  a separate, local only interface and is not part of this deployment.
- Required environment variables are documented in `.env.example`, including
  `FRONTEND_ORIGIN` for CORS once a Vercel URL exists.

The frontend deploys to Vercel independently. Its only required environment
variable is `NEXT_PUBLIC_API_BASE`, pointed at the deployed backend URL.

## Safety

This project trades on Alpaca's paper trading endpoint only. `agent/config.py`
checks `ALPACA_BASE_URL` at startup and refuses to run if it does not look
like a paper trading endpoint, as a structural guard against ever pointing
this at a live account by accident. Treat any change to that check, or to
the execution path in general, as something to review carefully rather than
assume safe.
