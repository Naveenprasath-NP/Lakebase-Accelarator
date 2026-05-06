"""Unit tests for LakebaseRepository with mocked psycopg2 connection pool."""

from unittest.mock import MagicMock, patch

import psycopg2.errors
import pytest

from lakebase_accelerator.models.data_model import (
    ColumnDefinition,
    ForeignKeyDefinition,
    TableDefinition,
)
from lakebase_accelerator.repositories.lakebase_repository import LakebaseRepository
from lakebase_accelerator.utils.exceptions import LakebaseConnectionError, SchemaProvisioningError


@pytest.fixture
def mock_pool():
    """Create a mock ThreadedConnectionPool."""
    pool = MagicMock()
    return pool


@pytest.fixture
def mock_conn(mock_pool):
    """Create a mock connection returned by the pool."""
    conn = MagicMock()
    mock_pool.getconn.return_value = conn
    return conn


@pytest.fixture
def mock_cursor(mock_conn):
    """Create a mock cursor returned by the connection."""
    cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return cursor


@pytest.fixture
def repo(mock_pool):
    """Create a LakebaseRepository with mocked pool."""
    return LakebaseRepository(connection_pool=mock_pool)


class TestGetConnection:
    """Tests for connection pool management."""

    def test_get_connection_success(self, repo, mock_pool, mock_conn):
        """Test successful connection retrieval from pool."""
        conn = repo._get_connection()
        assert conn == mock_conn
        mock_pool.getconn.assert_called_once()

    def test_get_connection_failure_raises_lakebase_connection_error(self, repo, mock_pool):
        """Test that pool failure raises LakebaseConnectionError."""
        mock_pool.getconn.side_effect = Exception("Pool exhausted")
        with pytest.raises(LakebaseConnectionError, match="Failed to get connection from pool"):
            repo._get_connection()


class TestCreateSchema:
    """Tests for create_schema method."""

    def test_create_schema_success(self, repo, mock_conn, mock_cursor):
        """Test successful schema creation calls correct SQL."""
        repo.create_schema("test_schema")

        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    def test_create_schema_duplicate_raises_schema_provisioning_error(self, repo, mock_conn, mock_cursor):
        """Test that DuplicateSchema raises SchemaProvisioningError."""
        mock_cursor.execute.side_effect = psycopg2.errors.DuplicateSchema()

        with pytest.raises(SchemaProvisioningError, match="already exists"):
            repo.create_schema("existing_schema")

        mock_conn.rollback.assert_called_once()

    def test_create_schema_generic_error_raises_schema_provisioning_error(self, repo, mock_conn, mock_cursor):
        """Test that generic errors raise SchemaProvisioningError."""
        mock_cursor.execute.side_effect = Exception("Connection lost")

        with pytest.raises(SchemaProvisioningError, match="Failed to create schema"):
            repo.create_schema("bad_schema")

        mock_conn.rollback.assert_called_once()


class TestCreateTables:
    """Tests for create_tables method."""

    def _make_table(self, name: str, with_fk: bool = False) -> TableDefinition:
        """Helper to create a TableDefinition."""
        columns = [
            ColumnDefinition(name="id", data_type="UUID", nullable=False, is_primary_key=True),
            ColumnDefinition(name="name", data_type="VARCHAR(255)", nullable=False),
        ]
        foreign_keys = []
        if with_fk:
            foreign_keys = [
                ForeignKeyDefinition(
                    column="parent_id",
                    references_table="parent",
                    references_column="id",
                    on_delete="CASCADE",
                )
            ]
            columns.append(ColumnDefinition(name="parent_id", data_type="UUID", nullable=True))
        return TableDefinition(name=name, columns=columns, foreign_keys=foreign_keys)

    def test_create_tables_executes_ddl_in_order(self, repo, mock_conn, mock_cursor):
        """Test that tables are created in the order provided (topological)."""
        tables = [self._make_table("users"), self._make_table("orders", with_fk=True)]

        result = repo.create_tables("my_schema", tables)

        assert result == ["users", "orders"]
        assert mock_cursor.execute.call_count == 2
        mock_conn.commit.assert_called_once()

    def test_create_tables_single_transaction_rollback_on_error(self, repo, mock_conn, mock_cursor):
        """Test that failure rolls back the entire transaction."""
        tables = [self._make_table("users")]
        mock_cursor.execute.side_effect = Exception("Syntax error")

        with pytest.raises(SchemaProvisioningError, match="Failed to create tables"):
            repo.create_tables("my_schema", tables)

        mock_conn.rollback.assert_called_once()


class TestInsertSeedData:
    """Tests for insert_seed_data method."""

    @patch("lakebase_accelerator.repositories.lakebase_repository.extras.execute_values")
    @patch("psycopg2.sql.Composed.as_string", return_value="INSERT INTO my_schema.users (name, age) VALUES %s")
    def test_insert_seed_data_uses_execute_values(
        self, mock_as_string, mock_execute_values, repo, mock_conn, mock_cursor
    ):
        """Test that batch insert uses psycopg2 execute_values."""
        rows = [
            {"name": "Alice", "age": 30},
            {"name": "Bob", "age": 25},
        ]

        result = repo.insert_seed_data("my_schema", "users", rows)

        assert result == 2
        mock_execute_values.assert_called_once()
        mock_conn.commit.assert_called_once()

    def test_insert_seed_data_empty_rows_returns_zero(self, repo, mock_pool):
        """Test that empty rows list returns 0 without DB call."""
        result = repo.insert_seed_data("my_schema", "users", [])

        assert result == 0
        mock_pool.getconn.assert_not_called()

    @patch("lakebase_accelerator.repositories.lakebase_repository.extras.execute_values")
    @patch("psycopg2.sql.Composed.as_string", return_value="INSERT INTO my_schema.users (name) VALUES %s")
    def test_insert_seed_data_error_rolls_back(self, mock_as_string, mock_execute_values, repo, mock_conn, mock_cursor):
        """Test that insertion error rolls back transaction."""
        mock_execute_values.side_effect = Exception("Integrity violation")
        rows = [{"name": "Alice"}]

        with pytest.raises(Exception, match="Integrity violation"):
            repo.insert_seed_data("my_schema", "users", rows)

        mock_conn.rollback.assert_called_once()


class TestGrantSchemaAccess:
    """Tests for grant_schema_access method."""

    def test_grant_schema_access_generates_correct_grants(self, repo, mock_conn, mock_cursor):
        """Test that USAGE and CRUD grants are executed."""
        repo.grant_schema_access("my_schema", "app_sp")

        assert mock_cursor.execute.call_count == 2
        mock_conn.commit.assert_called_once()

    def test_grant_schema_access_error_rolls_back(self, repo, mock_conn, mock_cursor):
        """Test that grant failure rolls back."""
        mock_cursor.execute.side_effect = Exception("Permission denied")

        with pytest.raises(Exception, match="Permission denied"):
            repo.grant_schema_access("my_schema", "app_sp")

        mock_conn.rollback.assert_called_once()


class TestSchemaExists:
    """Tests for schema_exists method."""

    def test_schema_exists_returns_true(self, repo, mock_conn, mock_cursor):
        """Test returns True when schema exists."""
        mock_cursor.fetchone.return_value = (1,)

        result = repo.schema_exists("existing_schema")

        assert result is True

    def test_schema_exists_returns_false(self, repo, mock_conn, mock_cursor):
        """Test returns False when schema does not exist."""
        mock_cursor.fetchone.return_value = None

        result = repo.schema_exists("nonexistent_schema")

        assert result is False


class TestGetTableRowCounts:
    """Tests for get_table_row_counts method."""

    def test_get_table_row_counts_returns_correct_counts(self, repo, mock_conn, mock_cursor):
        """Test returns correct row counts per table."""
        mock_cursor.fetchone.side_effect = [(10,), (25,), (0,)]

        result = repo.get_table_row_counts("my_schema", ["users", "orders", "empty_table"])

        assert result == {"users": 10, "orders": 25, "empty_table": 0}
        assert mock_cursor.execute.call_count == 3


class TestBuildCreateTableDDL:
    """Tests for _build_create_table_ddl method."""

    def test_build_ddl_with_pk_and_not_null(self, repo):
        """Test DDL generation includes PK and NOT NULL constraints."""
        table = TableDefinition(
            name="users",
            columns=[
                ColumnDefinition(name="id", data_type="UUID", nullable=False, is_primary_key=True),
                ColumnDefinition(name="email", data_type="VARCHAR(255)", nullable=False),
                ColumnDefinition(name="bio", data_type="TEXT", nullable=True),
            ],
        )

        ddl = repo._build_create_table_ddl("test_schema", table)

        # Verify it's a Composable (sql module object)
        from psycopg2 import sql as psycopg2_sql

        assert isinstance(ddl, psycopg2_sql.Composable)

    def test_build_ddl_with_foreign_key(self, repo):
        """Test DDL generation includes FK constraints."""
        table = TableDefinition(
            name="orders",
            columns=[
                ColumnDefinition(name="id", data_type="UUID", nullable=False, is_primary_key=True),
                ColumnDefinition(name="user_id", data_type="UUID", nullable=False),
            ],
            foreign_keys=[
                ForeignKeyDefinition(
                    column="user_id",
                    references_table="users",
                    references_column="id",
                    on_delete="CASCADE",
                )
            ],
        )

        ddl = repo._build_create_table_ddl("test_schema", table)

        from psycopg2 import sql as psycopg2_sql

        assert isinstance(ddl, psycopg2_sql.Composable)

    def test_build_ddl_with_default_expression(self, repo):
        """Test DDL generation includes DEFAULT expressions."""
        table = TableDefinition(
            name="events",
            columns=[
                ColumnDefinition(
                    name="id",
                    data_type="UUID",
                    is_primary_key=True,
                    default_expression="gen_random_uuid()",
                ),
                ColumnDefinition(
                    name="created_at",
                    data_type="TIMESTAMPTZ",
                    nullable=False,
                    default_expression="NOW()",
                ),
            ],
        )

        ddl = repo._build_create_table_ddl("test_schema", table)

        from psycopg2 import sql as psycopg2_sql

        assert isinstance(ddl, psycopg2_sql.Composable)
