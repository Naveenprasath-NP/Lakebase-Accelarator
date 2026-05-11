"""Agent tools package.

Re-exports greenfield tools (backward compatibility) and brownfield exploration tools.
"""

from lakebase_accelerator.agent.tools.brownfield_tools import (
    BROWNFIELD_TOOLS,
    get_file_metadata,
    list_volume_directory,
    read_volume_file,
    read_volume_file_section,
    search_in_files,
    set_volume_cache,
)
from lakebase_accelerator.agent.tools.greenfield_tools import (
    ALL_TOOLS,
    _get_repo,
    _get_workspace_client,
    deploy_app,
    grant_schema_permissions,
    insert_seed_data,
    provision_schema,
    set_tool_dependencies,
    write_file,
)

__all__ = [
    # Greenfield tools (backward compatibility)
    "ALL_TOOLS",
    "_get_repo",
    "_get_workspace_client",
    "deploy_app",
    "grant_schema_permissions",
    "insert_seed_data",
    "provision_schema",
    "set_tool_dependencies",
    "write_file",
    # Brownfield tools
    "BROWNFIELD_TOOLS",
    "get_file_metadata",
    "list_volume_directory",
    "read_volume_file",
    "read_volume_file_section",
    "search_in_files",
    "set_volume_cache",
]
