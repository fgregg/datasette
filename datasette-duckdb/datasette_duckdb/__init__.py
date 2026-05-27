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

    return inner
