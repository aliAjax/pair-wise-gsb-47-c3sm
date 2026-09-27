# 化学事故分区接收台

纯Python标准库实现的化学事故伤员分区接收台原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 接收流程

```
registered →(pre_occupy)→ reserved →(start_decon)→ decontaminating →(finish_decon)→ awaiting_review →(review 通过)→ admitted →(discharge)→ closed
```

分支：

- 复核未过：`awaiting_review →(review 未过)→ pending_area`
- 池位故障：`registered/reserved/decontaminating →(report_pool_failure)→ pending_area`
- 重新安排：`pending_area →(reschedule)→ registered`（重新登记池位与时段后再走预占、洗消、复核）
- 取消：`registered/reserved/pending_area →(cancel)→ cancelled`

## 核心约束

- 每批登记染毒等级、人数、床位/呼吸机需求、洗消池与预计时长。
- 同一时段一个池位只接一批：登记和重新安排时做时段重叠检查，冲突即拒绝。
- 红色伤员也必须先洗消：任何分诊等级都不能跳过洗消直接收治。
- 洗消复核通过前床位/呼吸机只是预占（`reserved_*`），复核通过才转正式收治（`admitted_*`）。
- 池位故障或复核未过：批次留在待安排区，必须填写`gap_note`写清缺口，预占资源同步释放。
- 全部经过写入审计事件并持久化，第二天可按批次号查到预占、收治和退回经过。

## 角色

- `reception_officer`：登记批次、预占、重新安排。
- `decon_officer`：开始/完成洗消、上报池位故障。
- `review_officer`：去污复核（通过收治 / 未过退回）。
- `hospital_liaison`：伤员离院出院。
- `incident_commander`：登记批次、取消批次。
- `admin`：全部操作。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、池位时段冲突、预占容量和缺口记录规则。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8323
```

默认端口为`8323`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：批次列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：批次详情。
- `GET /api/records/{id}/audit`：审计时间线，可带`date=YYYY-MM-DD`按天过滤（UTC）。
- `GET /api/batches/{reference}`：批次档案（记录+完整时间线），按批次号查询。
- `GET /api/stats`：状态统计。
- `POST /api/records`：登记批次，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

登记`data`字段：`hospital`、`pool_id`、`triage`（red/yellow/green/black）、`contamination_level`（heavy/moderate/light）、`casualty_count`、`required_beds`、`required_ventilators`、`available_beds`、`available_ventilators`、`slot_start`（`YYYY-MM-DDTHH:MM`）、`estimated_minutes`。

动作`data`要点：

- `review`：`passed`必填；未过时`gap_note`必填（写清缺口）。
- `report_pool_failure`：`gap_note`必填。
- `reschedule`：`slot_start`、`estimated_minutes`必填，`pool_id`可选（默认沿用原池位）。
- `discharge`：`outcome`（treated/transferred/expired）必填。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、红色伤员必须先洗消、池位时段冲突与释放、预占容量缺口、池位故障与复核未过退回待安排区、批次档案次日可查、重复引用、权限拒绝和版本冲突。
