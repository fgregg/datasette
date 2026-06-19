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
so tables are created in dependency order with inline FKs. Each FK DuckDB can't
enforce -- referenced table/column isn't the parent's PK, a column-type mismatch,
or orphan child rows -- is dropped INDIVIDUALLY (the others on the table survive;
the data is still loaded) and reported. See ``_enforceable_fks``.

Source FTS virtual tables (``sqlite-utils enable-fts``) are mirrored into
equivalent DuckDB FTS indexes (``_read_fts`` / ``_create_fts_indexes``), so a
converted database is searchable out of the box (#4).

Usage:  python -m datasette_duckdb.convert source.db dest.duckdb
"""

import os
import re
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
        # Group foreign_key_list rows by FK id: a composite FK spans several rows
        # (one per column). Each FK -> (from_cols, referenced_table, to_cols).
        fk_groups = {}
        for f in cur.execute(f'PRAGMA foreign_key_list("{t}")').fetchall():
            if f[2] not in table_set:  # skip FKs to non-existent tables
                continue
            g = fk_groups.setdefault(f[0], {"ref": f[2], "cols": []})
            g["cols"].append((f[1], f[3], f[4]))  # (seq, from, to)
        fks = [
            (
                tuple(c[1] for c in sorted(g["cols"])),
                g["ref"],
                tuple(c[2] for c in sorted(g["cols"])),
            )
            for g in fk_groups.values()
        ]
        meta[t] = {
            "columns": [(c[1], _map_type(c[2])) for c in cols],
            "pks": pks,
            "fks": fks,
        }
    fts = _read_fts(cur, table_set)
    con.close()
    return tables, meta, fts


# `content='<table>'` or `content=[<table>]` in an FTS5 CREATE statement names
# the base (content) table the index covers.
_FTS_CONTENT_RE = re.compile(r"content\s*=\s*['\[]?([A-Za-z0-9_]+)", re.IGNORECASE)


def _read_fts(cur, table_set):
    """Detect FTS virtual tables in the source and the base table + columns each
    indexes, so the converter can rebuild an equivalent DuckDB FTS index (#4).

    sqlite-utils ``enable-fts`` creates ``<table>_fts USING fts5(col, ...,
    content=<table>)``. We read the content table from the CREATE statement and
    the indexed columns from the vtable's own ``table_info``. Returns
    ``{base_table: [columns]}`` for indexes whose base is a real converted table.
    """
    fts = {}
    rows = cur.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL"
    ).fetchall()
    for name, sql in rows:
        low = sql.lower()
        if (
            "using fts5" not in low
            and "using fts4" not in low
            and "using fts3" not in low
        ):
            continue
        m = _FTS_CONTENT_RE.search(sql)
        base = m.group(1) if m else None
        if base not in table_set:
            continue
        # The vtable's user columns are the indexed columns (sqlite-utils names
        # them identically to the base table's columns).
        cols = [c[1] for c in cur.execute(f'PRAGMA table_info("{name}")').fetchall()]
        if cols:
            fts[base] = cols
    return fts


def _sql_str(s):
    return "'" + s.replace("'", "''") + "'"


# Match SQLite FTS5's default unicode61 tokenizer (what the source indexes use)
# rather than DuckDB's defaults, so search behaves the same on both sites:
#   * ignore='(\.|[^a-z0-9])+'  — keep DIGITS (DuckDB's default ignores [^a-z],
#     which silently drops numbers; union locals are identified by numbers like
#     "1398", so that broke numeric search entirely).
#   * stemmer='none'   — SQLite FTS5 doesn't stem.
#   * stopwords='none' — SQLite FTS5 doesn't drop stopwords (and union names
#     contain "of"/"and"/etc.).
_FTS_OPTIONS = r"stemmer='none', stopwords='none', ignore='(\.|[^a-z0-9])+'"


def _create_fts_indexes(d, fts):
    """Build a DuckDB FTS index per detected source FTS config (#4).

    Uses ``rowid`` as the document id, matching Datasette's default ``fts_pk``
    for a rowid-bearing backend, so search resolves without per-table config.
    rowid is a fine doc id *within an immutable converted file* (it's rebuilt
    with the data on every conversion); the search clause qualifies it to dodge
    a shadow-capture in the fts macro (see DuckDBDialect.fts_search_clause).

    Tokenizer options (``_FTS_OPTIONS``) mirror SQLite FTS5 so search results
    match the SQLite site; the dialect pairs this with ``conjunctive := true``
    for SQLite's all-terms-must-match semantics.

    Resilient: if the fts extension can't be installed/loaded (e.g. offline),
    or one index fails, the data conversion is unaffected. Returns the list of
    ``(table, columns)`` indexes actually created.
    """
    if not fts:
        return []
    try:
        d.execute("INSTALL fts; LOAD fts;")
    except duckdb.Error:
        return []
    created = []
    for base, cols in fts.items():
        args = ", ".join(_sql_str(a) for a in (base, "rowid", *cols))
        try:
            d.execute(f"PRAGMA create_fts_index({args}, {_FTS_OPTIONS})")
            created.append((base, cols))
        except duckdb.Error:
            continue
    return created


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


def _enforceable_fks(d, t, m, meta, dropped):
    """Subset of ``m['fks']`` DuckDB can enforce inline; the rest are dropped
    INDIVIDUALLY (appended to ``dropped`` with a reason) rather than all-or-
    nothing. DuckDB requires: the FK column's type matches the referenced
    column, the referenced column is exactly the parent's PK (we only create
    PKs, not other UNIQUE constraints), and no orphan child rows. SQLite enforces
    none of this, so its schemas carry FKs DuckDB rejects -- but a single bad one
    must not strip a table's good ones.
    """
    child_types = dict(m["columns"])
    keep = []
    for from_cols, ref, to_cols in m["fks"]:
        parent = meta.get(ref)
        cols_label = ",".join(from_cols)
        to_label = ",".join(to_cols)
        reason = None
        if parent is None:
            reason = "missing parent table"
        elif set(to_cols) != set(parent["pks"]):
            # enforceable only if the FK references exactly the parent's PK
            # (DuckDB needs a unique key on the referenced columns; we only
            # create PKs). Covers single, composite, and non-PK-parent cases.
            reason = f"{ref}({to_label}) is not the parent PK"
        else:
            ptypes = dict(parent["columns"])
            mismatch = [
                (fc, tc)
                for fc, tc in zip(from_cols, to_cols)
                if child_types.get(fc) != ptypes.get(tc)
            ]
            if mismatch:
                reason = "type mismatch " + ", ".join(
                    f"{fc}:{child_types.get(fc)} vs {tc}:{ptypes.get(tc)}"
                    for fc, tc in mismatch
                )
            else:
                # Orphan check in the TARGET type(s), matching how DuckDB will
                # enforce on the converted tables -- not raw VARCHAR. Avoids false
                # orphans from TRY_CAST normalization ('' -> NULL = allowed null
                # FK; '01' vs '1' equal as BIGINT). Composite FK -> tuple match.
                not_null = " AND ".join(
                    f'TRY_CAST(c."{fc}" AS {child_types[fc]}) IS NOT NULL'
                    for fc in from_cols
                )
                joined = " AND ".join(
                    f'TRY_CAST(p."{tc}" AS {child_types[fc]}) '
                    f'= TRY_CAST(c."{fc}" AS {child_types[fc]})'
                    for fc, tc in zip(from_cols, to_cols)
                )
                orphan = d.execute(
                    f'SELECT 1 FROM s."{t}" c WHERE {not_null} AND NOT EXISTS '
                    f'(SELECT 1 FROM s."{ref}" p WHERE {joined}) LIMIT 1'
                ).fetchone()
                if orphan:
                    reason = f"orphan rows reference {ref}({to_label})"
        if reason:
            dropped.append((t, f"FK ({cols_label}) -> {ref}({to_label}): {reason}"))
        else:
            keep.append((from_cols, ref, to_cols))
    return keep


def convert_sqlite_to_duckdb(src, dst, infer_types=True):
    """Convert SQLite db at ``src`` to a DuckDB file at ``dst`` (overwritten).

    With ``infer_types`` (default), columns whose data is uniformly a tighter
    type are promoted (VARCHAR->UUID/DATE/TIMESTAMP/BIGINT, DOUBLE->REAL) -- see
    ``_infer_tighter_types``.

    FTS indexes are rebuilt for every source FTS virtual table (#4), so a
    converted database is searchable out of the box.

    Returns ``(dropped, promotions, fts_created)``: ``dropped`` is a list of
    ``(table, reason)`` for foreign keys dropped as referential orphans,
    ``promotions`` is ``{table: [(column, target_type), ...]}`` for the
    content-promoted columns, and ``fts_created`` is ``[(table, [columns]), ...]``
    for the FTS indexes built.
    """
    if os.path.exists(dst):
        os.remove(dst)
    tables, meta, fts = _read_schema(src)
    order = _topo_order(tables, meta)

    d = duckdb.connect(dst)
    d.execute("INSTALL sqlite; LOAD sqlite;")
    # Read every source column as VARCHAR; we TRY_CAST to the target type so
    # SQLite's loose values (e.g. '' in an int column) become NULL not errors.
    d.execute("SET GLOBAL sqlite_all_varchar=true;")
    d.execute(f"ATTACH '{src}' AS s (TYPE sqlite);")
    dropped = []
    promotions = {}
    fts_created = []
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

            # Only keep FKs DuckDB can actually enforce; drop the rest INDIVIDUALLY
            # (not all-or-nothing) so one bad FK doesn't strip a table's good ones.
            keep_fks = _enforceable_fks(d, t, m, meta, dropped)

            def create(fks):
                defs = [f'"{n}" {ty}' for n, ty in m["columns"]]
                if m["pks"]:
                    defs.append(
                        "PRIMARY KEY ({})".format(", ".join(f'"{p}"' for p in m["pks"]))
                    )
                for from_cols, ref, to_cols in fks:
                    fc = ", ".join(f'"{c}"' for c in from_cols)
                    tc = ", ".join(f'"{c}"' for c in to_cols)
                    defs.append(f'FOREIGN KEY ({fc}) REFERENCES "{ref}"({tc})')
                d.execute(f'DROP TABLE IF EXISTS "{t}"')
                d.execute(f'CREATE TABLE "{t}" ({", ".join(defs)})')

            try:
                create(keep_fks)
                d.execute(f'INSERT INTO "{t}" SELECT {select} FROM s."{t}"')
            except duckdb.Error as e:
                # Anything the pre-checks missed -> keep the data, drop FKs.
                create([])
                d.execute(f'INSERT INTO "{t}" SELECT {select} FROM s."{t}"')
                if keep_fks:
                    dropped.append(
                        (
                            t,
                            f"all FKs dropped (create/insert failed): "
                            f"{str(e).splitlines()[0]}",
                        )
                    )
        # Tables (and their rowids) are populated now, so the FTS indexes can be
        # built against them.
        fts_created = _create_fts_indexes(d, fts)
        # FK metadata sidecar: record EVERY single-column source FK (enforced or
        # not) so Datasette can render related-rows / label-expansion for
        # relationships DuckDB can't enforce -- e.g. an FK whose source data has
        # orphans/incomplete parents (NLRB CHIPS/CATS archives). Enforcement is a
        # data-integrity guarantee; navigation only needs the declared relationship.
        # Read by DuckDBIntrospector; hidden from the table list there.
        fk_rows = [
            (t, fc[0], ref, tc[0])
            for t in order
            for fc, ref, tc in meta[t]["fks"]
            if len(fc) == 1
        ]
        d.execute(
            'CREATE TABLE "_datasette_foreign_keys" '
            "(table_name VARCHAR, from_column VARCHAR, "
            "other_table VARCHAR, other_column VARCHAR)"
        )
        if fk_rows:
            d.executemany(
                'INSERT INTO "_datasette_foreign_keys" VALUES (?, ?, ?, ?)', fk_rows
            )
    finally:
        d.close()
    return dropped, promotions, fts_created


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2:
        print("usage: python -m datasette_duckdb.convert source.db dest.duckdb")
        return 1
    src, dst = argv
    dropped, promotions, fts_created = convert_sqlite_to_duckdb(src, dst)
    print(f"Converted {src} -> {dst}")
    if promotions:
        print("Columns promoted to tighter types by content inference:")
        for table, cols in promotions.items():
            print(f"  {table}: " + ", ".join(f"{c} -> {typ}" for c, typ in cols))
    if fts_created:
        print("FTS indexes created:")
        for table, cols in fts_created:
            print(f"  {table}: " + ", ".join(cols))
    if dropped:
        print("Foreign keys dropped (referential orphans / missing target):")
        for table, reason in dropped:
            print(f"  {table}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
