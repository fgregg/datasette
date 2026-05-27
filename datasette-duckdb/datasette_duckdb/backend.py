"""
A DuckDB backend for Datasette, implemented against the backend seams in
``datasette.backends`` (Backend / Dialect / Introspector / Features).

Proof of concept: read-only analytic browsing. Known gaps (tracked as we go):
- No query time limit yet (DuckDB has no progress handler; needs interrupt()).
- Foreign keys / FTS / table definitions are not introspected yet.
"""

from datasette.backends import (
    Backend,
    Dialect,
    Features,
    Introspector,
    rewrite_named_parameters,
)
from datasette.utils import Column, CustomRow


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


class DuckDBBackend(Backend):
    name = "duckdb"

    dialect = DuckDBDialect()

    features = Features(
        supports_rowid=False,  # no stable implicit rowid -> offset pagination
        supports_fts=False,
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

    def prepare_connection(self, conn, datasette, database_name):
        # Nothing to do yet. We deliberately do NOT fire the prepare_connection
        # plugin hook here: those plugins assume a sqlite3 connection.
        pass

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
        from datasette.database import Results, QueryError

        # TODO: enforce time_limit_ms via conn.interrupt() from a watchdog.
        duck_sql, params = self.dialect.adapt_parameters(sql, params)
        cursor = conn.cursor()
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
        except duckdb.Error as e:
            # Don't let the duckdb-specific exception escape the backend
            raise QueryError(e, sql, params)
        # Wrap tuples so downstream row["col"] and row[i] both work
        rows = [CustomRow(columns, dict(zip(columns, r))) for r in raw]
        return Results(rows, truncated, description)

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
            "where table_name = :t and table_type = 'BASE TABLE'",
            {"t": table},
        )
        return bool(results.rows)

    async def view_exists(self, view):
        results = await self.db.execute(
            "select 1 from information_schema.tables "
            "where table_name = :t and table_type = 'VIEW'",
            {"t": view},
        )
        return bool(results.rows)

    async def table_columns(self, table):
        results = await self.db.execute(
            "select column_name from information_schema.columns "
            "where table_name = :t order by ordinal_position",
            {"t": table},
        )
        return [r[0] for r in results.rows]

    async def primary_keys(self, table):
        results = await self.db.execute(
            "select constraint_column_names from duckdb_constraints() "
            "where table_name = :t and constraint_type = 'PRIMARY KEY'",
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
            "where table_name = :t order by ordinal_position",
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
        return {
            col.name: (_python_type(col.type), bool(col.is_pk)) for col in details
        }

    async def foreign_keys_for_table(self, table):
        # TODO: introspect DuckDB foreign keys via duckdb_constraints()
        return []

    async def get_all_foreign_keys(self):
        names = await self.table_names()
        return {name: {"incoming": [], "outgoing": []} for name in names}

    async def fts_table(self, table):
        return None

    async def hidden_table_names(self):
        return []

    async def get_table_definition(self, table, type_="table"):
        # TODO: reconstruct DDL (DuckDB has no sqlite_master.sql equivalent)
        return None

    async def attached_databases(self):
        return []

    async def schema_version(self):
        # DuckDB has no cheap schema-version token; treat as static so the
        # catalog is populated once (fine for read-only analytic browsing).
        return 0
