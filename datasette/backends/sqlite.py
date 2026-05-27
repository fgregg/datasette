"""
The default SQLite backend.

This is a faithful extraction of the connection and query-execution code that
previously lived inline in ``datasette.database.Database``. Behaviour is
intended to be identical; the existing test suite is the correctness oracle.
"""

import sys

import sqlite_utils

from ..utils import (
    detect_fts,
    detect_json1,
    detect_primary_keys,
    detect_spatialite,
    escape_sqlite,
    get_all_foreign_keys,
    get_outbound_foreign_keys,
    sqlite_timelimit,
    sqlite3,
    table_columns,
    table_column_details,
)
from ..utils.sqlite import sqlite_version
from . import Backend, Dialect, Features, Introspector


class SqliteDialect(Dialect):
    def escape_identifier(self, name):
        return escape_sqlite(name)

    def fts_search_clause(self, *, fts_table, fts_pk, column, param, raw):
        match = f":{param}" if raw else f"escape_fts(:{param})"
        if column is None:
            return "{pk} in (select rowid from {t} where {t} match {m})".format(
                pk=self.escape_identifier(fts_pk),
                t=self.escape_identifier(fts_table),
                m=match,
            )
        return "rowid in (select rowid from {t} where {c} match {m})".format(
            t=self.escape_identifier(fts_table),
            c=self.escape_identifier(column),
            m=match,
        )


class SqliteBackend(Backend):
    name = "sqlite"

    dialect = SqliteDialect()

    features = Features(
        supports_rowid=True,
        supports_fts=True,
        # Reflects whether this SQLite build has the JSON1 extension, matching
        # the historical detect_json1() gate on the array-contains filters.
        supports_json=detect_json1(),
        supports_glob=True,
        supports_load_extension=True,
        supports_attach=True,
        supports_write=True,
        supports_explain=True,
    )

    def connect(self, db, write: bool = False):
        extra_kwargs = {}
        if write:
            extra_kwargs["isolation_level"] = "IMMEDIATE"
        if db.memory_name:
            uri = "file:{}?mode=memory&cache=shared".format(db.memory_name)
            conn = sqlite3.connect(
                uri, uri=True, check_same_thread=False, **extra_kwargs
            )
            if not write:
                conn.execute("PRAGMA query_only=1")
            return conn
        if db.is_memory:
            return sqlite3.connect(":memory:", uri=True)

        # mode=ro or immutable=1?
        if db.is_mutable:
            qs = "?mode=ro"
            if db.ds.nolock:
                qs += "&nolock=1"
        else:
            qs = "?immutable=1"
        assert not (write and not db.is_mutable)
        if write:
            qs = ""
        if db.mode is not None:
            qs = f"?mode={db.mode}"
        conn = sqlite3.connect(
            f"file:{db.path}{qs}", uri=True, check_same_thread=False, **extra_kwargs
        )
        db._all_file_connections.append(conn)
        if db.is_temp_disk and not db._wal_enabled:
            conn.execute("PRAGMA journal_mode=WAL")
            db._wal_enabled = True
        return conn

    def prepare_connection(self, conn, datasette, database_name):
        # Imported lazily to avoid a circular import at module load time:
        # app imports database imports this module.
        from ..app import INTERNAL_DB_NAME, SQLITE_LIMIT_ATTACHED
        from ..plugins import pm

        conn.row_factory = sqlite3.Row
        conn.text_factory = lambda x: str(x, "utf-8", "replace")
        if datasette.sqlite_extensions and database_name != INTERNAL_DB_NAME:
            conn.enable_load_extension(True)
            for extension in datasette.sqlite_extensions:
                # "extension" is either a string path to the extension
                # or a 2-item tuple that specifies which entrypoint to load.
                if isinstance(extension, tuple):
                    path, entrypoint = extension
                    conn.execute("SELECT load_extension(?, ?)", [path, entrypoint])
                else:
                    conn.execute("SELECT load_extension(?)", [extension])
        if datasette.setting("cache_size_kb"):
            conn.execute(f"PRAGMA cache_size=-{datasette.setting('cache_size_kb')}")
        # pylint: disable=no-member
        if database_name != INTERNAL_DB_NAME:
            pm.hook.prepare_connection(
                conn=conn, database=database_name, datasette=datasette
            )
        # If crossdb and this is _memory, connect the first
        # SQLITE_LIMIT_ATTACHED databases
        if datasette.crossdb and database_name == "_memory":
            count = 0
            for db_name, db in datasette.databases.items():
                if count >= SQLITE_LIMIT_ATTACHED or db.is_memory:
                    continue
                sql = 'ATTACH DATABASE "file:{path}?{qs}" AS [{name}];'.format(
                    path=db.path,
                    qs="mode=ro" if db.is_mutable else "immutable=1",
                    name=db_name,
                )
                conn.execute(sql)
                count += 1

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
        # Imported lazily to avoid a circular import: database.py imports this
        # module at load time, and these classes live in database.py.
        from ..database import Results, QueryInterrupted, QueryError

        with sqlite_timelimit(conn, time_limit_ms):
            try:
                cursor = conn.cursor()
                cursor.execute(sql, params if params is not None else {})
                if max_returned_rows == page_size:
                    max_returned_rows += 1
                if max_returned_rows and truncate:
                    rows = cursor.fetchmany(max_returned_rows + 1)
                    truncated = len(rows) > max_returned_rows
                    rows = rows[:max_returned_rows]
                else:
                    rows = cursor.fetchall()
                    truncated = False
            except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
                if e.args == ("interrupted",):
                    raise QueryInterrupted(e, sql, params)
                if log_sql_errors:
                    sys.stderr.write(
                        "ERROR: conn={}, sql = {}, params = {}: {}\n".format(
                            conn, repr(sql), params, e
                        )
                    )
                    sys.stderr.flush()
                # Don't let the sqlite3-specific exception escape the backend
                raise QueryError(e, sql, params)

        if truncate:
            return Results(rows, truncated, cursor.description)
        else:
            return Results(rows, False, cursor.description)

    def introspector(self, db):
        return SqliteIntrospector(db)


class SqliteIntrospector(Introspector):
    async def table_names(self):
        results = await self.db.execute(
            "select name from sqlite_master where type='table' order by name"
        )
        return [r[0] for r in results.rows]

    async def view_names(self):
        results = await self.db.execute(
            "select name from sqlite_master where type='view'"
        )
        return [r[0] for r in results.rows]

    async def table_exists(self, table):
        results = await self.db.execute(
            "select 1 from sqlite_master where type='table' and name=?",
            params=(table,),
        )
        return bool(results.rows)

    async def view_exists(self, view):
        results = await self.db.execute(
            "select 1 from sqlite_master where type='view' and name=?",
            params=(view,),
        )
        return bool(results.rows)

    async def table_columns(self, table):
        return await self.db.execute_fn(lambda conn: table_columns(conn, table))

    async def table_column_details(self, table):
        return await self.db.execute_fn(
            lambda conn: table_column_details(conn, table)
        )

    async def column_details_with_uniqueness(self, table):
        # Returns {column_name: (type, is_unique)}
        def column_details(conn):
            db = sqlite_utils.Database(conn)
            columns = db[table].columns_dict
            indexes = db[table].indexes
            details = {}
            for name in columns:
                is_unique = any(
                    index
                    for index in indexes
                    if index.columns == [name] and index.unique
                )
                details[name] = (columns[name], is_unique)
            return details

        return await self.db.execute_fn(column_details)

    async def primary_keys(self, table):
        return await self.db.execute_fn(
            lambda conn: detect_primary_keys(conn, table)
        )

    async def fts_table(self, table):
        return await self.db.execute_fn(lambda conn: detect_fts(conn, table))

    async def foreign_keys_for_table(self, table):
        return await self.db.execute_fn(
            lambda conn: get_outbound_foreign_keys(conn, table)
        )

    async def get_all_foreign_keys(self):
        return await self.db.execute_fn(get_all_foreign_keys)

    async def schema_version(self):
        return (await self.db.execute("PRAGMA schema_version")).first()[0]

    async def attached_databases(self):
        # Defined in database.py; imported lazily to avoid an import cycle.
        from ..database import AttachedDatabase

        # This used to be:
        #   select seq, name, file from pragma_database_list() where seq > 0
        # But SQLite prior to 3.16.0 doesn't support pragma functions
        results = await self.db.execute("PRAGMA database_list;")
        # {'seq': 0, 'name': 'main', 'file': ''}
        return [
            AttachedDatabase(*row)
            for row in results.rows
            # Filter out the SQLite internal "temp" database, refs #2557
            if row["seq"] > 0 and row["name"] != "temp"
        ]

    async def get_table_definition(self, table, type_="table"):
        table_definition_rows = list(
            await self.db.execute(
                "select sql from sqlite_master where name = :n and type=:t",
                {"n": table, "t": type_},
            )
        )
        if not table_definition_rows:
            return None
        bits = [table_definition_rows[0][0] + ";"]
        # Add on any indexes
        index_rows = list(
            await self.db.execute(
                "select sql from sqlite_master where tbl_name = :n and type='index' and sql is not null",
                {"n": table},
            )
        )
        for index_row in index_rows:
            bits.append(index_row[0] + ";")
        return "\n".join(bits)

    async def hidden_table_names(self):
        hidden_tables = []
        # Add any tables marked as hidden in config
        db_config = self.db.ds.config.get("databases", {}).get(self.db.name, {})
        if "tables" in db_config:
            hidden_tables += [
                t for t in db_config["tables"] if db_config["tables"][t].get("hidden")
            ]

        if sqlite_version()[1] >= 37:
            hidden_tables += [x[0] for x in await self.db.execute("""
                      with shadow_tables as (
                        select name
                        from pragma_table_list
                        where [type] = 'shadow'
                        order by name
                      ),
                      core_tables as (
                        select name
                        from sqlite_master
                        WHERE  name in ('sqlite_stat1', 'sqlite_stat2', 'sqlite_stat3', 'sqlite_stat4')
                          OR substr(name, 1, 1) == '_'
                      ),
                      combined as (
                        select name from shadow_tables
                        union all
                        select name from core_tables
                      )
                      select name from combined order by 1
                    """)]
        else:
            hidden_tables += [x[0] for x in await self.db.execute("""
                      WITH base AS (
                        SELECT name
                        FROM sqlite_master
                        WHERE  name IN ('sqlite_stat1', 'sqlite_stat2', 'sqlite_stat3', 'sqlite_stat4')
                          OR substr(name, 1, 1) == '_'
                      ),
                      fts_suffixes AS (
                        SELECT column1 AS suffix
                        FROM (VALUES ('_data'), ('_idx'), ('_docsize'), ('_content'), ('_config'))
                      ),
                      fts5_names AS (
                        SELECT name
                        FROM sqlite_master
                        WHERE sql LIKE '%VIRTUAL TABLE%USING FTS%'
                      ),
                      fts5_shadow_tables AS (
                        SELECT
                          printf('%s%s', fts5_names.name, fts_suffixes.suffix) AS name
                        FROM fts5_names
                        JOIN fts_suffixes
                      ),
                      fts3_suffixes AS (
                        SELECT column1 AS suffix
                        FROM (VALUES ('_content'), ('_segdir'), ('_segments'), ('_stat'), ('_docsize'))
                      ),
                      fts3_names AS (
                        SELECT name
                        FROM sqlite_master
                        WHERE sql LIKE '%VIRTUAL TABLE%USING FTS3%'
                          OR sql LIKE '%VIRTUAL TABLE%USING FTS4%'
                      ),
                      fts3_shadow_tables AS (
                        SELECT
                          printf('%s%s', fts3_names.name, fts3_suffixes.suffix) AS name
                        FROM fts3_names
                        JOIN fts3_suffixes
                      ),
                      final AS (
                        SELECT name FROM base
                        UNION ALL
                        SELECT name FROM fts5_shadow_tables
                        UNION ALL
                        SELECT name FROM fts3_shadow_tables
                      )
                      SELECT name FROM final ORDER BY 1
                    """)]
        # Also hide any FTS tables that have a content= argument
        hidden_tables += [x[0] for x in await self.db.execute("""
                  SELECT name
                  FROM sqlite_master
                  WHERE sql LIKE '%VIRTUAL TABLE%'
                    AND sql LIKE '%USING FTS%'
                    AND sql LIKE '%content=%'
                """)]

        has_spatialite = await self.db.execute_fn(detect_spatialite)
        if has_spatialite:
            # Also hide Spatialite internal tables
            hidden_tables += [
                "ElementaryGeometries",
                "SpatialIndex",
                "geometry_columns",
                "spatial_ref_sys",
                "spatialite_history",
                "sql_statements_log",
                "sqlite_sequence",
                "views_geometry_columns",
                "virts_geometry_columns",
                "data_licenses",
                "KNN",
                "KNN2",
            ] + [
                r[0] for r in (await self.db.execute("""
                        select name from sqlite_master
                        where name like "idx_%"
                        and type = "table"
                    """)).rows
            ]

        return hidden_tables
