from datasette import hookimpl
from datasette.database import Database
from .backend import DuckDBBackend

__all__ = ["DuckDBBackend"]


@hookimpl
def startup(datasette):
    # Mount DuckDB databases declared in plugin config:
    #   {"plugins": {"datasette-duckdb": {"databases": {"name": "/path.duckdb"}}}}
    config = datasette.plugin_config("datasette-duckdb") or {}
    databases = config.get("databases") or {}

    async def inner():
        for name, path in databases.items():
            if name in datasette.databases:
                continue
            db = Database(
                datasette, path=path, is_mutable=False, backend=DuckDBBackend()
            )
            datasette.add_database(db, name=name)
        # If --crossdb is on and we mounted DuckDB databases, the cross-database
        # host (_memory) must be DuckDB-backed so its connection can ATTACH the
        # .duckdb files and run cross-database queries in DuckDB syntax. Core
        # creates _memory with the default (SQLite) backend, so re-back it here.
        # We don't support a mixed SQLite+DuckDB crossdb: one _memory, one
        # backend -- the engine that owns the data owns the crossdb host.
        if databases and datasette.crossdb and "_memory" in datasette.databases:
            datasette.remove_database("_memory")
            datasette.add_database(
                Database(
                    datasette, is_mutable=False, is_memory=True, backend=DuckDBBackend()
                ),
                name="_memory",
            )

    return inner
