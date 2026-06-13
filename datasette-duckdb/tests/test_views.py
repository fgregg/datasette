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


def test_database_schema_page_does_not_500(tmp_path):
    # /<db>/-/schema ran a hardcoded `... from sqlite_master` query, which
    # DuckDB can't parse -> every schema page 500'd. It now composes from the
    # introspector's table/view definitions. Regression for /<db>/-/schema.
    src = tmp_path / "source.db"
    dst = tmp_path / "out.duckdb"
    con = sqlite3.connect(str(src))
    con.executescript("""
        create table widget (id integer primary key, name text);
        create view widget_names as select name from widget;
        insert into widget (id, name) values (1, 'a'), (2, 'b');
        """)
    con.commit()
    con.close()
    convert_sqlite_to_duckdb(str(src), str(dst))

    ds = Datasette(
        config={"plugins": {"datasette-duckdb": {"databases": {"d": str(dst)}}}}
    )

    async def fetch():
        await ds.invoke_startup()
        return await ds.client.get("/d/-/schema.json")

    response = asyncio.run(fetch())
    assert response.status_code == 200
    schema = response.json()["schema"]
    assert "widget" in schema  # composed CREATE statements present
