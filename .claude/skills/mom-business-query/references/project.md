# Project Queries

Use this reference for project overview, project progress, project plan, project board, project tasks, project milestones, and project report context.

Load `common.md` before calling APIs.

## Recommended Capabilities

### `query_project_overview`

Use these endpoints to understand a project and its current state.

| Endpoint | Method | Use |
| --- | --- | --- |
| `/ProjectInfo/GetListPaged` | GET | Project base information, paged. |
| `/ProjectInfo/PostListPaged` | POST | Project base information with complex filters. |
| `/ProjectInfo/GetListByAuth` | GET | Project list filtered by accessible scope. |
| `/ProjectInfo/GetReportPagedList` | GET | Project report-oriented list. |
| `/ProjectInfo/GetAnyCycleList` | GET | Project cycle related list. |
| `/ProjectInfo/GetProjectNum` | GET | Project count summary. |
| `/ProjectInfo/Get` | GET | Project detail by `id`. |

### Project Plan And Board

| Endpoint | Method | Use |
| --- | --- | --- |
| `/ProjectPlanLookBoard/GetLookBoardCycleStageList` | POST | Project board plan data: stage/node, department, dates, status, overdue days. Body: `ProjectPlanConditionDTO`; requires `ProjectID`. |
| `/ProjectPlanLookBoard/GetLookBoardEntityByProjectId` | POST | Today's project manpower summary for board cards. Body: `ProjectManpowerSummaryConditionDTO`; requires `ProjectID`. |
| `/ProjectPlanLookBoard/GetLookBoardRightTreetList` | POST | Project assembly/material usage summary. Body: `ProjectLineBodyConditionDTO`; requires `ProjectCode`. |
| `/ProjectPlan/GetCycleStageList` | GET | Cycle/stage list. Query: `ProjectPlanConditionDTO`. |
| `/ProjectPlan/GetStagePlanList` | GET | Stage plan list. Query: `ProjectPlanConditionDTO`. |
| `/ProjectPlan/GetListPaged` | GET | Project plan paged detail. |
| `/ProjectPlan/GetListView` | GET | Project plan view list. |

### Tasks And Milestones

| Endpoint | Method | Use |
| --- | --- | --- |
| `/ProjectTask/GetListPaged` | GET | Project task paged list. |
| `/ProjectTask/GetPageList` | POST | Project task complex query. Body: `ProjectTaskConditionDTO`. |
| `/ProjectTask/GetPageListByCharge` | POST | Tasks by charge/responsible scope. Body: `ProjectTaskConditionDTO`. |
| `/ProjectTask/GetEntity` | GET | Task detail by `id`. |
| `/TaskMileage/GetListPaged` | GET | Task milestone/mileage paged list. |
| `/TaskRelatedPerson/GetReportPersonList` | GET | Reporting related person list. Query: `TaskRelatedPersonConditionDTO`. |

### Task Detail Analysis

| Endpoint | Method | Use |
| --- | --- | --- |
| `/TaskDetails/GetTaskCompletionRateDetails` | POST | Task completion rate details. Body: `TaskDetailsConditionDTO`. |
| `/TaskDetails/GetPersonnelStatistics` | POST | Personnel statistics for installation/task details. Body: `InstallationDetailConditionDTO`. |
| `/TaskDetails/GetProjectManPowerLists` | GET | Project manpower list. Query: `ProjectManpowerDetailConditionDTO`, plus `page`, `limit`. |
| `/TaskDetails/GetProjectManPowerChart` | POST | Project manpower chart. Body: `ProjectManpowerDetailConditionDTO`. |
| `/TaskDetails/GetProjectMainHourChart` | POST | Project main-hour chart. Body: `TaskDetailsConditionDTO`. |
| `/TaskDetails/GetProjectMainHourLists` | POST | Project main-hour list. Body: `TaskDetailsConditionDTO`. |
| `/TaskDetails/GetTaskWorkHours` | POST | Task work-hour summary. Body: `TaskDetailsConditionDTO`. |
| `/TaskDetails/GetTaskWorkHoursDetail` | POST | Task work-hour detail. Body: `TaskDetailsConditionDTO`. |

## Query Notes

- Prefer `ProjectCode` for business-facing project lookup and `ProjectID` for board/detail APIs.
- If only project name is known, start with `/ProjectInfo/GetListPaged` and a narrow keyword/name filter if supported by `ProjectInfoConditionDTO`.
- For project progress reports, combine project info, plan board, task detail analysis, manpower/capacity from `personnel.md`, and material risk from `material.md`.
