"""Tests for the SQLite -> DuckDB converter, focused on content-based type
inference (#9): VARCHAR -> UUID/DATE/TIMESTAMP/BIGINT and DOUBLE -> REAL."""

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
    assert promotions == {
        "events": [
            ("event_date", "DATE"),
            ("created_at", "TIMESTAMP"),  # phase 2: datetimes -> TIMESTAMP
            ("midnight", "DATE"),
        ]
    }


def test_real_datetimes_promote_to_timestamp_not_date(converted):
    # created_at carries 13:45:00, so DATE must not claim it (would truncate);
    # phase 2 promotes it to TIMESTAMP instead.
    dst, _, _ = converted
    assert _duckdb_types(dst, "events")["created_at"] == "TIMESTAMP"


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


# --- phase 2: TIMESTAMP / BIGINT / UUID, and DOUBLE -> REAL ---


def _make_phase2_source(path, rows):
    con = sqlite3.connect(str(path))
    con.execute("""
        CREATE TABLE t (
            ts_col    TEXT,   -- datetimes with time-of-day -> TIMESTAMP
            int_col   TEXT,   -- plain integers             -> BIGINT
            zip_col   TEXT,   -- leading-zero ids           -> stay VARCHAR
            uuid_col  TEXT,   -- uuids                      -> UUID
            dbl_fit   REAL,   -- float32-exact values       -> REAL
            dbl_prec  REAL    -- needs float64              -> stay DOUBLE
        )
        """)
    con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


@pytest.fixture
def phase2_types(tmp_path):
    src = tmp_path / "p2.db"
    dst = tmp_path / "p2.duckdb"
    n = _MIN_SAMPLE + 50
    rows = []
    for i in range(n):
        day = 1 + (i % 28)
        rows.append(
            (
                f"2020-03-{day:02d} 13:45:00",  # ts_col: real time-of-day
                str(1000 + i),  # int_col: plain integers
                f"{i:05d}",  # zip_col: zero-padded -> identifier, keep VARCHAR
                f"{i:08x}-0000-4000-8000-000000000000",  # uuid_col: valid v4-shaped
                1.0 + (i % 4) * 0.25,  # dbl_fit: 1.0/1.25/1.5/1.75 exact in float32
                1.0 + i * 0.1,  # dbl_prec: 0.1 steps not float32-exact
            )
        )
    _make_phase2_source(src, rows)
    convert_sqlite_to_duckdb(str(src), str(dst))
    return _duckdb_types(dst, "t")


def test_datetimes_promote_to_timestamp(phase2_types):
    assert phase2_types["ts_col"] == "TIMESTAMP"


def test_integers_promote_to_bigint(phase2_types):
    assert phase2_types["int_col"] == "BIGINT"


def test_leading_zero_ids_stay_varchar(phase2_types):
    # '00007' -> 7 -> '7' != '00007': the round-trip guard blocks renumbering.
    assert phase2_types["zip_col"] == "VARCHAR"


def test_uuids_promote_to_uuid(phase2_types):
    assert phase2_types["uuid_col"] == "UUID"


def test_float32_exact_doubles_promote_to_real(phase2_types):
    assert phase2_types["dbl_fit"] == "FLOAT"  # DuckDB reports REAL as FLOAT


def test_high_precision_doubles_stay_double(phase2_types):
    # 0.1-step values aren't float32-exact -> strict guard keeps DOUBLE.
    assert phase2_types["dbl_prec"] == "DOUBLE"


def test_bigint_holds_integers(tmp_path):
    # Promoted BIGINT column carries ints, and a leading-zero column nearby is
    # untouched (verifies the per-column round-trip guard end to end).
    src = tmp_path / "b.db"
    dst = tmp_path / "b.duckdb"
    rows = [(str(1000 + i), f"{i:05d}") for i in range(_MIN_SAMPLE + 50)]
    con = sqlite3.connect(str(src))
    con.execute("CREATE TABLE q (num TEXT, code TEXT)")
    con.executemany("INSERT INTO q VALUES (?,?)", rows)
    con.commit()
    con.close()
    convert_sqlite_to_duckdb(str(src), str(dst))
    types = _duckdb_types(dst, "q")
    assert types["num"] == "BIGINT"
    assert types["code"] == "VARCHAR"
