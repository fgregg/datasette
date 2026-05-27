"""
Pluggable query backends for Datasette.

This package extracts the SQLite-specific behaviour that used to live directly
in ``Database`` (connection creation, query execution) behind a ``Backend``
interface, so that alternative backends (e.g. DuckDB) can be supplied later.

SQLite is implemented as :class:`datasette.backends.sqlite.SqliteBackend` and is
the default backend used by every ``Database`` unless another is supplied.

See ``design/backend-protocol.md`` for the full design and ``design/
backend-abstraction-audit.md`` for the coupling audit this extraction is based
on. This module currently covers audit seams 1 (connection/execution) and 11
(capability advertisement); introspection, dialect and ``prepare_connection``
remain in their original locations and are the next extraction steps.
"""

import re
from dataclasses import dataclass


_NAME_RE = re.compile(r"\w+")


def rewrite_named_parameters(sql, render):
    """Rewrite ``:name`` placeholders in *sql*, calling ``render(name)`` for each.

    Datasette speaks the ``:name`` parameter style (SQLite's). A backend whose
    driver wants a different style converts at its boundary. Doing that with a
    plain regex is wrong because ``:`` is meaningful inside string literals,
    comments and ``::`` casts; this walks the SQL lexically so only genuine
    placeholders are rewritten.

    Returns ``(new_sql, names)`` — ``names`` is the placeholder names in order.
    """
    out = []
    names = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if c == "'" or c == '"':
            # String literal / quoted identifier; "" or '' escapes the quote
            quote = c
            out.append(c)
            i += 1
            while i < n:
                ch = sql[i]
                if ch == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        out.append(quote * 2)
                        i += 2
                        continue
                    out.append(quote)
                    i += 1
                    break
                out.append(ch)
                i += 1
        elif c == "-" and nxt == "-":
            # Line comment
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append(sql[i:j])
            i = j
        elif c == "/" and nxt == "*":
            # Block comment
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(sql[i:j])
            i = j
        elif c == ":" and nxt == ":":
            # Cast operator, not a placeholder
            out.append("::")
            i += 2
        elif c == ":":
            m = _NAME_RE.match(sql, i + 1)
            if m:
                name = m.group(0)
                out.append(render(name))
                names.append(name)
                i += 1 + len(name)
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out), names


@dataclass(frozen=True)
class Features:
    """Capability advertisement, in the style of Django's ``DatabaseFeatures``.

    Backends advertise what they support; consuming code reads these flags and
    adapts (e.g. a backend with ``supports_rowid=False`` makes keyless-table
    code paths take their fallback branch) rather than testing the backend name.

    Nothing consumes these flags yet — they are wired in here so later steps can
    route the filter/facet/pagination paths through them.
    """

    supports_rowid: bool = True
    supports_fts: bool = False
    supports_json: bool = False
    supports_glob: bool = False
    supports_load_extension: bool = False
    supports_attach: bool = False
    supports_write: bool = True
    supports_explain: bool = True


class Dialect:
    """SQL-string generation for a backend (audit seams 6/8/9).

    Pure string building, no I/O. The base class implements portable SQL in
    terms of :meth:`escape_identifier`; subclasses supply the engine-specific
    quoting (and, in later slices, filter/facet operator fragments).
    """

    def escape_identifier(self, name: str) -> str:
        """Quote a table/column identifier for this dialect."""
        raise NotImplementedError

    def adapt_parameters(self, sql, params):
        """Adapt ``:name``-style SQL + params to this backend's parameter style.

        Identity by default (SQLite's driver speaks ``:name``). Backends whose
        driver wants a different style override this, typically using
        :func:`rewrite_named_parameters`. Returns ``(sql, params)``.
        """
        return sql, params

    def keyset_after_sql(self, pks, start_index: int = 0) -> str:
        """Keyset-pagination WHERE fragment for "rows ordered after this one".

        For pk1/pk2/pk3 returns::

            ([pk1] > :p0)
              or
            ([pk1] = :p0 and [pk2] > :p1)
              or
            ([pk1] = :p0 and [pk2] = :p1 and [pk3] > :p2)

        The comparison structure is portable standard SQL; only identifier
        quoting (via :meth:`escape_identifier`) is dialect-specific, so this
        lives in the base class.
        See https://github.com/simonw/datasette/issues/190
        """
        or_clauses = []
        pks_left = list(pks)
        while pks_left:
            last = pks_left[-1]
            rest = pks_left[:-1]
            and_clauses = [
                f"{self.escape_identifier(pk)} = :p{i + start_index}"
                for i, pk in enumerate(rest)
            ]
            and_clauses.append(
                f"{self.escape_identifier(last)} > :p{len(rest) + start_index}"
            )
            or_clauses.append(f"({' and '.join(and_clauses)})")
            pks_left.pop()
        or_clauses.reverse()
        return "({})".format("\n  or\n".join(or_clauses))


class Backend:
    """Base class for a Datasette query backend.

    A backend owns connection creation and query execution for a ``Database``.
    The async-over-threads execution machinery stays in ``Database``; the
    backend supplies only the engine-specific pieces.
    """

    #: Short identifier, e.g. ``"sqlite"`` or ``"duckdb"``.
    name: str = "base"

    #: Capability flags for this backend.
    features: Features = Features()

    #: SQL-string generation for this backend.
    dialect: Dialect = Dialect()

    @classmethod
    def handles(cls, source: str) -> bool:
        """Whether this backend recognises a given path / URL source.

        Used during database resolution. The default backend (SQLite) does not
        need to claim sources, so this returns ``False`` here.
        """
        return False

    def connect(self, db, write: bool = False):
        """Open and return a new DB-API-style connection for ``db``.

        ``db`` is the :class:`datasette.database.Database` instance, which
        carries the configuration the backend needs (path, mutability, etc.).
        """
        raise NotImplementedError

    def prepare_connection(self, conn, datasette, database_name):
        """Configure a freshly opened connection.

        Applies row/text handling, loads any configured extensions and engine
        settings, fires the ``prepare_connection`` plugin hook, and performs any
        cross-database attachment. Called once per connection, immediately after
        :meth:`connect`.
        """
        raise NotImplementedError

    def execute_query(
        self,
        conn,
        sql,
        params,
        *,
        time_limit_ms,
        max_returned_rows,
        page_size,
        truncate,
        log_sql_errors,
    ):
        """Run a read query on ``conn`` and return a ``Results``.

        The backend owns the time-limit strategy, row fetching/truncation, and
        mapping engine errors to ``QueryInterrupted``.
        """
        raise NotImplementedError

    def introspector(self, db) -> "Introspector":
        """Return an :class:`Introspector` bound to ``db`` for schema queries."""
        raise NotImplementedError


class Introspector:
    """Schema introspection for a single database, supplied by its backend.

    Bound to one :class:`datasette.database.Database`; methods run queries via
    that database's execution methods. This is audit seam 5 from
    ``design/backend-abstraction-audit.md`` — the ``sqlite_master`` / ``PRAGMA``
    queries that ``Database`` used to issue inline.
    """

    def __init__(self, db):
        self.db = db

    async def table_names(self):
        raise NotImplementedError

    async def view_names(self):
        raise NotImplementedError

    async def table_exists(self, table):
        raise NotImplementedError

    async def view_exists(self, view):
        raise NotImplementedError

    async def table_columns(self, table):
        raise NotImplementedError

    async def table_column_details(self, table):
        raise NotImplementedError

    async def column_details_with_uniqueness(self, table):
        """Return ``{column_name: (type, is_unique)}`` for ``table``.

        Used to pick a label column. ``type`` is the Python type; ``is_unique``
        reflects whether a single-column unique index exists on the column.
        """
        raise NotImplementedError

    async def primary_keys(self, table):
        raise NotImplementedError

    async def fts_table(self, table):
        raise NotImplementedError

    async def foreign_keys_for_table(self, table):
        raise NotImplementedError

    async def get_all_foreign_keys(self):
        raise NotImplementedError

    async def hidden_table_names(self):
        raise NotImplementedError

    async def get_table_definition(self, table, type_="table"):
        raise NotImplementedError

    async def attached_databases(self):
        raise NotImplementedError

    async def schema_version(self):
        """A token that changes when the database schema changes.

        Used to invalidate the cached catalog. Backends without a cheap
        equivalent may return a constant (the catalog is then populated once).
        """
        raise NotImplementedError
