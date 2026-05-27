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

from dataclasses import dataclass


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
