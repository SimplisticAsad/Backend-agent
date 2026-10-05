"""PostgreSQL access: one pooled, bounded resource with explicit transactions.

`transaction()` is the only way services touch the database:
    with db.transaction() as conn:   # BEGIN
        ...                          # statements
                                     # COMMIT on normal exit, ROLLBACK on any exception
Connections come from a pool (psycopg_pool); exhaustion, timeouts and outages are translated to safe 503 errors.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg
import psycopg_pool
from psycopg.rows import dict_row

from .config import Settings
from .errors import AppError, ServiceUnavailable, translate_db_error
from .logging_setup import db_stats_var

log = logging.getLogger("app.db")

ISOLATION = {  # complete literal statements: nothing is ever interpolated
    "read committed": "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
    "repeatable read": "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
    "serializable": "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
}


class Database:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._pool: psycopg_pool.ConnectionPool | None = None

    # ---- lifecycle ----------------------------------------------------------------------------------------------
    def open(self) -> None:
        if self._pool is not None:
            return
        if not self._s.database_url:
            raise ServiceUnavailable("DATABASE_URL is not configured.", code="DATABASE_NOT_CONFIGURED")
        options = f"-c statement_timeout={self._s.db_statement_timeout_ms} -c lock_timeout=5000 -c idle_in_transaction_session_timeout=60000"
        if self._s.database_schema:
            options = f"-c search_path={self._s.database_schema} " + options
        self._pool = psycopg_pool.ConnectionPool(
            self._s.database_url, min_size=self._s.db_pool_min, max_size=self._s.db_pool_max, timeout=self._s.db_pool_timeout_seconds,
            kwargs={"row_factory": dict_row, "options": options, "connect_timeout": 5}, open=False, name="backend-pool",
        )
        self._pool.open(wait=False)  # the app can start (and report /ready=503) while the database is still coming up

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close(timeout=5)
            self._pool = None

    # ---- usage ---------------------------------------------------------------------------------------------------
    @contextmanager
    def transaction(self, isolation: str | None = None, *, deleting: bool = False) -> Iterator[psycopg.Connection]:
        if self._pool is None:
            raise ServiceUnavailable("The database is not available.", code="DATABASE_UNAVAILABLE")
        stats = db_stats_var.get()
        t0 = time.perf_counter()
        try:
            with self._pool.connection() as conn:
                if isolation:  # applies to this transaction only; nothing leaks back into the pool
                    conn.execute(ISOLATION[isolation])
                yield conn
        except AppError:
            raise  # already safe; the pool rolled the transaction back
        except psycopg.Error as e:  # includes PoolTimeout / PoolClosed (OperationalError subclasses)
            raise translate_db_error(e, deleting=deleting) from None
        finally:
            if stats is not None:
                stats["ops"] = stats.get("ops", 0) + 1
                stats["ms"] = round(stats.get("ms", 0.0) + (time.perf_counter() - t0) * 1000, 2)

    def ping(self) -> bool:
        try:
            with self.transaction() as conn:
                conn.execute("SELECT 1")
            return True
        except AppError:
            return False
