"""Tests for datasette.backends helpers."""

import pytest

from datasette.backends import rewrite_named_parameters, Dialect


def _to_dollar(sql):
    return rewrite_named_parameters(sql, lambda name: "$" + name)


@pytest.mark.parametrize(
    "sql,expected_sql,expected_names",
    [
        # Basic placeholder
        ("where a = :foo", "where a = $foo", ["foo"]),
        # :: cast must be left alone (this is the bug that started it)
        (
            "year(day::date) and t = :case_type",
            "year(day::date) and t = $case_type",
            ["case_type"],
        ),
        # Colon inside a single-quoted string literal is not a placeholder
        ("select '12:30' as x, :foo", "select '12:30' as x, $foo", ["foo"]),
        # Colon inside a double-quoted identifier is not a placeholder
        ('select "a:b", :foo', 'select "a:b", $foo', ["foo"]),
        # Colon inside a line comment is not a placeholder
        ("select 1 -- :nope\nwhere a = :real", "select 1 -- :nope\nwhere a = $real", ["real"]),
        # Colon inside a block comment is not a placeholder
        ("select /* :nope */ :real", "select /* :nope */ $real", ["real"]),
        # Repeated placeholder appears once per use, in order
        ("where a = :x or b = :x", "where a = $x or b = $x", ["x", "x"]),
        # Escaped quote inside a string ('') doesn't end the literal early
        ("select 'it''s :nope', :foo", "select 'it''s :nope', $foo", ["foo"]),
    ],
)
def test_rewrite_named_parameters(sql, expected_sql, expected_names):
    out_sql, names = _to_dollar(sql)
    assert out_sql == expected_sql
    assert names == expected_names


def test_dialect_adapt_parameters_identity():
    # Base Dialect leaves :name SQL + params untouched (SQLite's native style)
    sql = "select * from t where x = :foo"
    params = {"foo": 1}
    assert Dialect().adapt_parameters(sql, params) == (sql, params)
