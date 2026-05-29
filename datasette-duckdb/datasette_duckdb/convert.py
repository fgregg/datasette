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
types, a content-inference pass (``_infer_tighter_types``) probes each VARCHAR
column with DuckDB's own ``TRY_CAST`` and promotes it where the data is
uniformly a tighter type. Phase 1 (#9) infers DATE only.

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
    """Promote VARCHAR columns whose data is uniformly a tighter type.

    Probes the *attached source* (``s."table"``, read as VARCHAR via
    ``sqlite_all_varchar``) with DuckDB's own ``TRY_CAST`` and promotes on a
    clean, well-sampled fit.

    Phase 1 (#9): DATE only. A column promotes to DATE iff every non-null,
    non-empty value casts to DATE *and none carries a non-midnight time* -- so a
    real datetime column isn't silently truncated to a date; it stays VARCHAR
    until the TIMESTAMP phase. Strict: a single unparsable value blocks it.

    Returns ``(refined_columns, promoted_names)``.
    """
    candidates = [n for n, ty in columns if ty == "VARCHAR"]
    if not candidates:
        return columns, []
    # One scan over the table: per candidate, count non-empty values, the ones
    # that fail a DATE cast, and the ones that carry a real time-of-day (a
    # TIMESTAMP that isn't its own date at midnight -> DATE would lose data).
    aggs = []
    for n in candidates:
        ne = f'"{n}" IS NOT NULL AND "{n}" <> \'\''
        aggs.append(f"COUNT(*) FILTER (WHERE {ne})")
        aggs.append(f'COUNT(*) FILTER (WHERE {ne} AND TRY_CAST("{n}" AS DATE) IS NULL)')
        aggs.append(
            f"COUNT(*) FILTER (WHERE {ne} "
            f'AND TRY_CAST("{n}" AS TIMESTAMP) IS NOT NULL '
            f'AND TRY_CAST("{n}" AS TIMESTAMP) <> CAST(TRY_CAST("{n}" AS DATE) AS TIMESTAMP))'
        )
    row = d.execute(f'SELECT {", ".join(aggs)} FROM s."{table}"').fetchone()
    promoted = set()
    for i, n in enumerate(candidates):
        n_nonempty, n_bad_date, n_lossy_time = row[3 * i : 3 * i + 3]
        if n_nonempty >= _MIN_SAMPLE and n_bad_date == 0 and n_lossy_time == 0:
            promoted.add(n)
    refined = [(n, "DATE" if n in promoted else ty) for n, ty in columns]
    return refined, [n for n, _ in columns if n in promoted]


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

    With ``infer_types`` (default), VARCHAR columns whose data is uniformly a
    tighter type are promoted (phase 1: DATE) -- see ``_infer_tighter_types``.

    Returns ``(dropped, promotions)`` where ``dropped`` is a list of
    ``(table, reason)`` for foreign keys dropped as referential orphans, and
    ``promotions`` is ``{table: [column, ...]}`` for content-promoted columns.
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
        print("Columns promoted from VARCHAR by content inference:")
        for table, cols in promotions.items():
            print(f"  {table}: " + ", ".join(f"{c} -> DATE" for c in cols))
    if dropped:
        print("Foreign keys dropped (referential orphans / missing target):")
        for table, reason in dropped:
            print(f"  {table}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
