"""
The default SQLite backend.

This is a faithful extraction of the connection and query-execution code that
previously lived inline in ``datasette.database.Database``. Behaviour is
intended to be identical; the existing test suite is the correctness oracle.
"""

import sys

from ..utils import sqlite_timelimit, sqlite3
from . import Backend, Features


class SqliteBackend(Backend):
    name = "sqlite"

    features = Features(
        supports_rowid=True,
        supports_fts=True,
        supports_json=True,
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
        from ..database import Results, QueryInterrupted

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
                raise

        if truncate:
            return Results(rows, truncated, cursor.description)
        else:
            return Results(rows, False, cursor.description)
