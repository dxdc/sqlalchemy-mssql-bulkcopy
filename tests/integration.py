#!/usr/bin/env python3
"""Integration test: verify all insert paths work and log comparison.

Tests:
  1. insertmanyvalues  — SA 2.x default (multi-row INSERT VALUES)
  2. executemany        — SA with use_insertmanyvalues=False
  3. bulkcopy (direct)  — bulkcopy() function
  4. bulkcopy (hook)    — register_bulkcopy + on_complete callback

Uses deterministic seeded data so results are reproducible.
Verifies correctness (row counts) and logs timing comparison.
"""

from __future__ import annotations

import sys
import time

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text

URL = "mssql+mssqlpython://sa:BenchMark!Pass123@localhost/master?Encrypt=yes&TrustServerCertificate=yes"
TABLE = "__bcp_integration"
N = 5_000


def generate_rows(n: int) -> list[dict[str, object]]:
    """Deterministic test data — same output every run."""
    return [{"id": i, "name": f"user_{i:06d}"} for i in range(n)]


def generate_tuples(n: int) -> list[tuple[int, str]]:
    return [(i, f"user_{i:06d}") for i in range(n)]


def count_rows(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar()


def truncate(engine):
    with engine.connect() as conn:
        conn.execute(text(f"TRUNCATE TABLE {TABLE}"))
        conn.commit()


def main():
    from sqlalchemy_mssql_bulkcopy import bulkcopy, register_bulkcopy

    metadata = MetaData()
    t = Table(
        TABLE,
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("name", String(50)),
    )

    engine = create_engine(URL)
    metadata.create_all(engine)

    rows = generate_rows(N)
    tuples = generate_tuples(N)
    results: list[tuple[str, float, int]] = []
    errors: list[str] = []

    # ---------------------------------------------------------------
    # 1. insertmanyvalues (SA default)
    # ---------------------------------------------------------------
    truncate(engine)
    t0 = time.perf_counter()
    with engine.begin() as conn:
        conn.execute(t.insert(), rows)
    elapsed = time.perf_counter() - t0
    got = count_rows(engine)
    if got != N:
        errors.append(f"insertmanyvalues: expected {N}, got {got}")
    results.append(("insertmanyvalues", elapsed, got))

    # ---------------------------------------------------------------
    # 2. executemany (insertmanyvalues disabled)
    # ---------------------------------------------------------------
    engine_em = create_engine(URL, use_insertmanyvalues=False)
    truncate(engine)
    t0 = time.perf_counter()
    with engine_em.begin() as conn:
        conn.execute(t.insert(), rows)
    elapsed = time.perf_counter() - t0
    got = count_rows(engine)
    if got != N:
        errors.append(f"executemany: expected {N}, got {got}")
    results.append(("executemany", elapsed, got))
    engine_em.dispose()

    # ---------------------------------------------------------------
    # 3. bulkcopy (direct function)
    # ---------------------------------------------------------------
    truncate(engine)
    t0 = time.perf_counter()
    with engine.begin() as conn:
        bcp_result = bulkcopy(
            conn,
            tuples,
            f"dbo.{TABLE}",
            columns=["id", "name"],
            batch_size=2_500,
            table_lock=True,
        )
    elapsed = time.perf_counter() - t0
    got = count_rows(engine)
    if got != N:
        errors.append(f"bulkcopy direct: expected {N}, got {got}")
    if bcp_result.get("rows_copied") != N:
        errors.append(
            f"bulkcopy direct: result says {bcp_result.get('rows_copied')}, expected {N}"
        )
    results.append(("bulkcopy (direct)", elapsed, got))

    # ---------------------------------------------------------------
    # 4. bulkcopy (event hook + on_complete callback)
    # ---------------------------------------------------------------
    engine_hook = create_engine(URL, use_insertmanyvalues=False)
    callback_results: list[tuple[str, dict]] = []
    register_bulkcopy(
        engine_hook,
        batch_size=2_500,
        table_lock=True,
        on_complete=lambda tbl, res: callback_results.append((tbl, res)),
    )

    truncate(engine)
    t0 = time.perf_counter()
    with engine_hook.begin() as conn:
        conn.execute(
            t.insert().execution_options(use_bulkcopy=True),
            rows,
        )
    elapsed = time.perf_counter() - t0
    got = count_rows(engine)
    if got != N:
        errors.append(f"bulkcopy hook: expected {N}, got {got}")
    if not callback_results:
        errors.append("bulkcopy hook: on_complete callback never fired")
    elif callback_results[0][1].get("rows_copied") != N:
        errors.append(
            f"bulkcopy hook: callback says "
            f"{callback_results[0][1].get('rows_copied')}, expected {N}"
        )
    results.append(("bulkcopy (hook)", elapsed, got))
    engine_hook.dispose()

    # ---------------------------------------------------------------
    # 5. bulkcopy (pandas df.to_sql)
    # ---------------------------------------------------------------
    import pandas as pd

    from sqlalchemy_mssql_bulkcopy import bulkcopy_insert_method

    engine_pd = create_engine(URL, use_insertmanyvalues=False)
    register_bulkcopy(engine_pd, batch_size=2_500, table_lock=True)

    truncate(engine)
    df = pd.DataFrame(generate_rows(N))
    t0 = time.perf_counter()
    df.to_sql(TABLE, engine_pd, if_exists="append", index=False, method=bulkcopy_insert_method)
    elapsed = time.perf_counter() - t0
    got = count_rows(engine)
    if got != N:
        errors.append(f"bulkcopy pandas: expected {N}, got {got}")
    results.append(("bulkcopy (pandas)", elapsed, got))
    engine_pd.dispose()

    # ---------------------------------------------------------------
    # Report
    # ---------------------------------------------------------------
    print()
    print(f"{'Method':<25s} {'Time':>8s} {'Rows':>7s} {'Rows/s':>10s}")
    print("-" * 55)
    for method, elapsed, got in results:
        rps = round(got / elapsed) if elapsed > 0 else 0
        print(f"{method:<25s} {elapsed:>7.3f}s {got:>7,d} {rps:>10,d}")

    if callback_results:
        tbl, res = callback_results[0]
        print("\non_complete callback received:")
        print(f"  table: {tbl}")
        print(f"  rows_copied: {res.get('rows_copied')}")
        print(f"  batch_count: {res.get('batch_count')}")
        print(f"  elapsed_time: {res.get('elapsed_time', 0):.3f}s")

    # Cleanup
    with engine.begin() as conn:
        t.drop(engine)
    engine.dispose()

    # Verdict
    if errors:
        print("\nFAILED:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print(f"\nPASS: all 5 methods inserted {N} rows correctly")
        sys.exit(0)


if __name__ == "__main__":
    main()
