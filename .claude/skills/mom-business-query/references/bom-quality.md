# BOM, Design Change, And Quality Queries

Use this reference for BOM inventory, BOM alteration bills, design change rate, destuffing, and quality inspection.

Load `common.md` before calling APIs.

## BOM And Alteration

| Endpoint | Method | Use |
| --- | --- | --- |
| `/AlterationBillHead/GetProcessForecast` | GET | Process forecast by `projectCode`. |
| `/AlterationBillHead/GetListPaged` | GET | BOM alteration bill head paged list. |
| `/AlterationBillHead/Get` | GET | Alteration bill head detail by `id`. |
| `/AlterationBillDetail/GetListPaged` | GET | BOM alteration bill detail paged list. |
| `/Detail/GetListPaged` | GET | BOM detail paged list. |
| `/MaterialInventory/GetListPaged` | GET | BOM material inventory paged list. |
| `/AllocationRecords/GetListPaged` | GET | BOM allocation records. |
| `/NumberOfAudits/GetListPaged` | GET | BOM audit count/list. |
| `/Destuffing/GetListPaged` | GET | Destuffing/kitting related list. |
| `/DestuffingReleaseHistory/GetListPaged` | GET | Destuffing release history. |

## Design Change Rate

| Endpoint | Method | Use |
| --- | --- | --- |
| `/DesignChangeRate/GetListPaged` | GET | Design change rate paged list. |
| `/DesignChangeRate/GetList` | GET | Design change rate unpaged list; use only for small scopes. |
| `/DesignChangeRate/GetDesignChangeRateCount` | POST | Design change rate count. Body: `DesignChangeRateCountDto`. |

## Quality

| Endpoint | Method | Use |
| --- | --- | --- |
| `/QualityInspectionRecord/GetListPaged` | GET | Quality inspection records. |
| `/QualityInspectionRecord/GetLatestQualityInspectionData` | POST | Latest quality inspection data. Body: `List<AchieveLatestDataDTO>`. |
| `/QualityInspectionRecord/GetStatisticsByMonth` | GET | Monthly quality inspection statistics. Query: `QualityInspectionStatisticsConditionDTO`. |
| `/QualityInspectionRecord/Get` | GET | Quality inspection detail by `id`. |

## Query Notes

- For project quality or design-change reports, combine project identity from `project.md`, BOM alteration, design change count, and quality monthly statistics.
- Do not call `RealTimeDesignChangeRate`; it performs real-time calculation and is outside the read-only whitelist.
- Do not call quality update/import routes.
