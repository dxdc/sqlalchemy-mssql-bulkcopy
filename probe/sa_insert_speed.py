"""Throwaway: SQLAlchemy insert speed on mssql-python (default pyformat vs qmark)."""

import sys
import time

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text

URL = (
    "mssql+mssqlpython://sa:BenchMark!Pass123@localhost/master"
    "?Encrypt=yes&TrustServerCertificate=yes"
)
N = 100_000
md = MetaData()
t = Table("p_speed", md, Column("id", Integer, primary_key=True, autoincrement=False),
          Column("name", String(20)), schema="dbo")
rows = [{"id": i, "name": f"user_{i:06d}"} for i in range(N)]
base = create_engine(URL)
md.drop_all(base)
md.create_all(base)
for label, kw in (("SA default (pyformat)", {}), ("SA paramstyle='qmark'", {"paramstyle": "qmark"})):
    eng = create_engine(URL, **kw)
    best = None
    for _ in range(2):
        with base.begin() as c:
            c.execute(text("TRUNCATE TABLE dbo.p_speed"))
        t0 = time.perf_counter()
        with eng.begin() as conn:
            conn.execute(t.insert(), rows)
        el = time.perf_counter() - t0
        best = el if best is None else min(best, el)
    with base.connect() as c:
        ok = c.execute(text("SELECT COUNT(*) FROM dbo.p_speed")).scalar() == N
    print(f"[{sys.argv[1]}] {label:<24s} {N / best:>9,.0f} rows/s {'OK' if ok else 'WRONG COUNT'}")
    eng.dispose()
md.drop_all(base)
