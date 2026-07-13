# Material, Procurement, Inventory, And Arrival Queries

Use this reference for procurement requirements, purchase details, material risks, inventory, arrival rates, issued materials, and project material completion.

Load `common.md` before calling APIs.

## Procurement Requirements

| Endpoint | Method | Use |
| --- | --- | --- |
| `/RequirementsPlanning/QueryProcurementRequirementList` | POST | Query procurement requirements without paging. Body includes `DeliveryNoteType`, `ContractNos` or `MaterialCodes`, `CompanyCode`, fuzzy matching options. |
| `/RequirementsPlanning/GetPageList` | GET | Procurement requirement arrival-rate view, paged. |
| `/RequirementsPlanning/GetListPaged` | GET | Procurement requirements paged raw data. |
| `/RequirementsPlanning/GetProjectOrderCompletionRateList` | GET | Project order completion rate. Query: `OrderCompletionRateConditionDTO`. |
| `/RequirementsPlanning/GetClaimerOrderCompletionRateList` | GET | Claimer order completion rate. Query: `ClaimerOrderCompletionRateView`. |
| `/RequirementsPlanning/GetSpecification` | GET | Specification list by `contractNo`. |
| `/RequirementsPlanning/ExecuteQuery` | POST | Query materials allowed for stock-in. Body: `ExecuteQueryDTO`. |

Procurement requirement body example:

```json
{
  "DeliveryNoteType": 0,
  "ContractNos": ["HSJM20260206-133"],
  "CompanyCode": "C001",
  "EnableFuzzyMatching": false,
  "RecentDays": 30,
  "SimilarityThreshold": 3
}
```

Use `DeliveryNoteType=0` with `ContractNos` for non-self-made items. Use `DeliveryNoteType=1` with `MaterialCodes` for self-made items.

## Inventory And Arrival Rate

| Endpoint | Method | Use |
| --- | --- | --- |
| `/InventoryBalance/GetListPaged` | GET | Inventory balance paged list. |
| `/InventoryBalance/GetKingDeeInventory` | POST | Kingdee inventory query. |
| `/MaterialArrivalRate/GetListPaged` | GET | Material arrival rate paged list. |
| `/MaterialArrivalRate/GetArrivalRateStatisticsCharts` | POST | Material arrival rate chart. Body: `MaterialArrivalRateConditionDTO`. |
| `/PlanArrivalRate/GetListPaged` | GET | Plan arrival rate paged list. |
| `/PlanArrivalRate/GetArrivalRateStatisticsCharts` | POST | Plan arrival rate chart. Body: `PlanArrivalRateConditionDTO`. |
| `/ArrivalRateStatistics/GetListPaged` | GET | Arrival rate statistics paged list. |
| `/ArrivalRateStatistics/GetArrivalRateStatisticsCharts` | POST | Arrival rate statistics chart. Body: `ArrivalRateStatisticsConditionDTO`. |
| `/MaterialCostReductionRate/GetListPaged` | GET | Material cost reduction rate paged list. |
| `/MaterialHistoricalPriceLedger/GetListPaged` | GET | Historical material price ledger paged list. |

## Request Source And Abnormal Materials

| Endpoint | Method | Use |
| --- | --- | --- |
| `/RequestSource/GetTreeList` | GET | Requirement source tree. |
| `/RequestSource/GetTreeListOverDelivery` | GET | Over-delivery source tree. |
| `/RequestSource/GetListPaged` | GET | Requirement source paged list. |
| `/RequestSource/GetListPage` | GET | Requirement source business paged view. |
| `/RequestSource/GetListPageByParentId` | GET | Requirement source by parent. |
| `/RequestSource/GetListPageByExcessReceipt` | GET | Excess receipt list. |
| `/RequestSource/GetListPageByNoPlan` | GET | No-plan material list. |
| `/RequestSource/GetListPageByExcessPlan` | GET | Excess-plan material list. |
| `/RequestSource/GetListPageByNoReceiptList` | GET | No-receipt list. |
| `/RequestSource/GetAbnormalMaterialsList` | GET | Abnormal materials list. |
| `/RequestSource/GetProjectArrivalRate` | GET | Project arrival rate. |
| `/RequestSource/GetLeftTreetList` | GET | Left tree list for material analysis. |

## Issued Materials And Purchase Details

| Endpoint | Method | Use |
| --- | --- | --- |
| `/DistributeRecord/GetListPaged` | GET | Distribution/issued material records. |
| `/DistributeRecord/GetMaterial` | GET | Material info from distribution records. |
| `/DistributeRecord/GetTaskInfo` | GET | Task info for distribution. |
| `/DistributeRecord/GetIssuedMaterialPageList` | GET | Issued material paged list. |
| `/DistributeRecord/GetTreetList` | POST | Distribution tree. Body: `ProjectLineBodyConditionDTO`. |
| `/DistributeRecord/GetRightTreetList` | POST | Right tree/project material tree. Body: `ProjectLineBodyConditionDTO`. |
| `/DistributeRecord/GetMaterialDetailView` | GET | Material detail view. |
| `/DistributeRecord/GetProjectCompletionRate` | GET | Project material completion rate. |
| `/ProcurementRequestDetail/GetListPaged` | GET | Procurement request detail paged list. |
| `/AssistRequestDetail/GetListPaged` | GET | Assist/outsource request detail paged list. |
| `/PurchaseReceiptDetail/GetListPaged` | GET | Purchase receipt detail paged list. |
| `/ReceiptDetail/GetListPaged` | GET | Receipt detail paged list. |
| `/PODetail/GetListPaged` | GET | Purchase order detail paged list. |
| `/UrgencyBills/GetListPaged` | GET | Urgency bill paged list. |
| `/StockInLog/GetListPaged` | GET | Stock-in log paged list. |

## Query Notes

- For material risk reports, combine procurement requirements, abnormal materials, no-plan/no-receipt lists, arrival rate charts, and inventory.
- Use contract number for customer/project-linked procurement questions and material code for item-level questions.
- Avoid `SetArrivalRateStatistics`, `CalculateArrivalRateStatistics`, `PlanArrivalRateStatistics`, `AutoMaterialPushDown`, `ManualMaterialPushDown`, and notice routes.
