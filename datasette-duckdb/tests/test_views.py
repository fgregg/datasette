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


def test_facet_suggestion_on_keyed_table_does_not_500(tmp_path):
    # Facet suggestion ran `select <col> as value ... where value is not null
    # group by value`, referencing the SELECT alias. SQLite resolves the alias;
    # DuckDB rejects it for the primary-key column with "column <pk> must appear
    # in the GROUP BY clause". Regression for the 500 browsing /opdr/ar_assets_fixed
    # (a table with an integer pk) once faceting is enabled.
    src = tmp_path / "source.db"
    dst = tmp_path / "out.duckdb"
    con = sqlite3.connect(str(src))
    con.executescript("""
        create table assets (
            oid integer primary key,
            asset_type text
        );
        insert into assets (oid, asset_type)
            values (1, 'a'), (2, 'a'), (3, 'b'), (4, 'b'), (5, 'c');
        """)
    con.commit()
    con.close()
    convert_sqlite_to_duckdb(str(src), str(dst))

    ds = Datasette(
        config={"plugins": {"datasette-duckdb": {"databases": {"d": str(dst)}}}}
    )

    async def fetch():
        await ds.invoke_startup()
        # The HTML table view runs facet suggestion over every column, incl. the
        # pk; previously this 500'd. ?asset_type=a also exercises the filtered view.
        return [
            await ds.client.get("/d/assets"),
            await ds.client.get("/d/assets?asset_type=a"),
        ]

    plain, filtered = asyncio.run(fetch())
    assert plain.status_code == 200
    assert filtered.status_code == 200
