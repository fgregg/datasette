"""View-level regression tests for the DuckDB backend."""

import asyncio
import sqlite3

import pytest

# Skip where duckdb isn't installed (e.g. datasette core's own CI).
pytest.importorskip("duckdb")

from datasette.app import Datasette  # noqa: E402
from datasette_duckdb.convert import convert_sqlite_to_duckdb  # noqa: E402


def test_fk_label_expansion_empty_result_does_not_500(tmp_path):
    # A search/filter that matches no rows must not make Datasette's FK-label
    # expansion build `... in ()` -- a DuckDB syntax error. Regression for the
    # 500 on /opdr/lm_data?_search=<no hits> (SQLite tolerated the empty IN).
    src = tmp_path / "source.db"
    dst = tmp_path / "out.duckdb"
    con = sqlite3.connect(str(src))
    con.executescript("""
        create table region (id integer primary key, name text);
        create table office (
            id integer primary key,
            region_id integer references region(id)
        );
        insert into region values (1, 'Midwest');
        insert into office values (1, 1), (2, 1);
        """)
    con.commit()
    con.close()
    convert_sqlite_to_duckdb(str(src), str(dst))

    ds = Datasette(
        config={"plugins": {"datasette-duckdb": {"databases": {"d": str(dst)}}}}
    )

    async def fetch():
        await ds.invoke_startup()
        # id__gt=999 -> zero office rows, so region_id label expansion is empty
        return await ds.client.get("/d/office?id__gt=999")

    response = asyncio.run(fetch())
    assert response.status_code == 200


def test_unenforceable_fk_still_navigable_via_sidecar(tmp_path):
    # An FK whose source data has orphans can't be enforced by DuckDB, so it's
    # absent from the catalog constraints -- but the converter's FK sidecar still
    # records it, so Datasette renders related-rows / label-expansion (parity with
    # SQLite, which declares-but-doesn't-enforce). #18.
    import duckdb

    src = tmp_path / "s.db"
    dst = tmp_path / "d.duckdb"
    con = sqlite3.connect(str(src))
    con.executescript("""
        CREATE TABLE region (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE office (
            id INTEGER PRIMARY KEY,
            region_id INTEGER REFERENCES region(id)
        );
        INSERT INTO region VALUES (1, 'Midwest');
        INSERT INTO office VALUES (1, 1), (2, 999);  -- 999 orphan -> not enforceable
        """)
    con.commit()
    con.close()
    convert_sqlite_to_duckdb(str(src), str(dst))

    # genuinely NOT an enforced constraint (orphan data)
    enforced = (
        duckdb.connect(str(dst), read_only=True)
        .execute(
            "select count(*) from duckdb_constraints() "
            "where table_name = 'office' and constraint_type = 'FOREIGN KEY'"
        )
        .fetchone()[0]
    )
    assert enforced == 0

    ds = Datasette(
        config={"plugins": {"datasette-duckdb": {"databases": {"d": str(dst)}}}}
    )

    async def fetch():
        await ds.invoke_startup()
        fks = await ds.databases["d"].foreign_keys_for_table("office")
        row = await ds.client.get("/d/office/1")  # a row page that uses FK labels
        return fks, row.status_code

    fks, status = asyncio.run(fetch())
    # navigation is restored from the sidecar despite no enforced constraint
    assert fks == [
        {"column": "region_id", "other_table": "region", "other_column": "id"}
    ]
    assert status == 200
