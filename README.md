# sqlalchemy-mssql-bulkcopy

[![CI](https://github.com/dxdc/sqlalchemy-mssql-bulkcopy/actions/workflows/ci.yml/badge.svg)](https://github.com/dxdc/sqlalchemy-mssql-bulkcopy/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sqlalchemy-mssql-bulkcopy)](https://pypi.org/project/sqlalchemy-mssql-bulkcopy/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Bulk copy for SQLAlchemy + mssql-python — 10–50× faster writes via
SQL Server's native TDS bulk load protocol.

All `cursor.bulkcopy()` kwargs pass through to the driver. When
mssql-python adds new options, this package supports them automatically.

Based on the [event hook approach recommended by SQLAlchemy's maintainer](https://github.com/sqlalchemy/sqlalchemy/issues/13218).

## Install

```bash
pip install sqlalchemy-mssql-bulkcopy
```

Requires Python ≥ 3.11, SQLAlchemy ≥ 2.1 (the first release with the
`mssql+mssqlpython` dialect), mssql-python ≥ 1.4.

## Usage

### Direct (recommended)

No engine configuration needed. Returns the driver result dict.

```python
from sqlalchemy import create_engine
from sqlalchemy_mssql_bulkcopy import bulkcopy

engine = create_engine("mssql+mssqlpython://user:pass@host/db")

with engine.begin() as conn:
    result = bulkcopy(conn, df, "dbo.users", batch_size=50_000)
    print(f"{result['rows_copied']} rows in {result['elapsed_time']:.2f}s")
```

Accepts DataFrames (columns auto-detected), lists of tuples, generators,
or any iterable:

```python
# All driver options pass through as kwargs
result = bulkcopy(
    conn, df, "staging.events",
    batch_size=100_000,
    table_lock=True,
    keep_identity=True,
    check_constraints=True,
    fire_triggers=True,
    keep_nulls=True,
    timeout=300,
    use_internal_transaction=True,
)
```

### Event hook (SA Core / ORM)

Hooks into SQLAlchemy's INSERT workflow. Per-statement opt-in via
execution options:

```python
from sqlalchemy_mssql_bulkcopy import register_bulkcopy

engine = create_engine(
    "mssql+mssqlpython://user:pass@host/db",
    use_insertmanyvalues=False,  # required — see note below
)
register_bulkcopy(engine, batch_size=10_000, table_lock=True)

with engine.begin() as conn:
    conn.execute(
        users.insert().execution_options(use_bulkcopy=True),
        rows,
    )
```

Per-statement overrides use the `bulkcopy_` prefix:

```python
stmt = users.insert().execution_options(
    use_bulkcopy=True,
    bulkcopy_batch_size=100_000,
    bulkcopy_fire_triggers=True,
)
```

> **Note:** `register_bulkcopy()` raises `ValueError` if
> `use_insertmanyvalues` is not disabled. SA 2.x defaults to multi-row
> `INSERT … VALUES` batching which bypasses `executemany()` entirely —
> the event hook never fires without this flag. This is a global engine
> setting: non-bulkcopy inserts will use `executemany()` instead, which
> is slightly slower for medium-sized batches. If that matters, use
> `bulkcopy()` directly instead.

### Pandas

`bulkcopy(conn, df, "dbo.users")` takes a DataFrame directly, with no
engine setup. To keep using `to_sql()`, register the hook (same engine
requirements as above) and pass the insert method:

```python
from sqlalchemy_mssql_bulkcopy import bulkcopy_insert_method, register_bulkcopy

engine = create_engine("mssql+mssqlpython://...", use_insertmanyvalues=False)
register_bulkcopy(engine)

df.to_sql("users", engine, method=bulkcopy_insert_method,
          if_exists="append", index=False)
```

Missing values (`NaN`, `NaT`, `pd.NA`, `None`) load as `NULL` either way.

## How bulk copy differs from INSERT

**Defaults match INSERT.** SQL Server's bulk copy normally replaces an
explicit `NULL` with the column's `DEFAULT`, accepts rows that break
CHECK and FOREIGN KEY constraints (and then marks those constraints
untrusted), and skips INSERT triggers. This package passes
`keep_nulls=True`, `check_constraints=True` and `fire_triggers=True` by
default so a bulk load stores what an INSERT would. Pass `False` for any
of them to trade that for speed (checking constraints cost about a
quarter of the throughput on a table with a foreign key and a CHECK).

**Not part of your transaction.** mssql-python runs each bulk copy on
its own new connection, which commits on its own. So:

- rolling back the surrounding SQLAlchemy transaction does **not**
  remove bulk-copied rows (this applies to the event hook too);
- if your transaction has uncommitted work on the target table
  (`TRUNCATE`, `CREATE TABLE`, an INSERT with `table_lock=True`), the
  bulk copy waits on those locks until it times out;
- `#temp` tables aren't visible to it.

Commit any setup first. For `to_sql()`, create the table beforehand and
use `if_exists="append"`: when `to_sql()` creates or replaces the table
itself, that happens inside its own transaction, so the hook times out
(30 s by default) and falls back to a regular INSERT.

**IDENTITY columns:** to load your own values into an IDENTITY column,
pass `keep_identity=True`. Without it the driver refuses the column
("Destination column ... not found").

**Small batches:** each call opens a connection and reads the table's
metadata, about 8 ms of fixed cost on a local server. Bulk copy
overtakes `executemany()` at around 2,000 rows per call; below that,
a regular INSERT is faster.

**Timeout:** the driver's default `timeout` is 30 seconds for the whole
load. Pass `timeout=0` (no limit) or a larger value for big loads.

## Confirming bulkcopy was used

```python
# Direct — return value
result = bulkcopy(conn, df, "dbo.users")
assert result["rows_copied"] == len(df)

# Event hook — on_complete callback
results = []
register_bulkcopy(engine, on_complete=lambda t, r: results.append(r))
```

Both approaches log at INFO level:
`bulkcopy: 1000 rows -> dbo.users (0.050s)`

## Performance

CI benchmark — SQL Server 2022, 2-column table (GitHub Actions runner;
SQLAlchemy 2.1.8, mssql-python 1.17.0), rows per second:

| Method                          | 10K rows   | 100K rows  | 500K rows  |
| ------------------------------- | ---------- | ---------- | ---------- |
| `insertmanyvalues` (SA default) | 16.5K      | 17.3K      | 17.2K      |
| `executemany`                   | 95.2K      | 81.5K      | 77.3K      |
| pyodbc `fast_executemany`       | 120.4K     | 122.5K     | 118.6K     |
| pyodbc `insertmanyvalues`       | 176.2K     | 171.6K     | 161.0K     |
| `bulkcopy` (pandas)             | 141.9K     | 121.4K     | 179.9K     |
| `bulkcopy` (hook)               | 232.2K     | 230.2K     | 223.7K     |
| **`bulkcopy` (direct)**         | **316.7K** | **434.4K** | **481.6K** |

At 500K rows, `bulkcopy` (direct) is **28x** faster than the SA default
and **4x** faster than pyodbc's `fast_executemany`.

**Tip for regular inserts:** mssql-python declares the `pyformat`
parameter style, so SQLAlchemy sends `%(name)s` placeholders that the
driver rewrites to `?` in Python on every call. That rewrite is what
holds the SA default near 17K rows/s. Asking SQLAlchemy for `qmark`
skips it, about 8x faster in CI (138K vs 17K rows/s for 100K rows, on
par with pyodbc), and works with everything in this package:

```python
engine = create_engine("mssql+mssqlpython://...", paramstyle="qmark")
```

Full benchmark results (JSON + interactive HTML report) are generated
on every CI run and available as
[workflow artifacts](https://github.com/dxdc/sqlalchemy-mssql-bulkcopy/actions).

## License

MIT
