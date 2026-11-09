#!/usr/bin/env python3
"""Probe: how do pandas missing values and dtypes survive each insert path?

Writes the same DataFrame (one full row, one all-missing row, one mixed)
through every path and compares what lands in SQL Server against a plain
INSERT baseline. Reports only; never exits non-zero.
"""

from __future__ import annotations

import decimal
import logging
import traceback

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from sqlalchemy_mssql_bulkcopy import bulkcopy, bulkcopy_insert_method, register_bulkcopy

URL = (
    "mssql+mssqlpython://sa:BenchMark!Pass123@localhost/master"
    "?Encrypt=yes&TrustServerCertificate=yes"
)
TABLE = "__bcp_nulls_probe"
DDL = f"""
CREATE TABLE dbo.{TABLE} (
    id   INT NOT NULL PRIMARY KEY,
    f    FLOAT NULL,
    i    BIGINT NULL,
    s    NVARCHAR(20) NULL,
    ss   NVARCHAR(20) NULL,
    ts   DATETIME2 NULL,
    b    BIT NULL,
    d    DECIMAL(10, 2) NULL,
    dflt INT NULL CONSTRAINT df_probe_dflt DEFAULT 42
)
"""

logging.basicConfig(level=logging.WARNING, format="    log %(levelname)s %(name)s: %(message)s")


def make_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": [1, 2, 3],
            "f": [1.5, np.nan, None],  # float64: NaN, NaN
            "i": pd.array([7, pd.NA, 9], dtype="Int64"),  # nullable int: pd.NA
            "s": ["a", None, np.nan],  # object: None and float NaN
            "ss": pd.array(["x", pd.NA, "z"], dtype="string"),  # string dtype: pd.NA
            "ts": pd.to_datetime(["2026-01-02 03:04:05", None, "2026-03-04 00:00:00"]),  # NaT
            "b": pd.array([True, pd.NA, False], dtype="boolean"),  # pd.NA
            "d": [decimal.Decimal("1.25"), None, decimal.Decimal("3.50")],
            "dflt": pd.array([5, pd.NA, pd.NA], dtype="Int64"),  # NULL into a DEFAULT column
        }
    )


def fetch(engine) -> list[tuple]:
    with engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(text(f"SELECT * FROM dbo.{TABLE} ORDER BY id")).fetchall()
        ]


def reset(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"IF OBJECT_ID('dbo.{TABLE}') IS NOT NULL DROP TABLE dbo.{TABLE}"))
        conn.execute(text(DDL))


def run(label, engine, fn, baseline):
    print(f"\n=== {label}")
    reset(engine)
    fired: list[dict] = []
    try:
        fn(fired)
    except Exception as exc:
        msg = " ".join(str(exc).split())[:300]
        print(f"  ERROR {type(exc).__name__}: {msg}")
        tb = traceback.format_exc().strip().splitlines()
        print(f"  at: {tb[-3].strip() if len(tb) >= 3 else ''}")
        return None
    rows = fetch(engine)
    print(f"  bulkcopy actually used: {fired if fired else 'NO (fell back or n/a)'}")
    for r in rows:
        print(f"  {r!r}")
    if baseline is not None:
        if rows == baseline:
            print("  => MATCHES plain INSERT baseline")
        else:
            for got, want in zip(rows, baseline, strict=False):
                if got != want:
                    print(f"  => DIFF id={got[0]}: got {got!r}")
                    print(f"           want {want!r}")
            if len(rows) != len(baseline):
                print(f"  => DIFF row count {len(rows)} vs {len(baseline)}")
    return rows


def main() -> None:
    df = make_df()
    print("pandas", pd.__version__, "| dtypes:", dict(df.dtypes.astype(str)))
    print("row 2 as itertuples:", next(df.iloc[[1]].itertuples(index=False, name=None)))

    engine = create_engine(URL)

    # Baseline: ordinary to_sql (multi-row INSERT, no bulkcopy)
    baseline = run(
        "baseline: df.to_sql() plain INSERT",
        engine,
        lambda fired: df.to_sql(TABLE, engine, schema="dbo", if_exists="append", index=False),
        None,
    )

    def direct(**kw):
        def _fn(fired):
            with engine.begin() as conn:
                fired.append(bulkcopy(conn, df, f"dbo.{TABLE}", **kw))

        return _fn

    run("bulkcopy(conn, df) direct", engine, direct(), baseline)
    run("bulkcopy(conn, df, keep_nulls=True) direct", engine, direct(keep_nulls=True), baseline)

    def via_hook(**defaults):
        def _fn(fired):
            eng = create_engine(URL, use_insertmanyvalues=False)
            register_bulkcopy(eng, on_complete=lambda t, r: fired.append(r), **defaults)
            try:
                df.to_sql(
                    TABLE,
                    eng,
                    schema="dbo",
                    if_exists="append",
                    index=False,
                    method=bulkcopy_insert_method,
                )
            finally:
                eng.dispose()

        return _fn

    run("df.to_sql(method=bulkcopy_insert_method) hook", engine, via_hook(), baseline)
    run(
        "df.to_sql(method=bulkcopy_insert_method) hook, keep_nulls=True",
        engine,
        via_hook(keep_nulls=True),
        baseline,
    )

    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE dbo.{TABLE}"))
    engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
