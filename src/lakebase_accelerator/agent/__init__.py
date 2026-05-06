"""Multi-agent LangGraph pipeline for the Lakebase Accelerator.

Architecture:
- Orchestrator (StateGraph) routes through sequential nodes
- Some nodes are sub-agents (with their own tool-calling loops)
- Some nodes are deterministic tools (no LLM reasoning needed)

Nodes:
1. intake_agent — Intent classification + entity extraction
2. data_model_agent — PostgreSQL schema design
3. schema_provisioning — Create schema/tables (deterministic)
4. seed_data_agent — Generate + insert data per table
5. backend_dev_agent — Generate backend files with validation loop
6. frontend_dev_agent — Generate frontend files with validation loop
7. integration — Bundle + validate all files (deterministic)
8. deployment — Write to workspace, deploy, test (deterministic)
"""
