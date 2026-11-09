#!/usr/bin/env python3
"""Integration test: verify all insert paths work and log comparison.

Tests:
  1. insertmanyvalues  — SA 2.x default (multi-row INSERT VALUES)
  2. executemany        — SA with use_insertmanyvalues=False
  3. bulkcopy (direct)  — bulkcopy() function
  4. bulkcopy (hook)    — register_bulkcopy + on_complete callback
  5. bulkcopy (pandas)  — df.to_sql(method=bulkcopy_insert_method)
  6. missing values      — DataFrame NaN/NaT/pd.NA round-trip as NULL
                           via bulkcopy() and the to_sql hook
  7. INSERT-like defaults — NULL kept in a DEFAULT column, CHECK
                           constraint enforced, trigger fired

Uses deterministic seeded data so results are reproducible.
Verifies correctness (row counts) and logs timing comparison.
"""

from __future__ import annotations

import datetime
import sys
import time

import numpy as np
import pandas as pd
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text

from sqlalchemy_mssql_bulkcopy import (
    bulkcopy,
    bulkcopy_insert_method,
    register_bulkcopy,
)

URL = (
    "mssql+mssqlpython://sa:BenchMark!Pass123@localhost/master"
    "?Encrypt=yes&TrustServerCertificate=yes"
)
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


NULLS_TABLE = "__bcp_integration_nulls"
NULLS_DDL = f"""
CREATE TABLE dbo.{NULLS_TABLE} (
    id INT NOT NULL PRIMARY KEY,
    f  FLOAT NULL,
    ni BIGINT NULL,
    s  NVARCHAR(20) NULL,
    ts DATETIME2 NULL,
    b  BIT NULL
)
"""
NULLS_EXPECTED = [
    (1, 1.5, 7, "x", datetime.datetime(2026, 1, 2, 3, 4, 5), True),
    (2, None, None, None, None, None),
    (3, None, 9, None, datetime.datetime(2026, 3, 4), False),
]


def nulls_frame() -> pd.DataFrame:
    """One full row, one all-missing row, one mixed: every pandas flavour
    of missing (float NaN, pd.NA in nullable dtypes, NaT, None)."""
    return pd.DataFrame(
        {
            "id": [1, 2, 3],
            "f": [1.5, np.nan, None],
            "ni": pd.array([7, pd.NA, 9], dtype="Int64"),
            "s": ["x", None, np.nan],
            "ts": pd.to_datetime(["2026-01-02 03:04:05", None, "2026-03-04 00:00:00"]),
            "b": pd.array([True, pd.NA, False], dtype="boolean"),
        }
    )


def check_nulls(engine, label: str, load, errors: list[str]) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(f"IF OBJECT_ID('dbo.{NULLS_TABLE}') IS NOT NULL DROP TABLE dbo.{NULLS_TABLE}")
        )
        conn.execute(text(NULLS_DDL))
    try:
        load()
    except Exception as exc:
        errors.append(f"{label}: {type(exc).__name__}: {exc}")
        return
    with engine.connect() as conn:
        got = [
            tuple(r) for r in conn.execute(text(f"SELECT * FROM dbo.{NULLS_TABLE} ORDER BY id"))
        ]
    if got != NULLS_EXPECTED:
        errors.append(f"{label}: got {got!r}, expected {NULLS_EXPECTED!r}")
    else:
        print(f"{label}: missing values round-trip as NULL")


def main():
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
    engine_pd = create_engine(URL, use_insertmanyvalues=False)
    register_bulkcopy(engine_pd, batch_size=2_500, table_lock=True)

    truncate(engine)
    df = pd.DataFrame(generate_rows(N))
    t0 = time.perf_counter()
    df.to_sql(
        TABLE,
        engine_pd,
        if_exists="append",
        index=False,
        method=bulkcopy_insert_method,
    )
    elapsed = time.perf_counter() - t0
    got = count_rows(engine)
    if got != N:
        errors.append(f"bulkcopy pandas: expected {N}, got {got}")
    results.append(("bulkcopy (pandas)", elapsed, got))
    engine_pd.dispose()

    # ---------------------------------------------------------------
    # 6. DataFrame missing values (NaN / NaT / pd.NA) -> NULL
    # ---------------------------------------------------------------
    nulls_df = nulls_frame()

    def load_direct():
        with engine.begin() as conn:
            bulkcopy(conn, nulls_df, f"dbo.{NULLS_TABLE}")

    check_nulls(engine, "bulkcopy (direct, DataFrame)", load_direct, errors)

    engine_nulls = create_engine(URL, use_insertmanyvalues=False)
    nulls_fired: list[dict] = []
    register_bulkcopy(engine_nulls, on_complete=lambda tbl, res: nulls_fired.append(res))

    def load_hook():
        nulls_df.to_sql(
            NULLS_TABLE,
            engine_nulls,
            schema="dbo",
            if_exists="append",
            index=False,
            method=bulkcopy_insert_method,
        )
        if not nulls_fired:
            raise RuntimeError("bulkcopy did not run (fell back to executemany)")

    check_nulls(engine, "bulkcopy (pandas hook, DataFrame)", load_hook, errors)
    engine_nulls.dispose()
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE dbo.{NULLS_TABLE}"))

    # ---------------------------------------------------------------
    # 7. INSERT-like defaults (keep_nulls, check_constraints, fire_triggers)
    # ---------------------------------------------------------------
    with engine.begin() as conn:
        for name in ("__bcp_defaults_audit", "__bcp_defaults"):
            conn.execute(text(f"IF OBJECT_ID('dbo.{name}') IS NOT NULL DROP TABLE dbo.{name}"))
        conn.execute(
            text(
                "CREATE TABLE dbo.__bcp_defaults (id INT PRIMARY KEY,"
                " d INT NULL DEFAULT 42, qty INT NULL CHECK (qty > 0))"
            )
        )
        conn.execute(text("CREATE TABLE dbo.__bcp_defaults_audit (id INT)"))
        conn.execute(
            text(
                "CREATE TRIGGER dbo.__bcp_defaults_trg ON dbo.__bcp_defaults AFTER INSERT AS "
                "INSERT INTO dbo.__bcp_defaults_audit SELECT id FROM inserted"
            )
        )
    with engine.begin() as conn:
        bulkcopy(conn, [(1, None, 5)], "dbo.__bcp_defaults", columns=["id", "d", "qty"])
    with engine.connect() as conn:
        stored = conn.execute(text("SELECT id, d, qty FROM dbo.__bcp_defaults")).fetchall()
        audited = conn.execute(text("SELECT COUNT(*) FROM dbo.__bcp_defaults_audit")).scalar()
    if [tuple(r) for r in stored] != [(1, None, 5)]:
        errors.append(f"defaults: NULL not kept in DEFAULT column, got {stored!r}")
    if audited != 1:
        errors.append(f"defaults: trigger did not fire (audit rows: {audited})")
    try:
        with engine.begin() as conn:
            bulkcopy(conn, [(2, None, -1)], "dbo.__bcp_defaults", columns=["id", "d", "qty"])
        errors.append("defaults: row violating CHECK (qty > 0) was accepted")
    except Exception:
        pass
    if not errors:
        print("INSERT-like defaults: NULL kept, CHECK enforced, trigger fired")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE dbo.__bcp_defaults"))
        conn.execute(text("DROP TABLE dbo.__bcp_defaults_audit"))

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
    t.drop(engine)
    engine.dispose()

    # Verdict
    if errors:
        print("\nFAILED:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print(f"\nPASS: all 5 methods inserted {N} rows correctly; NULL handling and defaults OK")
        sys.exit(0)


if __name__ == "__main__":
    main()
