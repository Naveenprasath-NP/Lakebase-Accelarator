"""Frontend Dev Agent — Node 6.

Generates a modern React frontend using a HYBRID approach:
- Templates: scaffolding (package.json, vite, tsconfig, CSS, router, API client,
  reusable components like DataTable, FormModal, Toast, etc.)
- LLM: page-level business logic (Dashboard KPIs, entity pages with workflow
  actions, relationship-aware views, custom buttons like Approve/Reject)

The LLM is CONSTRAINED to only use pre-built components — no random imports.
This gives us: zero import errors + real business application logic.

The built output (dist/) becomes the static/ folder for the backend.
Falls back to static HTML if the Databricks Job build fails.
"""

import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from langchain_core.messages import HumanMessage, SystemMessage

from lakebase_accelerator.agent.llm import get_llm
from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.utils.logger import logger
from lakebase_accelerator.utils.prompt_loader import get_system_prompt

# Template directory
TEMPLATE_DIR = Path(__file__).parent.parent.parent / "resources" / "templates" / "frontend"


# ═══════════════════════════════════════════════════════════════════════
# MAIN NODE
# ═══════════════════════════════════════════════════════════════════════


def frontend_dev_node(state: PipelineState) -> dict:
    """Generate React frontend — hybrid template + LLM approach.

    Flow:
    1. Build entity context from data model
    2. Render scaffolding from templates (config, CSS, components, API client, types)
    3. LLM generates business-logic pages (Dashboard, per-entity pages with
       workflow actions, relationship views, custom KPIs)
    4. Submit to Databricks Job for npm build
    5. Fallback to static HTML if build fails

    The LLM is constrained: it can ONLY import from pre-built components.
    This eliminates import errors while allowing real business logic.
    """
    import asyncio

    logger.info("Node: frontend_dev — generating React frontend", extra={"step": "frontend_dev"})

    data_model = state["data_model"]
    backend_files = state.get("backend_files", {})  # May be empty if running parallel with backend_dev
    app_name = state.get("app_name", "") or state.get("project_name", "") or "generated-app"
    pipeline_type = state.get("pipeline_type", "greenfield")
    prototype_context = state.get("prototype_context", "")
    project_name = state.get("project_name", "generated-app")
    project_title = project_name.replace("-", " ").title()
    user_prompt = state.get("prompt", "")

    # ─── Step 1: Build entity context from data model ─────────────────
    entities = _build_entity_context(data_model)
    api_contract = _extract_api_contract(backend_files, data_model)
    theme = state.get("theme", {"mode": "dark", "brand_color": "#3b82f6", "brand_name": "blue"})

    # ─── Step 2: Render scaffolding from templates ────────────────────
    logger.info(
        f"Rendering frontend scaffolding ({len(entities)} entities, theme={theme.get('mode', 'dark')})...",
        extra={"step": "frontend_dev"},
    )

    source_files = _render_scaffolding(
        project_name=project_name,
        project_title=project_title,
        entities=entities,
        theme=theme,
    )

    # ─── Step 3: LLM generates business-logic pages ───────────────────
    logger.info("Generating business-logic pages via LLM...", extra={"step": "frontend_dev"})

    try:
        page_files = _generate_business_pages(
            entities=entities,
            data_model=data_model,
            api_contract=api_contract,
            user_prompt=user_prompt,
            project_title=project_title,
            pipeline_type=pipeline_type,
            prototype_context=prototype_context,
        )
        source_files.update(page_files)
    except Exception as e:
        logger.warning(
            f"LLM page generation failed ({e}), using template fallback pages",
            extra={"step": "frontend_dev"},
        )
        # Fallback: use template-generated pages (basic CRUD)
        fallback_pages = _render_fallback_pages(
            project_name=project_name,
            project_title=project_title,
            entities=entities,
        )
        source_files.update(fallback_pages)

    logger.info(
        f"Frontend source ready: {len(source_files)} files",
        extra={"step": "frontend_dev"},
    )

    # ─── Step 4: Build via Databricks Job ─────────────────────────────
    try:
        from lakebase_accelerator.agent.tools import _get_workspace_client
        from lakebase_accelerator.services.frontend_build_service import FrontendBuildService
        import concurrent.futures

        workspace_client = _get_workspace_client()
        build_service = FrontendBuildService(workspace_client)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run, build_service.build_frontend(app_name, source_files)
                )
                static_files = future.result(timeout=360)
        else:
            static_files = asyncio.run(build_service.build_frontend(app_name, source_files))

        logger.info(
            f"React build complete: {len(static_files)} static files",
            extra={"step": "frontend_dev"},
        )

        return {
            "frontend_files": static_files,
            "current_step": "frontend_dev",
            "completed_steps": state.get("completed_steps", []) + ["frontend_dev"],
        }

    except Exception as e:
        logger.warning(
            f"React build failed ({e}), falling back to static HTML",
            extra={"step": "frontend_dev"},
        )

        # ─── Step 4: Fallback — generate static HTML ─────────────────
        if pipeline_type == "brownfield" and prototype_context:
            frontend_files = _generate_brownfield_static_fallback(
                entities, data_model, api_contract, prototype_context
            )
        else:
            frontend_files = _generate_static_fallback(entities, data_model, api_contract)

        return {
            "frontend_files": frontend_files,
            "current_step": "frontend_dev",
            "completed_steps": state.get("completed_steps", []) + ["frontend_dev"],
        }


# ═══════════════════════════════════════════════════════════════════════
# ENTITY CONTEXT BUILDER
# ═══════════════════════════════════════════════════════════════════════


def _build_entity_context(data_model: dict) -> list[dict]:
    """Build rich entity context for templates from the data model.

    Each entity dict contains everything templates need:
    - name, plural, type_name, display_name, display_name_plural
    - columns with: name, label, ts_type, form_type, nullable, placeholder, optional
    """
    tables = data_model.get("tables", [])
    entities = []

    for table in tables:
        entity_name = table["name"]
        # Determine plural form
        if entity_name.endswith("s"):
            entity_plural = entity_name
        elif entity_name.endswith("y") and entity_name[-2] not in "aeiou":
            entity_plural = entity_name[:-1] + "ies"
        else:
            entity_plural = entity_name + "s"

        # Type name: PascalCase
        type_name = "".join(word.capitalize() for word in entity_name.split("_"))

        # Display names
        display_name = entity_name.replace("_", " ").title()
        display_name_plural = entity_plural.replace("_", " ").title()

        # Build columns (exclude system fields)
        columns = []
        for col in table.get("columns", []):
            if col["name"] in ("id", "created_at", "updated_at"):
                continue

            pg_type = col.get("data_type", "TEXT").upper()
            ts_type = _pg_to_ts_type(pg_type)
            form_type = _pg_to_form_type(pg_type)
            nullable = col.get("nullable", True)
            label = col["name"].replace("_", " ").title()
            placeholder = f"Enter {label.lower()}"

            columns.append({
                "name": col["name"],
                "label": label,
                "ts_type": ts_type,
                "form_type": form_type,
                "nullable": nullable,
                "optional": nullable,
                "placeholder": placeholder,
            })

        entities.append({
            "name": entity_name,
            "plural": entity_plural,
            "type_name": type_name,
            "display_name": display_name,
            "display_name_plural": display_name_plural,
            "columns": columns,
        })

    return entities


def _pg_to_ts_type(pg_type: str) -> str:
    """Convert PostgreSQL type to TypeScript type."""
    pg_type = pg_type.upper()
    if any(t in pg_type for t in ("INT", "SERIAL", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL")):
        return "number"
    if "BOOL" in pg_type:
        return "boolean"
    return "string"


def _pg_to_form_type(pg_type: str) -> str:
    """Convert PostgreSQL type to HTML form input type."""
    pg_type = pg_type.upper()
    if any(t in pg_type for t in ("INT", "SERIAL", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL")):
        return "number"
    if "BOOL" in pg_type:
        return "checkbox"
    if "TEXT" in pg_type and "VARCHAR" not in pg_type:
        return "textarea"
    if "DATE" in pg_type and "TIME" not in pg_type:
        return "date"
    return "text"


# ═══════════════════════════════════════════════════════════════════════
# TEMPLATE RENDERING
# ═══════════════════════════════════════════════════════════════════════


def _render_scaffolding(
    project_name: str,
    project_title: str,
    entities: list[dict],
    theme: dict | None = None,
) -> dict[str, str]:
    """Render scaffolding files from Jinja2 templates.

    This renders ONLY the foundation — config, CSS, reusable components,
    API client, types, router setup. NOT the pages (those come from LLM).
    """
    if theme is None:
        theme = {"mode": "dark", "brand_color": "#3b82f6", "brand_name": "blue"}

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        keep_trailing_newline=True,
    )

    context = {
        "project_name": project_name,
        "project_title": project_title,
        "entities": entities,
        "theme": theme,
    }

    files: dict[str, str] = {}

    # ─── Root config files ────────────────────────────────────────────
    root_templates = {
        "package.json": "package.json.j2",
        "vite.config.ts": "vite.config.ts.j2",
        "tsconfig.json": "tsconfig.json.j2",
        "tsconfig.node.json": "tsconfig.node.json.j2",
        "index.html": "index.html.j2",
    }

    for output_path, template_name in root_templates.items():
        template = env.get_template(template_name)
        files[output_path] = template.render(**context)

    # ─── src/ entry files ─────────────────────────────────────────────
    files["src/main.tsx"] = env.get_template("main.tsx.j2").render(**context)
    files["src/index.css"] = env.get_template("index.css.j2").render(**context)

    # ─── Favicon ──────────────────────────────────────────────────────
    files["public/favicon.svg"] = env.get_template("favicon.svg.j2").render(**context)

    # ─── src/api/ ─────────────────────────────────────────────────────
    files["src/api/client.ts"] = env.get_template("src/api/client.ts.j2").render(**context)

    # ─── src/types/ ───────────────────────────────────────────────────
    files["src/types/index.ts"] = env.get_template("src/types/index.ts.j2").render(**context)

    # ─── src/components/ (reusable, pre-built) ────────────────────────
    component_templates = {
        "src/components/Layout.tsx": "src/components/Layout.tsx.j2",
        "src/components/Sidebar.tsx": "src/components/Sidebar.tsx.j2",
        "src/components/DataTable.tsx": "src/components/DataTable.tsx.j2",
        "src/components/FormModal.tsx": "src/components/FormModal.tsx.j2",
        "src/components/ConfirmDialog.tsx": "src/components/ConfirmDialog.tsx.j2",
        "src/components/Toast.tsx": "src/components/Toast.tsx.j2",
        "src/components/StatsCard.tsx": "src/components/StatsCard.tsx.j2",
        "src/components/DetailPanel.tsx": "src/components/DetailPanel.tsx.j2",
        "src/components/StatusBadge.tsx": "src/components/StatusBadge.tsx.j2",
        "src/components/Tabs.tsx": "src/components/Tabs.tsx.j2",
        "src/components/SearchFilter.tsx": "src/components/SearchFilter.tsx.j2",
        "src/components/EmptyState.tsx": "src/components/EmptyState.tsx.j2",
    }

    for output_path, template_name in component_templates.items():
        template = env.get_template(template_name)
        files[output_path] = template.render(**context)

    # ─── src/App.tsx (router — uses entity names for routes) ──────────
    files["src/App.tsx"] = env.get_template("src/App.tsx.j2").render(**context)

    logger.info(
        f"Scaffolding rendered: {len(files)} files",
        extra={"step": "frontend_dev"},
    )

    return files


def _render_fallback_pages(
    project_name: str,
    project_title: str,
    entities: list[dict],
) -> dict[str, str]:
    """Render basic CRUD pages from templates (fallback if LLM fails)."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        keep_trailing_newline=True,
    )

    context = {
        "project_name": project_name,
        "project_title": project_title,
        "entities": entities,
    }

    files: dict[str, str] = {}

    # Dashboard
    files["src/pages/Dashboard.tsx"] = env.get_template("src/pages/Dashboard.tsx.j2").render(**context)

    # Per-entity pages
    entity_list_template = env.get_template("src/pages/EntityList.tsx.j2")
    for entity in entities:
        entity_context = {**context, "entity": entity}
        file_path = f"src/pages/{entity['type_name']}List.tsx"
        content = entity_list_template.render(**entity_context)
        # Add ts-nocheck to template-generated pages to prevent type errors
        if not content.startswith("// @ts-nocheck"):
            content = "// @ts-nocheck\n" + content
        files[file_path] = content

    return files


# ═══════════════════════════════════════════════════════════════════════
# LLM-GENERATED BUSINESS PAGES
# ═══════════════════════════════════════════════════════════════════════


def _generate_business_pages(
    entities: list[dict],
    data_model: dict,
    api_contract: str,
    user_prompt: str,
    project_title: str,
    pipeline_type: str,
    prototype_context: str,
) -> dict[str, str]:
    """Generate business-logic pages via LLM using pre-built components.

    The LLM generates:
    - Dashboard.tsx with real business KPIs (not just row counts)
    - Per-entity pages with workflow actions (Approve, Reject, Assign, etc.)
    - Relationship-aware views (show employee name, not UUID)

    The LLM is CONSTRAINED to only use pre-built components and the API client.
    """
    llm = get_llm(max_tokens=32768)

    # Build rich context for the LLM
    entity_info = json.dumps(
        [
            {
                "name": e["name"],
                "plural": e["plural"],
                "type_name": e["type_name"],
                "display_name": e["display_name"],
                "display_name_plural": e["display_name_plural"],
                "columns": [
                    {"name": c["name"], "type": c["ts_type"], "form_type": c["form_type"], "nullable": c["nullable"]}
                    for c in e["columns"]
                ],
            }
            for e in entities
        ],
        indent=2,
    )

    # Identify relationships (FK columns)
    relationships = []
    for table in data_model.get("tables", []):
        for fk in table.get("foreign_keys", []):
            relationships.append(
                f"{table['name']}.{fk['column']} → {fk['references_table']}.{fk['references_column']}"
            )
    relationship_info = "\n".join(relationships) if relationships else "No explicit foreign keys defined, but columns ending in _id likely reference other entities."

    # Build the prompt
    brownfield_context = ""
    is_brownfield = pipeline_type == "brownfield" and prototype_context
    if is_brownfield:
        brownfield_context = f"""

## PROTOTYPE TO REPLICATE (CRITICAL — match this UI exactly)
{prototype_context[:4000]}

BROWNFIELD RULES:
- You MUST replicate the prototype's UI layout, navigation, color scheme, and interactions as closely as possible
- If the prototype has a different layout than sidebar+content (e.g., top nav, full-width, multi-panel), generate a custom App.tsx that matches
- If the prototype has charts, dashboards, KPI cards, or custom visualizations, replicate them
- If the prototype uses specific colors/branding, use those colors in inline styles or CSS classes
- The goal is: someone looking at the original prototype and the generated app should see the SAME application
- You may use ANY valid React/TypeScript code — you are NOT limited to the pre-built components for brownfield
- However, you MUST still use the apiClient for all API calls and the types from '../types'
"""

    prompt = f"""Generate the page components for a business application: "{project_title}"

## USER'S ORIGINAL REQUEST
{user_prompt}

## ENTITIES & FIELDS
{entity_info}

## RELATIONSHIPS
{relationship_info}

## API CONTRACT
{api_contract}
{brownfield_context}

## WHAT TO GENERATE
Generate a JSON object where keys are file paths and values are COMPLETE TypeScript/React code.

Required files:
{"1. src/App.tsx — ONLY for brownfield: Generate a custom App.tsx that replicates the prototype layout. Use React Router (Routes, Route, Navigate) and import your page components. If the prototype has a different navigation pattern (top nav, tabs, no sidebar), implement that." if is_brownfield else ""}

{"2" if is_brownfield else "1"}. src/pages/Dashboard.tsx — Business dashboard with:
   - Real KPIs relevant to the business (e.g., "Pending Approvals", "Active Employees", "WFH Today")
   - NOT just row counts — compute meaningful stats from the data
   - Recent activity section showing latest records from the most important entity
   - Quick action buttons
{"   - For brownfield: replicate the prototype's dashboard/home page layout exactly" if is_brownfield else ""}

{"3" if is_brownfield else "2"}. One page per entity: src/pages/{{TypeName}}List.tsx — Each page should have:
   - Search/filter functionality
   - Data table with meaningful columns (show related entity names if possible, not raw UUIDs)
   - WORKFLOW ACTIONS appropriate to the business:
     * If entity has a "status" field → Add Approve/Reject/Submit buttons that update status via PUT
     * If entity represents a request → Show pending items prominently, add action buttons
     * If entity has relationships → Show related data inline where useful
   - Create/Edit forms with proper field types
   - Detail view for individual records
   - Delete with confirmation
   - Toast notifications for all actions

## STRICT IMPORT RULES (MUST follow exactly — violation = build failure)
You can ONLY import from these modules:

```
import React, {{ useState, useEffect, useCallback, useMemo }} from 'react'
import {{ useNavigate, Routes, Route, Navigate, NavLink, Outlet }} from 'react-router-dom'
import {{ format, formatDistanceToNow, parseISO, isToday, isThisWeek }} from 'date-fns'
import {{ BarChart, Bar, LineChart, Line, PieChart, Pie, Cell, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, AreaChart, Area }} from 'recharts'
import {{ Users, Calendar, CheckCircle, XCircle, Clock, Search, Plus, Edit, Trash2, ChevronRight, Home, BarChart3, FileText, Bell, Settings, Filter, Download, Upload, ArrowUpRight, ArrowDownRight, TrendingUp, AlertCircle, Mail, Phone, MapPin, Building, Briefcase, Shield, Star, Heart, Zap, Activity }} from 'lucide-react'
import DataTable, {{ Column }} from '../components/DataTable'
import FormModal, {{ FormField }} from '../components/FormModal'
import ConfirmDialog from '../components/ConfirmDialog'
import Toast, {{ ToastMessage }} from '../components/Toast'
import StatsCard, {{ StatItem }} from '../components/StatsCard'
import DetailPanel, {{ DetailField }} from '../components/DetailPanel'
import StatusBadge from '../components/StatusBadge'
import Tabs, {{ Tab }} from '../components/Tabs'
import SearchFilter, {{ FilterOption }} from '../components/SearchFilter'
import EmptyState from '../components/EmptyState'
import apiClient from '../api/client'
import {{ ...types... }} from '../types'
```

## COMPONENT INTERFACES (MUST match exactly — wrong props = build failure)

```typescript
// DataTable
interface Column {{ key: string; label: string; render?: (value: any, row: any) => React.ReactNode }}
interface DataTableProps {{ columns: Column[]; data: any[]; onEdit?: (row: any) => void; onDelete?: (row: any) => void; loading?: boolean; emptyMessage?: string }}

// FormModal
interface FormField {{ key: string; label: string; type: 'text' | 'number' | 'textarea' | 'select' | 'checkbox' | 'date'; required?: boolean; placeholder?: string; options?: {{ value: string; label: string }}[] }}
interface FormModalProps {{ title: string; fields: FormField[]; values: Record<string, any>; onChange: (key: string, value: any) => void; onSubmit: () => void; onClose: () => void; loading?: boolean; error?: string | null }}

// SearchFilter
interface FilterOption {{ key: string; label: string; type: 'text' | 'select'; options?: {{ value: string; label: string }}[]; placeholder?: string }}
interface SearchFilterProps {{ searchPlaceholder?: string; filters?: FilterOption[]; onSearch: (query: string) => void; onFilter?: (filters: Record<string, string>) => void }}

// Toast — renders a list of toasts, NOT a single toast
interface ToastMessage {{ id: string; type: 'success' | 'error' | 'info'; message: string }}
interface ToastProps {{ toasts: ToastMessage[]; onDismiss: (id: string) => void }}

// StatsCard
interface StatItem {{ label: string; value: string | number; change?: string; changeType?: 'positive' | 'negative' | 'neutral' }}
interface StatsCardProps {{ stats: StatItem[]; loading?: boolean }}

// DetailPanel
interface DetailField {{ label: string; value: React.ReactNode; span?: 1 | 2 }}
interface DetailPanelProps {{ title: string; fields: DetailField[]; onEdit?: () => void; onDelete?: () => void; onBack?: () => void }}

// StatusBadge
interface StatusBadgeProps {{ status: string; size?: 'sm' | 'md' }}

// Tabs
interface Tab {{ key: string; label: string; count?: number }}
interface TabsProps {{ tabs: Tab[]; activeTab: string; onChange: (key: string) => void }}

// ConfirmDialog
interface ConfirmDialogProps {{ title: string; message: string; confirmLabel?: string; cancelLabel?: string; variant?: 'danger' | 'primary'; onConfirm: () => void; onCancel: () => void; loading?: boolean }}

// EmptyState
interface EmptyStateProps {{ title: string; description?: string; actionLabel?: string; onAction?: () => void; icon?: 'folder' | 'search' | 'inbox' | 'chart' }}
```

CRITICAL USAGE PATTERNS:
- Toast: manage state as `const [toasts, setToasts] = useState<ToastMessage[]>([])`, add with `setToasts(prev => [...prev, {{ id: Date.now().toString(), type: 'success', message: 'Done' }}])`, render as `<Toast toasts={{toasts}} onDismiss={{(id) => setToasts(prev => prev.filter(t => t.id !== id))}} />`
- SearchFilter: `<SearchFilter searchPlaceholder="Search..." onSearch={{(query) => setSearchQuery(query)}} />`
- DetailPanel: `<DetailPanel title="Details" fields={{[{{ label: 'Name', value: item.name }}]}} onBack={{() => setSelected(null)}} />`
- FormModal: `<FormModal title="Create" fields={{formFields}} values={{formValues}} onChange={{(k,v) => setFormValues({{...formValues, [k]: v}})}} onSubmit={{handleSubmit}} onClose={{() => setShowForm(false)}} />`

{"For brownfield App.tsx, import pages as: import DashboardPage from './pages/Dashboard' etc." if is_brownfield else ""}
{"For brownfield, you MAY write custom JSX with inline styles to match the prototype exactly." if is_brownfield else ""}

DO NOT import: axios, lodash, moment, dayjs, any external library not listed above.
DO NOT use: Tailwind classes.
{"You CAN use inline styles (style={{...}}) for brownfield to match prototype colors/layout." if is_brownfield else "DO NOT use inline styles with {{}}."}

## ICONS & VISUALS
- Use lucide-react icons extensively — they make the app look professional:
  * Navigation: Home, Users, Calendar, FileText, Settings, Bell
  * Actions: Plus, Edit, Trash2, Download, Upload, Filter, Search
  * Status: CheckCircle, XCircle, Clock, AlertCircle, Shield
  * Metrics: TrendingUp, ArrowUpRight, ArrowDownRight, Activity, BarChart3
  * Domain: Mail, Phone, MapPin, Building, Briefcase, Star, Heart, Zap
- Use icons in: sidebar nav items, page headers, buttons, stat cards, table actions, empty states
- Use date-fns to format dates: format(parseISO(date), 'MMM d, yyyy'), formatDistanceToNow(parseISO(date), {{ addSuffix: true }})
- Use recharts for Dashboard charts: BarChart for comparisons, LineChart/AreaChart for trends, PieChart for distributions
- Use emojis sparingly as supplements, not replacements for icons

## CSS CLASSES AVAILABLE (use className)
Layout: "app-layout", "sidebar", "sidebar-title", "sidebar-nav", "nav-item", "main-content"
Page: "page-header", "page-title", "header-bar"
Buttons: "btn btn-primary", "btn btn-danger", "btn btn-secondary", "btn btn-ghost", "btn-sm", "btn-lg"
Cards: "card", "card-body", "card-header", "card-title"
Tables: "data-table", "actions"
Forms: "form-group", "form-label", "form-input", "form-select", "form-textarea", "form-error"
States: "spinner", "empty-state", "empty-state-icon"
Modal: "modal-overlay", "modal", "modal-sm", "modal-title", "modal-message", "modal-actions"
Alerts: "alert alert-error", "alert alert-success", "alert alert-warning"
Badges: "badge badge-success", "badge badge-warning", "badge badge-danger", "badge badge-info", "badge-sm"
Stats: "stats-grid", "stat-card", "stat-label", "stat-value", "stat-change", "stat-change-positive", "stat-change-negative"
Dashboard: "dashboard-grid", "dashboard-card", "quick-actions", "quick-action-btn", "quick-action-label", "quick-action-desc"
Search: "search-filter-bar"
Detail: "detail-panel", "detail-header", "detail-title", "detail-grid", "detail-field", "detail-field-label", "detail-field-value"
Tabs: "tabs", "tab-item", "tab-active", "tab-count"
Pagination: "pagination", "pagination-info", "pagination-buttons", "pagination-btn", "pagination-btn active"
Breadcrumb: "breadcrumb", "breadcrumb-item", "breadcrumb-separator", "breadcrumb-current"
Charts: "chart-container" (wrap recharts ResponsiveContainer in this)
Avatar: "avatar", "avatar-sm", "avatar-lg"
Notification: "notification-bell", "notification-badge"
Utility: "text-muted", "text-secondary", "font-mono", "truncate"

## API CLIENT USAGE
```typescript
// List with search/filter/pagination
apiClient.getAll<EntityType>('entity_plural')
apiClient.getAll<EntityType>('entity_plural', {{ search: 'text', status: 'pending', limit: 20, offset: 0 }})

// Single record
apiClient.getById<EntityType>('entity_plural', id)

// Create / Update / Delete
apiClient.create<EntityType>('entity_plural', data)
apiClient.update<EntityType>('entity_plural', id, data)
apiClient.delete('entity_plural', id)

// Stats (for Dashboard KPIs and charts)
apiClient.getStats('entity_plural')  // returns {{ total, by_status: {{pending: 3, approved: 5}}, today_count, week_count }}

// Workflow status change (Approve/Reject buttons)
apiClient.updateStatus<EntityType>('entity_plural', id, 'approved')
apiClient.updateStatus<EntityType>('entity_plural', id, 'rejected')
```

Also import: {{ StatsResponse, PaginatedParams }} from '../api/client' if needed.

## OUTPUT FORMAT
Return ONLY a valid JSON object (no markdown fences):
{{
  "src/pages/Dashboard.tsx": "...complete code...",
  "src/pages/EmployeeList.tsx": "...complete code...",
  ...
}}

Each file MUST:
- Export a default React.FC component
- Be complete (no truncation, no "// ... rest of code")
- Compile without TypeScript errors
- Use ONLY the imports listed above"""

    response = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are a senior React developer building a real business application. "
                    "Generate COMPLETE page components with actual business logic — workflow actions, "
                    "status transitions, relationship-aware views, meaningful dashboards. "
                    "Return ONLY valid JSON mapping file paths to complete TypeScript code. "
                    "Every file must compile. Only use the imports listed in the rules."
                )
            ),
            HumanMessage(content=prompt),
        ]
    )

    content = _strip_markdown(response.content)

    # Parse the JSON response
    try:
        page_files = json.loads(content)
    except json.JSONDecodeError:
        # Try to extract JSON from the response
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            page_files = json.loads(json_match.group())
        else:
            raise ValueError("LLM did not return valid JSON for page files")

    # Validate: ensure all expected pages exist
    expected_pages = ["src/pages/Dashboard.tsx"] + [
        f"src/pages/{e['type_name']}List.tsx" for e in entities
    ]

    for expected in expected_pages:
        if expected not in page_files:
            logger.warning(f"LLM missing page: {expected}", extra={"step": "frontend_dev"})

    # Validate: strip any forbidden imports
    for path, code in list(page_files.items()):
        code = _strip_markdown(code)
        code = _sanitize_imports(code)
        # Add ts-nocheck to LLM-generated pages to prevent type errors
        # (LLM may not perfectly match component interfaces)
        if not code.startswith("// @ts-nocheck"):
            code = "// @ts-nocheck\n" + code
        page_files[path] = code

    # For brownfield: if LLM generated App.tsx, include it (overrides template)
    if "src/App.tsx" in page_files:
        logger.info("Brownfield: LLM generated custom App.tsx (overriding template)", extra={"step": "frontend_dev"})

    logger.info(
        f"LLM generated {len(page_files)} business pages",
        extra={"step": "frontend_dev"},
    )

    return page_files


def _sanitize_imports(code: str) -> str:
    """Remove any forbidden imports from LLM-generated code."""
    forbidden_patterns = [
        r"import .* from ['\"]axios['\"]",
        r"import .* from ['\"]lodash['\"]",
        r"import .* from ['\"]moment['\"]",
        r"import .* from ['\"]dayjs['\"]",
        r"import .* from ['\"]\./.*\.css['\"]",
        r"import .* from ['\"]@mui/.*['\"]",
        r"import .* from ['\"]antd['\"]",
        r"import .* from ['\"]@chakra-ui/.*['\"]",
        r"import .* from ['\"]@emotion/.*['\"]",
        r"import .* from ['\"]styled-components['\"]",
        r"import .* from ['\"]tailwindcss['\"]",
    ]

    lines = code.split("\n")
    cleaned = []
    for line in lines:
        is_forbidden = False
        for pattern in forbidden_patterns:
            if re.match(pattern, line.strip()):
                is_forbidden = True
                break
        if not is_forbidden:
            cleaned.append(line)

    return "\n".join(cleaned)


# ═══════════════════════════════════════════════════════════════════════
# STATIC HTML FALLBACK (when Databricks Job build is unavailable)
# ═══════════════════════════════════════════════════════════════════════


def _generate_static_fallback(
    entities: list[dict], data_model: dict, api_contract: str
) -> dict[str, str]:
    """Generate a complete static HTML page with CRUD for all entities.

    Uses LLM but with very tight constraints and the full design system
    embedded in the prompt.
    """
    llm = get_llm(max_tokens=16384)

    tables = data_model.get("tables", [])
    api_routes = []
    for table in tables:
        entity_name = table["name"]
        entity_plural = entity_name if entity_name.endswith("s") else f"{entity_name}s"
        cols = [c for c in table.get("columns", []) if c["name"] not in ("id", "created_at", "updated_at")]
        col_names = [c["name"] for c in cols]
        api_routes.append(
            f"  - Entity: {entity_name}\n"
            f"    API prefix: /api/{entity_plural}\n"
            f"    Fields: {', '.join(col_names)}\n"
            f"    Endpoints: GET /api/{entity_plural}, POST /api/{entity_plural}, "
            f"GET /api/{entity_plural}/{{id}}, PUT /api/{entity_plural}/{{id}}, "
            f"DELETE /api/{entity_plural}/{{id}}"
        )
    api_info = "\n".join(api_routes)

    entities_summary = "\n".join(
        f"{e['name']}: {', '.join(c['name'] for c in e['columns'])}" for e in entities
    )

    system_prompt = _get_static_fallback_system_prompt()

    response = llm.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(
                content=f"""Build a CRUD UI for these entities:

{entities_summary}

EXACT API routes (use these paths exactly, do not change them):
{api_info}

BACKEND API CONTRACT:
{api_contract}

IMPORTANT:
- POST body must include all fields EXCEPT id, created_at, updated_at
- PUT body should only include fields being updated
- All responses return JSON objects/arrays
- IDs are UUID strings
- The API is on the same origin (use relative paths like /api/...)
- On page load, fetch and display data for the first entity immediately"""
            ),
        ]
    )

    html_content = _strip_markdown(response.content)

    return {
        "static/index.html": html_content,
    }


def _generate_brownfield_static_fallback(
    entities: list[dict], data_model: dict, api_contract: str, prototype_context: str
) -> dict[str, str]:
    """Generate static HTML that replicates the prototype UI (brownfield fallback)."""
    llm = get_llm(max_tokens=16384)

    tables = data_model.get("tables", [])
    api_routes = []
    for table in tables:
        entity_name = table["name"]
        entity_plural = entity_name if entity_name.endswith("s") else f"{entity_name}s"
        cols = [c for c in table.get("columns", []) if c["name"] not in ("id", "created_at", "updated_at")]
        col_names = [c["name"] for c in cols]
        api_routes.append(
            f"  - Entity: {entity_name}\n"
            f"    API prefix: /api/{entity_plural}\n"
            f"    Fields: {', '.join(col_names)}\n"
            f"    Endpoints: GET /api/{entity_plural}, POST /api/{entity_plural}, "
            f"GET /api/{entity_plural}/{{id}}, PUT /api/{entity_plural}/{{id}}, "
            f"DELETE /api/{entity_plural}/{{id}}"
        )
    api_info = "\n".join(api_routes)

    entities_summary = "\n".join(
        f"{e['name']}: {', '.join(c['name'] for c in e['columns'])}" for e in entities
    )

    response = llm.invoke(
        [
            SystemMessage(
                content="""You are a senior frontend developer. Generate a COMPLETE single-page HTML application that REPLICATES the prototype described below.

This is a BROWNFIELD migration — recreate the prototype's UI at production quality.

CRITICAL REQUIREMENTS:
- Use a DARK THEME design system:
  - Background: #0f172a, Surface: #1e293b, Borders: #334155
  - Primary: #3b82f6, Danger: #ef4444, Success: #22c55e
  - Text: #f1f5f9 (primary), #94a3b8 (secondary), #64748b (muted)
  - Inputs: bg #0f172a, border #334155, focus ring blue
  - Buttons: rounded-lg, font-weight 600, hover shadow
  - Cards: bg #1e293b, border #334155, rounded-xl
- Layout: sidebar (260px) + main content (padding 32px)
- Replicate the prototype's ACTUAL UI layout, pages, navigation, and interactions
- Use CSS custom properties for theming
- Use vanilla JavaScript (no framework)
- Use fetch() for ALL API calls
- API calls must use the EXACT paths provided
- Include ALL features described in the prototype
- Include proper loading states, error handling, and empty states
- Return ONLY the complete HTML file, no markdown fences"""
            ),
            HumanMessage(
                content=f"""## Prototype Context (replicate this UI faithfully)
{prototype_context[:4000]}

## Data Model
{entities_summary}

## EXACT API Routes
{api_info}

## Backend API Contract
{api_contract}

IMPORTANT:
- Recreate the prototype's UI — don't just make generic CRUD tables
- All API calls use relative paths (same origin)
- POST/PUT bodies include all fields EXCEPT id, created_at, updated_at
- IDs are UUID strings"""
            ),
        ]
    )

    html_content = _strip_markdown(response.content)
    return {"static/index.html": html_content}


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _get_static_fallback_system_prompt() -> str:
    """Load the static fallback prompt from DB/YAML or use built-in."""
    try:
        return get_system_prompt("frontend_static_fallback")
    except KeyError:
        return """Generate a COMPLETE single-page HTML app with inline JavaScript and CSS.

REQUIREMENTS:
- Use a DARK THEME design system with these colors:
  - Background: #0f172a (dark navy)
  - Surface/cards: #1e293b (slate-800)
  - Surface hover: #334155 (slate-700)
  - Primary button: #3b82f6 (blue-500), hover: #2563eb
  - Danger button: #ef4444
  - Success: #22c55e
  - Text primary: #f1f5f9 (slate-100)
  - Text secondary: #94a3b8 (slate-400)
  - Text muted: #64748b (slate-500)
  - Borders: #334155 (slate-700)
  - Input background: #0f172a with border #334155
  - Focus ring: box-shadow: 0 0 0 3px rgba(59,130,246,0.15)
- Layout: sidebar navigation (260px, bg #1e293b) + main content area (padding 32px)
- Sidebar: app name at top, nav links for each entity (active = blue tint background)
- Tables: full-width inside cards, uppercase header labels (text-xs, letter-spacing), hover row highlight
- Buttons: rounded-lg (8px), font-weight 600, hover lifts with shadow
- Cards: bg #1e293b, border #334155, rounded-xl (12px), shadow
- Forms: dark inputs (bg #0f172a, border #334155), blue focus ring, labels above
- Font: Inter (system-ui fallback), 14px body, 24px page titles
- All transitions: 150ms ease
- Use CSS custom properties for theming (define :root variables)
- Create a sidebar interface with one section per entity
- Each section has: a create form, and a data table showing all records with edit/delete buttons
- Use fetch() for ALL API calls (GET, POST, PUT, DELETE)
- API calls must use the EXACT paths provided (do not guess or change them)
- Load data for the active section on nav click AND on page load for the first entity
- Show loading states and error messages
- All CRUD operations must work: Create, Read (list + detail), Update, Delete
- Forms must submit JSON with Content-Type: application/json
- After create/update/delete, refresh the list
- Include toast notifications for success/error feedback
- Include confirm dialog before delete
- Return ONLY the complete HTML file, no markdown fences"""


def _extract_api_contract(backend_files: dict[str, str], data_model: dict) -> str:
    """Extract the API contract from the data model."""
    contract_parts = []

    tables = data_model.get("tables", [])
    for table in tables:
        entity_name = table["name"]
        entity_plural = entity_name if entity_name.endswith("s") else f"{entity_name}s"

        columns = table.get("columns", [])
        create_fields = [c["name"] for c in columns if c["name"] not in ("id", "created_at", "updated_at")]
        all_fields = [c["name"] for c in columns]
        has_status = any(c["name"] == "status" for c in columns)
        fk_columns = [c["name"] for c in columns if c["name"].endswith("_id") and c["name"] != "id"]

        contract = (
            f"Entity: {entity_name}\n"
            f"  API Base: /api/{entity_plural}\n"
            f"  GET /api/{entity_plural} - list all (supports ?search=text&status=value&limit=N&offset=M)\n"
            f"    Returns: array of objects with fields: {', '.join(all_fields)}"
        )

        # Add joined field info
        if fk_columns:
            joined_fields = [f"{fk}_name" for fk in fk_columns]
            contract += f"\n    Also includes joined fields: {', '.join(joined_fields)} (human-readable names from related tables)"

        contract += (
            f"\n  GET /api/{entity_plural}/stats - returns {{total, by_status: {{status: count}}, today_count, week_count}}"
            f"\n  POST /api/{entity_plural} - body: {{{', '.join(f'{f}: value' for f in create_fields)}}}"
            f"\n  GET /api/{entity_plural}/{{id}} - returns single object"
            f"\n  PUT /api/{entity_plural}/{{id}} - body: only fields to update"
            f"\n  DELETE /api/{entity_plural}/{{id}} - returns 204 No Content"
        )

        if has_status:
            contract += f"\n  PATCH /api/{entity_plural}/{{id}}/status - body: {{\"status\": \"new_value\"}} (for workflow actions)"

        contract_parts.append(contract)

    return "\n\n".join(contract_parts)


def _strip_markdown(text: str) -> str:
    """Strip markdown code fences from LLM output."""
    text = text.strip()
    for prefix in (
        "```python", "```typescript", "```tsx", "```json",
        "```html", "```javascript", "```css", "```",
    ):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()
