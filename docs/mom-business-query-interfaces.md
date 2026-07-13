# MOM业务查询接口整理

生成时间: 2026-06-24

接口项目: `D:\project\hsmes\HSJM.Factory.MES`

扫描范围: `HSJM.Factory.MES\Controllers`

## 结论

可行。MOM接口项目已经按 ASP.NET Core Controller 暴露业务接口，且大部分 Controller 都有稳定的查询套路: `Get`, `GetListPaged`, `GetList`, `GetFirst`, `GetListByIds`。这些查询接口可以封装到 agent 技能里，用于业务数据了解、异常排查、经营/项目/物料报告生成。

建议先做只读白名单技能，不把所有接口直接暴露给 agent。尤其不要开放 `Insert`, `Update`, `Delete`, `Save`, `Submit`, `Sync`, `Set`, `Calculate`, `Auto`, `Manual`, `Import`, `Upload`, `PushDown`, `Notice` 等可能改数据、重算数据或触发通知的接口。

## 全量扫描概况

| 模块 | Controller数 | 路由数 | 查询候选 | 高价值统计/分析候选 |
| --- | ---: | ---: | ---: | ---: |
| PM 项目/任务/报工 | 53 | 690 | 450 | 218 |
| Material 物料/采购/库存 | 24 | 359 | 202 | 50 |
| Manufacture 生产/工单 | 33 | 395 | 218 | 23 |
| BOM | 14 | 191 | 105 | 15 |
| Basic 基础资料 | 17 | 198 | 116 | 1 |
| SYS 系统资料 | 22 | 274 | 163 | 1 |
| HR 人事考勤 | 13 | 148 | 87 | 2 |
| LOG 日志 | 8 | 81 | 48 | 0 |
| WF 流程 | 6 | 71 | 41 | 0 |
| Invoice 发票 | 4 | 55 | 29 | 2 |
| DC 人员分布 | 2 | 13 | 9 | 1 |
| QC 质量 | 1 | 16 | 9 | 1 |
| WM 仓储领料 | 1 | 13 | 6 | 0 |
| 其他 | 5 | 62 | 35 | 0 |

合计: 206 个 Controller, 2566 个路由, 1519 个查询候选。

## 分类规则

| 优先级 | 封装策略 | 说明 |
| --- | --- | --- |
| P0 | 首批封装 | 支撑报告、分析、看板、统计、异常定位的业务查询接口。 |
| P1 | 第二批封装 | 标准列表/分页/详情接口，作为 P0 数据下钻和补充。 |
| P2 | 暂不直接封装 | Excel导出、文件流、系统/日志/流程辅助接口，必要时再加。 |
| 禁用 | 不封装 | 会新增、修改、删除、同步、重算、下推、发通知、导入上传的接口。 |

标准只读接口模式:

| 模式 | 说明 |
| --- | --- |
| `GET /{Controller}/Get?id=...` | 主键详情。 |
| `GET /{Controller}/GetListPaged?page=1&limit=10&...` | 分页列表，通常带 `{Controller}ConditionDTO` 查询条件。 |
| `GET /{Controller}/GetList?...` | 不分页列表，数据量大时谨慎。 |
| `GET /{Controller}/GetFirst?...` | 按条件取第一条。 |
| `POST /{Controller}/GetListByIds` | 按 ID 批量查询，body 是 `List<long>`。 |
| `GET/POST /{Controller}/ExportToExcel` | 导出接口，先列为 P2，不作为报告数据源首选。 |

## 首批建议封装接口

### 1. 项目进度、计划、看板

| 优先级 | 接口 | 方法 | 用途 |
| --- | --- | --- | --- |
| P0 | `/ProjectPlanLookBoard/GetLookBoardCycleStageList` | POST | 项目计划看板数据，按项目查询阶段/节点/责任单位/计划时间/状态/超期天数。 |
| P0 | `/ProjectPlanLookBoard/GetLookBoardEntityByProjectId` | POST | 项目当天人力汇总，用于项目看板人力卡片。 |
| P0 | `/ProjectPlanLookBoard/GetLookBoardRightTreetList` | POST | 项目装配/物料用量汇总，用于项目看板右侧树。 |
| P0 | `/ProjectPlan/GetCycleStageList` | GET/POST 需复核 | 项目周期阶段列表，适合项目进度报告。 |
| P0 | `/ProjectPlan/GetStagePlanList` | GET | 阶段计划列表。 |
| P1 | `/ProjectPlan/GetListPaged` | GET | 项目计划分页下钻。 |
| P1 | `/ProjectInfo/GetListPaged` | GET | 项目基础信息分页。 |
| P1 | `/ProjectInfo/PostListPaged` | POST | 项目基础信息复杂条件分页。 |
| P1 | `/ProjectInfo/GetListByAuth` | GET | 按权限查询项目列表。 |
| P1 | `/ProjectInfo/GetReportPagedList` | GET | 项目报告分页列表。 |
| P1 | `/ProjectInfo/GetAnyCycleList` | GET | 项目周期相关列表。 |
| P1 | `/ProjectInfo/GetProjectNum` | GET | 项目数量统计。 |

### 2. 项目人力、产能、报工

| 优先级 | 接口 | 方法 | 用途 |
| --- | --- | --- | --- |
| P0 | `/ProjectCapacity/GetProjectCapacityChart` | GET/POST 需复核 | 项目产能图表。 |
| P0 | `/ProjectCapacity/GetDetailListPaged` | GET | 项目产能明细分页。 |
| P0 | `/ProjectCapacity/GetProjectList` | GET | 项目产能项目列表。 |
| P0 | `/CapacityReportStatistics/GetNeedCapacityCountProj` | GET/POST 需复核 | 需要产能统计的项目数量。 |
| P0 | `/CapacityReportStatistics/GetCapacityStatisticsChart` | GET/POST 需复核 | 产能统计图表。 |
| P0 | `/CapacityReportStatistics/CountCapacityReport` | GET/POST 需复核 | 产能报表统计。 |
| P0 | `/ProjectManpowerSummary/GetEntityByProjectIdAndDate` | GET | 按项目和日期取人力汇总。 |
| P0 | `/ProjectManpowerSummary/GetEntityByProjectId` | GET | 按项目取人力汇总。 |
| P0 | `/ProjectManpowerSummary/GetManpowerDistribution` | POST | 人力分布汇总。 |
| P0 | `/ProjectManpowerSummary/GetManpowerDistributionDetail` | POST | 人力分布明细。 |
| P0 | `/TaskReportStatistics/GetManPowerReportForm` | GET/POST 需复核 | 报工人力报表。 |
| P0 | `/TaskReportStatistics/GetManpowerChart` | GET/POST 需复核 | 报工人力图表。 |
| P0 | `/ReportingStatistics/GetReportingStatisticsCharts` | POST | 报工统计图表。 |
| P0 | `/ReportingStatistics/GetReportStatisticalTask` | POST | 报工任务统计。 |
| P0 | `/TaskReporting/GetPagedList` | GET | 报工记录视图分页。 |
| P0 | `/TaskReporting/GetPersonnelDistributionList` | POST | 报工人员分布数据。 |
| P1 | `/TaskReporting/GetListPaged` | GET | 报工原始记录分页。 |
| P1 | `/TaskReporting/GetEntity` | GET | 报工详情。 |

### 3. 任务明细、工时、人员分析

| 优先级 | 接口 | 方法 | 用途 |
| --- | --- | --- | --- |
| P0 | `/TaskDetails/GetTaskCompletionRateDetails` | POST | 任务完成率明细。 |
| P0 | `/TaskDetails/GetPersonnelStatistics` | POST | 人员统计。 |
| P0 | `/TaskDetails/GetProjectManPowerLists` | POST | 项目人力列表。 |
| P0 | `/TaskDetails/GetProjectManPowerChart` | POST | 项目人力图表。 |
| P0 | `/TaskDetails/GetProjectMainHourChart` | POST | 项目主工时图表。 |
| P0 | `/TaskDetails/GetProjectMainHourLists` | POST | 项目主工时列表。 |
| P0 | `/TaskDetails/GetTaskWorkHours` | POST | 任务工时汇总。 |
| P0 | `/TaskDetails/GetTaskWorkHoursDetail` | POST | 任务工时明细。 |
| P1 | `/ProjectTask/GetListPaged` | GET | 项目任务分页。 |
| P1 | `/ProjectTask/GetPageList` | POST | 项目任务复杂条件分页。 |
| P1 | `/TaskMileage/GetListPaged` | GET | 任务里程碑/里程信息分页。 |
| P1 | `/TaskRelatedPerson/GetReportPersonList` | GET/POST 需复核 | 报工相关人员列表。 |

### 4. 物料、采购、库存、到货

| 优先级 | 接口 | 方法 | 用途 |
| --- | --- | --- | --- |
| P0 | `/RequirementsPlanning/QueryProcurementRequirementList` | POST | 采购需求查询，不分页。已有接口文档显示可按合同号/物料编码查询，且标注 `AllowAnonymous`。 |
| P0 | `/RequirementsPlanning/GetPageList` | GET | 采购需求到货率视图分页。 |
| P0 | `/RequirementsPlanning/GetProjectOrderCompletionRateList` | GET | 项目下单完成率。 |
| P0 | `/RequirementsPlanning/GetClaimerOrderCompletionRateList` | GET | 认领人下单完成率。 |
| P0 | `/RequirementsPlanning/GetSpecification` | GET | 按合同号取规格列表。 |
| P0 | `/RequirementsPlanning/ExecuteQuery` | POST | 查询允许入库物料。 |
| P0 | `/InventoryBalance/GetListPaged` | GET | 库存余额分页。 |
| P0 | `/InventoryBalance/GetKingDeeInventory` | POST | 金蝶库存查询。 |
| P0 | `/MaterialArrivalRate/GetArrivalRateStatisticsCharts` | POST | 物料到货率图表。 |
| P0 | `/PlanArrivalRate/GetArrivalRateStatisticsCharts` | POST | 计划到货率图表。 |
| P0 | `/ArrivalRateStatistics/GetArrivalRateStatisticsCharts` | POST | 到货率统计图表。 |
| P0 | `/MaterialCostReductionRate/GetListPaged` | GET | 物料降本率分页。 |
| P0 | `/MaterialHistoricalPriceLedger/GetListPaged` | GET | 物料历史价格台账分页。 |
| P0 | `/RequestSource/GetProjectArrivalRate` | GET/POST 需复核 | 项目到货率。 |
| P0 | `/RequestSource/GetAbnormalMaterialsList` | GET/POST 需复核 | 异常物料列表。 |
| P0 | `/RequestSource/GetListPageByNoPlan` | GET/POST 需复核 | 无计划物料分页。 |
| P0 | `/RequestSource/GetListPageByExcessPlan` | GET/POST 需复核 | 超计划物料分页。 |
| P0 | `/RequestSource/GetListPageByNoReceiptList` | GET/POST 需复核 | 未收货列表。 |
| P0 | `/DistributeRecord/GetProjectCompletionRate` | GET | 项目齐套/完成率相关数据。 |
| P0 | `/DistributeRecord/GetIssuedMaterialPageList` | GET/POST 需复核 | 已发料分页。 |
| P0 | `/DistributeRecord/GetTreetList` | GET/POST 需复核 | 发料树。 |
| P0 | `/DistributeRecord/GetRightTreetList` | GET/POST 需复核 | 物料右侧树/项目物料树。 |
| P1 | `/ProcurementRequestDetail/GetListPaged` | GET | 采购申请明细分页。 |
| P1 | `/AssistRequestDetail/GetListPaged` | GET | 外协申请明细分页。 |
| P1 | `/PurchaseReceiptDetail/GetListPaged` | GET | 采购入库明细分页。 |
| P1 | `/ReceiptDetail/GetListPaged` | GET | 收货明细分页。 |
| P1 | `/PODetail/GetListPaged` | GET | 采购订单明细分页。 |

### 5. 生产、工单、加工需求

| 优先级 | 接口 | 方法 | 用途 |
| --- | --- | --- | --- |
| P0 | `/WorkOrders/GetListPaged` | GET | 工单分页。 |
| P0 | `/WorkOrders/GetTotalMainCount` | POST | 工单主统计数量。 |
| P0 | `/WorkOrders/GetMobileListPaged` | GET/POST 需复核 | 移动端工单分页，可作为综合视图。 |
| P0 | `/WorkOrderHandler/GetListPagedTree` | GET/POST 需复核 | 工单处理树形分页。 |
| P0 | `/WorkOrderHandler/GetListPagedTreeNew` | GET/POST 需复核 | 新版工单处理树形分页。 |
| P0 | `/WorkOrderHandler/GetTotalMainCount` | POST | 工单处理主统计数量。 |
| P0 | `/OrderHandlerStatics/GetTotalMainCount` | POST | 订单处理统计数量。 |
| P0 | `/OrderHandlerStatics/GetMobileListPagedGroup` | GET/POST 需复核 | 订单处理移动端分组分页。 |
| P0 | `/OrderHandlerStatics/GetMobileListPagedGroupbyDemandNo` | GET/POST 需复核 | 按需求号分组的订单处理分页。 |
| P1 | `/DemandBasicInfos/GetListPaged` | GET | 加工需求基础信息分页。 |
| P1 | `/DemandMeterialInfos/GetListPaged` | GET | 加工需求物料信息分页。 |
| P1 | `/DemandPucharseOrders/GetListPaged` | GET | 加工需求采购单分页。 |
| P1 | `/DemandPucharseOrderDetails/GetListPaged` | GET | 加工需求采购明细分页。 |
| P1 | `/Process/GetListPaged` | GET | 工序分页。 |
| P1 | `/ProcessWorkerList/GetListPaged` | GET | 工序人员分页。 |
| P1 | `/ProgramInfos/GetListPaged` | GET | 程序信息分页。 |
| P1 | `/Drawpapers/GetListPaged` | GET | 图纸分页。 |

### 6. BOM、设计变更、质量

| 优先级 | 接口 | 方法 | 用途 |
| --- | --- | --- | --- |
| P0 | `/AlterationBillHead/GetProcessForecast` | GET | 按项目编码获取流程预测/变更相关数据。 |
| P0 | `/DesignChangeRate/GetDesignChangeRateCount` | POST | 设计变更率统计。 |
| P0 | `/DesignChangeRate/RealTimeDesignChangeRate` | POST | 实时设计变更率，是否只读需复核。 |
| P0 | `/MaterialInventory/GetListPaged` | GET | BOM物料库存分页。 |
| P1 | `/AlterationBillHead/GetListPaged` | GET | BOM变更单头分页。 |
| P1 | `/AlterationBillDetail/GetListPaged` | GET | BOM变更单明细分页。 |
| P1 | `/Detail/GetListPaged` | GET | BOM明细分页。 |
| P1 | `/Destuffing/GetListPaged` | GET | 拆套/齐套相关分页。 |
| P0 | `/QualityInspectionRecord/GetStatisticsByMonth` | GET/POST 需复核 | 质量检验月度统计。 |
| P0 | `/QualityInspectionRecord/GetLatestQualityInspectionData` | POST | 获取最新质检数据。 |
| P1 | `/QualityInspectionRecord/GetListPaged` | GET | 质量检验记录分页。 |

### 7. 人员、组织、基础资料

| 优先级 | 接口 | 方法 | 用途 |
| --- | --- | --- | --- |
| P0 | `/PersonnelDistribution/GetProjectPersonneDistributionStatisticsList` | POST | 项目人员分布统计。 |
| P1 | `/ProjectLineBody/GetListPaged` | GET | 项目线体基础资料。 |
| P1 | `/WorkshopInfo/GetListPaged` | GET | 车间基础资料。 |
| P1 | `/MaterialCategory/GetListPaged` | GET | 物料分类。 |
| P1 | `/StandardMaterials/GetListPaged` | GET | 标准物料。 |
| P1 | `/DataDictionary/GetListPaged` | GET | 数据字典。 |
| P2 | `/Employee/GetListPaged`, `/OU/GetListPaged`, `/Position/GetListPaged` | GET | 组织人员资料，作为权限/归属解释的辅助数据。 |

## 已有采购需求接口文档

项目中已有文档: `HSJM.Factory.MES\Controllers\Material\RequirementsPlanning\MOM采购需求查询接口文档.md`。

里面记录的核心接口:

| 项 | 内容 |
| --- | --- |
| 地址 | `47.116.10.40:10050` |
| IIS站点 | `10088-MES-Agent` |
| 路径 | `/api/RequirementsPlanning/QueryProcurementRequirementList` |
| 方法 | POST |
| 鉴权 | 文档标注无需鉴权，Controller 上也有 `AllowAnonymous` |
| 入参 | `DeliveryNoteType`, `ContractNos`, `MaterialCodes`, `CompanyCode`, `EnableFuzzyMatching`, `RecentDays`, `SimilarityThreshold` |
| 用途 | 根据合同号或物料编码查询采购需求，支持精确匹配和相似度匹配。 |

注意: 当前文件读取时中文出现编码问题，后续封装技能时建议重新生成 UTF-8 版接口说明。

## 技能封装建议

建议新建一个只读技能，例如 `mom-business-query`。技能不要让模型自由拼 URL，而是把上面的接口封装成少量语义化能力:

| 技能能力 | 背后接口范围 |
| --- | --- |
| `query_project_overview` | `ProjectInfo`, `ProjectPlan`, `ProjectPlanLookBoard`, `ProjectCycle` |
| `query_project_manpower_capacity` | `ProjectManpowerSummary`, `ProjectCapacity`, `CapacityReportStatistics`, `TaskReportStatistics` |
| `query_task_reporting` | `TaskReporting`, `ReportingStatistics`, `TaskDetails`, `ProjectTask` |
| `query_material_procurement` | `RequirementsPlanning`, `RequestSource`, `DistributeRecord`, `ProcurementRequestDetail`, `PODetail` |
| `query_inventory_arrival` | `InventoryBalance`, `MaterialArrivalRate`, `PlanArrivalRate`, `ArrivalRateStatistics` |
| `query_manufacture_workorders` | `WorkOrders`, `WorkOrderHandler`, `OrderHandlerStatics`, `DemandBasicInfos`, `DemandMeterialInfos` |
| `query_bom_quality` | `AlterationBillHead`, `DesignChangeRate`, `MaterialInventory`, `QualityInspectionRecord` |
| `build_business_report_dataset` | 聚合多接口结果，统一输出给报告生成流程。 |

技能层需要做的保护:

1. 只允许访问白名单中的 GET/查询型 POST。
2. 默认加分页限制，建议 `limit <= 100`，必要时分批翻页。
3. 对 `GetList` 这类不分页接口加确认或内部限制，避免一次拉全量。
4. 明确过滤禁用接口: 新增、修改、删除、同步、重算、下推、通知、导入、上传、导出文件流。
5. 为每个业务能力定义入参字典，例如 `projectCode`, `projectId`, `dateRange`, `materialCode`, `contractNo`, `companyCode`, `departmentId`, `personName`。
6. 统一处理鉴权。除 `RequirementsPlanning/QueryProcurementRequirementList` 外，大多数 Controller 有 `[Authorize]`，需要由 agent 后端注入 token 或走内部代理。
7. 返回结构统一成 `data`, `total`, `source`, `filters`, `warnings`，便于后续分析和报告引用。

## 下一步

1. 复核 P0 接口的方法签名和 DTO 字段，补全每个能力的入参映射。
2. 确认 MOM 服务访问地址、鉴权方式、是否需要通过 agent 后端中转。
3. 建立 `mom-business-query` 技能目录，只放只读白名单和调用规则。
4. 先实现 3 个报告场景验证: 项目进度报告、物料到货/采购风险报告、报工/人力产能报告。
