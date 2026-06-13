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


def test_glob_filter_offered_and_works(tmp_path):
    # DuckDB supports the GLOB operator, so the backend advertises supports_glob
    # and the __glob table filter must work (case-sensitive, * wildcard).
    src = tmp_path / "source.db"
    dst = tmp_path / "out.duckdb"
    con = sqlite3.connect(str(src))
    con.executescript("""
        create table org (id integer primary key, name text);
        insert into org (id, name) values
            (1,'Alpha'), (2,'Apex'), (3,'Beacon'), (4,'apex lower');
        """)
    con.commit()
    con.close()
    convert_sqlite_to_duckdb(str(src), str(dst))

    ds = Datasette(
        config={"plugins": {"datasette-duckdb": {"databases": {"d": str(dst)}}}}
    )

    async def fetch():
        await ds.invoke_startup()
        return await ds.client.get("/d/org.json?name__glob=A*&_shape=array")

    response = asyncio.run(fetch())
    assert response.status_code == 200
    names = sorted(r["name"] for r in response.json())
    # case-sensitive: 'Alpha' and 'Apex' match 'A*', 'apex lower' does not
    assert names == ["Alpha", "Apex"]
