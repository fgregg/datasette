"""The `external_access: false` + `allowed_directories` filesystem sandbox.

Datasette only lets SELECT/WITH reach the query endpoint, but DuckDB table
functions run inside a SELECT, so without this anyone who can reach /-/query
can read any file the process can (`read_text('/etc/hostname')`, `glob`,
`read_csv`). With it, only the listed directories resolve -- which must still
include the data directory, so views over Parquet files there keep working.
"""

import asyncio

import pytest

duckdb = pytest.importorskip("duckdb")

from datasette.app import Datasette  # noqa: E402
from datasette_duckdb import backend  # noqa: E402


def _make(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    pq = data / "rows.parquet"
    db = data / "v.duckdb"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT 1 AS x UNION ALL SELECT 2) TO '{}' (FORMAT parquet)".format(pq)
    )
    con.close()
    con = duckdb.connect(str(db))
    con.execute("CREATE VIEW rows AS SELECT * FROM read_parquet('{}')".format(pq))
    con.execute("CREATE TABLE t AS SELECT 42 AS val")
    con.close()
    # A file the sandbox must NOT be able to read.
    outside = tmp_path / "secret.txt"
    outside.write_text("hush")
    return data, db, outside


def _ds(tmp_path, shared):
    data, db, outside = _make(tmp_path)
    cfg = {
        "databases": {"v": str(db)},
        "external_access": False,
        "allowed_directories": [str(data)],
    }
    if shared:
        cfg["shared_instance"] = True
    ds = Datasette(crossdb=shared, config={"plugins": {"datasette-duckdb": cfg}})
    asyncio.run(ds.invoke_startup())
    return ds, outside


@pytest.fixture(autouse=True)
def _fresh_master():
    # The shared instance is process-global; don't inherit another test's.
    backend._MASTER = None
    yield
    backend._MASTER = None


@pytest.mark.parametrize("shared", [False, True])
def test_sandbox_blocks_outside_and_allows_data_dir(tmp_path, shared):
    ds, outside = _ds(tmp_path, shared)

    async def run():
        v = ds.databases["v"]
        # The data directory: table, and a view over a Parquet file there.
        table = await v.execute("SELECT val FROM t")
        view = await v.execute("SELECT count(*) AS n FROM rows")
        # Outside it: blocked.
        blocked = await ds.client.get(
            "/v/-/query.json",
            params={
                "sql": "SELECT content FROM read_text('{}')".format(outside),
                "_shape": "array",
            },
        )
        return table.first()["val"], view.first()["n"], blocked

    val, n, blocked = asyncio.run(run())
    assert val == 42
    assert n == 2
    assert blocked.status_code == 400
    assert "file system operations are disabled" in blocked.json()["error"]


@pytest.mark.parametrize("shared", [False, True])
def test_sandbox_settings_visible(tmp_path, shared):
    ds, _ = _ds(tmp_path, shared)
    rows = asyncio.run(
        ds.databases["v"].execute(
            "SELECT name, value FROM duckdb_settings() "
            "WHERE name IN ('enable_external_access', 'allowed_directories') ORDER BY name"
        )
    )
    got = {r["name"]: r["value"] for r in rows}
    assert got["enable_external_access"] == "false"
    assert "data" in got["allowed_directories"]


def test_default_leaves_access_on(tmp_path):
    data, db, outside = _make(tmp_path)
    ds = Datasette(
        config={"plugins": {"datasette-duckdb": {"databases": {"v": str(db)}}}}
    )
    asyncio.run(ds.invoke_startup())
    rows = asyncio.run(
        ds.databases["v"].execute(
            "SELECT length(content) AS n FROM read_text('{}')".format(outside)
        )
    )
    assert rows.first()["n"] == 4
