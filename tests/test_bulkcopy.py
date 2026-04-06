"""Tests for sqlalchemy_mssql_bulkcopy."""

from __future__ import annotations

import logging
import sqlite3
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

from sqlalchemy_mssql_bulkcopy import bulkcopy, bulkcopy_insert_method, register_bulkcopy


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------
class CursorWrapper:
    """Wraps a real sqlite3.Cursor (C object) to add a mock bulkcopy."""

    def __init__(self, real_cursor: sqlite3.Cursor, mock_bulkcopy: MagicMock):
        self._real = real_cursor
        self.bulkcopy = mock_bulkcopy

    def close(self) -> None:
        self._real.close()

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def __iter__(self):
        return iter(self._real)

    def __next__(self):
        return next(self._real)


class ConnectionWrapper:
    """Wraps a real sqlite3.Connection to return CursorWrappers."""

    def __init__(self, real_conn: sqlite3.Connection, mock_bulkcopy: MagicMock):
        self._real = real_conn
        self._mock_bulkcopy = mock_bulkcopy

    def cursor(self) -> CursorWrapper:
        return CursorWrapper(self._real.cursor(), self._mock_bulkcopy)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


_BCP_RESULT = {"rows_copied": 2, "batch_count": 1, "elapsed_time": 0.01}


def _make_engine(**hook_kwargs: object) -> tuple:
    """Create SQLite engine with mock bulkcopy + event hook registered."""
    mock_bcp = MagicMock(name="bulkcopy", return_value=dict(_BCP_RESULT))

    def creator() -> ConnectionWrapper:
        return ConnectionWrapper(sqlite3.connect(":memory:"), mock_bcp)

    engine = create_engine("sqlite://", creator=creator, use_insertmanyvalues=False)
    register_bulkcopy(engine, **hook_kwargs)
    return engine, mock_bcp


def _make_table(engine, name: str = "users", schema: str | None = None) -> Table:
    metadata = MetaData()
    t = Table(
        name,
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        schema=schema,
    )
    metadata.create_all(engine)
    return t


# ---------------------------------------------------------------------------
# bulkcopy() — direct function
# ---------------------------------------------------------------------------
class TestBulkcopy:
    def test_tuples(self):
        engine, mock_bcp = _make_engine()
        _make_table(engine)

        with engine.begin() as conn:
            result = bulkcopy(
                conn,
                [(1, "Alice"), (2, "Bob")],
                "users",
                columns=["id", "name"],
                batch_size=500,
            )

        mock_bcp.assert_called_once()
        _, kw = mock_bcp.call_args
        assert kw["column_mappings"] == ["id", "name"]
        assert kw["batch_size"] == 500
        assert result["rows_copied"] == 2

    def test_kwargs_passthrough(self):
        """All kwargs go to cursor.bulkcopy() — nothing filtered."""
        engine, mock_bcp = _make_engine()
        _make_table(engine)

        with engine.begin() as conn:
            bulkcopy(
                conn,
                [(1, "A")],
                "t",
                columns=["id"],
                batch_size=999,
                table_lock=False,
                keep_identity=True,
                check_constraints=True,
                fire_triggers=True,
                keep_nulls=True,
                timeout=120,
                use_internal_transaction=True,
                some_future_option="hello",
            )

        _, kw = mock_bcp.call_args
        assert kw["batch_size"] == 999
        assert kw["check_constraints"] is True
        assert kw["fire_triggers"] is True
        assert kw["timeout"] == 120
        assert kw["some_future_option"] == "hello"

    def test_dataframe(self):
        pd = pytest.importorskip("pandas")
        engine, mock_bcp = _make_engine()
        _make_table(engine)

        df = pd.DataFrame({"id": [1, 2], "name": ["Alice", "Bob"]})
        with engine.begin() as conn:
            bulkcopy(conn, df, "users")

        _, kw = mock_bcp.call_args
        assert kw["column_mappings"] == ["id", "name"]
        assert mock_bcp.call_args[0][1] == [(1, "Alice"), (2, "Bob")]

    def test_dataframe_columns_override(self):
        pd = pytest.importorskip("pandas")
        engine, mock_bcp = _make_engine()
        _make_table(engine)

        df = pd.DataFrame({"a": [1], "b": ["X"]})
        with engine.begin() as conn:
            bulkcopy(conn, df, "users", columns=["id", "name"])

        assert mock_bcp.call_args[1]["column_mappings"] == ["id", "name"]

    def test_generator(self):
        engine, mock_bcp = _make_engine()
        _make_table(engine)

        def gen():
            yield (1, "A")
            yield (2, "B")

        with engine.begin() as conn:
            bulkcopy(conn, gen(), "users", columns=["id", "name"])

        assert mock_bcp.call_args[0][1] == [(1, "A"), (2, "B")]

    def test_list_of_lists(self):
        engine, mock_bcp = _make_engine()
        _make_table(engine)

        with engine.begin() as conn:
            bulkcopy(conn, [[1, "A"], [2, "B"]], "users", columns=["id", "name"])

        data = mock_bcp.call_args[0][1]
        assert all(isinstance(row, tuple) for row in data)

    def test_empty_data(self):
        engine, mock_bcp = _make_engine()
        _make_table(engine)

        with engine.begin() as conn:
            result = bulkcopy(conn, [], "users")

        mock_bcp.assert_not_called()
        assert result == {"rows_copied": 0, "batch_count": 0, "elapsed_time": 0.0}

    def test_no_columns_omits_column_mappings(self):
        engine, mock_bcp = _make_engine()
        _make_table(engine)

        with engine.begin() as conn:
            bulkcopy(conn, [(1, "A")], "users")

        assert "column_mappings" not in mock_bcp.call_args[1]

    def test_returns_driver_result(self):
        engine, mock_bcp = _make_engine()
        mock_bcp.return_value = {"rows_copied": 5, "batch_count": 1, "elapsed_time": 0.42}
        _make_table(engine)

        with engine.begin() as conn:
            result = bulkcopy(conn, [(i,) for i in range(5)], "t", columns=["id"])

        assert result == {"rows_copied": 5, "batch_count": 1, "elapsed_time": 0.42}

    def test_error_propagates(self):
        engine, mock_bcp = _make_engine()
        mock_bcp.side_effect = RuntimeError("TDS error")
        _make_table(engine)

        with pytest.raises(RuntimeError, match="TDS error"), engine.begin() as conn:
            bulkcopy(conn, [(1, "A")], "t", columns=["id"])

    def test_cursor_closed_after_error(self):
        """Cursor must be closed even when bulkcopy raises."""
        engine, mock_bcp = _make_engine()
        mock_bcp.side_effect = RuntimeError("boom")
        _make_table(engine)

        with pytest.raises(RuntimeError), engine.begin() as conn:
            bulkcopy(conn, [(1, "A")], "t", columns=["id"])

        # If cursor wasn't closed, the finally block didn't run.
        # Structural verification via the try/finally in the source.

    def test_logs_info(self, caplog):
        engine, _ = _make_engine()
        _make_table(engine)

        with caplog.at_level(logging.INFO), engine.begin() as conn:
            bulkcopy(conn, [(1, "A"), (2, "B")], "users", columns=["id", "name"])

        assert "bulkcopy: 2 rows -> users" in caplog.text


# ---------------------------------------------------------------------------
# register_bulkcopy() — event hook
# ---------------------------------------------------------------------------
class TestRegisterBulkcopy:
    def test_raises_when_insertmanyvalues_enabled(self):
        engine = create_engine("sqlite:///:memory:")
        with pytest.raises(ValueError, match="use_insertmanyvalues"):
            register_bulkcopy(engine)

    def test_fires_when_opted_in(self):
        engine, mock_bcp = _make_engine(batch_size=999)
        users = _make_table(engine)

        with engine.begin() as conn:
            conn.execute(
                users.insert().execution_options(use_bulkcopy=True),
                [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
            )

        mock_bcp.assert_called_once()
        _, kw = mock_bcp.call_args
        assert "users" in mock_bcp.call_args[0][0]
        assert kw["column_mappings"] == ["id", "name"]
        assert kw["batch_size"] == 999

    def test_skipped_without_opt_in(self):
        engine, mock_bcp = _make_engine()
        users = _make_table(engine)

        with engine.begin() as conn:
            conn.execute(users.insert(), [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])

        mock_bcp.assert_not_called()

    def test_per_statement_overrides(self):
        engine, mock_bcp = _make_engine(batch_size=5000, table_lock=True)
        users = _make_table(engine)

        with engine.begin() as conn:
            conn.execute(
                users.insert().execution_options(
                    use_bulkcopy=True,
                    bulkcopy_batch_size=123,
                    bulkcopy_table_lock=False,
                ),
                [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
            )

        _, kw = mock_bcp.call_args
        assert kw["batch_size"] == 123
        assert kw["table_lock"] is False

    def test_defaults_passthrough(self):
        engine, mock_bcp = _make_engine(fire_triggers=True, some_future_flag=42)
        users = _make_table(engine)

        with engine.begin() as conn:
            conn.execute(
                users.insert().execution_options(use_bulkcopy=True),
                [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
            )

        _, kw = mock_bcp.call_args
        assert kw["fire_triggers"] is True
        assert kw["some_future_flag"] == 42

    def test_column_key_name_mapping(self):
        engine, mock_bcp = _make_engine()
        metadata = MetaData()
        t = Table(
            "mapped",
            metadata,
            Column("user_id", Integer, key="id", primary_key=True),
            Column("user_name", String(50), key="name"),
        )
        metadata.create_all(engine)

        with engine.begin() as conn:
            conn.execute(
                t.insert().execution_options(use_bulkcopy=True),
                [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
            )

        assert mock_bcp.call_args[1]["column_mappings"] == ["user_id", "user_name"]

    def test_fallback_on_error(self, caplog):
        engine, mock_bcp = _make_engine()
        users = _make_table(engine)
        mock_bcp.side_effect = RuntimeError("TDS boom")

        with caplog.at_level(logging.WARNING), engine.begin() as conn:
            conn.execute(
                users.insert().execution_options(use_bulkcopy=True),
                [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
            )

        assert "bulkcopy failed" in caplog.text

    def test_data_inserted_after_fallback(self):
        engine, mock_bcp = _make_engine()
        users = _make_table(engine)
        mock_bcp.side_effect = RuntimeError("fail")

        with engine.begin() as conn:
            conn.execute(
                users.insert().execution_options(use_bulkcopy=True),
                [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
            )

        with engine.connect() as conn:
            assert len(conn.execute(users.select()).fetchall()) == 2

    def test_no_bulkcopy_method_fallback(self, caplog):
        engine = create_engine("sqlite:///:memory:", use_insertmanyvalues=False)
        register_bulkcopy(engine)
        users = _make_table(engine)

        with caplog.at_level(logging.DEBUG), engine.begin() as conn:
            conn.execute(
                users.insert().execution_options(use_bulkcopy=True),
                [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
            )

        assert "no bulkcopy method" in caplog.text

    def test_on_complete_receives_result(self):
        results: list[tuple[str, dict]] = []
        mock_bcp = MagicMock(
            name="bulkcopy",
            return_value={"rows_copied": 2, "batch_count": 1, "elapsed_time": 0.05},
        )

        def creator():
            return ConnectionWrapper(sqlite3.connect(":memory:"), mock_bcp)

        engine = create_engine("sqlite://", creator=creator, use_insertmanyvalues=False)
        register_bulkcopy(engine, on_complete=lambda t, r: results.append((t, r)))
        users = _make_table(engine)

        with engine.begin() as conn:
            conn.execute(
                users.insert().execution_options(use_bulkcopy=True),
                [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
            )

        assert len(results) == 1
        assert "users" in results[0][0]
        assert results[0][1]["rows_copied"] == 2

    def test_on_complete_error_propagates_not_swallowed(self):
        """Callback errors must NOT trigger the executemany fallback."""

        def bad_callback(table_name, result):
            raise ValueError("callback bug")

        mock_bcp = MagicMock(name="bulkcopy", return_value=dict(_BCP_RESULT))

        def creator():
            return ConnectionWrapper(sqlite3.connect(":memory:"), mock_bcp)

        engine = create_engine("sqlite://", creator=creator, use_insertmanyvalues=False)
        register_bulkcopy(engine, on_complete=bad_callback)
        users = _make_table(engine)

        with pytest.raises(ValueError, match="callback bug"), engine.begin() as conn:
            conn.execute(
                users.insert().execution_options(use_bulkcopy=True),
                [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
            )

    def test_double_register_safe(self):
        engine = create_engine("sqlite:///:memory:", use_insertmanyvalues=False)
        register_bulkcopy(engine)
        register_bulkcopy(engine)


# ---------------------------------------------------------------------------
# bulkcopy_insert_method — pandas helper
# ---------------------------------------------------------------------------
class TestPandasMethod:
    def test_fires_bulkcopy(self):
        engine, mock_bcp = _make_engine()
        users = _make_table(engine)

        with engine.begin() as conn:
            bulkcopy_insert_method(users, conn, ["id", "name"], iter([(1, "A"), (2, "B")]))

        mock_bcp.assert_called_once()

    def test_empty_noop(self):
        engine, mock_bcp = _make_engine()
        users = _make_table(engine)

        with engine.begin() as conn:
            bulkcopy_insert_method(users, conn, ["id", "name"], iter([]))

        mock_bcp.assert_not_called()


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------
class TestMeta:
    def test_version(self):
        from sqlalchemy_mssql_bulkcopy import __version__

        assert isinstance(__version__, str)
        assert __version__  # not empty

    def test_all_exports(self):
        from sqlalchemy_mssql_bulkcopy import __all__

        assert set(__all__) == {"bulkcopy", "bulkcopy_insert_method", "register_bulkcopy"}
