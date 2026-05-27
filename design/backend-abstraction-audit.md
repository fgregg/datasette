# Backend abstraction surface audit

**Goal:** scope what it would take to let Datasette run queries against backends
other than SQLite (DuckDB first), via a well-designed plugin seam rather than a
`datasette-parquet`-style impersonation of SQLite.

**Context / prior art:**
- simonw/datasette#1193 — "Research plugin hook for alternative database backends"
- simonw/datasette#670 — "Prototype for Datasette on PostgreSQL"
- simonw/datasette#968 — "Any thoughts/future plans for using DuckDB?"
- cldellow/datasette-parquet — the one real implementation; wraps DuckDB in a
  `sqlite3`-shaped proxy + `sqlglot` transpile, and disables faceting/timeouts.
  We explicitly do **not** want to go down the impersonation route.

Simon's two working hypotheses from #1193:
1. The `Database` class is small enough to abstract per-backend.
2. SQL generation is concentrated in `TableView`, so a per-database "SQL builder"
   could make it tractable.

This audit tests both against the current code. **Verdict up front:** (1) is
largely true; (2) is *half* true — generation is concentrated but spread across
`table.py`, `filters.py`, `facets.py`, and two shared primitives.

All references are `file:line` against this checkout (branch `no_limit_csv`).

---

## The surface, by seam

### 1. Connection & execution model — `datasette/database.py`

The async-over-threads core is the cleanest seam and is **already backend-neutral**.
`execute_fn`, the write-queue thread `_execute_writes`, `execute_write*`, isolated
connections, and non-threaded mode only assume `conn` exposes DB-API
`.execute()/.cursor()/.executemany()/.executescript()` and context-manager
transactions.

SQLite-bound points inside it:

| Location | Couples to | Notes |
|----------|-----------|-------|
| `database.py:135-169` `connect()` | `sqlite3.connect`, `file:?mode=ro` URIs, `PRAGMA query_only`, `PRAGMA journal_mode=WAL`, `isolation_level="IMMEDIATE"` | **Single connection factory** — the natural override point. |
| `database.py:481-533` `execute()` | wraps query in `sqlite_timelimit`; catches `sqlite3.OperationalError/DatabaseError` → `QueryInterrupted` | timeout + error mapping are SQLite-specific (seam 10). |
| `database.py:253` `executescript` | not present in DuckDB's API | writes only. |
| `database.py:526-529,919-927` `Results` | DB-API `cursor.description` | portable; DuckDB supplies it. |

**Abstraction effort: low.** Subclass / inject a connection factory + error mapper.

### 2. Connection preparation — `datasette/app.py:1232-1262` (`_prepare_connection`)

| Location | Couples to |
|----------|-----------|
| `app.py:1233-1234` | `conn.row_factory = sqlite3.Row`, `conn.text_factory` — **SQLite-only attributes** |
| `app.py:1236-1244` | `enable_load_extension` / `SELECT load_extension(?)` |
| `app.py:1246` | `PRAGMA cache_size` |
| `app.py:1251-1262` | `crossdb` `ATTACH DATABASE "file:...?mode=ro"` |
| `app.py:1249` | fires the `prepare_connection` plugin hook |

DuckDB returns tuples, so a Row-like wrapper has to be installed here instead of
`row_factory`. The abstraction must decide what `prepare_connection` means for a
non-SQLite connection.

**Abstraction effort: low–medium.**

### 3. Result / row representation

The codebase relies on `sqlite3.Row`'s dual access — `row["colname"]` *and*
`row[0]` — pervasively in the views layer, and type-checks it directly:

- `renderer.py:62,96` — `isinstance(data["rows"][0], sqlite3.Row)`
- `utils/__init__.py:223-226` — `CustomJSONEncoder` special-cases `sqlite3.Row`/`sqlite3.Cursor`
- `utils/__init__.py:867` — comment: "Loose imitation of sqlite3.Row …"

A backend returning bare tuples silently misses these branches. The abstraction
must supply a Row type with both index and key access (and update the
`isinstance` checks to target it).

**Abstraction effort: medium** (touches renderer + encoder + every `row[...]` site indirectly).

### 4. Parameter binding style

Datasette uses **named `:name` / `:p0` parameters everywhere** (filters,
pagination, FTS). SQLite accepts `:name`; DuckDB's Python API wants `$name` or
positional `?`. `datasette-parquet` rewrites params at the `execute` boundary.

Param *extraction* is already engine-free: `named_parameters()`
(`utils/__init__.py:1259`) is pure regex (no longer runs `EXPLAIN`). What's needed
is a **param-style adapter** colocated with `execute()`.

**Abstraction effort: low**, but easy to get subtly wrong (quoting, `::` casts).

### 5. Schema introspection — `database.py` + `utils/__init__.py`

The largest single chunk. Almost all of it is raw SQLite-internal SQL:

| Location | Couples to |
|----------|-----------|
| `database.py:605-621` | `select … from sqlite_master where type='table'/'view'` (`table_exists`, `view_exists`, `table_names`) |
| `database.py:801-803` | `view_names` via `sqlite_master` |
| `database.py:808-827` | `get_table_definition` reads `sql` column of `sqlite_master`, appends `index` rows |
| `database.py:596` | `attached_databases` → `PRAGMA database_list` |
| `database.py:688-799` | `hidden_table_names` — large block: `pragma_table_list` shadow tables, FTS3/4/5 shadow-table name derivation, `sqlite_stat*`, Spatialite internals |
| `utils:600` | `get_outbound_foreign_keys` → `PRAGMA foreign_key_list([table])` |
| `utils:627-633` | `get_all_foreign_keys` → `sqlite_master` |
| `utils:666-670` | `detect_spatialite` → `sqlite_master` |
| `utils:673-694` | `detect_fts` / `detect_fts_sql` → `sqlite_master`, FTS virtual-table sql LIKE matching |
| `utils:712-733` | `table_columns` / `table_column_details` → `PRAGMA table_xinfo` / `table_info` (gated on `supports_table_xinfo()`) |

These map conceptually to DuckDB's `information_schema` / `duckdb_columns()` /
`duckdb_constraints()`, but each query needs a per-backend rewrite. **This is the
strongest argument for an introspection *interface*** (`columns(table)`,
`primary_keys(table)`, `foreign_keys(table)`, `table_names()`, `fts_table(table)`,
…) rather than shared SQL strings — otherwise we maintain N copies of
`hidden_table_names`.

**Abstraction effort: high** (volume), but mechanical once the interface exists.

### 6. Identifier quoting — `escape_sqlite` (`utils/__init__.py:404`)

Single function, bracket-quotes `[identifier]`; used everywhere SQL is built.
DuckDB/Postgres use `"identifier"`. **Best-concentrated chokepoint in the audit** —
one function to make dialect-aware (or route through `sqlglot`).

**Abstraction effort: low** (one function), **high reach** (called everywhere).

### 7. Pagination & sort — `table.py` + `utils`

- **`rowid` dependency — the deep one.** Tables without explicit PKs fall back to
  the `rowid` pseudo-column for selection, row links, and keyset pagination:
  `table.py:191,195,253,256,374,403,554,629,1182-1188,1278`, plus
  `path_from_row_pks` (`utils:178-192`) and `row_sql_params_pks`
  (`utils:1351-1357`). **DuckDB has no stable implicit rowid for views/Parquet** —
  this is the exact wall `datasette-parquet` hit. For analytic tables this needs a
  real answer: require/derive a key, or use an explicit ordering surrogate.
- `compound_keys_after_sql` (`utils:195-218`) — keyset pagination SQL. Comparison
  syntax is portable but bracket-quoted and assumes SQLite NULL/collation ordering.
- Sort: `escape_sqlite(sort)` + ` desc` (`table.py:930,936,1323`).

**Abstraction effort: high** (rowid is a design problem, not a rewrite).

### 8. Filters — `datasette/filters.py`

Per-operator SQL fragments. Portable: `=`, `like`, `<`/`>`, `is null`.
Dialect-specific:

| Location | Couples to |
|----------|-----------|
| `filters.py:316` | `glob` operator — no DuckDB/Postgres equivalent |
| `filters.py:79-106` + `utils:990` | FTS `match` against virtual tables + `escape_fts()` — entirely SQLite FTS |
| `filters.py:325,331` | `json_each(...)` array-contains, gated on `detect_json1` |
| `filters.py:340` | `date("col")` — SQLite date semantics |
| `filters.py:57` | FTS pk defaults to `rowid` |

**Abstraction effort: medium** — needs a dialect-dispatched operator → SQL-fragment map.

### 9. Facets — `datasette/facets.py`

Column facets (`group by … count(*)`) are portable. Dialect-specific:

| Location | Couples to |
|----------|-----------|
| `facets.py:320-414` | array facets: `json_type` / `json_array_length` / `json_each` (gated on `detect_json1`) |
| `facets.py:480-529` | date facets: `date()` + `glob "????-??-*"` |

**Abstraction effort: medium** — shares the operator-mapping problem with seam 8.

### 10. SQL validation & query safety — `utils/__init__.py`

- `validate_sql_select` + `allowed_pragmas` (`utils:281-324`) — regex allowlist
  tuned to SQLite (`explain query plan`, `pragma_*`). Needs per-dialect rules.
- **`sqlite_timelimit`** (`utils:240-259`) — kills long queries via
  `conn.set_progress_handler`. **DuckDB has no progress handler**; it uses
  `conn.interrupt()` from another thread. No drop-in equivalent. This is the
  **critical safety gap** for untrusted analytic queries — `datasette-parquet`
  simply has no timeouts and flags itself unsafe for untrusted users.

**Abstraction effort: high** (timeout needs a per-backend strategy + watchdog thread).

### 11. Capability / version gating

`sqlite_version()`, `supports_table_xinfo()`, `supports_generated_columns()`
(`utils/sqlite.py`), `detect_json1()` (`utils:697`). Want a generic capability map
keyed by backend rather than `sqlite_version >= …` checks.

**Abstraction effort: low.**

---

## Verdict on Simon's hypotheses

**"The `Database` class is small enough to abstract" — largely true.** The
execution/threading core (seam 1) is already backend-neutral. SQLite coupling
concentrates in three overridable points: `connect()` (the factory),
`_prepare_connection()`, and the introspection methods. A backend protocol with
those overridden is realistic.

**"SQL generation is concentrated in TableView" — half true, and this is the
catch.** It's *concentrated* but not *contained*: generation spans `table.py`,
`filters.py`, `facets.py`, plus the shared primitives `escape_sqlite` and
`compound_keys_after_sql`. Simon's "SQL builder customized per-database" idea is
the right shape, but it must absorb all five.

## The hard problems (not solved by quoting/transpile)

1. **`rowid`** — no implicit key on DuckDB views/Parquet breaks selection +
   pagination for keyless tables.
2. **Query timeouts** — `set_progress_handler` has no DuckDB analog; needs an
   `interrupt()`-from-watchdog design. Real safety concern for untrusted queries.
3. **Introspection** — must become an interface, not rewritten SQL strings.

## Effort summary

| Seam | Coupling | Effort |
|------|----------|--------|
| 1. Connection/execution core | low | low |
| 2. `_prepare_connection` | medium | low–medium |
| 3. Row representation | pervasive | medium |
| 4. Param binding style | medium | low |
| 5. Schema introspection | heavy | high (volume) |
| 6. `escape_sqlite` quoting | central | low (high reach) |
| 7. Pagination / `rowid` | deep | high (design) |
| 8. Filters | medium | medium |
| 9. Facets | medium | medium |
| 10. SQL validation + timeout | heavy | high (timeout) |
| 11. Capability gating | low | low |

## Proposed direction

Define an explicit **backend protocol** that names these seams, so a DuckDB plugin
*overrides behavior* instead of impersonating SQLite:

- a `Backend` / `Connection` interface — connection factory, execute, param-style
  adapter, row wrapping, timeout strategy, error mapping (seams 1, 2, 3, 4, 10);
- an `Introspector` interface — columns / pks / fks / table-names / fts-detect /
  hidden-tables (seam 5);
- dialect dispatch for `escape_sqlite` and the filter/facet operator fragments
  (seams 6, 8, 9);
- a key/ordering strategy to replace the implicit-`rowid` assumption (seam 7);
- a capability map to replace version sniffing (seam 11).

See `design/backend-protocol.md` for the interface sketch.
