"""
Tests for datasette.renderer.json_renderer.

In particular, the row-shape handling is backend-neutral: rows may be
``sqlite3.Row`` objects, ``CustomRow`` dicts, or plain tuples (as a backend like
DuckDB would return). The renderer distinguishes dict-like from positional rows
rather than checking for ``sqlite3.Row`` specifically.
"""

import json
import pytest

from datasette.utils.asgi import Request
from datasette.renderer import json_renderer


def _render(shape, rows, columns):
    request = Request.fake(f"/x.json?_shape={shape}")
    response = json_renderer(
        request,
        request.args,
        {"rows": rows, "columns": columns, "ok": True},
        None,
    )
    return json.loads(response.body)


# Each case: shape -> expected output for rows [(1, "a"), (2, "b")] / cols id,name
@pytest.mark.parametrize(
    "shape,expected",
    [
        ("arrays", [[1, "a"], [2, "b"]]),
        ("arrayfirst", [1, 2]),
        ("objects", [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]),
    ],
)
def test_json_renderer_handles_plain_tuple_rows(shape, expected):
    # A backend returning plain tuples (not sqlite3.Row) must render correctly.
    result = _render(shape, [(1, "a"), (2, "b")], ["id", "name"])
    got = result if shape == "arrayfirst" else result["rows"]
    assert got == expected
