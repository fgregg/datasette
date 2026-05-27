# Backend protocol sketch

A proposed interface for letting a plugin supply a non-SQLite query backend,
derived from the seams in `design/backend-abstraction-audit.md` and the direction
in simonw/datasette#1193 ("a SQL builder that could be customized per-database").

**Design principles**

1. **No impersonation.** A backend implements an interface; it does not pretend to
   be `sqlite3`. This is the explicit lesson from `datasette-parquet`.
2. **SQLite is just the default backend.** We refactor today's behavior into a
   `SqliteBackend` that implements the protocol. If the test suite passes
   unchanged with SQLite routed through the protocol, the abstraction is correct
   by construction (dogfooding).
3. **Minimal core surface.** Keep the async-over-threads execution core in
   `Database` (it's already backend-neutral) and delegate only the SQLite-specific
   bits out to the backend.
4. **Capability-driven adaptation (Django's model).** Backends *advertise* what
   they support; the rest of the code reads those flags and adapts. DuckDB does not
   advertise `rowid` or `fts`, so the keyset-pagination and search-box paths take
   their fallback branches automatically — no DuckDB-specific conditionals
   scattered through the codebase, just `if features.supports_X`. This is exactly
   Django's `DatabaseFeatures` split.

This maps almost one-to-one onto Django's backend architecture:

| Datasette object | Django equivalent | Concern |
|------------------|-------------------|---------|
| `Backend` | `DatabaseWrapper` | connections + execution lifecycle |
| `Features` | `DatabaseFeatures` | capability flags |
| `Dialect` | `DatabaseOperations` | SQL-string generation |
| `Introspector` | `DatabaseIntrospection` | schema queries |

---

## 1. Registration & resolution

New plugin hook (in `datasette/hookspecs.py`):

```python
@hookspec
def register_backends(datasette):
    "Return a list of Backend subclasses (or instances) this plugin provides."
```

A database opts into a backend via config, falling back to SQLite:

```json
{
  "databases": {
    "warehouse": {
      "backend": "duckdb",
      "source": "/data/warehouse.duckdb"
    }
  }
}
```

Resolution order when `Datasette.add_database` runs:
1. explicit `backend:` key in the database's config, else
2. a backend that claims the path/URL scheme (`Backend.handles(source) -> bool`),
   else
3. `SqliteBackend` (default).

Backends are keyed by `Backend.name` (`"sqlite"`, `"duckdb"`, …).

## 2. `Backend` — connections, execution, capabilities

Owns everything currently hardcoded in `Database.connect`,
`app._prepare_connection`, and `Database.execute` (audit seams 1, 2, 3, 4, 10, 11).

```python
class Backend:
    name: str                       # "sqlite", "duckdb"
    dialect: "Dialect"
    features: "Features"            # Django-style capability flags

    @classmethod
    def handles(cls, source: str) -> bool:
        "Does this backend recognise this path / URL? (scheme/extension match)"

    # --- connection lifecycle (replaces Database.connect + _prepare_connection)
    def connect(self, db: "Database", *, write: bool = False) -> "Connection": ...
    def prepare_connection(self, conn, db: "Database", datasette) -> None:
        "row/text factory, extensions, cache settings, ATTACH, plugin hook."

    # --- read execution (replaces the body of Database.execute)
    def execute_query(
        self, conn, sql: str, params,
        *, time_limit_ms: int, max_returned_rows: int, truncate: bool,
    ) -> "Results":
        """Run a read query and return Results(rows, truncated, columns).

        Each backend owns: param-style adaptation, row wrapping (-> Row),
        the time-limit strategy, and mapping engine errors to QueryInterrupted.
        """

    # --- write execution primitives (used by the write-thread fns)
    def execute_write(self, conn, sql, params): ...
    def execute_write_many(self, conn, sql, params_seq): ...
    def execute_write_script(self, conn, sql): ...

    # --- safety
    def validate_read_sql(self, sql: str) -> None:
        "raise InvalidSql if not a permitted read statement (per-dialect allowlist)."

    def introspector(self, db: "Database") -> "Introspector": ...
```

`Connection` is a *narrow* protocol — only what the execution core in
`database.py` actually touches — not the full `sqlite3` API:

```python
class Connection(Protocol):
    def execute(self, sql: str, params=...) -> Any: ...
    def cursor(self) -> Any: ...
    def close(self) -> None: ...
    def __enter__(self): ...           # transaction context
    def __exit__(self, *exc): ...
```

SQLite's connection already satisfies this. DuckDB needs a *thin* adapter (cursor
+ transaction + param-style only) — not a sqlite emulation.

### Time limit strategy (audit seam 10 — the safety gap)

`execute_query` owns the timeout so each backend uses its native mechanism:

- **SQLite:** today's `conn.set_progress_handler` deadline (`utils:240`).
- **DuckDB:** spawn a watchdog that calls `conn.interrupt()` after the deadline;
  catch the resulting error and raise `QueryInterrupted`.

This removes `sqlite_timelimit` from `Database.execute` and makes "no timeout" an
explicit, visible choice rather than a silent gap.

### `Features` — capability advertisement (audit seam 11)

The backbone of adaptation. Modeled on Django's `DatabaseFeatures`: a flat set of
declarative flags the backend advertises, that every consumer reads instead of
sniffing versions or hardcoding `if backend == "duckdb"`.

```python
@dataclass(frozen=True)
class Features:
    supports_rowid: bool = True          # implicit rowid for keyless tables
    supports_fts: bool = False           # full-text search virtual tables
    supports_json: bool = False          # json_each / json_extract family
    supports_glob: bool = False          # GLOB operator
    supports_load_extension: bool = False
    supports_attach: bool = False        # cross-database ATTACH
    supports_write: bool = True
    supports_explain: bool = True
    # date facet support depends on a date() expression the dialect provides

class SqliteFeatures(Features):
    supports_rowid = True
    supports_fts = True
    supports_json = True                 # was detect_json1()
    supports_glob = True
    supports_load_extension = True
    supports_attach = True

class DuckDBFeatures(Features):
    supports_rowid = False               # -> keyless tables take the fallback path
    supports_fts = False                 # -> no search box, no match filter
    supports_json = True                 # different functions; dialect maps them
    supports_glob = False                # -> glob filter / date-glob facet disabled
    supports_write = True
```

Replaces scattered `sqlite_version() >= …` / `detect_json1()` checks.

### How consumers adapt to `features`

The point of advertisement is that absence cascades through existing code as
plain `if features.supports_X` branches — no backend names anywhere:

| Flag off | Consumer | Adaptation |
|----------|----------|------------|
| `supports_rowid` | `table.py` keyless-table paths (`191,195,253,256,374,403,554,629,1182-1188,1278`), `path_from_row_pks`, `row_sql_params_pks` | take the no-rowid branch: require a PK, derive a key, or fall back to OFFSET pagination (see §3) |
| `supports_fts` | `detect_fts`/`fts_table`, search box in `table.py`, `_search` filter in `filters.py:79-106` | no search UI offered; `match` filter unavailable |
| `supports_json` | array facets (`facets.py:320-414`), `arraycontains` filters (`filters.py:325,331`) | facet/filter not suggested |
| `supports_glob` | `glob` filter (`filters.py:316`), date facet glob (`facets.py:482`) | operator omitted from filter list |
| `supports_load_extension` | `_prepare_connection` (`app.py:1236-1244`) | extension loading skipped |
| `supports_attach` | `crossdb` ATTACH (`app.py:1251-1262`) | cross-db disabled |
| `supports_write` | write UIs / `execute_write*` | database treated read-only |

Concretely, today's `if not pks:` (assume rowid) sites become
`if not pks and self.backend.features.supports_rowid:` … `else:` fallback — so the
adaptation lives in shared code, parameterized by the flag, not duplicated per
backend.

## 3. `Dialect` — pure SQL-string generation

Centralizes audit seams 6, 7, 8, 9. No I/O — just string building, so it's
trivially testable.

```python
class Dialect:
    def escape_identifier(self, name: str) -> str:
        "[name] for SQLite, \"name\" for DuckDB/Postgres. Replaces escape_sqlite()."

    def escape_string(self, s: str) -> str: ...

    # pagination (replaces compound_keys_after_sql + rowid assumptions)
    def keyset_after_sql(self, pks: list[str], start_index: int) -> str: ...
    def row_key_columns(self, pks: list[str]) -> list[str]:
        "What to select/order by for a row's stable identity. SQLite -> ['rowid'] "
        "when no PKs; backends without rowid must supply an alternative (seam 7)."

    # filter operators (replaces the per-operator fragments in filters.py)
    def operator_sql(self, op: str, column_sql: str, param: str) -> str | None:
        "e.g. op='glob' -> dialect-specific; return None if unsupported."

    # facet building blocks (replaces json_each/json_type/date()+glob in facets.py)
    def json_array_items_sql(self, column_sql: str) -> str | None: ...
    def date_trunc_day_sql(self, column_sql: str) -> str | None: ...
```

The existing `Filter` classes in `filters.py` and the facet classes in
`facets.py` would ask `db.backend.dialect` for fragments instead of hardcoding
SQLite syntax. Where `operator_sql`/`json_*`/`date_*` return `None`, that
filter/facet is unavailable on that backend (consistent with `Features`, which
should be the source of truth — a `None` fragment and a `False` flag must agree).

### The `rowid` problem (audit seam 7)

With capability advertisement the *mechanism* is now settled: a backend that sets
`supports_rowid = False` makes every keyless-table path take its fallback branch
(see the adaptation table), and `Dialect.row_key_columns` returns the fallback
identity instead of `["rowid"]`. No DuckDB-specific code is involved.

What still has to be *chosen* is the fallback behavior when `supports_rowid` is
off and a table has no PK:
- require a primary key (error if none),
- derive a key from a unique column during introspection,
- fall back to `OFFSET` pagination (loses keyset stability — acceptable for some
  analytic browsing).

This is a behavior decision to make before the DuckDB plugin ships, but it's no
longer an architectural unknown — it's the body of one `else` branch gated on the
flag.

## 4. `Introspector` — schema queries

Replaces the raw `sqlite_master` / `PRAGMA` queries in `database.py` and
`utils/__init__.py` (audit seam 5) with an async interface. The `Database`
introspection methods become thin pass-throughs to `self.backend.introspector(self)`.

```python
class Introspector:
    async def table_names(self) -> list[str]: ...
    async def view_names(self) -> list[str]: ...
    async def table_exists(self, table: str) -> bool: ...
    async def view_exists(self, view: str) -> bool: ...
    async def columns(self, table: str) -> list[Column]: ...     # name, type, is_pk, ...
    async def primary_keys(self, table: str) -> list[str]: ...
    async def foreign_keys(self, table: str) -> dict: ...         # incoming/outgoing
    async def fts_table(self, table: str) -> str | None: ...
    async def hidden_table_names(self) -> list[str]: ...
    async def table_definition(self, table: str, type_="table") -> str | None: ...
    async def attached_databases(self) -> list: ...
```

- **SQLite impl:** wraps today's queries verbatim (`sqlite_master`, `PRAGMA
  table_xinfo`, `PRAGMA foreign_key_list`, the `hidden_table_names` block).
- **DuckDB impl:** `information_schema` / `duckdb_columns()` /
  `duckdb_constraints()`; `fts_table` / `hidden_table_names` likely return empty
  until FTS support is designed.

`Column` becomes a backend-neutral dataclass (today it's the `PRAGMA table_xinfo`
namedtuple).

## 5. Row representation (audit seam 3)

Define a backend-neutral `Row` with both `row[0]` and `row["col"]` access, and
update the three `isinstance(..., sqlite3.Row)` sites (`renderer.py:62,96`,
`utils/__init__.py:223`) to target it. `SqliteBackend` can keep using
`sqlite3.Row` (it already conforms); `DuckDBBackend.execute_query` wraps tuples +
`cursor.description` into the neutral `Row`.

## 6. Wiring into `Database`

`Database` keeps its async/threading core and delegates the SQLite-specific bits:

```python
class Database:
    def __init__(self, ds, ..., backend: Backend | None = None):
        self.backend = backend or SqliteBackend()

    def connect(self, write=False):
        return self.backend.connect(self, write=write)        # was sqlite3.connect

    async def execute(self, sql, params=None, ...):
        def op(conn):
            return self.backend.execute_query(
                conn, sql, params or {},
                time_limit_ms=..., max_returned_rows=..., truncate=truncate,
            )
        return await self.execute_fn(op)

    async def table_columns(self, table):
        return [c.name for c in await self.backend.introspector(self).columns(table)]
    # ... other introspection methods become pass-throughs
```

`_prepare_connection` in `app.py` calls `db.backend.prepare_connection(conn, db, self)`.

## 7. Rollout plan (dogfood-first)

1. **Extract `SqliteBackend` as a pure refactor.** Move `connect`,
   `prepare_connection`, `execute_query`, the introspection queries, and
   `escape_sqlite`/`compound_keys_after_sql` behind the protocol — SQLite still the
   only backend. **Success = existing test suite passes unchanged.** This validates
   the seams before any DuckDB code exists.
2. **Add `register_backends` hook + config resolution.**
3. **Route `Dialect` through filters/facets.** Replace hardcoded fragments with
   `dialect.*` calls; SQLite dialect returns today's strings. Tests still pass.
4. **Build `datasette-duckdb` as an external plugin** implementing
   `Backend`/`Dialect`/`Introspector`. Start read-only, no FTS, explicit timeout
   via `interrupt()`, and a chosen answer to the `rowid` question.

Step 1 is the real test of this whole design and should be the first PR.

## 7a. Progress & residuals (living checklist)

Tracks the actual extraction on branch `backend-abstraction`. Update as slices land.

**Done (SQLite routed through the protocol, tests green):**
- ✅ Seam 1 — `connect` + `execute_query` → `SqliteBackend`
- ✅ Seam 2 — `prepare_connection` → `SqliteBackend` (and `Datasette._prepare_connection` removed; hook-firing is backend-owned)
- ✅ Seam 5 — `sqlite_master`/`PRAGMA` introspection → `SqliteIntrospector`,
  including `label_column_for_table`'s column+uniqueness lookup
  (`Introspector.column_details_with_uniqueness`)
- ✅ Seam 10 (partial) — query time-limit now lives in `SqliteBackend.execute_query`
- ✅ Seam 11 — `Features` defined (`SqliteBackend.features`) **and now consumed**:
  `Filters` gates `glob` on `supports_glob` and the array-contains operators on
  `supports_json` (which now mirrors `detect_json1()` for SQLite)

**Residuals — still SQLite-coupled, must be extracted before a DuckDB backend works:**
- ⬜ **`Database.table_counts`** — composed of already-abstracted `execute()` /
  `table_names()`, but still catches `sqlite3.OperationalError/DatabaseError`
  directly. Error-type mapping should come from the backend (seam 1 follow-up).
- ⬜ **`validate_sql_select` + `allowed_pragmas`** (`utils`) — SQLite-tuned SQL
  allowlist still called from views; needs per-dialect rules (seam 10).
- ⬜ Seam 3 — row representation still `sqlite3.Row` (renderer + encoder isinstance checks).
- 🟡 Seams 6/8/9 — `Dialect` started: `escape_identifier` + `keyset_after_sql`
  live on `Dialect`/`SqliteDialect` (`backend.dialect`, `Database.dialect`);
  `utils.compound_keys_after_sql` is now a thin alias to the dialect, and
  `table.py` pagination uses `db.dialect.keyset_after_sql`. `Filters` now takes a
  `features` arg and gates operators on it (see seam 11). **Still to convert:**
  the ~39 remaining `escape_sqlite` call sites; the same `Features` gating in
  `facets.py` (array/date facets still call `detect_json1()` directly); and
  per-dialect SQL for operators a backend supports differently (`date()`, FTS
  `match`) rather than just hiding them.
- ⬜ Seam 7 — `rowid` / keyless-table pagination fallback (gated on `supports_rowid`).

## 8. Open questions

- **rowid / keyless pagination** (seam 7) — the `supports_rowid=False` fallback
  *behavior* (require PK vs. derive key vs. OFFSET). Mechanism settled; pick before
  step 4.
- **Timeouts** — confirm `conn.interrupt()` + watchdog is reliable under
  Datasette's thread-per-connection model. (No feature flag hides this; "no
  timeout" should never be silent.)
- **Writes** — is the write-queue thread model meaningful for analytic backends?
  `Features.supports_write = False` marks a backend read-only, but is that the
  right granularity?
- **Cross-database joins** — `crossdb` ATTACH gated on `supports_attach`; SQLite
  only for v1.
- **FTS & faceting** — `supports_fts = False` degrades gracefully (no search box)
  for now; a pluggable backend-FTS interface is a later question.
- **Granularity** — is `Dialect.operator_sql` the right shape, or should each
  `Filter` subclass own per-dialect rendering? (Leaning toward the dialect owning
  it, to keep filters declarative.)
```
