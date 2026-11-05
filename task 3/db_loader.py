"""
db_loader.py - Database loader with schema creation, batch upserts and
transaction safety. Works with SQLite and PostgreSQL via SQLAlchemy Core.

Usage:
    loader = DatabaseLoader()                      # uses DATABASE_URL or sqlite:///frontier.db
    loader.create_schema()
    with loader.transaction() as conn:             # all-or-nothing across tables
        loader.upsert("companies", rows, conn=conn)
        loader.upsert("products", rows, conn=conn)
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy import (
    Column, Connection, Date, DateTime, Engine, ForeignKey, Index, Integer,
    MetaData, String, Table, Text, create_engine, event, func,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

log = logging.getLogger("db_loader")

DEFAULT_URL = "sqlite:///frontier.db"

# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
metadata = MetaData()

companies = Table(
    "companies", metadata,
    Column("slug", String(100), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("website", String(500)),
    Column("country", String(100)),
    Column("founded_year", Integer),
    Column("created_at", DateTime, server_default=func.now(), nullable=False),
    Column("updated_at", DateTime, server_default=func.now(), nullable=False),
)

products = Table(
    "products", metadata,
    Column("product_id", String(100), primary_key=True),
    Column("company_slug", String(100),
           ForeignKey("companies.slug", onupdate="CASCADE", ondelete="CASCADE"),
           nullable=False),
    Column("name", String(200), nullable=False),
    Column("category", String(100)),
    Column("description", Text),
    Column("created_at", DateTime, server_default=func.now(), nullable=False),
    Column("updated_at", DateTime, server_default=func.now(), nullable=False),
    Index("ix_products_company", "company_slug"),
)

papers = Table(
    "papers", metadata,
    Column("arxiv_id", String(50), primary_key=True),
    Column("title", Text, nullable=False),
    Column("published_date", Date),
    Column("url", String(500)),
    Column("company_slug", String(100),
           ForeignKey("companies.slug", ondelete="SET NULL")),
    Column("created_at", DateTime, server_default=func.now(), nullable=False),
    Column("updated_at", DateTime, server_default=func.now(), nullable=False),
    Index("ix_papers_company", "company_slug"),
)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
def make_engine(url: str | None = None, echo: bool = False) -> Engine:
    """Create an engine. Falls back to $DATABASE_URL, then local SQLite."""
    url = url or os.getenv("DATABASE_URL", DEFAULT_URL)
    # Some hosts (Heroku/Neon snippets) hand out the legacy 'postgres://' scheme.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    engine = create_engine(url, echo=echo, pool_pre_ping=True)

    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")   # SQLite ignores FKs by default
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    return engine


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------
class DatabaseLoader:
    def __init__(self, url: str | None = None, echo: bool = False):
        self.engine = make_engine(url, echo)
        self.dialect = self.engine.dialect.name
        if self.dialect not in ("sqlite", "postgresql"):
            raise ValueError(f"Unsupported dialect: {self.dialect}")
        self.metadata = metadata

    # ---- schema ----------------------------------------------------------
    def create_schema(self) -> None:
        """Idempotent: only creates tables that don't exist yet."""
        self.metadata.create_all(self.engine)
        log.info("Schema ready (%s)", ", ".join(self.metadata.tables))

    def drop_schema(self) -> None:
        self.metadata.drop_all(self.engine)

    # ---- transactions ----------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Commit on success, roll back everything on any exception."""
        with self.engine.begin() as conn:
            yield conn

    # ---- upsert ----------------------------------------------------------
    def upsert(
        self,
        table_name: str,
        rows: Sequence[Mapping[str, Any]],
        conflict_cols: Sequence[str] | None = None,
        update_cols: Sequence[str] | None = None,
        batch_size: int = 500,
        conn: Connection | None = None,
    ) -> int:
        """
        Insert rows; on conflict of `conflict_cols` update `update_cols`.

        conflict_cols : defaults to the table's primary key
        update_cols   : defaults to every supplied column except conflict cols
        conn          : pass an open transaction() connection to make this call
                        part of a larger atomic unit; otherwise it runs in its
                        own transaction.
        Returns the number of distinct rows written.
        """
        if not rows:
            return 0
        table = self._table(table_name)
        conflict_cols = list(conflict_cols or [c.name for c in table.primary_key])
        rows = self._clean(table, rows, conflict_cols)

        supplied = list(rows[0].keys())
        if update_cols is None:
            update_cols = [c for c in supplied if c not in conflict_cols]
        update_cols = list(update_cols)

        stmt = (sqlite_insert if self.dialect == "sqlite" else pg_insert)(table)
        if update_cols:
            set_ = {c: stmt.excluded[c] for c in update_cols}
            if "updated_at" in table.c and "updated_at" not in set_:
                set_["updated_at"] = func.now()
            stmt = stmt.on_conflict_do_update(index_elements=conflict_cols, set_=set_)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=conflict_cols)

        def _run(c: Connection) -> None:
            for i in range(0, len(rows), batch_size):
                c.execute(stmt, rows[i:i + batch_size])

        if conn is not None:
            _run(conn)
        else:
            with self.transaction() as c:
                _run(c)
        log.info("Upserted %d rows into %s", len(rows), table_name)
        return len(rows)

    # ---- helpers ---------------------------------------------------------
    def _table(self, name: str) -> Table:
        try:
            return self.metadata.tables[name]
        except KeyError:
            raise ValueError(f"Unknown table '{name}'. Known: {list(self.metadata.tables)}")

    @staticmethod
    def _clean(table: Table, rows: Sequence[Mapping[str, Any]],
               conflict_cols: Sequence[str]) -> list[dict[str, Any]]:
        """Validate columns, enforce uniform keys, de-duplicate by conflict key.

        De-duplication matters: PostgreSQL raises "ON CONFLICT DO UPDATE cannot
        affect row a second time" if one statement holds the same key twice.
        Last occurrence wins.
        """
        valid = set(table.c.keys())
        keys = set(rows[0].keys())
        unknown = keys - valid
        if unknown:
            raise ValueError(f"Unknown columns for {table.name}: {sorted(unknown)}")
        missing = [c for c in conflict_cols if c not in keys]
        if missing:
            raise ValueError(f"Rows must include conflict columns: {missing}")

        deduped: dict[tuple, dict[str, Any]] = {}
        for r in rows:
            if set(r.keys()) != keys:
                raise ValueError("All rows in one upsert call must have the same columns")
            deduped[tuple(r[c] for c in conflict_cols)] = dict(r)
        return list(deduped.values())
