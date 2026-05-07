"""Tests for topological sort of table creation order."""

import pytest

from lakebase_accelerator.core.topological_sort import compute_creation_order
from lakebase_accelerator.models.data_model import (
    ColumnDefinition,
    ForeignKeyDefinition,
    TableDefinition,
)
from lakebase_accelerator.utils.exceptions import CIRCULAR_FK_DEPENDENCY, PipelineStepError


def _make_table(name: str, fk_refs: list[tuple[str, str]] | None = None) -> TableDefinition:
    """Helper to create a TableDefinition with optional FK references."""
    columns = [ColumnDefinition(name="id", data_type="UUID", is_primary_key=True)]
    foreign_keys = []
    if fk_refs:
        for col_name, ref_table in fk_refs:
            columns.append(ColumnDefinition(name=col_name, data_type="UUID"))
            foreign_keys.append(
                ForeignKeyDefinition(
                    column=col_name,
                    references_table=ref_table,
                    references_column="id",
                )
            )
    return TableDefinition(name=name, columns=columns, foreign_keys=foreign_keys)


class TestComputeCreationOrder:
    """Tests for compute_creation_order function."""

    def test_empty_list_returns_empty(self) -> None:
        """Empty input returns empty output."""
        assert compute_creation_order([]) == []

    def test_single_table(self) -> None:
        """Single table with no FKs returns that table."""
        tables = [_make_table("users")]
        result = compute_creation_order(tables)
        assert result == ["users"]

    def test_tables_with_no_fks(self) -> None:
        """Tables with no FK dependencies can appear in any order."""
        tables = [_make_table("users"), _make_table("products"), _make_table("categories")]
        result = compute_creation_order(tables)
        assert set(result) == {"users", "products", "categories"}
        assert len(result) == 3

    def test_linear_chain(self) -> None:
        """Linear chain A -> B -> C: A must come before B, B before C."""
        # C depends on B, B depends on A
        table_a = _make_table("a")
        table_b = _make_table("b", fk_refs=[("a_id", "a")])
        table_c = _make_table("c", fk_refs=[("b_id", "b")])
        tables = [table_c, table_b, table_a]  # Shuffled input order

        result = compute_creation_order(tables)

        assert result.index("a") < result.index("b")
        assert result.index("b") < result.index("c")

    def test_diamond_dependency(self) -> None:
        """Diamond: D depends on B and C, B and C depend on A."""
        table_a = _make_table("a")
        table_b = _make_table("b", fk_refs=[("a_id", "a")])
        table_c = _make_table("c", fk_refs=[("a_id", "a")])
        table_d = _make_table("d", fk_refs=[("b_id", "b"), ("c_id", "c")])
        tables = [table_d, table_c, table_b, table_a]

        result = compute_creation_order(tables)

        assert result.index("a") < result.index("b")
        assert result.index("a") < result.index("c")
        assert result.index("b") < result.index("d")
        assert result.index("c") < result.index("d")

    def test_circular_dependency_raises_error(self) -> None:
        """Circular FK dependencies raise PipelineStepError."""
        # A -> B -> C -> A (cycle)
        table_a = _make_table("a", fk_refs=[("c_id", "c")])
        table_b = _make_table("b", fk_refs=[("a_id", "a")])
        table_c = _make_table("c", fk_refs=[("b_id", "b")])
        tables = [table_a, table_b, table_c]

        with pytest.raises(PipelineStepError) as exc_info:
            compute_creation_order(tables)

        assert exc_info.value.step_name == "data_model_inference"
        assert exc_info.value.error_code == CIRCULAR_FK_DEPENDENCY
