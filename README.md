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

The `mssql+mssqlpython` dialect requires SQLAlchemy ≥ 2.1.0b2,
which is not yet on PyPI. Install SA from git until then:

```bash
pip install "sqlalchemy @ git+https://github.com/sqlalchemy/sqlalchemy.git@main"
pip install sqlalchemy-mssql-bulkcopy
```

Once SA 2.1 is stable:

```bash
pip install sqlalchemy-mssql-bulkcopy
```

Requires Python ≥ 3.10, SQLAlchemy ≥ 2.1 (beta), mssql-python ≥ 1.4.

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

```python
from sqlalchemy_mssql_bulkcopy import bulkcopy_insert_method

df.to_sql("users", engine, method=bulkcopy_insert_method,
          if_exists="append", index=False)
```

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

CI benchmark — SQL Server 2022, 2-column table (GitHub Actions runner):

| Method                          | 10K rows   | 100K rows  | 500K rows  |
| ------------------------------- | ---------- | ---------- | ---------- |
| `insertmanyvalues` (SA default) | 8.6K       | 8.9K       | 9.0K       |
| `executemany`                   | 53.1K      | 49.6K      | 48.9K      |
| pyodbc `fast_executemany`       | 81.9K      | 82.8K      | 77.6K      |
| pyodbc `insertmanyvalues`       | 132.0K     | 137.8K     | 127.7K     |
| `bulkcopy` (pandas)             | 75.6K      | 144.5K     | 151.4K     |
| `bulkcopy` (hook)               | 102.0K     | 171.1K     | 166.8K     |
| **`bulkcopy` (direct)**         | **119.5K** | **312.3K** | **391.9K** |

At 500K rows, `bulkcopy` (direct) is **44x** faster than the SA default
and **5x** faster than pyodbc's `fast_executemany`.

Full benchmark results (JSON + interactive HTML report) are generated
on every CI run and available as
[workflow artifacts](https://github.com/dxdc/sqlalchemy-mssql-bulkcopy/actions).

## License

MIT
