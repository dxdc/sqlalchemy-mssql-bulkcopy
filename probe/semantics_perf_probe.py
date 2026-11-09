#!/usr/bin/env python3
"""Throwaway probe, round 2. Reports only; never exits non-zero."""

from __future__ import annotations

import time
import traceback
from importlib.metadata import version

import mssql_python
import pandas as pd
import pyodbc
from mssql_python import perf_timer
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event, text

from sqlalchemy_mssql_bulkcopy import bulkcopy, bulkcopy_insert_method, register_bulkcopy

PWD = "BenchMark!Pass123"
Q = "Encrypt=yes&TrustServerCertificate=yes"
URL = f"mssql+mssqlpython://sa:{PWD}@localhost/master?{Q}"
PYODBC_URL = f"mssql+pyodbc://sa:{PWD}@localhost/master?driver=ODBC+Driver+18+for+SQL+Server&{Q}"
MS_CS = f"SERVER=localhost;DATABASE=master;UID=sa;PWD={PWD};Encrypt=yes;TrustServerCertificate=yes"
ODBC_CS = "DRIVER={ODBC Driver 18 for SQL Server};" + MS_CS

engine = create_engine(URL)


def short(exc):
    return f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}"


def run_sql(*stmts):
    with engine.begin() as conn:
        for s in stmts:
            conn.execute(text(s))


def q(stmt):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(stmt))]


def drop(*names):
    for n in names:
        run_sql(f"IF OBJECT_ID('{n}') IS NOT NULL DROP TABLE {n}")


def section(t):
    print(f"\n=== {t}", flush=True)


def guarded(fn):
    try:
        fn()
    except Exception:
        traceback.print_exc()


def k_hook_rollback():
    section("K. Hook: does rolling back the SA transaction undo the bulk-copied rows?")
    drop("dbo.p_k")
    md = MetaData()
    t = Table("p_k", md, Column("id", Integer, primary_key=True, autoincrement=False),
              Column("v", String(10)), schema="dbo")
    md.create_all(engine)
    eng = create_engine(URL, use_insertmanyvalues=False)
    fired = []
    register_bulkcopy(eng, on_complete=lambda tb, r: fired.append(r))
    try:
        with eng.begin() as conn:
            conn.execute(t.insert().execution_options(use_bulkcopy=True),
                         [{"id": i, "v": "x"} for i in range(100)])
            raise RuntimeError("rollback")
    except RuntimeError:
        pass
    print(f"  bulkcopy ran: {bool(fired)}; rows after rollback: {q('SELECT COUNT(*) FROM dbo.p_k')[0][0]}"
          " (an INSERT would leave 0)")
    eng.dispose()


def l_to_sql_new_table():
    section("L. df.to_sql(method=bulkcopy_insert_method) when to_sql creates the table")
    df = pd.DataFrame({"id": range(5000), "v": [f"v{i}" for i in range(5000)]})
    for mode, if_exists, fresh in (("new table, if_exists='fail'", "fail", True),
                                   ("if_exists='replace'", "replace", False),
                                   ("if_exists='append' on existing table", "append", False)):
        if fresh:
            drop("dbo.p_l")
        eng = create_engine(URL, use_insertmanyvalues=False)
        fired = []
        register_bulkcopy(eng, on_complete=lambda tb, r, fired=fired: fired.append(r))
        t0 = time.perf_counter()
        try:
            df.to_sql("p_l", eng, schema="dbo", index=False, method=bulkcopy_insert_method,
                      if_exists=if_exists)
            el = time.perf_counter() - t0
            print(f"  {mode}: {el:.1f}s, bulkcopy used: {bool(fired)}, rows: {q('SELECT COUNT(*) FROM dbo.p_l')[0][0]}")
        except Exception as exc:
            print(f"  {mode}: ERROR after {time.perf_counter() - t0:.1f}s {short(exc)}")
        eng.dispose()


def m_option_cost():
    section("M. Speed cost of INSERT-like options (keep_nulls, check_constraints, fire_triggers)")
    insert_like = {"keep_nulls": True, "check_constraints": True, "fire_triggers": True}
    drop("dbo.p_m", "dbo.p_mc", "dbo.p_mp")
    run_sql("CREATE TABLE dbo.p_m (id INT, v NVARCHAR(20))",
            "CREATE TABLE dbo.p_mp (id INT PRIMARY KEY)",
            "INSERT INTO dbo.p_mp SELECT TOP 1000 ROW_NUMBER() OVER (ORDER BY (SELECT 1)) FROM sys.all_objects a, sys.all_objects b",
            "CREATE TABLE dbo.p_mc (id INT PRIMARY KEY, parent_id INT REFERENCES dbo.p_mp(id),"
            " qty INT CHECK (qty > 0))")
    n = 500_000
    rows = [(i, f"user_{i:06d}") for i in range(n)]
    crow = [(i, (i % 1000) + 1, (i % 50) + 1) for i in range(n)]
    for label, table, data, cols in (("plain table", "dbo.p_m", rows, ["id", "v"]),
                                     ("table with FK + CHECK", "dbo.p_mc", crow, ["id", "parent_id", "qty"])):
        for opts_label, opts in (("driver defaults", {"keep_nulls": False}), ("INSERT-like", insert_like)):
            best = None
            for _ in range(2):
                run_sql(f"TRUNCATE TABLE {table}")
                with engine.begin() as conn:
                    t0 = time.perf_counter()
                    bulkcopy(conn, data, table, columns=cols, timeout=0, **opts)
                    el = time.perf_counter() - t0
                best = el if best is None else min(best, el)
            print(f"  {label:<22s} {opts_label:<16s} {n / best:>9,.0f} rows/s")


def p_shapes_and_drivers():
    section("P1. What SQLAlchemy sends per driver (10,000 rows, 2 columns)")
    md = MetaData()
    t = Table("p_imv", md, Column("id", Integer, primary_key=True, autoincrement=False),
              Column("name", String(20)), schema="dbo")
    rows = [{"id": i, "name": f"user_{i:06d}"} for i in range(10_000)]
    for label, url, kw in (("mssql-python", URL, {}), ("pyodbc", PYODBC_URL, {}),
                           ("pyodbc fast_executemany", PYODBC_URL, {"fast_executemany": True})):
        eng = create_engine(url, **kw)
        md.drop_all(eng)
        md.create_all(eng)
        calls = []

        @event.listens_for(eng, "before_cursor_execute")
        def _b(conn, cursor, statement, parameters, context, executemany, calls=calls):
            n = len(parameters) if parameters is not None and not executemany else len(parameters or [])
            calls.append((executemany, n, len(statement)))

        t0 = time.perf_counter()
        with eng.begin() as conn:
            conn.execute(t.insert(), rows)
        el = time.perf_counter() - t0
        ins = [c for c in calls if c[2] > 30]
        kinds = {"executemany" if c[0] else "execute" for c in ins}
        print(f"  {label:<24s} {len(rows) / el:>9,.0f} rows/s | {len(ins)} cursor calls ({', '.join(kinds)}),"
              f" params (or rows) per call max {max(c[1] for c in ins)}, SQL length max {max(c[2] for c in ins):,}")
        eng.dispose()

    section("P2. Same 1,000-row / 2,000-parameter INSERT, raw drivers (50 batches)")
    rows_per, n_batches = 1000, 50
    sql = "INSERT INTO dbo.p_imv (id, name) VALUES " + ", ".join(["(?, ?)"] * rows_per)
    batches, k = [], 0
    for _ in range(n_batches):
        flat = []
        for _ in range(rows_per):
            flat += [k, f"user_{k:06d}"]
            k += 1
        batches.append(flat)
    total = rows_per * n_batches

    def server_stats():
        r = q("SELECT COUNT(*), SUM(qs.execution_count), SUM(qs.total_elapsed_time)/1000,"
              " SUM(qs.total_worker_time)/1000"
              " FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st"
              " WHERE st.text LIKE '%INSERT INTO dbo.p_imv%' AND st.text NOT LIKE '%dm_exec%'")[0]
        return f"server side: {r[0]} cached plan(s), {r[1]} executions, {r[2]}ms elapsed, {r[3]}ms CPU"

    def reset():
        run_sql("TRUNCATE TABLE dbo.p_imv", "DBCC FREEPROCCACHE")

    def ms(label, **kw):
        reset()
        conn = mssql_python.connect(MS_CS)
        cur = conn.cursor()
        cur.execute(sql, batches[0], **kw)  # warm-up (prepare, plan)
        reset()
        perf_timer.reset()
        perf_timer.enable()
        t0 = time.perf_counter()
        for b in batches:
            cur.execute(sql, b, **kw)
        conn.commit()
        el = time.perf_counter() - t0
        perf_timer.disable()
        st = perf_timer.get_stats()
        ph = ", ".join(f"{k.split('::')[-1]} {v['total_us'] / 1000:.0f}ms"
                       for k, v in st.items() if k.startswith("py::execute"))
        conn.close()
        print(f"  {label:<36s} {total / el:>9,.0f} rows/s ({el * 1000:.0f}ms) | python phases: {ph}")
        print(f"    {server_stats()}")

    def odbc(label):
        reset()
        conn = pyodbc.connect(ODBC_CS)
        cur = conn.cursor()
        cur.execute(sql, batches[0])
        reset()
        t0 = time.perf_counter()
        for b in batches:
            cur.execute(sql, b)
        conn.commit()
        el = time.perf_counter() - t0
        conn.close()
        print(f"  {label:<36s} {total / el:>9,.0f} rows/s ({el * 1000:.0f}ms)")
        print(f"    {server_stats()}")

    guarded(lambda: ms("mssql-python execute()"))
    guarded(lambda: ms("mssql-python execute(use_prepare=False)", use_prepare=False))
    guarded(lambda: odbc("pyodbc execute()"))


if __name__ == "__main__":
    print("mssql-python", version("mssql-python"), "| pyodbc", version("pyodbc"),
          "| sqlalchemy", version("sqlalchemy"), "| pandas", pd.__version__)
    for fn in (k_hook_rollback, l_to_sql_new_table, m_option_cost, p_shapes_and_drivers):
        guarded(fn)
    print("\nPROBE DONE")
