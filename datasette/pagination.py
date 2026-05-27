"""
Pagination strategies for table views.

A table is paged either by **keyset** (using a stable row key — primary keys, or
SQLite's ``rowid``) or by **offset** (for views, and for keyless tables on a
backend that doesn't provide a stable implicit row id). The choice follows from
"is there a stable row key?", i.e. ``pks or use_rowid``; ``use_rowid`` is itself
gated on the backend's ``supports_rowid`` capability.

Each strategy owns the three pieces that used to be scattered through
``table_view_data`` and ``_next_value_and_url``:

* :meth:`Paginator.order_by` — the ``ORDER BY`` columns,
* :meth:`Paginator.where_and_offset` — the extra WHERE / ``offset`` for a page,
* :meth:`Paginator.next_value` — the ``?_next=`` token for the following page.

See ``design/backend-protocol.md`` (audit seam 7).
"""

from datasette.utils import path_from_row_pks, tilde_encode, urlsafe_components


def paginator_for(*, pks, use_rowid, sort, sort_desc, order_by_pks, dialect):
    """Pick the pagination strategy for a table.

    ``order_by_pks`` is the tie-breaker key SQL ("rowid", or the escaped primary
    keys). Keyset pagination needs a stable row key; without one (views, or
    keyless tables on a no-rowid backend) we fall back to offset pagination.
    """
    kwargs = dict(
        pks=pks,
        use_rowid=use_rowid,
        sort=sort,
        sort_desc=sort_desc,
        order_by_pks=order_by_pks,
        dialect=dialect,
    )
    if not pks and not use_rowid:
        return OffsetPaginator(**kwargs)
    return KeysetPaginator(**kwargs)


class Paginator:
    def __init__(self, *, pks, use_rowid, sort, sort_desc, order_by_pks, dialect):
        self.pks = pks
        self.use_rowid = use_rowid
        self.sort = sort
        self.sort_desc = sort_desc
        self.order_by_pks = order_by_pks
        self.dialect = dialect

    def order_by(self, base_order_by, has_next):
        """The ORDER BY columns (without the leading ``order by``).

        ``base_order_by`` is what ``_sort_order`` produced (the sort column, or
        the tie-breaker key when unsorted). ``has_next`` is whether a ``?_next=``
        token is being applied.
        """
        raise NotImplementedError

    def where_and_offset(self, _next, params):
        """Return ``(where_bits, offset_sql)`` for the requested page.

        ``where_bits`` is a list of SQL fragments to AND into the WHERE clause;
        ``params`` may be mutated to bind any new placeholders.
        """
        raise NotImplementedError

    async def next_value(self, db, table_name, rows, page_size, _next):
        """The ``?_next=`` token for the following page, or None if none."""
        raise NotImplementedError


class OffsetPaginator(Paginator):
    def order_by(self, base_order_by, has_next):
        return base_order_by

    def where_and_offset(self, _next, params):
        if _next:
            return [], f" offset {int(_next)}"
        return [], ""

    async def next_value(self, db, table_name, rows, page_size, _next):
        if 0 < page_size < len(rows):
            return int(_next or 0) + page_size
        return None


class KeysetPaginator(Paginator):
    def order_by(self, base_order_by, has_next):
        # When sorting, the stable key is appended as a tie-breaker, but only
        # once we're actually paging (matching historical behaviour).
        if (self.sort or self.sort_desc) and has_next:
            return f"{base_order_by}, {self.order_by_pks}"
        return base_order_by

    def where_and_offset(self, _next, params):
        where_bits = []
        if not _next:
            return where_bits, ""

        escape = self.dialect.escape_identifier
        sort, sort_desc = self.sort, self.sort_desc

        components = urlsafe_components(_next)
        # If a sort order is applied and there are multiple components, the
        # first of these is the sort value.
        sort_value = None
        if (sort or sort_desc) and (len(components) > 1):
            sort_value = components[0]
            # Special case for if non-urlencoded first token was $null
            if _next.split(",")[0] == "$null":
                sort_value = None
            components = components[1:]

        # SQL for next-based-on-primary-key (the tie-breaker)
        next_by_pk_clauses = []
        if self.use_rowid:
            next_by_pk_clauses.append(f"rowid > :p{len(params)}")
            params[f"p{len(params)}"] = components[0]
        else:
            if len(components) == len(self.pks):
                param_len = len(params)
                next_by_pk_clauses.append(
                    self.dialect.keyset_after_sql(self.pks, param_len)
                )
                for i, pk_value in enumerate(components):
                    params[f"p{param_len + i}"] = pk_value

        # Add the sort SQL, which may incorporate next_by_pk_clauses
        if sort or sort_desc:
            if sort_value is None:
                if sort_desc:
                    # Just items where column is null ordered by pk
                    where_bits.append(
                        "({column} is null and {next_clauses})".format(
                            column=escape(sort_desc),
                            next_clauses=" and ".join(next_by_pk_clauses),
                        )
                    )
                else:
                    where_bits.append(
                        "({column} is not null or ({column} is null and {next_clauses}))".format(
                            column=escape(sort),
                            next_clauses=" and ".join(next_by_pk_clauses),
                        )
                    )
            else:
                where_bits.append(
                    "({column} {op} :p{p}{extra_desc_only} or ({column} = :p{p} and {next_clauses}))".format(
                        column=escape(sort or sort_desc),
                        op=">" if sort else "<",
                        p=len(params),
                        extra_desc_only=(
                            ""
                            if sort
                            else " or {column2} is null".format(
                                column2=escape(sort or sort_desc)
                            )
                        ),
                        next_clauses=" and ".join(next_by_pk_clauses),
                    )
                )
                params[f"p{len(params)}"] = sort_value
        else:
            where_bits.extend(next_by_pk_clauses)

        return where_bits, ""

    async def next_value(self, db, table_name, rows, page_size, _next):
        if not (0 < page_size < len(rows)):
            return None
        escape = self.dialect.escape_identifier
        sort, sort_desc = self.sort, self.sort_desc
        next_value = path_from_row_pks(rows[-2], self.pks, self.use_rowid)
        # If there's a sort or sort_desc, add that value as a prefix
        if sort or sort_desc:
            try:
                prefix = rows[-2][sort or sort_desc]
            except IndexError:
                # sort/sort_desc column missing from SELECT - look up by PK
                prefix_where_clause = " and ".join(
                    "{} = :pk{}".format(escape(pk), i)
                    for i, pk in enumerate(self.pks)
                )
                prefix_lookup_sql = "select {} from {} where {}".format(
                    escape(sort or sort_desc), escape(table_name), prefix_where_clause
                )
                prefix = (
                    await db.execute(
                        prefix_lookup_sql,
                        {
                            "pk{}".format(i): rows[-2][pk]
                            for i, pk in enumerate(self.pks)
                        },
                    )
                ).single_value()
            if isinstance(prefix, dict) and "value" in prefix:
                prefix = prefix["value"]
            if prefix is None:
                prefix = "$null"
            else:
                prefix = tilde_encode(str(prefix))
            next_value = f"{prefix},{next_value}"
        return next_value
