"""
Convert a SQLite database to a DuckDB file, preserving column types, primary
keys and (valid, referentially-clean) foreign keys.

``CREATE TABLE AS SELECT`` via the sqlite scanner is quick but drops all
constraints and types. This instead reads the SQLite schema and recreates each
table with proper DuckDB types + PK + FK, loading data with ``TRY_CAST`` to cope
with SQLite's loose typing (e.g. ``''`` in an integer column -> NULL).

SQLite's declared types are advisory, and most importers default a column to
TEXT whenever they're unsure -- so a column of uniform ``'YYYY-MM-DD'`` strings
arrives declared TEXT even though it's really a DATE. After mapping declared
types, a content-inference pass (``_infer_tighter_types``) probes each column
with DuckDB's own ``TRY_CAST`` and promotes it where the data is uniformly a
tighter type: VARCHAR -> UUID / DATE / TIMESTAMP / BIGINT, and DOUBLE -> REAL
(see that function and #9 for the precedence and the profiling behind it).

DuckDB enforces foreign keys at insert and has no ``ALTER ... ADD FOREIGN KEY``,
so tables are created in dependency order with inline FKs. Foreign keys that
reference a missing table, or whose data has orphans, are dropped for that table
(the data is still loaded) and reported.

Usage:  python -m datasette_duckdb.convert source.db dest.duckdb
"""

import os
import sqlite3
import sys

import duckdb

_TYPE_MAP = {
    "text": "VARCHAR",
    "varchar": "VARCHAR",
    "char": "VARCHAR",
    "string": "VARCHAR",
    "int": "BIGINT",
    "integer": "BIGINT",
    "bigint": "BIGINT",
    "bint": "BIGINT",
    "real": "DOUBLE",
    "float": "DOUBLE",
    "double": "DOUBLE",
    "timestamp": "TIMESTAMP",
    "datetime": "TIMESTAMP",
    "date": "DATE",
    "blob": "BLOB",
    "boolean": "BOOLEAN",
}


def _map_type(sqlite_type):
    return _TYPE_MAP.get((sqlite_type or "").strip().lower(), "VARCHAR")


# Don't infer a tighter type from too few observed values -- an all-empty
# snapshot column would otherwise "pass" every probe on zero evidence. See #9.
_MIN_SAMPLE = 100


def _infer_tighter_types(d, table, columns):
    """Promote columns whose data is uniformly a tighter type than declared.

    Probes the *attached source* (``s."table"``, read as VARCHAR via
    ``sqlite_all_varchar`` -- so every column reads as text here regardless of
    its mapped target) with DuckDB's own ``TRY_CAST``. All promotions are
    **strict** (one non-conforming value blocks) and require >= ``_MIN_SAMPLE``
    observed values, so an all-empty snapshot column isn't promoted on no
    evidence. See #9 (the profiling that set this scope lives on the issue).

    Two independent promotions:

    * **VARCHAR -> UUID / DATE / TIMESTAMP / BIGINT**, in that most-restrictive-
      first precedence (first full pass wins). DATE requires *no* time-of-day,
      so real datetimes fall through to TIMESTAMP instead of being truncated.
      BIGINT requires a lossless text round-trip (``CAST(... AS VARCHAR)``
      equals the original), so leading-zero identifiers (zip/FIPS/account
      numbers) are NOT silently renumbered. No VARCHAR->DOUBLE: float-shaped
      text is rare and risky, and out of the profiled scope.
    * **DOUBLE -> REAL** when every value fits float32 losslessly
      (``CAST(CAST(x AS REAL) AS DOUBLE) = x``). The profiling found this is a
      ~50% on-disk win where applicable (ALP/ALPRD can't fully bridge the 8->4
      byte gap), but it must stay strict -- silently turning a $1234.56 penalty
      into $1234.5599 is a real bug. We do NOT narrow existing ints
      (BIGINT->INTEGER/SMALLINT): DuckDB's BitPacking already encodes at the
      optimal per-row width, so it's a ~0% no-op.

    Returns ``(refined_columns, [(name, target_type), ...])``.
    """
    varchar_cands = [n for n, ty in columns if ty == "VARCHAR"]
    double_cands = [n for n, ty in columns if ty == "DOUBLE"]
    if not varchar_cands and not double_cands:
        return columns, []

    # Build every probe for the table into one scan. `plan` records, per
    # candidate, the (name, kind, start, width) slice of the result row.
    aggs = []
    plan = []

    def _add(name, kind, exprs):
        plan.append((name, kind, len(aggs), len(exprs)))
        aggs.extend(exprs)

    for n in varchar_cands:
        c = f'"{n}"'
        ne = f"{c} IS NOT NULL AND {c} <> ''"
        _add(
            n,
            "varchar",
            [
                f"COUNT(*) FILTER (WHERE {ne})",  # n_nonempty
                f"COUNT(*) FILTER (WHERE {ne} AND TRY_CAST({c} AS UUID) IS NULL)",  # bad_uuid
                f"COUNT(*) FILTER (WHERE {ne} AND TRY_CAST({c} AS DATE) IS NULL)",  # bad_date
                # carries a real time-of-day (a TIMESTAMP that isn't its own date
                # at midnight) -> DATE would lose data, so DATE must not claim it
                f"COUNT(*) FILTER (WHERE {ne} AND TRY_CAST({c} AS TIMESTAMP) IS NOT NULL "
                f"AND TRY_CAST({c} AS TIMESTAMP) <> CAST(TRY_CAST({c} AS DATE) AS TIMESTAMP))",  # lossy_time
                f"COUNT(*) FILTER (WHERE {ne} AND TRY_CAST({c} AS TIMESTAMP) IS NULL)",  # bad_ts
                # non-integer OR not a lossless round-trip (leading zeros, signs,
                # whitespace) -> would renumber an identifier, so block it
                f"COUNT(*) FILTER (WHERE {ne} AND (TRY_CAST({c} AS BIGINT) IS NULL "
                f"OR CAST(TRY_CAST({c} AS BIGINT) AS VARCHAR) <> {c}))",  # bad_bigint
            ],
        )

    for n in double_cands:
        c = f'"{n}"'
        ne = f"{c} IS NOT NULL AND {c} <> ''"
        vd = f"TRY_CAST({c} AS DOUBLE)"
        _add(
            n,
            "double",
            [
                f"COUNT(*) FILTER (WHERE {ne} AND {vd} IS NOT NULL)",  # n_numeric
                f"COUNT(*) FILTER (WHERE {ne} AND {vd} IS NOT NULL "
                f"AND CAST(TRY_CAST({c} AS REAL) AS DOUBLE) <> {vd})",  # bad_real
            ],
        )

    row = d.execute(f'SELECT {", ".join(aggs)} FROM s."{table}"').fetchone()

    target = {}
    for name, kind, start, width in plan:
        vals = row[start : start + width]
        if kind == "varchar":
            n_nonempty, bad_uuid, bad_date, lossy_time, bad_ts, bad_bigint = vals
            if n_nonempty < _MIN_SAMPLE:
                continue
            if bad_uuid == 0:
                target[name] = "UUID"
            elif bad_date == 0 and lossy_time == 0:
                target[name] = "DATE"
            elif bad_ts == 0:
                target[name] = "TIMESTAMP"
            elif bad_bigint == 0:
                target[name] = "BIGINT"
        else:  # double -> real
            n_numeric, bad_real = vals
            if n_numeric >= _MIN_SAMPLE and bad_real == 0:
                target[name] = "REAL"

    refined = [(n, target.get(n, ty)) for n, ty in columns]
    promoted = [(n, target[n]) for n, _ in columns if n in target]
    return refined, promoted


def _read_schema(src):
    con = sqlite3.connect(src)
    cur = con.cursor()
    # PRAGMA table_list classifies each entry as table / view / virtual /
    # shadow. Selecting from sqlite_master with type='table' would also pull in
    # FTS5 virtual tables (CREATE VIRTUAL TABLE) and their shadow tables
    # (<name>_fts_data/_idx/_docsize/_config), which aren't real data and have
    # no DuckDB equivalent. Keep only genuine base tables in main.
    tables = [
        r[1]
        for r in cur.execute("PRAGMA table_list")
        if r[0] == "main" and r[2] == "table" and not r[1].startswith("sqlite_")
    ]
    table_set = set(tables)
    meta = {}
    for t in tables:
        cols = cur.execute(f'PRAGMA table_info("{t}")').fetchall()
        pks = [c[1] for c in sorted(cols, key=lambda c: c[5]) if c[5]]
        fks = [
            (f[3], f[2], f[4])  # (from_column, referenced_table, to_column)
            for f in cur.execute(f'PRAGMA foreign_key_list("{t}")').fetchall()
            if f[2] in table_set  # skip FKs to non-existent tables
        ]
        meta[t] = {
            "columns": [(c[1], _map_type(c[2])) for c in cols],
            "pks": pks,
            "fks": fks,
        }
    con.close()
    return tables, meta


def _topo_order(tables, meta):
    """Tables ordered so each appears after the tables it references."""
    order = []
    seen = set()

    def visit(t, stack):
        if t in seen or t in stack:
            return
        for _, ref, _ in meta[t]["fks"]:
            if ref != t:
                visit(ref, stack | {t})
        seen.add(t)
        order.append(t)

    for t in tables:
        visit(t, set())
    return order


def convert_sqlite_to_duckdb(src, dst, infer_types=True):
    """Convert SQLite db at ``src`` to a DuckDB file at ``dst`` (overwritten).

    With ``infer_types`` (default), columns whose data is uniformly a tighter
    type are promoted (VARCHAR->UUID/DATE/TIMESTAMP/BIGINT, DOUBLE->REAL) -- see
    ``_infer_tighter_types``.

    Returns ``(dropped, promotions)`` where ``dropped`` is a list of
    ``(table, reason)`` for foreign keys dropped as referential orphans, and
    ``promotions`` is ``{table: [(column, target_type), ...]}`` for the
    content-promoted columns.
    """
    if os.path.exists(dst):
        os.remove(dst)
    tables, meta = _read_schema(src)
    order = _topo_order(tables, meta)

    d = duckdb.connect(dst)
    d.execute("INSTALL sqlite; LOAD sqlite;")
    # Read every source column as VARCHAR; we TRY_CAST to the target type so
    # SQLite's loose values (e.g. '' in an int column) become NULL not errors.
    d.execute("SET GLOBAL sqlite_all_varchar=true;")
    d.execute(f"ATTACH '{src}' AS s (TYPE sqlite);")
    dropped = []
    promotions = {}
    try:
        if infer_types:
            # Refine declared types from content before creating the tables, so
            # CREATE declares the tighter type and INSERT's TRY_CAST targets it.
            for t in order:
                refined, promoted = _infer_tighter_types(d, t, meta[t]["columns"])
                meta[t]["columns"] = refined
                if promoted:
                    promotions[t] = promoted
        for t in order:
            m = meta[t]
            select = ", ".join(
                (f'TRY_CAST("{n}" AS {ty})' if ty != "VARCHAR" else f'"{n}"')
                for n, ty in m["columns"]
            )

            def create(with_fks):
                defs = [f'"{n}" {ty}' for n, ty in m["columns"]]
                if m["pks"]:
                    defs.append(
                        "PRIMARY KEY ({})".format(", ".join(f'"{p}"' for p in m["pks"]))
                    )
                if with_fks:
                    for frm, ref, to in m["fks"]:
                        defs.append(f'FOREIGN KEY ("{frm}") REFERENCES "{ref}"("{to}")')
                d.execute(f'DROP TABLE IF EXISTS "{t}"')
                d.execute(f'CREATE TABLE "{t}" ({", ".join(defs)})')

            try:
                create(with_fks=True)
                d.execute(f'INSERT INTO "{t}" SELECT {select} FROM s."{t}"')
            except duckdb.Error as e:
                # Orphan FK (or other constraint) -> keep the data, drop the FKs
                create(with_fks=False)
                d.execute(f'INSERT INTO "{t}" SELECT {select} FROM s."{t}"')
                if m["fks"]:
                    dropped.append((t, str(e).splitlines()[0]))
    finally:
        d.close()
    return dropped, promotions


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2:
        print("usage: python -m datasette_duckdb.convert source.db dest.duckdb")
        return 1
    src, dst = argv
    dropped, promotions = convert_sqlite_to_duckdb(src, dst)
    print(f"Converted {src} -> {dst}")
    if promotions:
        print("Columns promoted to tighter types by content inference:")
        for table, cols in promotions.items():
            print(f"  {table}: " + ", ".join(f"{c} -> {typ}" for c, typ in cols))
    if dropped:
        print("Foreign keys dropped (referential orphans / missing target):")
        for table, reason in dropped:
            print(f"  {table}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
