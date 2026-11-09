# sqlalchemy-mssql-bulkcopy

[![CI](https://github.com/dxdc/sqlalchemy-mssql-bulkcopy/actions/workflows/ci.yml/badge.svg)](https://github.com/dxdc/sqlalchemy-mssql-bulkcopy/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sqlalchemy-mssql-bulkcopy)](https://pypi.org/project/sqlalchemy-mssql-bulkcopy/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Bulk copy for SQLAlchemy + mssql-python: load large datasets through
SQL Server's native TDS bulk load protocol, 2-3x faster than
SQLAlchemy's fastest INSERT path (see [Performance](#performance)).

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
> setting: non-bulkcopy inserts will use `executemany()` instead, about
> half the speed of the default INSERT path with `paramstyle="qmark"`.
> If that matters, use `bulkcopy()` directly instead.

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

CI benchmark — SQL Server 2022, 2-column table, GitHub Actions runner,
SQLAlchemy 2.1.8 with its native `mssql+mssqlpython` dialect,
mssql-python 1.17.0. Rows per second:

| Method                                       | 10K rows   | 100K rows  | 500K rows  | In your transaction? |
| -------------------------------------------- | ---------- | ---------- | ---------- | -------------------- |
| SA `insertmanyvalues` (default)              | 19.2K      | 19.0K      | 20.9K      | yes                  |
| SA `executemany`                             | 46.2K      | 68.5K      | 72.9K      | yes                  |
| pyodbc `fast_executemany`                    | 123.7K     | 113.0K     | 110.9K     | yes                  |
| pyodbc `insertmanyvalues`                    | 135.1K     | 133.0K     | 121.9K     | yes                  |
| SA `insertmanyvalues`, `paramstyle="qmark"`  | 120.9K     | 139.0K     | 172.3K     | yes                  |
| `bulkcopy` (hook)                            | 146.0K     | 135.1K     | 170.8K     | **no**               |
| `bulkcopy` (pandas `to_sql`)                 | 199.0K     | 156.7K     | 171.3K     | **no**               |
| **`bulkcopy` (direct)**                      | **339.3K** | **403.5K** | **411.8K** | **no**               |

At 500K rows, `bulkcopy()` (direct) is **2.4x** faster than SQLAlchemy
with `paramstyle="qmark"` and **20x** faster than SQLAlchemy's default.
Numbers vary run to run by about ±20%.

**Which to use:**

- **Everyday inserts, or anything that must roll back with your
  transaction:** plain SQLAlchemy with `paramstyle="qmark"` (below). At
  100K+ rows it matches the event hook and `to_sql` path, and it stays
  transactional. Plain `df.to_sql()` uses the same INSERT path.
- **Large loads where a separate commit is fine** (staging tables,
  ETL, reloads): `bulkcopy()` directly, 2-3x faster again. See
  [How bulk copy differs from INSERT](#how-bulk-copy-differs-from-insert).
- **The event hook and `to_sql` method** pay SQLAlchemy's per-row
  processing before the bulk copy starts, so they end up close to the
  `qmark` INSERT path while giving up transactional behavior. They
  mostly help around 10K rows per call.

**Why `paramstyle="qmark"`:** mssql-python declares the `pyformat`
parameter style, so SQLAlchemy sends `%(name)s` placeholders that the
driver rewrites to `?` in Python on every call. On a 1,000-row
multi-row INSERT that rewrite takes about 50 ms, which is what holds
SQLAlchemy's default near 20K rows/s. Asking SQLAlchemy for `qmark`
skips it (the values are still sent as bound parameters), and it works
with everything in this package:

```python
engine = create_engine("mssql+mssqlpython://...", paramstyle="qmark")
```

Full benchmark results (JSON + interactive HTML report) are generated
on every CI run and available as
[workflow artifacts](https://github.com/dxdc/sqlalchemy-mssql-bulkcopy/actions).

## License

MIT
