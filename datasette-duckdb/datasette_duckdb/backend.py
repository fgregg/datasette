"""
A DuckDB backend for Datasette, implemented against the backend seams in
``datasette.backends`` (Backend / Dialect / Introspector / Features).

Proof of concept: read-only analytic browsing. Known gaps (tracked as we go):
- Foreign keys / FTS / table definitions are not introspected yet.
"""

import threading

from datasette.backends import (
    Backend,
    Dialect,
    Features,
    Introspector,
    rewrite_named_parameters,
)
from datasette.utils import Column


class DuckDBRow:
    """Lightweight dual-access row (``row[i]`` and ``row["col"]``).

    Datasette's CustomRow builds an OrderedDict per row, which dominates the
    cost of large DuckDB result sets (~30x the fetch). This stores the value
    tuple plus a column->index map shared across all rows of a result, so
    wrapping is essentially free. It is not a ``dict`` subclass, so the JSON
    renderer treats it as a positional row (correct: it iterates as values).
    """

    __slots__ = ("_columns", "_index", "_values")

    def __init__(self, columns, index, values):
        self._columns = columns
        self._index = index
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._index[key]]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._columns

    def values(self):
        return list(self._values)

    def items(self):
        return list(zip(self._columns, self._values))

    def get(self, key, default=None):
        i = self._index.get(key)
        return self._values[i] if i is not None else default


def _python_type(duckdb_type):
    t = (duckdb_type or "").upper()
    if "CHAR" in t or "TEXT" in t or t == "STRING" or "UUID" in t:
        return str
    if "INT" in t or t in ("HUGEINT", "UBIGINT") or "SERIAL" in t:
        return int
    if any(k in t for k in ("DOUBLE", "REAL", "FLOAT", "DECIMAL", "NUMERIC")):
        return float
    return str


class DuckDBDialect(Dialect):
    def escape_identifier(self, name):
        # ANSI double-quote quoting, doubling any embedded quotes
        return '"{}"'.format(str(name).replace('"', '""'))

    def adapt_parameters(self, sql, params):
        # DuckDB's driver doesn't accept `:name`; convert to `$name` (lexically,
        # so `::` casts and `:` inside strings/comments are left alone). Also
        # drop params the query doesn't reference -- DuckDB errors on excess
        # named params where SQLite silently ignores them.
        new_sql, names = rewrite_named_parameters(sql, lambda name: "$" + name)
        if isinstance(params, dict):
            used = set(names)
            params = {k: v for k, v in params.items() if k in used}
        return new_sql, params

    def fts_search_clause(self, *, fts_table, fts_pk, column, param, raw):
        # The DuckDB fts extension indexes a base table into schema
        # fts_main_<table>, queried via match_bm25(doc_id, query[, fields]).
        macro = self.escape_identifier("fts_main_" + fts_table) + ".match_bm25"
        if fts_pk == "rowid":
            # match_bm25 expands to a correlated subquery that joins
            # fts_main_<table>.docs, which has its *own* rowid pseudocolumn -- a
            # bare `rowid` doc-id gets shadow-captured there and the subquery
            # returns multiple rows (hard error). Qualify it to the base table's
            # rowid. (rowid is a fine doc-id within an immutable converted file;
            # see #4.)
            pk = self.escape_identifier(fts_table) + ".rowid"
        else:
            pk = self.escape_identifier(fts_pk)
        # conjunctive := true -> all query terms must match, matching SQLite
        # FTS5's default (DuckDB's match_bm25 defaults to any-term/OR). Keeps
        # multi-word searches like "SEIU 1398" precise instead of returning
        # everything containing either term.
        if column is None:
            return f"{macro}({pk}, :{param}, conjunctive := true) is not null"
        col_literal = "'" + column.replace("'", "''") + "'"
        return (
            f"{macro}({pk}, :{param}, fields := {col_literal}, "
            "conjunctive := true) is not null"
        )

    def date_extract_sql(self, column_sql):
        # DuckDB's date() raises on non-date input (date('') and date('garbage')
        # both error), unlike SQLite's date() which returns NULL. try_cast gives
        # the NULL-on-failure semantics DateFacet relies on. On a column already
        # typed DATE/TIMESTAMP this is a cheap no-op cast.
        return f"try_cast({column_sql} as date)"

    def date_facet_suggest_where(self, column_sql):
        # No glob guard needed: DuckDB's try_cast is strict (try_cast('12345'
        # as date) is NULL, not a Julian-day date as SQLite's date() would
        # give), so "extracts cleanly" is a sufficient date-likeness test.
        return f"try_cast({column_sql} as date) is not null"


class DuckDBBackend(Backend):
    name = "duckdb"

    dialect = DuckDBDialect()

    features = Features(
        # DuckDB has a rowid pseudocolumn, so it behaves like SQLite: keyless
        # tables get keyset pagination + rowid row pages. rowid is an unstable
        # permalink in both engines (SQLite VACUUM / DuckDB rebuild renumber
        # it); removing rowid-based row pages on immutable DBs is a shared fix
        # for both backends, tracked in #12 — not special-cased here.
        supports_rowid=True,
        supports_fts=True,  # via the fts extension + match_bm25 (per-table index)
        supports_json=False,  # DuckDB has JSON, but not SQLite's json_each() shape
        supports_glob=False,
        supports_load_extension=True,
        supports_attach=True,
        supports_write=False,  # read-only analytic browsing for now
        supports_explain=True,
    )

    def connect(self, db, write=False):
        import duckdb

        if db.is_memory:
            return duckdb.connect(":memory:")
        # read_only allows multiple concurrent reader connections (Datasette
        # opens one per thread)
        return duckdb.connect(db.path, read_only=not write)

    # crossdb_attach_limit inherits the base default (None): DuckDB has no
    # SQLITE_LIMIT_ATTACHED-style ceiling on the number of attached databases.

    def prepare_connection(self, conn, datasette, database_name):
        # We deliberately do NOT fire the prepare_connection plugin hook here:
        # those plugins assume a sqlite3 connection.
        if datasette.crossdb and database_name == "_memory":
            self.attach_others_for_crossdb(conn, datasette, database_name)

    def attach_others_for_crossdb(self, conn, datasette, current_db_name):
        # Attach every DuckDB-backed database read-only into the _memory host
        # connection so a query there can join across them (e.g. the union_names
        # canned query). DuckDB has no attach-count ceiling. Only this backend's
        # own databases are attachable; a different backend's files aren't
        # DuckDB, so skip them (we don't support mixed-backend crossdb).
        for db_name, db in datasette.databases.items():
            if db.is_memory or db.backend.name != self.name:
                continue
            conn.execute(f"ATTACH '{db.path}' AS \"{db_name}\" (READ_ONLY)")

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
        import duckdb
        from datasette.database import Results, QueryInterrupted, QueryError

        duck_sql, params = self.dialect.adapt_parameters(sql, params)
        cursor = conn.cursor()
        # Enforce the time limit with a watchdog: DuckDB has no progress
        # handler, but cursor.interrupt() from another thread cancels the
        # running query (raising InterruptException).
        timer = None
        if time_limit_ms and time_limit_ms > 0:
            timer = threading.Timer(time_limit_ms / 1000.0, cursor.interrupt)
            timer.start()
        try:
            if params:
                cursor.execute(duck_sql, params)
            else:
                cursor.execute(duck_sql)
            description = cursor.description or []
            columns = [d[0] for d in description]
            if max_returned_rows == page_size:
                max_returned_rows += 1
            if max_returned_rows and truncate:
                raw = cursor.fetchmany(max_returned_rows + 1)
                truncated = len(raw) > max_returned_rows
                raw = raw[:max_returned_rows]
            else:
                raw = cursor.fetchall()
                truncated = False
        except duckdb.InterruptException as e:
            raise QueryInterrupted(e, sql, params)
        except duckdb.Error as e:
            # Don't let the duckdb-specific exception escape the backend
            raise QueryError(e, sql, params)
        finally:
            if timer is not None:
                timer.cancel()
        # Wrap tuples so downstream row["col"] and row[i] both work, sharing one
        # column->index map across the result rather than a dict per row.
        index = {c: i for i, c in enumerate(columns)}
        rows = [DuckDBRow(columns, index, r) for r in raw]
        return Results(rows, truncated, description)

    def stream_query(self, conn, sql, params, *, chunk_size, time_limit_ms):
        import duckdb
        from datasette.database import QueryInterrupted, QueryError

        duck_sql, params = self.dialect.adapt_parameters(sql, params)
        cursor = conn.cursor()

        def guarded(fn):
            # DuckDB has no progress handler; cursor.interrupt() from a watchdog
            # thread cancels the in-flight compute. Arm it around each step so a
            # chunk that hangs in the engine (a blocking sort/join) is bounded,
            # while idle time between chunks (a slow client) is not.
            timer = None
            if time_limit_ms and time_limit_ms > 0:
                timer = threading.Timer(time_limit_ms / 1000.0, cursor.interrupt)
                timer.start()
            try:
                return fn()
            except duckdb.InterruptException as e:
                raise QueryInterrupted(e, sql, params)
            except duckdb.Error as e:
                raise QueryError(e, sql, params)
            finally:
                if timer is not None:
                    timer.cancel()

        if params:
            guarded(lambda: cursor.execute(duck_sql, params))
        else:
            guarded(lambda: cursor.execute(duck_sql))
        columns = [d[0] for d in (cursor.description or [])]
        index = {c: i for i, c in enumerate(columns)}
        while True:
            raw = guarded(lambda: cursor.fetchmany(chunk_size))
            if not raw:
                break
            yield columns, [DuckDBRow(columns, index, r) for r in raw]

    def introspector(self, db):
        return DuckDBIntrospector(db)


class DuckDBIntrospector(Introspector):
    async def table_names(self):
        results = await self.db.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'main' and table_type = 'BASE TABLE' "
            "order by table_name"
        )
        return [r[0] for r in results.rows]

    async def view_names(self):
        results = await self.db.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'main' and table_type = 'VIEW' "
            "order by table_name"
        )
        return [r[0] for r in results.rows]

    async def table_exists(self, table):
        results = await self.db.execute(
            "select 1 from information_schema.tables "
            "where table_schema = 'main' and table_name = :t "
            "and table_type = 'BASE TABLE'",
            {"t": table},
        )
        return bool(results.rows)

    async def view_exists(self, view):
        results = await self.db.execute(
            "select 1 from information_schema.tables "
            "where table_schema = 'main' and table_name = :t "
            "and table_type = 'VIEW'",
            {"t": view},
        )
        return bool(results.rows)

    async def table_columns(self, table):
        results = await self.db.execute(
            "select column_name from information_schema.columns "
            "where table_schema = 'main' and table_name = :t "
            "order by ordinal_position",
            {"t": table},
        )
        return [r[0] for r in results.rows]

    async def primary_keys(self, table):
        results = await self.db.execute(
            "select constraint_column_names from duckdb_constraints() "
            "where schema_name = 'main' and table_name = :t "
            "and constraint_type = 'PRIMARY KEY'",
            {"t": table},
        )
        if results.rows:
            return list(results.rows[0][0])
        return []

    async def table_column_details(self, table):
        pks = set(await self.primary_keys(table))
        results = await self.db.execute(
            "select ordinal_position, column_name, data_type, is_nullable, "
            "column_default from information_schema.columns "
            "where table_schema = 'main' and table_name = :t "
            "order by ordinal_position",
            {"t": table},
        )
        columns = []
        for r in results.rows:
            name = r[1]
            columns.append(
                Column(
                    cid=r[0],
                    name=name,
                    type=r[2],
                    notnull=1 if r[3] == "NO" else 0,
                    default_value=r[4],
                    is_pk=(1 if name in pks else 0),
                    hidden=0,
                )
            )
        return columns

    async def column_details_with_uniqueness(self, table):
        details = await self.table_column_details(table)
        return {col.name: (_python_type(col.type), bool(col.is_pk)) for col in details}

    async def foreign_keys_for_table(self, table):
        # Outbound single-column foreign keys (Datasette ignores compound FKs)
        results = await self.db.execute(
            "select constraint_column_names, referenced_table, "
            "referenced_column_names from duckdb_constraints() "
            "where schema_name = 'main' and table_name = :t "
            "and constraint_type = 'FOREIGN KEY'",
            {"t": table},
        )
        fks = []
        for cols, other_table, other_cols in results.rows:
            if len(cols) == 1 and len(other_cols) == 1:
                fks.append(
                    {
                        "column": cols[0],
                        "other_table": other_table,
                        "other_column": other_cols[0],
                    }
                )
        return fks

    async def get_all_foreign_keys(self):
        names = await self.table_names()
        result = {name: {"incoming": [], "outgoing": []} for name in names}
        results = await self.db.execute(
            "select table_name, constraint_column_names, referenced_table, "
            "referenced_column_names from duckdb_constraints() "
            "where schema_name = 'main' and constraint_type = 'FOREIGN KEY'"
        )
        for table_name, cols, other_table, other_cols in results.rows:
            if len(cols) != 1 or len(other_cols) != 1:
                continue
            if table_name not in result or other_table not in result:
                continue
            from_, to_ = cols[0], other_cols[0]
            result[table_name]["outgoing"].append(
                {"other_table": other_table, "column": from_, "other_column": to_}
            )
            result[other_table]["incoming"].append(
                {"other_table": table_name, "column": to_, "other_column": from_}
            )
        return result

    async def fts_table(self, table):
        # An fts index on <table> lives in schema fts_main_<table>. Datasette
        # uses the returned name as the FTS resource; for DuckDB that's the base
        # table itself (the dialect builds the fts_main_<table>.match_bm25 call).
        results = await self.db.execute(
            "select schema_name from information_schema.schemata "
            "where schema_name = :s",
            {"s": "fts_main_" + table},
        )
        return table if results.rows else None

    async def hidden_table_names(self):
        return []

    async def get_table_definition(self, table, type_="table"):
        # DuckDB has no sqlite_master, but duckdb_tables()/duckdb_views() each
        # expose a `sql` column with the full CREATE statement — including the
        # inline PRIMARY KEY / FOREIGN KEY our converter emits. Scope to the
        # main schema, matching the rest of this introspector.
        catalog = "duckdb_views()" if type_ == "view" else "duckdb_tables()"
        name_col = "view_name" if type_ == "view" else "table_name"
        results = await self.db.execute(
            f"select sql from {catalog} "
            f"where schema_name = 'main' and {name_col} = :t",
            {"t": table},
        )
        if not results.rows:
            return None
        ddl = results.rows[0][0]
        if ddl is None:
            return None
        bits = [ddl.rstrip(";") + ";"]
        if type_ != "view":
            # Append any user-defined indexes. Skip is_primary (the PK is
            # already inline in the table DDL) and rows with no reconstructable
            # sql, mirroring the SQLite backend's "sql is not null" filter.
            index_results = await self.db.execute(
                "select sql from duckdb_indexes() "
                "where schema_name = 'main' and table_name = :t "
                "and is_primary = false and sql is not null",
                {"t": table},
            )
            for row in index_results.rows:
                bits.append(row[0].rstrip(";") + ";")
        return "\n".join(bits)

    async def attached_databases(self):
        from datasette.database import AttachedDatabase

        # On the crossdb host (_memory) the other databases are ATTACHed; list
        # them so the database page can show what's joinable. Exclude system/temp
        # (internal) and the host's own catalog (current_database() -- 'memory'
        # for the in-memory host). For a regular database this is empty.
        results = await self.db.execute(
            "select database_name, path from duckdb_databases() "
            "where not internal and database_name != current_database() "
            "order by database_name"
        )
        return [
            AttachedDatabase(
                seq=i + 1, name=row["database_name"], file=row["path"] or ""
            )
            for i, row in enumerate(results.rows)
        ]

    async def schema_version(self):
        # DuckDB has no cheap schema-version token; treat as static so the
        # catalog is populated once (fine for read-only analytic browsing).
        return 0
