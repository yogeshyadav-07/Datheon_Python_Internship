# Database Loader (SQLite / PostgreSQL)

SQLAlchemy Core loader with schema creation, batch upserts and transaction safety.

## Run
    pip install -r requirements.txt
    python load_data.py          # creates schema + loads sample data -> frontier.db
    python verify_queries.py     # 13 checks, exit code 1 on any failure

## PostgreSQL
    export DATABASE_URL="postgresql+psycopg2://user:pass@host:5432/dbname"
    python load_data.py && python verify_queries.py

## Key design points
- `upsert()` uses `INSERT ... ON CONFLICT DO UPDATE` (native for both SQLite and PostgreSQL).
- Conflict target defaults to the primary key; `updated_at` refreshes automatically on update.
- Duplicate keys inside a batch are de-duplicated (last wins) - PostgreSQL would otherwise error.
- `with loader.transaction() as conn:` makes multi-table loads atomic (all commit or all roll back).
- SQLite foreign keys are switched on via PRAGMA (off by default in SQLite).
- Batches are chunked (`batch_size=500`) to keep statements small.
