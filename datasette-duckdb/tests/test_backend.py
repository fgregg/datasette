"""Unit tests for DuckDBDialect (FTS search-clause parity, #4)."""

import pytest

# Skip where duckdb isn't installed (e.g. datasette core's own CI).
pytest.importorskip("duckdb")

from datasette_duckdb.backend import DuckDBDialect  # noqa: E402


def test_fts_search_clause_qualifies_rowid_and_is_conjunctive():
    clause = DuckDBDialect().fts_search_clause(
        fts_table="f7", fts_pk="rowid", column=None, param="search", raw=False
    )
    # qualified rowid avoids the fts_main_f7.docs shadow-capture
    assert '"f7".rowid' in clause
    # conjunctive => all terms must match, matching SQLite FTS5 (not DuckDB's OR)
    assert "conjunctive := true" in clause
    assert "match_bm25" in clause


def test_fts_search_clause_per_column_is_conjunctive():
    clause = DuckDBDialect().fts_search_clause(
        fts_table="f7",
        fts_pk="rowid",
        column="union_name",
        param="search_0",
        raw=False,
    )
    assert "fields := 'union_name'" in clause
    assert "conjunctive := true" in clause


def test_fts_search_clause_real_pk_not_qualified():
    # A real (non-rowid) pk is used as-is, no table-qualification.
    clause = DuckDBDialect().fts_search_clause(
        fts_table="docs", fts_pk="id", column=None, param="search", raw=False
    )
    assert '"id"' in clause
    assert ".rowid" not in clause
