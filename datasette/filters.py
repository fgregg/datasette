from datasette import hookimpl
from datasette.resources import DatabaseResource
from datasette.views.base import DatasetteError
from datasette.utils.asgi import BadRequest
import json
from .utils import escape_sqlite, path_with_removed_args


@hookimpl(specname="filters_from_request")
def where_filters(request, database, datasette):
    # This one deals with ?_where=
    async def inner():
        where_clauses = []
        extra_wheres_for_ui = []
        if "_where" in request.args:
            if not await datasette.allowed(
                action="execute-sql",
                resource=DatabaseResource(database=database),
                actor=request.actor,
            ):
                raise DatasetteError("_where= is not allowed", status=403)
            else:
                where_clauses.extend(request.args.getlist("_where"))
                extra_wheres_for_ui = [
                    {
                        "text": text,
                        "remove_url": path_with_removed_args(request, {"_where": text}),
                    }
                    for text in request.args.getlist("_where")
                ]

        return FilterArguments(
            where_clauses,
            extra_context={
                "extra_wheres_for_ui": extra_wheres_for_ui,
            },
        )

    return inner


@hookimpl(specname="filters_from_request")
def search_filters(request, database, table, datasette):
    # ?_search= and _search_colname=
    async def inner():
        where_clauses = []
        params = {}
        human_descriptions = []
        extra_context = {}

        # Figure out which fts_table to use
        table_metadata = await datasette.table_config(database, table)
        db = datasette.get_database(database)
        fts_table = request.args.get("_fts_table")
        fts_table = fts_table or table_metadata.get("fts_table")
        fts_table = fts_table or await db.fts_table(table)
        fts_pk = request.args.get("_fts_pk") or table_metadata.get("fts_pk")
        if not fts_pk:
            # Default to rowid where the backend has one, else the table's PK
            if db.backend.features.supports_rowid:
                fts_pk = "rowid"
            else:
                pks = await db.primary_keys(table)
                fts_pk = pks[0] if pks else "rowid"
        search_args = {
            key: request.args[key]
            for key in request.args
            if key.startswith("_search") and key != "_searchmode"
        }
        search = ""
        search_mode_raw = table_metadata.get("searchmode") == "raw"
        # Or set search mode from the querystring
        qs_searchmode = request.args.get("_searchmode")
        if qs_searchmode == "escaped":
            search_mode_raw = False
        if qs_searchmode == "raw":
            search_mode_raw = True

        extra_context["supports_search"] = bool(fts_table)

        if fts_table and search_args:
            if "_search" in search_args:
                # Simple ?_search=xxx
                search = search_args["_search"]
                where_clauses.append(
                    db.dialect.fts_search_clause(
                        fts_table=fts_table,
                        fts_pk=fts_pk,
                        column=None,
                        param="search",
                        raw=search_mode_raw,
                    )
                )
                human_descriptions.append(f'search matches "{search}"')
                params["search"] = search
                extra_context["search"] = search
            else:
                # More complex: search against specific columns
                for i, (key, search_text) in enumerate(search_args.items()):
                    search_col = key.split("_search_", 1)[1]
                    if search_col not in await db.table_columns(fts_table):
                        raise BadRequest("Cannot search by that column")

                    where_clauses.append(
                        db.dialect.fts_search_clause(
                            fts_table=fts_table,
                            fts_pk=fts_pk,
                            column=search_col,
                            param="search_{}".format(i),
                            raw=search_mode_raw,
                        )
                    )
                    human_descriptions.append(
                        f'search column "{search_col}" matches "{search_text}"'
                    )
                    params[f"search_{i}"] = search_text
                    extra_context["search"] = search_text

        return FilterArguments(where_clauses, params, human_descriptions, extra_context)

    return inner


@hookimpl(specname="filters_from_request")
def through_filters(request, database, table, datasette):
    # ?_search= and _search_colname=
    async def inner():
        where_clauses = []
        params = {}
        human_descriptions = []
        extra_context = {}

        # Support for ?_through={table, column, value}
        if "_through" in request.args:
            for through in request.args.getlist("_through"):
                through_data = json.loads(through)
                through_table = through_data["table"]
                other_column = through_data["column"]
                value = through_data["value"]
                db = datasette.get_database(database)
                outgoing_foreign_keys = await db.foreign_keys_for_table(through_table)
                try:
                    fk_to_us = [
                        fk for fk in outgoing_foreign_keys if fk["other_table"] == table
                    ][0]
                except IndexError:
                    raise DatasetteError(
                        "Invalid _through - could not find corresponding foreign key"
                    )
                param = f"p{len(params)}"
                where_clauses.append(
                    "{our_pk} in (select {our_column} from {through_table} where {other_column} = :{param})".format(
                        through_table=escape_sqlite(through_table),
                        our_pk=escape_sqlite(fk_to_us["other_column"]),
                        our_column=escape_sqlite(fk_to_us["column"]),
                        other_column=escape_sqlite(other_column),
                        param=param,
                    )
                )
                params[param] = value
                human_descriptions.append(f'{through_table}.{other_column} = "{value}"')

        return FilterArguments(where_clauses, params, human_descriptions, extra_context)

    return inner


class FilterArguments:
    def __init__(
        self, where_clauses, params=None, human_descriptions=None, extra_context=None
    ):
        self.where_clauses = where_clauses
        self.params = params or {}
        self.human_descriptions = human_descriptions or []
        self.extra_context = extra_context or {}


def _quote_column(column, dialect):
    # Route identifier quoting through the dialect when one is supplied, so a
    # non-SQLite backend gets its own quoting (DuckDB rejects SQLite's [bracket]
    # form). SqliteDialect.escape_identifier is escape_sqlite, so SQLite output
    # is unchanged. Falls back to escape_sqlite when no dialect is passed.
    if dialect is not None:
        return dialect.escape_identifier(column)
    return escape_sqlite(column)


class Filter:
    key = None
    display = None
    no_argument = False

    def where_clause(self, table, column, value, param_counter, dialect=None):
        raise NotImplementedError

    def human_clause(self, column, value):
        raise NotImplementedError


class TemplatedFilter(Filter):
    def __init__(
        self,
        key,
        display,
        sql_template,
        human_template,
        format="{}",
        numeric=False,
        no_argument=False,
    ):
        self.key = key
        self.display = display
        self.sql_template = sql_template
        self.human_template = human_template
        self.format = format
        self.numeric = numeric
        self.no_argument = no_argument

    def where_clause(self, table, column, value, param_counter, dialect=None):
        # Templates quote the column inline with ANSI "{c}", which is portable,
        # so the dialect isn't needed here.
        converted = self.format.format(value)
        if self.numeric and converted.isdigit():
            converted = int(converted)
        if self.no_argument:
            kwargs = {"c": column}
            converted = None
        else:
            kwargs = {"c": column, "p": f"p{param_counter}", "t": table}
        return self.sql_template.format(**kwargs), converted

    def human_clause(self, column, value):
        if callable(self.human_template):
            template = self.human_template(column, value)
        else:
            template = self.human_template
        if self.no_argument:
            return template.format(c=column)
        else:
            return template.format(c=column, v=value)


class InFilter(Filter):
    key = "in"
    display = "in"

    def split_value(self, value):
        if value.startswith("["):
            return json.loads(value)
        else:
            return [v.strip() for v in value.split(",")]

    def where_clause(self, table, column, value, param_counter, dialect=None):
        values = self.split_value(value)
        params = [f":p{param_counter + i}" for i in range(len(values))]
        sql = f"{_quote_column(column, dialect)} in ({', '.join(params)})"
        return sql, values

    def human_clause(self, column, value):
        return f"{column} in {json.dumps(self.split_value(value))}"


class NotInFilter(InFilter):
    key = "notin"
    display = "not in"

    def where_clause(self, table, column, value, param_counter, dialect=None):
        values = self.split_value(value)
        params = [f":p{param_counter + i}" for i in range(len(values))]
        sql = f"{_quote_column(column, dialect)} not in ({', '.join(params)})"
        return sql, values

    def human_clause(self, column, value):
        return f"{column} not in {json.dumps(self.split_value(value))}"


class Filters:
    _filters = (
        [
            # key, display, sql_template, human_template, format=, numeric=, no_argument=
            TemplatedFilter(
                "exact",
                "=",
                '"{c}" = :{p}',
                lambda c, v: "{c} = {v}" if v.isdigit() else '{c} = "{v}"',
            ),
            TemplatedFilter(
                "not",
                "!=",
                '"{c}" != :{p}',
                lambda c, v: "{c} != {v}" if v.isdigit() else '{c} != "{v}"',
            ),
            TemplatedFilter(
                "contains",
                "contains",
                '"{c}" like :{p}',
                '{c} contains "{v}"',
                format="%{}%",
            ),
            TemplatedFilter(
                "notcontains",
                "does not contain",
                '"{c}" not like :{p}',
                '{c} does not contain "{v}"',
                format="%{}%",
            ),
            TemplatedFilter(
                "endswith",
                "ends with",
                '"{c}" like :{p}',
                '{c} ends with "{v}"',
                format="%{}",
            ),
            TemplatedFilter(
                "startswith",
                "starts with",
                '"{c}" like :{p}',
                '{c} starts with "{v}"',
                format="{}%",
            ),
            TemplatedFilter("gt", ">", '"{c}" > :{p}', "{c} > {v}", numeric=True),
            TemplatedFilter(
                "gte", "\u2265", '"{c}" >= :{p}', "{c} \u2265 {v}", numeric=True
            ),
            TemplatedFilter("lt", "<", '"{c}" < :{p}', "{c} < {v}", numeric=True),
            TemplatedFilter(
                "lte", "\u2264", '"{c}" <= :{p}', "{c} \u2264 {v}", numeric=True
            ),
            TemplatedFilter("like", "like", '"{c}" like :{p}', '{c} like "{v}"'),
            TemplatedFilter(
                "notlike", "not like", '"{c}" not like :{p}', '{c} not like "{v}"'
            ),
            TemplatedFilter("glob", "glob", '"{c}" glob :{p}', '{c} glob "{v}"'),
            InFilter(),
            NotInFilter(),
        ]
        + [
            # Catalog always lists these; only offered to a backend that
            # advertises supports_json (see _feature_gated).
            TemplatedFilter(
                "arraycontains",
                "array contains",
                """:{p} in (select value from json_each([{t}].[{c}]))""",
                '{c} contains "{v}"',
            ),
            TemplatedFilter(
                "arraynotcontains",
                "array does not contain",
                """:{p} not in (select value from json_each([{t}].[{c}]))""",
                '{c} does not contain "{v}"',
            ),
        ]
        + [
            TemplatedFilter(
                "date", "date", 'date("{c}") = :{p}', '"{c}" is on date {v}'
            ),
            TemplatedFilter(
                "isnull", "is null", '"{c}" is null', "{c} is null", no_argument=True
            ),
            TemplatedFilter(
                "notnull",
                "is not null",
                '"{c}" is not null',
                "{c} is not null",
                no_argument=True,
            ),
            TemplatedFilter(
                "isblank",
                "is blank",
                '("{c}" is null or "{c}" = "")',
                "{c} is blank",
                no_argument=True,
            ),
            TemplatedFilter(
                "notblank",
                "is not blank",
                '("{c}" is not null and "{c}" != "")',
                "{c} is not blank",
                no_argument=True,
            ),
        ]
    )
    # An operator is only offered when the backend advertises the matching
    # capability flag; operators absent from this map are always available.
    _feature_gated = {
        "glob": "supports_glob",
        "arraycontains": "supports_json",
        "arraynotcontains": "supports_json",
    }

    def __init__(self, pairs, features=None):
        self.pairs = pairs
        self.features = features
        if features is None:
            # No backend supplied: offer the full catalog (used by tests and
            # any caller that doesn't care about capabilities).
            enabled = list(self._filters)
        else:
            enabled = [
                f
                for f in self._filters
                if self._feature_gated.get(f.key) is None
                or getattr(features, self._feature_gated[f.key])
            ]
        self._enabled_filters = enabled
        self._filters_by_key = {f.key: f for f in enabled}

    def lookups(self):
        """Yields (lookup, display, no_argument) pairs"""
        for filter in self._enabled_filters:
            yield filter.key, filter.display, filter.no_argument

    def human_description_en(self, extra=None):
        bits = []
        if extra:
            bits.extend(extra)
        for column, lookup, value in self.selections():
            filter = self._filters_by_key.get(lookup, None)
            if filter:
                bits.append(filter.human_clause(column, value))
        # Comma separated, with an ' and ' at the end
        and_bits = []
        commas, tail = bits[:-1], bits[-1:]
        if commas:
            and_bits.append(", ".join(commas))
        if tail:
            and_bits.append(tail[0])
        s = " and ".join(and_bits)
        if not s:
            return ""
        return f"where {s}"

    def selections(self):
        """Yields (column, lookup, value) tuples"""
        for key, value in self.pairs:
            if "__" in key:
                column, lookup = key.rsplit("__", 1)
            else:
                column = key
                lookup = "exact"
            yield column, lookup, value

    def has_selections(self):
        return bool(self.pairs)

    def build_where_clauses(self, table, dialect=None):
        sql_bits = []
        params = {}
        i = 0
        for column, lookup, value in self.selections():
            filter = self._filters_by_key.get(lookup, None)
            if filter:
                sql_bit, param = filter.where_clause(
                    table, column, value, i, dialect=dialect
                )
                sql_bits.append(sql_bit)
                if param is not None:
                    if not isinstance(param, list):
                        param = [param]
                    for individual_param in param:
                        param_id = f"p{i}"
                        params[param_id] = individual_param
                        i += 1
        return sql_bits, params
