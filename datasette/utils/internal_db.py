import textwrap


async def init_internal_db(db):
    create_tables_sql = textwrap.dedent("""
    CREATE TABLE IF NOT EXISTS catalog_databases (
        database_name TEXT PRIMARY KEY,
        path TEXT,
        is_memory INTEGER,
        schema_version INTEGER
    );
    CREATE TABLE IF NOT EXISTS catalog_tables (
        database_name TEXT,
        table_name TEXT,
        PRIMARY KEY (database_name, table_name),
        FOREIGN KEY (database_name) REFERENCES catalog_databases(database_name)
    );
    CREATE TABLE IF NOT EXISTS catalog_views (
        database_name TEXT,
        view_name TEXT,
        PRIMARY KEY (database_name, view_name),
        FOREIGN KEY (database_name) REFERENCES catalog_databases(database_name)
    );
    CREATE TABLE IF NOT EXISTS catalog_columns (
        database_name TEXT,
        table_name TEXT,
        cid INTEGER,
        name TEXT,
        type TEXT,
        "notnull" INTEGER,
        default_value TEXT, -- renamed from dflt_value
        is_pk INTEGER, -- renamed from pk
        hidden INTEGER,
        PRIMARY KEY (database_name, table_name, name),
        FOREIGN KEY (database_name) REFERENCES catalog_databases(database_name),
        FOREIGN KEY (database_name, table_name) REFERENCES catalog_tables(database_name, table_name)
    );
    -- Outbound foreign keys, in the backend Introspector's shape (one row per
    -- single-column foreign key). Replaces the SQLite PRAGMA foreign_key_list
    -- shape so it can be populated from any backend.
    CREATE TABLE IF NOT EXISTS catalog_foreign_keys (
        database_name TEXT,
        table_name TEXT,
        "column" TEXT,
        other_table TEXT,
        other_column TEXT,
        FOREIGN KEY (database_name) REFERENCES catalog_databases(database_name),
        FOREIGN KEY (database_name, table_name) REFERENCES catalog_tables(database_name, table_name)
    );
    """).strip()
    await db.execute_write_script(create_tables_sql)
    await initialize_metadata_tables(db)


async def initialize_metadata_tables(db):
    await db.execute_write_script(textwrap.dedent("""
        CREATE TABLE IF NOT EXISTS metadata_instance (
            key text,
            value text,
            unique(key)
        );

        CREATE TABLE IF NOT EXISTS metadata_databases (
            database_name text,
            key text,
            value text,
            unique(database_name, key)
        );

        CREATE TABLE IF NOT EXISTS metadata_resources (
            database_name text,
            resource_name text,
            key text,
            value text,
            unique(database_name, resource_name, key)
        );

        CREATE TABLE IF NOT EXISTS metadata_columns (
            database_name text,
            resource_name text,
            column_name text,
            key text,
            value text,
            unique(database_name, resource_name, column_name, key)
        );

        CREATE TABLE IF NOT EXISTS column_types (
            database_name TEXT NOT NULL,
            resource_name TEXT NOT NULL,
            column_name TEXT NOT NULL,
            column_type TEXT NOT NULL,
            config TEXT,
            PRIMARY KEY (database_name, resource_name, column_name)
        );
            """))


async def populate_schema_tables(internal_db, db):
    database_name = db.name

    def delete_everything(conn):
        conn.execute(
            "DELETE FROM catalog_tables WHERE database_name = ?", [database_name]
        )
        conn.execute(
            "DELETE FROM catalog_views WHERE database_name = ?", [database_name]
        )
        conn.execute(
            "DELETE FROM catalog_columns WHERE database_name = ?", [database_name]
        )
        conn.execute(
            "DELETE FROM catalog_foreign_keys WHERE database_name = ?",
            [database_name],
        )

    await internal_db.execute_write_fn(delete_everything)

    # Collect schema metadata through the backend's introspector so this works
    # for any backend (was: raw sqlite_master reads + PRAGMA in a thread).
    table_names = await db.table_names()
    view_names = await db.view_names()
    all_foreign_keys = await db.get_all_foreign_keys()

    tables_to_insert = [(database_name, name) for name in table_names]
    views_to_insert = [(database_name, name) for name in view_names]

    columns_to_insert = []
    for table_name in table_names:
        for column in await db.table_column_details(table_name):
            columns_to_insert.append(
                {
                    "database_name": database_name,
                    "table_name": table_name,
                    **column._asdict(),
                }
            )

    foreign_keys_to_insert = []
    for table_name in table_names:
        for fk in all_foreign_keys.get(table_name, {}).get("outgoing", []):
            foreign_keys_to_insert.append(
                {
                    "database_name": database_name,
                    "table_name": table_name,
                    "column": fk["column"],
                    "other_table": fk["other_table"],
                    "other_column": fk["other_column"],
                }
            )

    await internal_db.execute_write_many(
        "INSERT INTO catalog_tables (database_name, table_name) VALUES (?, ?)",
        tables_to_insert,
    )
    await internal_db.execute_write_many(
        "INSERT INTO catalog_views (database_name, view_name) VALUES (?, ?)",
        views_to_insert,
    )
    await internal_db.execute_write_many(
        """
        INSERT INTO catalog_columns (
            database_name, table_name, cid, name, type, "notnull", default_value, is_pk, hidden
        ) VALUES (
            :database_name, :table_name, :cid, :name, :type, :notnull, :default_value, :is_pk, :hidden
        )
    """,
        columns_to_insert,
    )
    await internal_db.execute_write_many(
        """
        INSERT INTO catalog_foreign_keys (
            database_name, table_name, "column", other_table, other_column
        ) VALUES (
            :database_name, :table_name, :column, :other_table, :other_column
        )
    """,
        foreign_keys_to_insert,
    )
