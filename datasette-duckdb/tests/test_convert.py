"""Tests for the SQLite -> DuckDB converter, focused on content-based type
inference (#9): VARCHAR -> UUID/DATE/TIMESTAMP/BIGINT and DOUBLE -> REAL."""

import sqlite3

import pytest

# These exercise the datasette-duckdb plugin. Skip where duckdb isn't installed
# — e.g. datasette core's own CI, which collects this vendored dir but has no
# duckdb (and no plugin) installed.
duckdb = pytest.importorskip("duckdb")

from datasette_duckdb.convert import (  # noqa: E402
    convert_sqlite_to_duckdb,
    _MIN_SAMPLE,
)


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
    dropped, promotions, _fts = convert_sqlite_to_duckdb(str(src), str(dst))
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


# --- #4: FTS indexes mirrored from the SQLite source ---


def test_fts_index_mirrored_and_searchable(tmp_path):
    # Skip where the duckdb fts extension can't be installed/loaded (offline).
    probe = duckdb.connect()
    try:
        probe.execute("INSTALL fts; LOAD fts;")
    except duckdb.Error:
        pytest.skip("duckdb fts extension unavailable")
    finally:
        probe.close()

    src = tmp_path / "fts.db"
    dst = tmp_path / "fts.duckdb"
    con = sqlite3.connect(str(src))
    con.execute("CREATE TABLE filing (id INTEGER PRIMARY KEY, name TEXT, city TEXT)")
    con.executemany(
        "INSERT INTO filing VALUES (?,?,?)",
        [
            (1, "United Steelworkers", "pittsburgh"),
            (2, "Teamsters Local 705", "chicago"),
            (3, "Steelworkers District 7", "gary"),
        ],
    )
    # sqlite-utils enable-fts style virtual table
    try:
        con.execute(
            "CREATE VIRTUAL TABLE filing_fts USING fts5(name, city, content=filing)"
        )
    except sqlite3.OperationalError:
        con.close()
        pytest.skip("sqlite3 build lacks FTS5")
    con.commit()
    con.close()

    *_, fts_created = convert_sqlite_to_duckdb(str(src), str(dst))
    # Detected the base table + indexed columns from the source vtable.
    assert fts_created == [("filing", ["name", "city"])]

    d = duckdb.connect(str(dst))
    try:
        schemas = [
            r[0]
            for r in d.execute(
                "select schema_name from information_schema.schemata "
                "where schema_name = 'fts_main_filing'"
            ).fetchall()
        ]
        assert schemas == ["fts_main_filing"]

        def search(query, conjunctive=False):
            extra = ", conjunctive := true" if conjunctive else ""
            return [
                r[0]
                for r in d.execute(
                    "select name from filing where "
                    f"fts_main_filing.match_bm25(filing.rowid, ?{extra}) is not null "
                    "order by id",
                    [query],
                ).fetchall()
            ]

        # Search resolves via the qualified-rowid doc-id (the shadow-capture fix).
        assert search("steelworkers") == [
            "United Steelworkers",
            "Steelworkers District 7",
        ]
        # Digits are indexed (SQLite-parity tokenizer keeps numbers), so a union
        # local number is searchable -- this is broken under DuckDB's defaults.
        assert search("705") == ["Teamsters Local 705"]
        # conjunctive AND: only the row with *both* terms (name + city columns).
        assert search("steelworkers gary", conjunctive=True) == [
            "Steelworkers District 7"
        ]
        # without conjunctive the same query is OR (both steelworkers rows).
        assert search("steelworkers gary") == [
            "United Steelworkers",
            "Steelworkers District 7",
        ]
    finally:
        d.close()


def test_one_bad_fk_does_not_drop_a_tables_good_fks(tmp_path):
    # Regression (#18): a single un-enforceable FK used to make the converter drop
    # ALL of a table's FKs (create-with-all-fks failed -> fallback with none).
    # Now un-enforceable FKs are dropped individually; valid siblings survive.
    src = tmp_path / "s.db"
    dst = tmp_path / "d.duckdb"
    con = sqlite3.connect(str(src))
    con.executescript("""
        CREATE TABLE region (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE office (
            id        INTEGER PRIMARY KEY,
            region_id INTEGER REFERENCES region(id),   -- valid
            ghost_id  INTEGER REFERENCES region(id)     -- orphan -> can't enforce
        );
        INSERT INTO region VALUES (1, 'Midwest');
        INSERT INTO office VALUES (1, 1, 999);          -- ghost_id 999: no such region
        """)
    con.commit()
    con.close()
    dropped, _, _ = convert_sqlite_to_duckdb(str(src), str(dst))

    d = duckdb.connect(str(dst))
    fk_cols = {
        row[0][0]
        for row in d.execute(
            "select constraint_column_names from duckdb_constraints() "
            "where table_name='office' and constraint_type='FOREIGN KEY'"
        ).fetchall()
    }
    n = d.execute("select count(*) from office").fetchone()[0]
    d.close()

    assert "region_id" in fk_cols  # valid FK survived its bad sibling
    assert "ghost_id" not in fk_cols  # orphan FK dropped individually
    assert any("ghost_id" in msg for _, msg in dropped)
    assert n == 1  # data intact


def test_composite_fk_recreated(tmp_path):
    # Composite FKs span several PRAGMA rows; the converter used to flatten them
    # to single columns (-> "not the parent PK", dropped). They should now be
    # recreated as a real multi-column FK when they reference the parent's
    # composite PK. Regression for cats r_* -> r_bargaining_unit (#18).
    src = tmp_path / "s.db"
    dst = tmp_path / "d.duckdb"
    con = sqlite3.connect(str(src))
    con.executescript("""
        CREATE TABLE unit (
            case_no TEXT, unit_id INTEGER, name TEXT,
            PRIMARY KEY (case_no, unit_id)
        );
        CREATE TABLE action (
            id INTEGER PRIMARY KEY,
            case_no TEXT,
            unit_id INTEGER,
            FOREIGN KEY (case_no, unit_id) REFERENCES unit(case_no, unit_id)
        );
        INSERT INTO unit VALUES ('A', 1, 'x');
        INSERT INTO action VALUES (1, 'A', 1);
        """)
    con.commit()
    con.close()
    dropped, _, _ = convert_sqlite_to_duckdb(str(src), str(dst))

    d = duckdb.connect(str(dst))
    fk_cols = [
        sorted(row[0])
        for row in d.execute(
            "select constraint_column_names from duckdb_constraints() "
            "where table_name='action' and constraint_type='FOREIGN KEY'"
        ).fetchall()
    ]
    d.close()
    assert fk_cols == [["case_no", "unit_id"]]  # one composite FK, both columns
    assert not [m for _, m in dropped if "action" in m]
