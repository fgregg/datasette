"""Cross-database (--crossdb) querying on the DuckDB backend (#10).

When --crossdb is on and the plugin has mounted DuckDB databases, the _memory
host is re-backed by DuckDB and ATTACHes the .duckdb files read-only, so a query
against /_memory can join across them in DuckDB syntax.
"""

import asyncio

import pytest

# Skip where duckdb isn't installed (e.g. datasette core's own CI, which
# collects this vendored dir but has no duckdb / plugin installed).
duckdb = pytest.importorskip("duckdb")

from datasette.app import Datasette  # noqa: E402


def _make_db(path, val):
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
        con.execute("INSERT INTO t VALUES (1, ?)", [val])
    finally:
        con.close()


@pytest.fixture
def crossdb_datasette(tmp_path):
    a = tmp_path / "a.duckdb"
    b = tmp_path / "b.duckdb"
    _make_db(a, 10)
    _make_db(b, 32)
    ds = Datasette(
        crossdb=True,
        config={
            "plugins": {"datasette-duckdb": {"databases": {"a": str(a), "b": str(b)}}}
        },
    )
    asyncio.run(ds.invoke_startup())
    return ds


def test_memory_is_rebacked_by_duckdb(crossdb_datasette):
    ds = crossdb_datasette
    assert "_memory" in ds.databases
    assert ds.databases["_memory"].backend.name == "duckdb"


def test_cross_database_join_through_memory(crossdb_datasette):
    ds = crossdb_datasette
    sql = "SELECT (x.val + y.val) AS s FROM a.t AS x JOIN b.t AS y ON x.id = y.id"
    rows = asyncio.run(ds.databases["_memory"].execute(sql))
    assert [dict(r) for r in rows] == [{"s": 42}]


def test_memory_lists_attached_databases(crossdb_datasette):
    # The _memory database page shows what's joinable -> introspector must
    # report the attached databases (the warehouse template gates on this).
    ds = crossdb_datasette
    attached = asyncio.run(ds.databases["_memory"].attached_databases())
    assert sorted(d.name for d in attached) == ["a", "b"]


def test_regular_database_reports_no_attachments(crossdb_datasette):
    # A normal file database isn't a crossdb host -> no attachments listed.
    ds = crossdb_datasette
    assert asyncio.run(ds.databases["a"].attached_databases()) == []


def test_each_attached_database_queryable(crossdb_datasette):
    ds = crossdb_datasette
    a_rows = asyncio.run(ds.databases["_memory"].execute("SELECT val FROM a.t"))
    b_rows = asyncio.run(ds.databases["_memory"].execute("SELECT val FROM b.t"))
    assert a_rows.first()["val"] == 10
    assert b_rows.first()["val"] == 32


def test_no_crossdb_leaves_memory_sqlite(tmp_path):
    # Without --crossdb there's no _memory host to re-back (and if one exists for
    # other reasons, the plugin must not touch it).
    a = tmp_path / "a.duckdb"
    _make_db(a, 10)
    ds = Datasette(
        config={"plugins": {"datasette-duckdb": {"databases": {"a": str(a)}}}},
    )
    asyncio.run(ds.invoke_startup())
    mem = ds.databases.get("_memory")
    if mem is not None:
        assert mem.backend.name == "sqlite"
