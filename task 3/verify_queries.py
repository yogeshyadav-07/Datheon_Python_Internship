"""
verify_queries.py - verify the populated database.

    python verify_queries.py

Checks: tables exist, row counts, primary-key uniqueness, foreign-key orphans,
a sample analytical join, upsert idempotency/update behaviour, and rollback
on failure. Exits with code 1 if any check fails.
"""
import sys

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from db_loader import DatabaseLoader
from load_data import COMPANIES, load_all

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def scalar(conn, sql: str, **params):
    return conn.execute(text(sql), params).scalar()


def counts(conn) -> dict:
    return {t: scalar(conn, f"SELECT COUNT(*) FROM {t}") for t in ("companies", "products", "papers")}


def main() -> int:
    loader = DatabaseLoader()
    loader.create_schema()

    # 1. Tables exist ------------------------------------------------------
    existing = set(inspect(loader.engine).get_table_names())
    check("tables exist", {"companies", "products", "papers"} <= existing, str(sorted(existing)))

    with loader.engine.connect() as conn:
        # 2. Row counts ----------------------------------------------------
        c = counts(conn)
        for t, n in c.items():
            check(f"{t} has rows", n > 0, f"{n} rows")

        # 3. Primary-key uniqueness ---------------------------------------
        for t, pk in (("companies", "slug"), ("products", "product_id"), ("papers", "arxiv_id")):
            dupes = scalar(conn, f"SELECT COUNT(*) FROM (SELECT {pk} FROM {t} GROUP BY {pk} HAVING COUNT(*) > 1) d")
            check(f"{t}.{pk} unique", dupes == 0, f"{dupes} duplicate keys")

        # 4. Foreign-key orphans ------------------------------------------
        orphan_products = scalar(conn, """
            SELECT COUNT(*) FROM products p
            LEFT JOIN companies c ON c.slug = p.company_slug WHERE c.slug IS NULL""")
        check("no orphan products", orphan_products == 0, f"{orphan_products} orphans")
        orphan_papers = scalar(conn, """
            SELECT COUNT(*) FROM papers p
            LEFT JOIN companies c ON c.slug = p.company_slug
            WHERE p.company_slug IS NOT NULL AND c.slug IS NULL""")
        check("no orphan papers", orphan_papers == 0, f"{orphan_papers} orphans")

        # 5. Sample analytical query --------------------------------------
        rows = conn.execute(text("""
            SELECT c.name, COUNT(p.product_id) AS n_products
            FROM companies c LEFT JOIN products p ON p.company_slug = c.slug
            GROUP BY c.name ORDER BY n_products DESC, c.name""")).all()
        print("\n  Products per company:")
        for name, n in rows:
            print(f"    {name:<12} {n}")
        print()
        check("join query returns rows", len(rows) == c["companies"], f"{len(rows)} companies")

    # 6. Idempotency: re-running the load must not change counts -----------
    with loader.engine.connect() as conn:
        before = counts(conn)
    load_all(loader)
    with loader.engine.connect() as conn:
        after = counts(conn)
    check("re-load is idempotent", before == after, f"{before} -> {after}")

    # 7. Upsert updates existing rows --------------------------------------
    changed = [dict(r) for r in COMPANIES[:1]]
    changed[0]["country"] = "XX"
    loader.upsert("companies", changed)
    with loader.engine.connect() as conn:
        country = scalar(conn, "SELECT country FROM companies WHERE slug = :s", s=changed[0]["slug"])
    check("upsert updates on conflict", country == "XX", f"country={country}")
    loader.upsert("companies", [COMPANIES[0]])  # restore original value

    # 8. Transaction rollback on failure -----------------------------------
    failed = False
    try:
        with loader.transaction() as conn:
            loader.upsert("companies", [{"slug": "tmp-rollback", "name": "Temp"}], conn=conn)
            loader.upsert("products", [{"product_id": "tmp-p", "company_slug": "does-not-exist",
                                        "name": "Bad FK"}], conn=conn)
    except IntegrityError:
        failed = True
    with loader.engine.connect() as conn:
        leaked = scalar(conn, "SELECT COUNT(*) FROM companies WHERE slug = 'tmp-rollback'")
    check("failed batch rolls back fully", failed and leaked == 0,
          f"error raised={failed}, leaked rows={leaked}")

    # Summary ----------------------------------------------------------------
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{len(results) - n_fail}/{len(results)} checks passed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
