---
name: mom-business-query
description: Query HSJM MOM/MES business data for project progress, project manpower, task reporting, materials, procurement, inventory, manufacturing work orders, BOM changes, quality inspection, and business report datasets. Use when the user asks to 查询/分析/了解/生成报告 for MOM/MES business data such as 项目, 计划, 人员, 人力, 报工, 物料, 采购, 到货, 库存, 工单, BOM, 设计变更, or 质量. This skill is read-only and must not be used for create/update/delete/sync/import/upload/push/notice actions.
---

# MOM Business Query

Use this skill to query MOM/MES business data through approved read-only HTTP APIs and turn the results into business understanding, analysis, or report inputs.

## Hard Rules

1. Only call query/read APIs listed in this skill or its references.
2. Use `internal_api_request` when it is available. Do not use raw `curl`, direct database access, or custom ad hoc tools.
3. Always send `Authorization: Bearer 123`.
4. Use full URLs under `http://47.116.10.40:10050/api`.
5. Default paging to `page=1`, `limit=50`; never request `limit > 100`.
6. Avoid unpaged `GetList` unless the user explicitly needs a small complete list.
7. Never call routes whose action starts with or clearly implies:
   `Insert`, `Add`, `Update`, `Delete`, `Save`, `Submit`, `Audit`, `Approve`, `Reject`, `Sync`, `Synchronous`, `Set`, `Calculate`, `Generate`, `Auto`, `Manual`, `Import`, `Upload`, `Push`, `PushDown`, `Notice`, `Send`, `ExecuteTask`, `Finish`, `Pause`, `Restore`, `Terminate`, `Distribute`, `InventoryIn`, or `Match`.
8. If a requested report needs data from several domains, query the smallest useful dataset from each source, then summarize by source and filters. Do not invent missing values.

## Quick Workflow

1. Identify the business domain and load only the matching reference:
   - Project progress, plans, tasks: `references/project.md`
   - Personnel, manpower, capacity, task reporting: `references/personnel.md`
   - Materials, procurement, arrival, inventory: `references/material.md`
   - Manufacturing work orders and machining demand: `references/manufacture.md`
   - BOM, design change, quality: `references/bom-quality.md`
2. Load `references/common.md` for request format, defaults, safety checks, and output shape.
3. Choose a semantic capability and map it to the smallest endpoint set.
4. Call `internal_api_request` with full URL, headers, params or JSON body, and a conservative `max_chars`.
5. Return a business answer with data source, filters, key rows/metrics, gaps, and any API errors.

## Semantic Capabilities

| Capability | Use for | References |
| --- | --- | --- |
| `query_project_overview` | Project base info, plan progress, project board, task status | `project.md` |
| `query_project_manpower_capacity` | Project manpower summary, manpower distribution, capacity charts | `personnel.md`, optionally `project.md` |
| `query_task_reporting` | Task reporting records, reporting statistics, work hours, personnel distribution | `personnel.md`, `project.md` |
| `query_material_procurement` | Procurement requirements, purchase details, abnormal materials, order completion | `material.md` |
| `query_inventory_arrival` | Inventory balance, Kingdee inventory, material/plan arrival rate | `material.md` |
| `query_manufacture_workorders` | Work orders, work order handling trees, machining demand, process data | `manufacture.md` |
| `query_bom_quality` | BOM inventory, design change rate, alteration bills, inspection statistics | `bom-quality.md` |
| `build_business_report_dataset` | Multi-domain report data collection | Load each needed domain reference plus `common.md` |

## Response Style

Keep report-facing answers concise and traceable:

- State the queried domain, endpoint names, and filters.
- Separate facts returned by APIs from inferences.
- Mention empty results and API errors plainly.
- Do not expose the bearer token in the answer unless the user explicitly asks about configuration.
