"""Repository for all Lakebase (PostgreSQL) database operations."""

import psycopg2
from psycopg2 import extras, pool, sql

from lakebase_accelerator.models.data_model import IndexDefinition, TableDefinition
from lakebase_accelerator.utils.exceptions import LakebaseConnectionError, SchemaProvisioningError
from lakebase_accelerator.utils.logger import logger


class LakebaseRepository:
    """Repository for all Lakebase database operations.

    Uses psycopg2 connection pool. All queries use parameterized statements.
    """

    def __init__(self, connection_pool: pool.ThreadedConnectionPool) -> None:
        self._pool = connection_pool

    def _get_connection(self):
        """Get a connection from the pool."""
        try:
            return self._pool.getconn()
        except Exception as e:
            raise LakebaseConnectionError(f"Failed to get connection from pool: {e}") from e

    def _return_connection(self, conn) -> None:
        """Return a connection to the pool."""
        self._pool.putconn(conn)

    def create_schema(self, schema_name: str) -> None:
        """Create a new schema in Lakebase.

        Uses sql.Identifier for safe schema name injection (not string interpolation).
        """
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
            conn.commit()
            logger.info(f"Schema created: {schema_name}")
        except psycopg2.errors.DuplicateSchema as e:
            conn.rollback()
            raise SchemaProvisioningError(f"Schema '{schema_name}' already exists") from e
        except Exception as e:
            conn.rollback()
            raise SchemaProvisioningError(f"Failed to create schema '{schema_name}': {e}") from e
        finally:
            self._return_connection(conn)

    def create_tables(self, schema_name: str, tables: list[TableDefinition]) -> list[str]:
        """Create tables in topological order within a single transaction.

        Generates CREATE TABLE with PKs, FKs, NOT NULL, and DEFAULT constraints.
        Uses sql.Identifier for all identifiers.

        Returns list of created table names.
        """
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                created = []
                for table in tables:
                    ddl = self._build_create_table_ddl(schema_name, table)
                    cur.execute(ddl)
                    created.append(table.name)
                    logger.info(f"Table created: {schema_name}.{table.name}")
            conn.commit()
            return created
        except Exception as e:
            conn.rollback()
            raise SchemaProvisioningError(f"Failed to create tables in '{schema_name}': {e}") from e
        finally:
            self._return_connection(conn)

    def create_indexes(self, schema_name: str, indexes: list[IndexDefinition]) -> None:
        """Create indexes on specified columns."""
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                for idx in indexes:
                    idx_sql = self._build_create_index_ddl(schema_name, idx)
                    cur.execute(idx_sql)
                    logger.info(f"Index created: {idx.name}")
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise SchemaProvisioningError(f"Failed to create indexes in '{schema_name}': {e}") from e
        finally:
            self._return_connection(conn)

    def insert_seed_data(self, schema_name: str, table_name: str, rows: list[dict]) -> int:
        """Insert seed data rows using parameterized queries.

        Uses psycopg2.extras.execute_values for batch insertion.
        Returns the number of rows inserted.
        """
        if not rows:
            return 0

        conn = self._get_connection()
        try:
            columns = list(rows[0].keys())
            insert_sql = sql.SQL("INSERT INTO {}.{} ({}) VALUES %s").format(
                sql.Identifier(schema_name),
                sql.Identifier(table_name),
                sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            )

            values = [tuple(row[c] for c in columns) for row in rows]

            with conn.cursor() as cur:
                extras.execute_values(cur, insert_sql.as_string(conn), values)
            conn.commit()

            row_count = len(rows)
            logger.info(f"Inserted {row_count} rows into {schema_name}.{table_name}")
            return row_count
        except Exception:
            conn.rollback()
            raise
        finally:
            self._return_connection(conn)

    def grant_schema_access(self, schema_name: str, principal: str) -> None:
        """Grant USAGE + CRUD on schema to a service principal.

        Grants:
        - USAGE ON SCHEMA
        - SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA
        """
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(schema_name),
                        sql.Identifier(principal),
                    )
                )
                cur.execute(
                    sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(
                        sql.Identifier(schema_name),
                        sql.Identifier(principal),
                    )
                )
            conn.commit()
            logger.info(f"Granted schema access: {schema_name} -> {principal}")
        except Exception:
            conn.rollback()
            raise
        finally:
            self._return_connection(conn)

    def schema_exists(self, schema_name: str) -> bool:
        """Check if a schema already exists."""
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                    (schema_name,),
                )
                return cur.fetchone() is not None
        finally:
            self._return_connection(conn)

    def get_table_row_counts(self, schema_name: str, table_names: list[str]) -> dict[str, int]:
        """Return row counts per table for validation."""
        conn = self._get_connection()
        try:
            counts = {}
            with conn.cursor() as cur:
                for table_name in table_names:
                    count_sql = sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                        sql.Identifier(schema_name),
                        sql.Identifier(table_name),
                    )
                    cur.execute(count_sql)
                    result = cur.fetchone()
                    counts[table_name] = result[0] if result else 0
            return counts
        finally:
            self._return_connection(conn)

    def execute_query(self, schema_name: str, query: str, params: tuple | None = None) -> list[dict]:
        """Execute a parameterized query. Returns list of dicts for queries with results."""
        conn = self._get_connection()
        try:
            with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
                cur.execute(query, params)
                # Fetch results if the query returns rows (SELECT, INSERT...RETURNING, etc.)
                if cur.description is not None:
                    rows = [dict(row) for row in cur.fetchall()]
                else:
                    rows = []
                # Always commit — handles INSERT, UPDATE, DELETE, and SELECT safely
                conn.commit()
                return rows
        except Exception:
            conn.rollback()
            raise
        finally:
            self._return_connection(conn)

    def table_exists(self, schema_name: str, table_name: str) -> bool:
        """Check if a table exists in the given schema."""
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
                    (schema_name, table_name),
                )
                return cur.fetchone() is not None
        finally:
            self._return_connection(conn)

    def _build_create_table_ddl(self, schema_name: str, table: TableDefinition) -> sql.Composable:
        """Build CREATE TABLE DDL from a TableDefinition."""
        parts = []
        pk_columns = []

        for col in table.columns:
            col_parts = [sql.Identifier(col.name), sql.SQL(col.data_type)]
            if col.is_primary_key:
                pk_columns.append(col.name)
            if not col.nullable and not col.is_primary_key:
                col_parts.append(sql.SQL("NOT NULL"))
            if col.default_expression:
                col_parts.append(sql.SQL("DEFAULT " + col.default_expression))
            parts.append(sql.SQL(" ").join(col_parts))

        # Primary key constraint
        if pk_columns:
            pk_constraint = sql.SQL("PRIMARY KEY ({})").format(
                sql.SQL(", ").join(sql.Identifier(c) for c in pk_columns)
            )
            parts.append(pk_constraint)

        # Foreign key constraints
        for fk in table.foreign_keys:
            fk_constraint = sql.SQL("FOREIGN KEY ({}) REFERENCES {}.{} ({}) ON DELETE {}").format(
                sql.Identifier(fk.column),
                sql.Identifier(schema_name),
                sql.Identifier(fk.references_table),
                sql.Identifier(fk.references_column),
                sql.SQL(fk.on_delete),
            )
            parts.append(fk_constraint)

        return sql.SQL("CREATE TABLE {}.{} ({})").format(
            sql.Identifier(schema_name),
            sql.Identifier(table.name),
            sql.SQL(", ").join(parts),
        )

    def _build_create_index_ddl(self, schema_name: str, index: IndexDefinition) -> sql.Composable:
        """Build CREATE INDEX DDL from an IndexDefinition."""
        unique = sql.SQL("UNIQUE ") if index.unique else sql.SQL("")
        return sql.SQL("CREATE {}INDEX {} ON {}.{} ({})").format(
            unique,
            sql.Identifier(index.name),
            sql.Identifier(schema_name),
            sql.Identifier(index.table_name),
            sql.SQL(", ").join(sql.Identifier(c) for c in index.columns),
        )
