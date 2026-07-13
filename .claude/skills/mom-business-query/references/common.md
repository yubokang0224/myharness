# Common MOM Query Rules

## Base Request

Base URL: `http://47.116.10.40:10050/api`

Required header:

```json
{"Authorization": "Bearer 123"}
```

Use `internal_api_request` with full URLs.

GET example:

```json
{
  "method": "GET",
  "url": "http://47.116.10.40:10050/api/ProjectInfo/GetListPaged",
  "params": {
    "page": "1",
    "limit": "50"
  },
  "headers": {
    "Authorization": "Bearer 123"
  },
  "max_chars": 30000
}
```

POST query example:

```json
{
  "method": "POST",
  "url": "http://47.116.10.40:10050/api/RequirementsPlanning/QueryProcurementRequirementList",
  "headers": {
    "Authorization": "Bearer 123"
  },
  "json": {
    "DeliveryNoteType": 0,
    "ContractNos": ["HSJM20260206-133"],
    "CompanyCode": "C001",
    "EnableFuzzyMatching": false,
    "RecentDays": 30,
    "SimilarityThreshold": 3
  },
  "max_chars": 30000
}
```

## Paging Defaults

- Default: `page=1`, `limit=50`.
- Maximum: `limit=100`.
- Use `GetListPaged`, `GetPageList`, or other paged variants before `GetList`.
- For report datasets, page through results only when the user needs more than one page and the endpoint is clearly read-only.

## Forbidden Actions

Do not call routes whose action starts with or clearly implies mutation, batch operation, synchronization, recalculation, import/export side effects, or notification:

`Insert`, `Add`, `Update`, `Delete`, `Save`, `Submit`, `Audit`, `Approve`, `Reject`, `Sync`, `Synchronous`, `Set`, `Calculate`, `Generate`, `Auto`, `Manual`, `Import`, `Upload`, `Push`, `PushDown`, `Notice`, `Send`, `ExecuteTask`, `Finish`, `Pause`, `Restore`, `Terminate`, `Distribute`, `InventoryIn`, `Match`.

`ExportToExcel` and file download routes are not first-choice report data sources. Use paged JSON queries instead unless the user explicitly needs an export file.

## Output Shape

When reporting results, preserve traceability:

```json
{
  "source": "Controller/Action",
  "filters": {"page": 1, "limit": 50},
  "data_summary": "key facts from returned rows",
  "notable_records": [],
  "warnings": []
}
```

If the API returns an error, include endpoint, status, and parameter summary. Do not invent replacement data.
