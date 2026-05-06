"""Topological sort for table creation order using Kahn's algorithm."""

from collections import deque

from lakebase_accelerator.models.data_model import TableDefinition
from lakebase_accelerator.utils.exceptions import CIRCULAR_FK_DEPENDENCY, PipelineStepError


def compute_creation_order(tables: list[TableDefinition]) -> list[str]:
    """Compute topological creation order for tables respecting FK dependencies.

    Uses Kahn's algorithm. Tables with no FK dependencies come first.

    Args:
        tables: List of table definitions with foreign key information.

    Returns:
        List of table names in valid creation order.

    Raises:
        PipelineStepError: If circular FK dependencies are detected.
    """
    if not tables:
        return []

    table_names = {t.name for t in tables}

    # Build adjacency graph: edge from referenced_table -> dependent_table
    # in_degree counts how many tables a given table depends on
    in_degree: dict[str, int] = {t.name: 0 for t in tables}
    adjacency: dict[str, list[str]] = {t.name: [] for t in tables}

    for table in tables:
        for fk in table.foreign_keys:
            # Only consider FK references to tables in our set
            if fk.references_table in table_names:
                adjacency[fk.references_table].append(table.name)
                in_degree[table.name] += 1

    # Initialize queue with tables that have no dependencies
    queue: deque[str] = deque(name for name, degree in in_degree.items() if degree == 0)

    result: list[str] = []

    while queue:
        current = queue.popleft()
        result.append(current)

        for neighbor in adjacency[current]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(result) != len(tables):
        raise PipelineStepError(
            step_name="data_model_inference",
            message="Circular foreign key dependencies detected among tables",
            error_code=CIRCULAR_FK_DEPENDENCY,
        )

    return result
