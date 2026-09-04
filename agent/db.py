"""Shared asyncpg connection pool for the Neon Postgres data layer (audit
item 11). Lazy-singleton pattern reused in shape from the old crypto
codebase's database/connection.py, adapted to a single DSN (Neon gives one
connection string) instead of discrete host/port/user/password fields."""
import asyncio

import asyncpg

from agent.config import DATABASE_URL

_pool: asyncpg.Pool | None = None
# doc/zerodte_and_watchlist.md: the check-then-create below wasn't atomic -
# with enough concurrent FIRST callers at startup (this project now runs 4
# background loops plus whatever HTTP requests land in that same window),
# each one sees `_pool is None` and starts its OWN create_pool() call,
# several connecting to Neon's pooler endpoint at once. That's what actually
# produced the real ConnectionDoesNotExistError seen on a fresh boot after
# adding a fifth concurrent caller (the new 0DTE monitor loop) - not a
# regression in any one loop, a pre-existing race this just made more likely
# to hit. A lock serializes pool creation; every caller after the first one
# just gets the already-created pool back immediately.
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            _pool = await asyncpg.create_pool(
                dsn=DATABASE_URL,
                min_size=2,
                max_size=10,
                # Neon's DSN here is the POOLER endpoint (PgBouncer, transaction-pooling
                # mode) - asyncpg's default server-side prepared-statement cache is a
                # known incompatibility with that mode (the next query on a client
                # connection can get routed to a different backend that never prepared
                # the statement), which produced real protocol errors and dead
                # connections in production (ProtocolViolationError: Authentication
                # timed out / ConnectionDoesNotExistError). Disabling it is Neon's own
                # documented fix for asyncpg + the pooled connection string.
                statement_cache_size=0,
                # asyncpg's own default is command_timeout=None (unbounded) - a
                # connection that's live at the TCP level but never gets a response
                # to a specific query would otherwise hang the caller forever, the
                # same class of bug that turned out to be the actual root cause of
                # the decision loop's repeated freezes (doc/loop_and_ui_fixes.md).
                command_timeout=30,
            )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
