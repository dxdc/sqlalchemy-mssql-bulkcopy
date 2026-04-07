#!/usr/bin/env python3
"""Test patching the mssqlpython dialect to see what helps.

Variants:
  A. baseline              — unmodified dialect
  B. use_setinputsizes     — patch __init__ to accept and enable it
  C. wo_returning=False    — skip insertmanyvalues when RETURNING not needed
  D. both B + C combined

Run against SQL Server with:
  python tests/dialect_patch_test.py
"""

from __future__ import annotations

import time

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text
from sqlalchemy.dialects.mssql.mssqlpython import MSDialect_mssqlpython

URL = (
    "mssql+mssqlpython://sa:BenchMark!Pass123@localhost/master"
    "?Encrypt=yes&TrustServerCertificate=yes"
)
TABLE = "__dialect_patch_bench"
N = 100_000

# Save originals
_orig_init = MSDialect_mssqlpython.__init__


def _patched_init_sis(self, enable_pooling=False, use_setinputsizes=True, **kw):
    """Patch A: accept and enable use_setinputsizes."""
    _orig_init(self, enable_pooling=enable_pooling, **kw)
    self.use_setinputsizes = use_setinputsizes


def _patched_init_wo_returning(self, enable_pooling=False, **kw):
    """Patch B: disable insertmanyvalues when RETURNING not needed."""
    _orig_init(self, enable_pooling=enable_pooling, **kw)
    self.use_insertmanyvalues_wo_returning = False


def _patched_init_both(self, enable_pooling=False, use_setinputsizes=True, **kw):
    """Patch C: both fixes."""
    _orig_init(self, enable_pooling=enable_pooling, **kw)
    self.use_setinputsizes = use_setinputsizes
    self.use_insertmanyvalues_wo_returning = False


def bench(label: str):
    metadata = MetaData()
    t = Table(
        TABLE,
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("name", String(50)),
    )

    engine = create_engine(URL)
    metadata.create_all(engine)

    # Truncate
    with engine.connect() as conn:
        conn.execute(text(f"TRUNCATE TABLE {TABLE}"))
        conn.commit()

    rows = [{"id": i, "name": f"user_{i:06d}"} for i in range(N)]

    t0 = time.perf_counter()
    with engine.begin() as conn:
        conn.execute(t.insert(), rows)
    elapsed = time.perf_counter() - t0
    rps = round(N / elapsed)

    # Verify
    with engine.connect() as conn:
        got = conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar()

    engine.dispose()
    ok = "OK" if got == N else "FAIL"
    print(f"  {label:40s}  {elapsed:>7.2f}s  {rps:>10,d} rows/s  {ok}")
    return rps


def main():
    print(f"\nDialect patch test — {N:,d} rows, 2 columns\n")
    print(f"  {'Variant':40s}  {'Time':>7s}  {'Rows/s':>10s}  {'OK':>4s}")
    print("  " + "-" * 68)

    # A. Baseline
    MSDialect_mssqlpython.__init__ = _orig_init
    baseline = bench("A. baseline (unmodified)")

    # B. use_setinputsizes=True
    MSDialect_mssqlpython.__init__ = _patched_init_sis
    sis = bench("B. use_setinputsizes=True")

    # C. use_insertmanyvalues_wo_returning=False
    MSDialect_mssqlpython.__init__ = _patched_init_wo_returning
    wo_ret = bench("C. wo_returning=False (uses executemany)")

    # D. Both
    MSDialect_mssqlpython.__init__ = _patched_init_both
    both = bench("D. both (sis + wo_returning=False)")

    # Restore
    MSDialect_mssqlpython.__init__ = _orig_init

    # Cleanup
    engine = create_engine(URL)
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
    engine.dispose()

    print()
    print(f"  setinputsizes effect:   {sis / baseline:.1f}x vs baseline")
    print(f"  wo_returning effect:    {wo_ret / baseline:.1f}x vs baseline")
    print(f"  combined effect:        {both / baseline:.1f}x vs baseline")


if __name__ == "__main__":
    main()
