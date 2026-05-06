# Zeb Agentic Lakebase Accelerator — requirement.md

## 1. Project goal
Build a Databricks-native agentic application accelerator that supports two flows:
- Greenfield: create a new app from a business prompt.
- Brownfield: ingest an existing prototype or repo and convert it into a production Databricks App.

The solution must use:
- Databricks Apps as the application runtime.
- Lakebase as the transactional operational database.
- Unity Catalog as the governed data and tool layer.
- Model Serving as the LLM endpoint for the agent.
- Unity Catalog functions as the agent tool interface.

## 2. Core user journey
User flow:
Landing page -> project creation -> choose Greenfield or Brownfield -> input capture -> AI analysis -> data model generation -> Lakebase setup -> reverse ETL mapping -> app generation -> testing -> deploy to Databricks Apps -> runtime monitoring.

## 3. Main architecture
### 3.1 App layer
- One Databricks App acts as the UI and orchestration layer.
- The app contains the chat interface, project setup, tool execution, and deployment controls.

### 3.2 Agent layer
- The LLM/agent makes reasoning decisions.
- The agent calls tools only through approved Unity Catalog functions or managed tool integration.

### 3.3 Data layer
- Lakebase stores operational state, app records, workflow data, and agent memory.
- Synced tables bring curated Unity Catalog data into Lakebase for low-latency app serving.

### 3.4 Governance layer
- Unity Catalog governs source data, function tools, permissions, and catalog visibility.
- App/service-principal access must be least-privilege.

## 4. Functional requirements
### 4.1 Greenfield mode
The system must:
- accept a business prompt,
- ask clarification questions if needed,
- infer business entities and workflows,
- generate an operational data model,
- create Lakebase schema/tables,
- map source Gold/Silver data for reverse ETL,
- generate the app structure,
- deploy the app automatically.

### 4.2 Brownfield mode
The system must:
- accept prototype code/screens/repo,
- infer entities, screens, workflows, and APIs,
- identify missing production backend components,
- map the prototype to Databricks App modules,
- generate the target Lakebase schema,
- migrate or adapt the application for production deployment.

### 4.3 Agent tools
Define agent tools for:
- requirement intake,
- prototype ingestion,
- data model inference,
- reverse ETL mapping,
- Lakebase provisioning,
- schema evolution,
- data ingest,
- query,
- write/update,
- app generation,
- deployment,
- model serving call,
- validation/testing,
- monitoring/audit.

## 5. Tool design requirement
Use Unity Catalog functions as the primary tool mechanism for structured tool actions.
Each function must:
- have a clear purpose,
- accept explicit typed parameters,
- return a structured output,
- be documented with meaningful comments/docstrings,
- be permissioned through Unity Catalog.

Use MCP only if later required for standardizing tool access across multiple agents.

## 6. Data requirements
### 6.1 Lakehouse source
Use curated Unity Catalog data as the source for operational activation.
Preferred order:
- Gold tables first,
- Silver tables if needed,
- Bronze only for raw/history, not for direct app serving.

### 6.2 Lakebase operational data
Store:
- customer/application records,
- workflow state,
- user actions,
- agent memory,
- audit logs,
- synced serving tables.

### 6.3 Reverse ETL
Sync curated data from Unity Catalog into Lakebase as operational tables.
This is the serving layer for the app and agent.

## 7. LLM/model serving requirements
- The agent must use a model serving endpoint for reasoning and structured output generation.
- The model serving endpoint must be added as an app resource.
- The app should have at least `Can query` permission on the endpoint.
- The endpoint must be in READY state before deployment.

## 8. Databricks Apps requirements
- The generated app must deploy to Databricks Apps.
- The app must support app resources for:
  - Lakebase,
  - model serving endpoint,
  - Unity Catalog resources if needed.
- The app must run with a service principal.
- The app must expose a user-facing UI and an agent interaction area.

## 9. Security and access requirements
The implementation must use:
- least-privilege service principal access,
- governed Unity Catalog permissions,
- secure secret storage,
- environment separation for Dev/Test/Prod,
- audit logging for tool calls and deployment actions.

## 10. Testing requirements
The system must validate:
- schema generation,
- tool registration,
- data access permissions,
- synced table connectivity,
- model endpoint connectivity,
- app rendering,
- write/update actions,
- deployment readiness.

## 11. Deployment requirements
The system must support:
- local/dev testing,
- deployment to Databricks Apps,
- attachment of Lakebase resource,
- attachment of model serving endpoint,
- auto-publish of customer app,
- runtime monitoring after go-live.

## 12. Customer inputs required
From the customer, collect:
- Databricks workspace details,
- admin contacts,
- source catalog/schema/table names,
- business domain and workflow details,
- prototype repo or screenshots if brownfield,
- security/compliance constraints,
- go-live environment strategy,
- approval to use Lakebase and model serving.

## 13. Acceptance criteria
The project is successful when:
- a greenfield prompt can generate and deploy a working Databricks App,
- a brownfield prototype can be migrated into a production-ready app,
- Lakebase is used as the operational runtime database,
- Unity Catalog functions work as agent tools,
- the LLM endpoint is callable from the app,
- the final app is deployed and monitorable in Databricks.

## 14. Implementation note for Kiro notebook
Build the accelerator in this order:
1. Project setup and input capture.
2. Greenfield and brownfield analysis.
3. Domain model generation.
4. Lakebase schema creation.
5. Reverse ETL mapping.
6. Unity Catalog function tool creation.
7. Model serving integration.
8. App generation.
9. Validation/testing.
10. Databricks Apps deployment.
11. Monitoring and audit.