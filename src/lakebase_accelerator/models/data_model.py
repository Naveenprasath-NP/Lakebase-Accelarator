"""Data model definitions — DDL-ready table, column, FK, and index structures."""

from pydantic import BaseModel, ConfigDict, Field


class ColumnDefinition(BaseModel):
    """A DDL-ready column definition."""

    model_config = ConfigDict(frozen=True)

    name: str
    """Column name."""

    data_type: str
    """Full PostgreSQL type expression (e.g., 'VARCHAR(255)', 'UUID')."""

    nullable: bool = True
    is_primary_key: bool = False
    default_expression: str | None = None
    """SQL default expression."""


class ForeignKeyDefinition(BaseModel):
    """A foreign key constraint."""

    model_config = ConfigDict(frozen=True)

    column: str
    """Local column name."""

    references_table: str
    """Referenced table name."""

    references_column: str
    """Referenced column name."""

    on_delete: str = "CASCADE"


class IndexDefinition(BaseModel):
    """An index definition."""

    model_config = ConfigDict(frozen=True)

    name: str
    """Index name."""

    table_name: str
    columns: list[str]
    unique: bool = False


class TableDefinition(BaseModel):
    """A complete DDL-ready table definition."""

    model_config = ConfigDict(frozen=True)

    name: str
    """Table name (snake_case, plural)."""

    columns: list[ColumnDefinition]
    foreign_keys: list[ForeignKeyDefinition] = Field(default_factory=list)
    indexes: list[IndexDefinition] = Field(default_factory=list)


class DataModel(BaseModel):
    """Complete data model with tables in creation order."""

    model_config = ConfigDict(frozen=True)

    tables: list[TableDefinition]
    """Tables in topological order (respecting FK dependencies)."""

    creation_order: list[str]
    """Table names in the order they should be created."""
