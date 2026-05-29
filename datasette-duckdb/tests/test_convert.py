"""Tests for the SQLite -> DuckDB converter, focused on content-based type
inference (#9, phase 1: DATE)."""

import sqlite3

import duckdb
import pytest

from datasette_duckdb.convert import convert_sqlite_to_duckdb, _MIN_SAMPLE


def _make_source(path, rows):
    """Build a SQLite db whose columns are all declared TEXT/INTEGER, with
    contents chosen to exercise each inference branch."""
    con = sqlite3.connect(str(path))
    con.execute("""
        CREATE TABLE events (
            id          INTEGER PRIMARY KEY,
            event_date  TEXT,     -- uniform ISO dates  -> promote to DATE
            created_at  TEXT,     -- real datetimes     -> stay VARCHAR (lossy)
            midnight    TEXT,     -- dates-at-midnight   -> promote to DATE
            label       TEXT,     -- free text           -> stay VARCHAR
            dirty_date  TEXT,     -- dates + 1 garbage   -> stay VARCHAR (strict)
            few_dates   TEXT,     -- < _MIN_SAMPLE dates  -> stay VARCHAR
            n           INTEGER   -- declared int         -> BIGINT (untouched)
        )
        """)
    con.executemany(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


def _duckdb_types(path, table):
    d = duckdb.connect(str(path))
    try:
        return dict(
            d.execute(
                "SELECT column_name, data_type FROM duckdb_columns() "
                "WHERE table_name = ?",
                [table],
            ).fetchall()
        )
    finally:
        d.close()


@pytest.fixture
def converted(tmp_path):
    src = tmp_path / "source.db"
    dst = tmp_path / "dest.duckdb"
    n = _MIN_SAMPLE + 50  # comfortably over the sampling floor
    rows = []
    for i in range(n):
        day = 1 + (i % 28)
        rows.append(
            (
                i,  # id
                f"2020-03-{day:02d}",  # event_date: clean ISO date
                f"2020-03-{day:02d} 13:45:00",  # created_at: real time-of-day
                f"2020-03-{day:02d} 00:00:00",  # midnight: no time-of-day
                f"label-{i}",  # label: free text
                # dirty_date: dates everywhere except one garbage value
                ("not-a-date" if i == 0 else f"2020-03-{day:02d}"),
                # few_dates: only a handful of non-empty date values
                (f"2021-01-{day:02d}" if i < 5 else ""),
                i * 10,  # n
            )
        )
    _make_source(src, rows)
    dropped, promotions = convert_sqlite_to_duckdb(str(src), str(dst))
    return dst, dropped, promotions


def test_uniform_iso_dates_promote_to_date(converted):
    dst, _, promotions = converted
    types = _duckdb_types(dst, "events")
    assert types["event_date"] == "DATE"
    assert types["midnight"] == "DATE"
    assert promotions == {"events": ["event_date", "midnight"]}


def test_real_datetimes_stay_varchar(converted):
    # Promoting created_at to DATE would silently drop 13:45:00 -> guard holds.
    dst, _, _ = converted
    assert _duckdb_types(dst, "events")["created_at"] == "VARCHAR"


def test_free_text_stays_varchar(converted):
    dst, _, _ = converted
    assert _duckdb_types(dst, "events")["label"] == "VARCHAR"


def test_single_garbage_value_blocks_promotion(converted):
    # Strict: one "not-a-date" among otherwise-clean dates keeps it VARCHAR.
    dst, _, _ = converted
    assert _duckdb_types(dst, "events")["dirty_date"] == "VARCHAR"


def test_too_few_samples_stay_varchar(converted):
    # Only 5 non-empty values (< _MIN_SAMPLE) -> not enough evidence to promote.
    dst, _, _ = converted
    assert _duckdb_types(dst, "events")["few_dates"] == "VARCHAR"


def test_declared_types_untouched(converted):
    dst, _, _ = converted
    types = _duckdb_types(dst, "events")
    # INTEGER -> BIGINT per _TYPE_MAP; the point is inference left them alone.
    assert types["n"] == "BIGINT"
    assert types["id"] == "BIGINT"  # INTEGER PRIMARY KEY


def test_promoted_date_column_holds_real_dates(converted):
    # The promoted column must actually carry DATE values, not strings.
    dst, _, _ = converted
    d = duckdb.connect(str(dst))
    try:
        row = d.execute("SELECT event_date FROM events ORDER BY id LIMIT 1").fetchone()
    finally:
        d.close()
    import datetime

    assert row is not None
    assert isinstance(row[0], datetime.date)


def test_infer_types_false_keeps_varchar(tmp_path):
    src = tmp_path / "s.db"
    dst = tmp_path / "d.duckdb"
    rows = [
        (i, f"2020-03-{1 + (i % 28):02d}", "x", "x", "x", "x", "", i)
        for i in range(_MIN_SAMPLE + 50)
    ]
    _make_source(src, rows)
    convert_sqlite_to_duckdb(str(src), str(dst), infer_types=False)
    assert _duckdb_types(dst, "events")["event_date"] == "VARCHAR"
