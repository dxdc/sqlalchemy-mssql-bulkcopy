#!/usr/bin/env python3
"""Throwaway probe, round 3: does paramstyle="qmark" fix mssql-python's slow SA inserts?"""

from __future__ import annotations

import time
import traceback
from importlib.metadata import version

import pandas as pd
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text

from sqlalchemy_mssql_bulkcopy import bulkcopy_insert_method, register_bulkcopy

PWD = "BenchMark!Pass123"
Q = "Encrypt=yes&TrustServerCertificate=yes"
URL = f"mssql+mssqlpython://sa:{PWD}@localhost/master?{Q}"
PYODBC_URL = f"mssql+pyodbc://sa:{PWD}@localhost/master?driver=ODBC+Driver+18+for+SQL+Server&{Q}"

md = MetaData()
t = Table("p_q", md, Column("id", Integer, primary_key=True, autoincrement=False),
          Column("name", String(20)), schema="dbo")
N = 100_000
rows = [{"id": i, "name": f"user_{i:06d}"} for i in range(N)]
base = create_engine(URL)
md.drop_all(base)
md.create_all(base)


def count():
    with base.connect() as c:
        return c.execute(text("SELECT COUNT(*) FROM dbo.p_q")).scalar()


def trunc():
    with base.begin() as c:
        c.execute(text("TRUNCATE TABLE dbo.p_q"))


def run(label, url, **kw):
    eng = create_engine(url, **kw)
    best = None
    for _ in range(2):
        trunc()
        t0 = time.perf_counter()
        with eng.begin() as conn:
            conn.execute(t.insert(), rows)
        el = time.perf_counter() - t0
        best = el if best is None else min(best, el)
    ok = count() == N
    print(f"  {label:<52s} {N / best:>9,.0f} rows/s  {'OK' if ok else 'ROW COUNT WRONG'}  (paramstyle={eng.dialect.paramstyle})")
    eng.dispose()


print("mssql-python", version("mssql-python"), "| sqlalchemy", version("sqlalchemy"))
print(f"\n=== SA insert of {N:,} rows, best of 2")
for args in (
    ("mssql-python, SA default (insertmanyvalues)", URL, {}),
    ("mssql-python, paramstyle='qmark'", URL, {"paramstyle": "qmark"}),
    ("mssql-python, executemany", URL, {"use_insertmanyvalues": False}),
    ("mssql-python, executemany + paramstyle='qmark'", URL, {"use_insertmanyvalues": False, "paramstyle": "qmark"}),
    ("pyodbc, SA default (insertmanyvalues)", PYODBC_URL, {}),
):
    try:
        run(*args[:2], **args[2])
    except Exception:
        traceback.print_exc()

print("\n=== bulkcopy hook + to_sql still work with paramstyle='qmark'")
try:
    eng = create_engine(URL, use_insertmanyvalues=False, paramstyle="qmark")
    fired = []
    register_bulkcopy(eng, on_complete=lambda tb, r: fired.append(r))
    trunc()
    with eng.begin() as conn:
        conn.execute(t.insert().execution_options(use_bulkcopy=True), rows[:5000])
    hook_ok = count() == 5000
    trunc()
    pd.DataFrame(rows[:5000]).to_sql("p_q", eng, schema="dbo", if_exists="append", index=False,
                                     method=bulkcopy_insert_method)
    print(f"  hook: {'OK' if hook_ok else 'WRONG'}, to_sql: {'OK' if count() == 5000 else 'WRONG'},"
          f" bulkcopy calls: {len(fired)}")
except Exception:
    traceback.print_exc()
print("\nPROBE DONE")
