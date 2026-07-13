# Manufacturing And Work Order Queries

Use this reference for manufacturing work orders, work order handling, order handling statistics, machining demand, process, program, drawing, and worker lists.

Load `common.md` before calling APIs.

## Work Orders

| Endpoint | Method | Use |
| --- | --- | --- |
| `/WorkOrders/GetListPaged` | GET | Work order paged list. |
| `/WorkOrders/GetMobileListPaged` | GET | Mobile work order paged view. |
| `/WorkOrders/GetTotalMainCount` | POST | Work order main count. |
| `/WorkOrders/GetList` | GET | Unpaged work order list; use only for small confirmed scopes. |
| `/WorkOrders/Get` | GET | Work order detail by `id`. |
| `/WorkOrders/GetPreProcess` | POST | Previous process lookup. Body: `TaskProcessDTO`; use only when confirming process context. |

Do not call execution routes such as `Excute`, `Pause`, `Restore`, `Terminate`, `Finish`, `InventoryIn`, or callback routes.

## Work Order Handling

| Endpoint | Method | Use |
| --- | --- | --- |
| `/WorkOrderHandler/GetListPaged` | GET | Work order handler paged list. |
| `/WorkOrderHandler/GetListPagedTree` | GET | Work order handler tree paged list. |
| `/WorkOrderHandler/GetListPagedTreeNew` | GET | New work order handler tree paged list. |
| `/WorkOrderHandler/GetTotalMainCount` | POST | Work order handler main count. |
| `/WorkOrderHandler/GetMobileMainGroup` | GET | Mobile main group. |
| `/WorkOrderHandler/GetMobileMainGroupList` | GET | Mobile main group list. |
| `/WorkOrderHandlerDetails/GetListPaged` | GET | Work order handler detail paged list. |
| `/WorkOrderInventoryDetails/GetListPaged` | GET | Work order inventory detail paged list. |
| `/WorkOrderMaterialRecivedDetails/GetListPaged` | GET | Work order received material detail paged list. |
| `/WorkOrderAssignmentDetails/GetListPaged` | GET | Work order assignment detail paged list. |
| `/WorkOrderTransferDetails/GetListPaged` | GET | Work order transfer detail paged list. |

## Order Handling Statistics

| Endpoint | Method | Use |
| --- | --- | --- |
| `/OrderHandlerStatics/GetListPaged` | GET | Order handling statistics paged list. |
| `/OrderHandlerStatics/GetTotalMainCount` | POST | Order handling main count. |
| `/OrderHandlerStatics/GetMobileListPagedGroup` | GET | Mobile grouped order handling list. |
| `/OrderHandlerStatics/GetMobileListPagedGroupbyDemandNo` | GET | Mobile grouped list by demand number. |
| `/OrderHandlerStaticsGroup/GetListPaged` | GET | Order handling statistics group paged list. |
| `/OrderHandlerStaticsGroup0/GetListPaged` | GET | Order handling statistics group0 paged list. |

## Machining Demand And Process

| Endpoint | Method | Use |
| --- | --- | --- |
| `/DemandBasicInfos/GetListPaged` | GET | Machining demand base info. |
| `/DemandMeterialInfos/GetListPaged` | GET | Demand material info. |
| `/DemandPucharseOrders/GetListPaged` | GET | Demand purchase orders. |
| `/DemandPucharseOrderDetails/GetListPaged` | GET | Demand purchase order details. |
| `/DemandFeeDetails/GetListPaged` | GET | Demand fee details. |
| `/DemandStatics/GetListPaged` | GET | Demand statistics. |
| `/DemandStaticsGroup/GetTotalMainCount` | POST | Demand statistics group main count. |
| `/DemandStaticsGroup/GetListPaged` | GET | Demand statistics group paged list. |
| `/Process/GetListPaged` | GET | Process paged list. |
| `/ProcessbyDemand/GetListPaged` | GET | Process by demand paged list. |
| `/ProcessWorkerList/GetListPaged` | GET | Process worker list. |
| `/ProcessOperatorList/GetListPaged` | GET | Process operator list. |
| `/ProgramInfos/GetListPaged` | GET | Program info. |
| `/ProgramDetails/GetListPaged` | GET | Program detail. |
| `/Drawpapers/GetListPaged` | GET | Drawing/paper list. |

## Query Notes

- For production status, start with work orders, then work order handler trees, then demand/process details.
- For demand number questions, use the grouped order handling endpoints and demand purchase/detail endpoints.
- Treat all execution/status-changing/detail-create routes as forbidden even if they are exposed with GET or POST, including `HandlerDetail/{id}` and `TransferDetail/{id}`.
