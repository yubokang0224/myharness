# Personnel, Manpower, Capacity, And Reporting Queries

Use this reference for project manpower, personnel distribution, task reporting, reporting statistics, and capacity analysis.

Load `common.md` before calling APIs.

## Project Manpower

| Endpoint | Method | Use |
| --- | --- | --- |
| `/ProjectManpowerSummary/GetEntityByProjectIdAndDate` | GET | Project manpower summary by project and date. |
| `/ProjectManpowerSummary/GetEntityByProjectId` | GET | Project manpower summary by project. |
| `/ProjectManpowerSummary/GetEntityByTaskId` | GET | Manpower summary by task. |
| `/ProjectManpowerSummary/GetListPaged` | GET | Manpower summary paged list. |
| `/ProjectManpowerSummary/GetManpowerDistribution` | GET | Manpower distribution summary. Query: `ManpowerDistributionDTO`. |
| `/ProjectManpowerSummary/GetManpowerDistributionDetail` | GET | Manpower distribution detail. Query: `ManpowerDistributionDTO`. |
| `/PersonnelDistribution/GetProjectPersonneDistributionStatisticsList` | POST | Project personnel distribution statistics. Body: `PersonnelDistributionConditionDTO`. |

## Capacity

| Endpoint | Method | Use |
| --- | --- | --- |
| `/ProjectCapacity/GetProjectCapacityChart` | POST | Project capacity chart. Body: `ProjectCapacityConditionDTO`. |
| `/ProjectCapacity/GetDetailListPaged` | GET | Project capacity detail paged list. |
| `/ProjectCapacity/GetProjectList` | GET | Project list for capacity analysis. |
| `/ProjectCapacity/GetListPaged` | GET | Capacity paged list. |
| `/CapacityReportStatistics/GetNeedCapacityCountProj` | GET | Count of projects needing capacity statistics. |
| `/CapacityReportStatistics/GetCapacityStatisticsChart` | GET | Capacity statistics chart. Query: `CapacityStatisticsChartDTO`. |
| `/CapacityReportStatistics/CountCapacityReport` | POST | Count capacity report by date. Body: `CountCapacityReportByDate`. |

## Task Reporting

| Endpoint | Method | Use |
| --- | --- | --- |
| `/TaskReporting/GetPagedList` | GET | Task reporting view paged list. Limit is guarded in controller; keep `limit <= 100`. |
| `/TaskReporting/GetListPaged` | GET | Raw task reporting records. |
| `/TaskReporting/GetEntity` | GET | Task reporting detail by `id`. |
| `/TaskReporting/GetSinglePersonEntity` | GET | Single-person reporting record detail. |
| `/TaskReporting/GetTop50MyPersonalTaskInfo` | GET | Top personal task info by `reportingType`. |
| `/TaskReporting/GetPersonnelDistributionList` | POST | Personnel distribution data from task reporting. Body: `TaskReportingConditionDTO`. |

## Reporting Statistics

| Endpoint | Method | Use |
| --- | --- | --- |
| `/ReportingStatistics/GetListPaged` | GET | Reporting statistics paged list. |
| `/ReportingStatistics/GetReportingStatisticsCharts` | POST | Reporting statistics charts. |
| `/ReportingStatistics/GetReportStatisticalTask` | POST | Report statistical task data. |
| `/TaskReportStatistics/GetManPowerReportForm` | GET | Manpower report form. Query: `ManPowerChartCondition`. |
| `/TaskReportStatistics/GetManpowerChart` | GET | Manpower chart. Query: `ManPowerChartCondition`. |
| `/TaskReportProduceStatistics/GetListPaged` | GET | Task report production statistics paged list. |
| `/TaskReportProduceRelatedPerson/GetListPaged` | GET | Task report production related person paged list. |
| `/TaskReportEvaluate/GetPersonnelAnalysis` | GET | Personnel analysis. Query: `TaskReportEvaluateConditionDTO`. |

## Query Notes

- For a manpower/capacity report, start with project identity from `project.md`, then query manpower summary, distribution, task reporting, and capacity chart.
- Prefer date filters whenever possible.
- Treat charts/statistics as summarized facts and paged lists as drill-down evidence.
