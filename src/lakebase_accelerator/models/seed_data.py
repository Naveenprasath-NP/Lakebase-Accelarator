"""Seed data models for generated table data."""

from pydantic import BaseModel, ConfigDict, Field


class SeedDataTable(BaseModel):
    """Seed data for a single table."""

    model_config = ConfigDict(frozen=True)

    table_name: str
    rows: list[dict]
    """List of row dicts. Keys are column names."""


class SeedData(BaseModel):
    """Seed data for all tables in insertion order."""

    model_config = ConfigDict(frozen=True)

    tables: list[SeedDataTable] = Field(default_factory=list)
    """Seed data per table, in topological insertion order."""
