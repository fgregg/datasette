"""
Convert a SQLite database to a DuckDB file, preserving column types, primary
keys and (valid, referentially-clean) foreign keys.

``CREATE TABLE AS SELECT`` via the sqlite scanner is quick but drops all
constraints and types. This instead reads the SQLite schema and recreates each
table with proper DuckDB types + PK + FK, loading data with ``TRY_CAST`` to cope
with SQLite's loose typing (e.g. ``''`` in an integer column -> NULL).

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


def convert_sqlite_to_duckdb(src, dst):
    """Convert SQLite db at ``src`` to a DuckDB file at ``dst`` (overwritten).

    Returns a list of ``(table, reason)`` for foreign keys that had to be
    dropped (referential orphans).
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
    try:
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
                        "PRIMARY KEY ({})".format(
                            ", ".join(f'"{p}"' for p in m["pks"])
                        )
                    )
                if with_fks:
                    for frm, ref, to in m["fks"]:
                        defs.append(
                            f'FOREIGN KEY ("{frm}") REFERENCES "{ref}"("{to}")'
                        )
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
    return dropped


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2:
        print("usage: python -m datasette_duckdb.convert source.db dest.duckdb")
        return 1
    src, dst = argv
    dropped = convert_sqlite_to_duckdb(src, dst)
    print(f"Converted {src} -> {dst}")
    if dropped:
        print("Foreign keys dropped (referential orphans / missing target):")
        for table, reason in dropped:
            print(f"  {table}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
