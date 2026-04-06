"""SQLAlchemy integration for mssql-python's ``cursor.bulkcopy()``.

Uses SQL Server's native TDS bulk load protocol for 10-50x faster writes.
All ``cursor.bulkcopy()`` kwargs pass through to the driver unchanged.

Primary API::

    from sqlalchemy_mssql_bulkcopy import bulkcopy

    with engine.begin() as conn:
        result = bulkcopy(conn, df, "dbo.users", batch_size=50_000)

Event hook for SA Core/ORM::

    from sqlalchemy_mssql_bulkcopy import register_bulkcopy

    engine = create_engine("...", use_insertmanyvalues=False)
    register_bulkcopy(engine)
    conn.execute(
        users.insert().execution_options(use_bulkcopy=True), rows
    )

See https://github.com/sqlalchemy/sqlalchemy/issues/13218
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from importlib.metadata import version as _pkg_version
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

__version__ = _pkg_version(__name__)
__all__ = [
    "bulkcopy",
    "bulkcopy_insert_method",
    "register_bulkcopy",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Primary API: direct bulkcopy
# ---------------------------------------------------------------------------


def bulkcopy(
    conn: Connection,
    data: Any,
    table_name: str,
    *,
    columns: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Bulk-load data into SQL Server via the TDS bulk copy protocol.

    Accesses the raw DBAPI cursor from *conn* and calls
    ``cursor.bulkcopy()`` directly.  No engine configuration required.
    All keyword arguments pass through to the driver unchanged.

    Parameters
    ----------
    conn : sqlalchemy.engine.Connection
        An active SA connection (from ``engine.connect()`` or
        ``engine.begin()``).
    data : DataFrame | Iterable[tuple | list]
        Rows to load.  DataFrames are auto-converted; column names
        are extracted unless *columns* is provided explicitly.
    table_name : str
        Target table, optionally schema-qualified (``"dbo.users"``).
    columns : list[str] | None
        Column names passed as ``column_mappings`` to the driver.
        Auto-detected from DataFrames.
    **kwargs
        Forwarded to ``cursor.bulkcopy()``.  See mssql-python docs
        for the full list: ``batch_size``, ``table_lock``,
        ``keep_identity``, ``check_constraints``, ``fire_triggers``,
        ``keep_nulls``, ``timeout``, ``use_internal_transaction``.

    Returns
    -------
    dict[str, Any]
        Driver result: ``rows_copied``, ``batch_count``,
        ``elapsed_time``.

    Raises
    ------
    AttributeError
        If the cursor does not have a ``bulkcopy`` method (wrong
        driver or driver version < 1.4).
    """
    dbapi_conn = conn.connection.dbapi_connection
    rows, columns = _normalize_data(data, columns)

    if not rows:
        return {"rows_copied": 0, "batch_count": 0, "elapsed_time": 0.0}

    if columns is not None:
        kwargs["column_mappings"] = columns

    cursor = dbapi_conn.cursor()
    try:
        result: dict[str, Any] = cursor.bulkcopy(table_name, rows, **kwargs)
    finally:
        cursor.close()

    logger.info(
        "bulkcopy: %d rows -> %s (%.3fs)",
        result.get("rows_copied", len(rows)),
        table_name,
        result.get("elapsed_time", 0),
    )
    return result


# ---------------------------------------------------------------------------
# Event hook for SA Core / ORM
# ---------------------------------------------------------------------------


def register_bulkcopy(
    engine: Engine,
    *,
    on_complete: Callable[[str, dict[str, Any]], Any] | None = None,
    **defaults: Any,
) -> None:
    """Register a ``do_executemany`` event hook that routes INSERT
    batches through ``cursor.bulkcopy()`` when
    ``execution_options(use_bulkcopy=True)`` is set.

    Parameters
    ----------
    engine : Engine
        **Must** be created with ``use_insertmanyvalues=False``.
        SA 2.x defaults to multi-row ``INSERT … VALUES`` batching
        which bypasses ``executemany()`` entirely; without this flag
        the event hook will never fire.
    on_complete : callable | None
        ``on_complete(table_name, result_dict)`` is called after each
        successful bulkcopy.  Errors in the callback propagate to the
        caller (they do **not** trigger the executemany fallback).
    **defaults
        Default kwargs forwarded to ``cursor.bulkcopy()``.
        Overridable per-statement via
        ``execution_options(bulkcopy_<key>=value)``.

    Raises
    ------
    ValueError
        If the engine's dialect has ``use_insertmanyvalues`` enabled.
    """
    if getattr(engine.dialect, "use_insertmanyvalues", False):
        raise ValueError(
            "register_bulkcopy() requires use_insertmanyvalues=False. "
            "SA 2.x defaults to multi-row INSERT batching which "
            "bypasses executemany() — the event hook will never fire. "
            "Create the engine with: "
            "create_engine(..., use_insertmanyvalues=False)  "
            "This does not affect bulkcopy(); only register_bulkcopy() "
            "has this requirement."
        )

    @event.listens_for(engine, "do_executemany", retval=True)
    def _do_bulkcopy(
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
    ) -> bool:
        opts = context.execution_options
        if not opts.get("use_bulkcopy", False):
            return False

        if not hasattr(cursor, "bulkcopy"):
            logger.debug(
                "use_bulkcopy set but cursor has no bulkcopy method; falling back to executemany"
            )
            return False

        try:
            table = context.compiled.statement.table
        except AttributeError:
            logger.debug(
                "use_bulkcopy set but no table metadata on compiled "
                "statement; falling back to executemany"
            )
            return False

        # Schema-qualified table name
        schema = table.schema or context.dialect.default_schema_name
        table_name = f"{schema}.{table.name}" if schema else table.name

        # Map SA column keys → DB column names (key != name when
        # Column("db_name", key="python_name") is used)
        col_names = [table.c[key].name for key in context.compiled.column_keys]

        # SA sends tuples for qmark paramstyle (mssql-python) and
        # dicts for named paramstyle.  Handle both.
        if parameters and isinstance(parameters[0], dict):
            rows = [tuple(row[key] for key in context.compiled.column_keys) for row in parameters]
        else:
            rows = [tuple(row) for row in parameters]

        # Engine defaults ← per-statement overrides (bulkcopy_ prefix)
        bcp_kwargs: dict[str, Any] = {**defaults}
        bcp_kwargs["column_mappings"] = col_names
        for key, value in opts.items():
            if key.startswith("bulkcopy_"):
                bcp_kwargs[key.removeprefix("bulkcopy_")] = value

        try:
            result = cursor.bulkcopy(table_name, rows, **bcp_kwargs)
        except Exception:
            logger.warning(
                "bulkcopy failed for %s; falling back to executemany",
                table_name,
                exc_info=True,
            )
            return False

        logger.info(
            "bulkcopy: %d rows -> %s (%d cols)",
            len(rows),
            table_name,
            len(col_names),
        )

        # Callback runs outside try/except so errors propagate
        # to the caller instead of silently triggering fallback.
        if on_complete is not None:
            on_complete(table_name, result)

        return True


# ---------------------------------------------------------------------------
# Pandas helper
# ---------------------------------------------------------------------------


def bulkcopy_insert_method(
    table: Any,
    conn: Any,
    keys: list[str],
    data_iter: Iterable[tuple[Any, ...]],
) -> None:
    """``method`` callable for :meth:`~pandas.DataFrame.to_sql`.

    Requires :func:`register_bulkcopy` on the engine::

        df.to_sql("users", engine, method=bulkcopy_insert_method,
                  if_exists="append", index=False)
    """
    from sqlalchemy import insert as sa_insert

    data = [dict(zip(keys, row, strict=True)) for row in data_iter]
    if not data:
        return

    sa_table = getattr(table, "table", table)
    stmt = sa_insert(sa_table).execution_options(use_bulkcopy=True)
    conn.execute(stmt, data)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_data(
    data: Any,
    columns: list[str] | None,
) -> tuple[list[tuple[Any, ...]], list[str] | None]:
    """Convert input to ``(list_of_tuples, columns)``.

    Handles DataFrames, generators, lists-of-lists, and plain tuples.
    Extracts column names from DataFrames when *columns* is ``None``.
    """
    try:
        import pandas as pd

        if isinstance(data, pd.DataFrame):
            if columns is None:
                columns = list(data.columns)
            return list(data.itertuples(index=False, name=None)), columns
    except ImportError:
        pass

    if not isinstance(data, list):
        data = list(data)

    if not data:
        return data, columns

    if not isinstance(data[0], tuple):
        return [tuple(row) for row in data], columns

    return data, columns
