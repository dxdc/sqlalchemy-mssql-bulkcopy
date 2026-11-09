#!/usr/bin/env python3
"""Throwaway probe: bulk copy semantics vs INSERT, and where insert time goes.

Reports only; never exits non-zero.
"""

from __future__ import annotations

import statistics
import time
import traceback

import mssql_python
import numpy as np
import pandas as pd
import pyodbc
from mssql_python import perf_timer
from sqlalchemy import create_engine, text

from sqlalchemy_mssql_bulkcopy import bulkcopy

PWD = "BenchMark!Pass123"
URL = f"mssql+mssqlpython://sa:{PWD}@localhost/master?Encrypt=yes&TrustServerCertificate=yes"
MS_CS = f"SERVER=localhost;DATABASE=master;UID=sa;PWD={PWD};Encrypt=yes;TrustServerCertificate=yes"
ODBC_CS = "DRIVER={ODBC Driver 18 for SQL Server};" + MS_CS

engine = create_engine(URL)


def short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {' '.join(str(exc).split())[:220]}"


def run_sql(*stmts: str) -> None:
    with engine.begin() as conn:
        for s in stmts:
            conn.execute(text(s))


def q(stmt: str):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(stmt))]


def drop(*names: str) -> None:
    for n in names:
        run_sql(f"IF OBJECT_ID('{n}') IS NOT NULL DROP TABLE {n}")


def section(title: str) -> None:
    print(f"\n=== {title}", flush=True)


def attempt(label: str, fn):
    t0 = time.perf_counter()
    try:
        out = fn()
        print(f"  {label}: OK in {time.perf_counter() - t0:.2f}s {out if out else ''}")
        return out
    except Exception as exc:
        print(f"  {label}: ERROR after {time.perf_counter() - t0:.2f}s {short(exc)}")
        return None


def guarded(fn):
    try:
        fn()
    except Exception:
        traceback.print_exc()


# ---------------------------------------------------------------------------
def a_transaction():
    section("A. Does rolling back the SA transaction undo bulkcopy()?")
    drop("dbo.p_tx")
    run_sql("CREATE TABLE dbo.p_tx (id INT PRIMARY KEY, v NVARCHAR(10))")

    class Boom(Exception):
        pass

    try:
        with engine.begin() as conn:
            bulkcopy(conn, [(1, "a"), (2, "b")], "dbo.p_tx", columns=["id", "v"])
            raise Boom
    except Boom:
        pass
    print(f"  rows after rollback: {q('SELECT COUNT(*) FROM dbo.p_tx')[0][0]} (0 = transactional)")


def b_locks():
    section("B. bulkcopy() vs uncommitted work in the same SA transaction (timeout=5)")
    drop("dbo.p_tx", "dbo.p_new")
    run_sql("CREATE TABLE dbo.p_tx (id INT PRIMARY KEY, v NVARCHAR(10))")
    with engine.connect() as conn:
        conn.execute(text("INSERT INTO dbo.p_tx VALUES (10, 'x')"))
        attempt(
            "after an uncommitted INSERT, row locks",
            lambda: bulkcopy(conn, [(11, "y")], "dbo.p_tx", columns=["id", "v"], timeout=5),
        )
        attempt(
            "after an uncommitted INSERT, table_lock=True",
            lambda: bulkcopy(
                conn, [(12, "z")], "dbo.p_tx", columns=["id", "v"], timeout=5, table_lock=True
            ),
        )
        conn.rollback()
    with engine.connect() as conn:
        conn.execute(text("TRUNCATE TABLE dbo.p_tx"))
        attempt(
            "TRUNCATE then bulkcopy in one transaction",
            lambda: bulkcopy(conn, [(1, "a")], "dbo.p_tx", columns=["id", "v"], timeout=5),
        )
        conn.rollback()
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE dbo.p_new (id INT)"))
        attempt(
            "CREATE TABLE then bulkcopy in one transaction",
            lambda: bulkcopy(conn, [(1,)], "dbo.p_new", columns=["id"], timeout=5),
        )
        conn.rollback()
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE #p_tmp (id INT)"))
        attempt(
            "bulkcopy into a #temp table",
            lambda: bulkcopy(conn, [(1,)], "#p_tmp", columns=["id"], timeout=5),
        )


def d_identity():
    section("D. Explicit values for an IDENTITY column")
    for kw in ({}, {"keep_identity": True}):
        drop("dbo.p_id")
        run_sql("CREATE TABLE dbo.p_id (id INT IDENTITY(1,1) PRIMARY KEY, v NVARCHAR(10))")
        with engine.begin() as conn:
            attempt(
                f"bulkcopy {kw or 'defaults'}",
                lambda conn=conn, kw=kw: bulkcopy(
                    conn, [(100, "a"), (200, "b")], "dbo.p_id", columns=["id", "v"], **kw
                ),
            )
        print(f"    stored: {q('SELECT id, v FROM dbo.p_id ORDER BY id')}")
    attempt("plain INSERT with explicit id", lambda: run_sql("INSERT INTO dbo.p_id (id, v) VALUES (300, 'c')"))


def e_constraints():
    section("E. CHECK and FOREIGN KEY constraints")
    for kw in ({}, {"check_constraints": True}):
        drop("dbo.p_child", "dbo.p_parent")
        run_sql(
            "CREATE TABLE dbo.p_parent (id INT PRIMARY KEY)",
            "INSERT INTO dbo.p_parent VALUES (1)",
            "CREATE TABLE dbo.p_child (id INT PRIMARY KEY,"
            " parent_id INT CONSTRAINT fk_p FOREIGN KEY REFERENCES dbo.p_parent(id),"
            " qty INT CONSTRAINT ck_p CHECK (qty > 0))",
        )
        with engine.begin() as conn:
            attempt(
                f"bulkcopy bad row (parent 999, qty -5) {kw or 'defaults'}",
                lambda conn=conn, kw=kw: bulkcopy(
                    conn, [(1, 999, -5)], "dbo.p_child", columns=["id", "parent_id", "qty"], **kw
                ),
            )
        print(f"    stored: {q('SELECT * FROM dbo.p_child')}")
        print(
            "    untrusted after load: "
            f"check={q('SELECT is_not_trusted FROM sys.check_constraints WHERE name = ' + chr(39) + 'ck_p' + chr(39))} "
            f"fk={q('SELECT is_not_trusted FROM sys.foreign_keys WHERE name = ' + chr(39) + 'fk_p' + chr(39))}"
        )
    drop("dbo.p_child", "dbo.p_parent")
    run_sql(
        "CREATE TABLE dbo.p_parent (id INT PRIMARY KEY)",
        "CREATE TABLE dbo.p_child (id INT PRIMARY KEY,"
        " parent_id INT CONSTRAINT fk_p FOREIGN KEY REFERENCES dbo.p_parent(id),"
        " qty INT CONSTRAINT ck_p CHECK (qty > 0))",
    )
    with engine.begin() as conn:
        bulkcopy(conn, [(1, None, 5)], "dbo.p_child", columns=["id", "parent_id", "qty"])
    print(
        "  valid row loaded with defaults -> untrusted: "
        f"check={q('SELECT is_not_trusted FROM sys.check_constraints WHERE name = ' + chr(39) + 'ck_p' + chr(39))} "
        f"fk={q('SELECT is_not_trusted FROM sys.foreign_keys WHERE name = ' + chr(39) + 'fk_p' + chr(39))}"
    )


def f_triggers():
    section("F. AFTER INSERT triggers")
    for kw in ({}, {"fire_triggers": True}):
        drop("dbo.p_trg", "dbo.p_audit")
        run_sql(
            "CREATE TABLE dbo.p_trg (id INT PRIMARY KEY)",
            "CREATE TABLE dbo.p_audit (id INT)",
        )
        run_sql(
            "CREATE TRIGGER dbo.trg_p ON dbo.p_trg AFTER INSERT AS "
            "INSERT INTO dbo.p_audit SELECT id FROM inserted"
        )
        with engine.begin() as conn:
            bulkcopy(conn, [(1,), (2,)], "dbo.p_trg", columns=["id"], **kw)
        print(f"  {kw or 'defaults'}: audit rows written = {q('SELECT COUNT(*) FROM dbo.p_audit')[0][0]} (INSERT would write 2)")


def g_not_null_default():
    section("G. NULL into a NOT NULL column that has a DEFAULT")
    for kw in ({}, {"keep_nulls": True}):
        drop("dbo.p_nn")
        run_sql("CREATE TABLE dbo.p_nn (id INT PRIMARY KEY, v INT NOT NULL CONSTRAINT df_nn DEFAULT 7)")
        with engine.begin() as conn:
            attempt(
                f"bulkcopy (1, NULL) {kw or 'defaults'}",
                lambda conn=conn, kw=kw: bulkcopy(conn, [(1, None)], "dbo.p_nn", columns=["id", "v"], **kw),
            )
        print(f"    stored: {q('SELECT * FROM dbo.p_nn')}")
    attempt("plain INSERT (2, NULL)", lambda: run_sql("INSERT INTO dbo.p_nn VALUES (2, NULL)"))


def i_overhead():
    section("I. Fixed cost per call")
    drop("dbo.p_ovh")
    run_sql("CREATE TABLE dbo.p_ovh (id INT, v NVARCHAR(20))")

    def med(xs):
        return f"median {statistics.median(xs) * 1000:.1f}ms (first {xs[0] * 1000:.1f}ms)"

    t = []
    for _ in range(5):
        t0 = time.perf_counter()
        c = mssql_python.connect(MS_CS)
        c.close()
        t.append(time.perf_counter() - t0)
    print(f"  mssql_python.connect()+close: {med(t)}")
    for n in (1, 1000, 10_000):
        rows = [(i, f"user_{i:06d}") for i in range(n)]
        bc, em = [], []
        for _ in range(6):
            with engine.begin() as conn:
                t0 = time.perf_counter()
                bulkcopy(conn, rows, "dbo.p_ovh", columns=["id", "v"])
                bc.append(time.perf_counter() - t0)
            with engine.begin() as conn:
                cur = conn.connection.dbapi_connection.cursor()
                t0 = time.perf_counter()
                cur.executemany("INSERT INTO dbo.p_ovh (id, v) VALUES (?, ?)", rows)
                em.append(time.perf_counter() - t0)
                cur.close()
        print(f"  {n:>6,} rows  bulkcopy {med(bc)} | executemany {med(em)}")


def j_arrow():
    section("J. cursor.bulkcopy_arrow() with a DataFrame")
    drop("dbo.p_arrow")
    run_sql(
        "CREATE TABLE dbo.p_arrow (id INT, f FLOAT NULL, ni BIGINT NULL, s NVARCHAR(20) NULL,"
        " ts DATETIME2 NULL, b BIT NULL)"
    )
    df = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "f": [1.5, np.nan, None],
            "ni": pd.array([7, pd.NA, 9], dtype="Int64"),
            "s": ["x", None, np.nan],
            "ts": pd.to_datetime(["2026-01-02 03:04:05", None, "2026-03-04 00:00:00"]),
            "b": pd.array([True, pd.NA, False], dtype="boolean"),
        }
    )
    with engine.begin() as conn:
        cur = conn.connection.dbapi_connection.cursor()
        attempt("bulkcopy_arrow(df) with missing values", lambda: cur.bulkcopy_arrow("dbo.p_arrow", df))
    print(f"    stored: {q('SELECT * FROM dbo.p_arrow ORDER BY id')}")

    drop("dbo.p_arrow")
    run_sql("CREATE TABLE dbo.p_arrow (id INT, name NVARCHAR(20))")
    n = 500_000
    big = pd.DataFrame({"id": np.arange(n), "name": [f"user_{i:06d}" for i in range(n)]})
    tuples = list(big.itertuples(index=False, name=None))
    for label, fn in (
        ("bulkcopy_arrow(df)", lambda cur, conn: cur.bulkcopy_arrow("dbo.p_arrow", big)),
        ("bulkcopy(conn, df)  [our conversion]", lambda cur, conn: bulkcopy(conn, big, "dbo.p_arrow")),
        ("bulkcopy(conn, tuples)", lambda cur, conn: bulkcopy(conn, tuples, "dbo.p_arrow", columns=["id", "name"])),
    ):
        run_sql("TRUNCATE TABLE dbo.p_arrow")
        with engine.begin() as conn:
            cur = conn.connection.dbapi_connection.cursor()
            t0 = time.perf_counter()
            try:
                fn(cur, conn)
                el = time.perf_counter() - t0
                print(f"  {label:<38s} {n:,} rows in {el:.2f}s = {n / el:,.0f} rows/s")
            except Exception as exc:
                print(f"  {label}: ERROR {short(exc)}")


def p_insertmanyvalues():
    section("P. Same 1,049-row / 2,098-parameter INSERT: mssql-python vs pyodbc")
    rows_per = 1049
    n_batches = 40
    sql = "INSERT INTO dbo.p_imv (id, name) VALUES " + ", ".join(["(?, ?)"] * rows_per)
    batches = []
    k = 0
    for _ in range(n_batches):
        flat = []
        for _ in range(rows_per):
            flat += [k, f"user_{k:06d}"]
            k += 1
        batches.append(flat)
    total = rows_per * n_batches

    def server_stats():
        r = q(
            "SELECT COUNT(*), SUM(qs.execution_count), SUM(qs.total_elapsed_time)/1000,"
            " SUM(qs.total_worker_time)/1000, MAX(qs.plan_generation_num)"
            " FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st"
            " WHERE st.text LIKE '%INSERT INTO dbo.p_imv%' AND st.text NOT LIKE '%dm_exec%'"
        )[0]
        return f"server: plans={r[0]} execs={r[1]} elapsed={r[2]}ms cpu={r[3]}ms plan_gen={r[4]}"

    def reset():
        drop("dbo.p_imv")
        run_sql("CREATE TABLE dbo.p_imv (id INT, name NVARCHAR(20))", "DBCC FREEPROCCACHE")

    def ms_run(label, **kw):
        reset()
        conn = mssql_python.connect(MS_CS)
        cur = conn.cursor()
        perf_timer.reset()
        perf_timer.enable()
        t0 = time.perf_counter()
        for b in batches:
            cur.execute(sql, b, **kw)
        conn.commit()
        el = time.perf_counter() - t0
        perf_timer.disable()
        st = perf_timer.get_stats()
        phases = ", ".join(
            f"{k.split('::')[-1]}={v['total_us'] / 1000:.0f}ms"
            for k, v in st.items()
            if k.startswith("py::execute")
        )
        cur.close()
        conn.close()
        print(f"  {label:<34s} {total / el:>9,.0f} rows/s ({el:.2f}s) | {phases}")
        print(f"    {server_stats()}")

    def odbc_run(label, fast=False):
        reset()
        conn = pyodbc.connect(ODBC_CS)
        cur = conn.cursor()
        t0 = time.perf_counter()
        for b in batches:
            cur.execute(sql, b)
        conn.commit()
        el = time.perf_counter() - t0
        conn.close()
        print(f"  {label:<34s} {total / el:>9,.0f} rows/s ({el:.2f}s)")
        print(f"    {server_stats()}")

    ms_run("mssql-python execute()")
    ms_run("mssql-python execute(use_prepare=False)", use_prepare=False)
    odbc_run("pyodbc execute()")

    # executemany for reference, same rows
    reset()
    conn = mssql_python.connect(MS_CS)
    cur = conn.cursor()
    rows = [(i, f"user_{i:06d}") for i in range(total)]
    perf_timer.reset()
    perf_timer.enable()
    t0 = time.perf_counter()
    cur.executemany("INSERT INTO dbo.p_imv (id, name) VALUES (?, ?)", rows)
    conn.commit()
    el = time.perf_counter() - t0
    perf_timer.disable()
    st = perf_timer.get_stats()
    phases = ", ".join(
        f"{k.split('::')[-1]}={v['total_us'] / 1000:.0f}ms" for k, v in st.items() if "executemany" in k
    )
    print(f"  {'mssql-python executemany()':<34s} {total / el:>9,.0f} rows/s ({el:.2f}s) | {phases}")
    print(f"    {server_stats()}")
    conn.close()


if __name__ == "__main__":
    from importlib.metadata import version

    print("mssql-python", version("mssql-python"), "| pyodbc", pyodbc.version, "| pandas", pd.__version__)
    for fn in (a_transaction, b_locks, d_identity, e_constraints, f_triggers, g_not_null_default,
               i_overhead, j_arrow, p_insertmanyvalues):
        guarded(fn)
    print("\nPROBE DONE")
