# 群体伤亡医院应急扩容协调

纯Python标准库实现的群体伤亡医院应急扩容协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、伤员资源需求、医院容量和分流和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/chemical.py`：化学事故分区接收台规则（分区状态、池位时段、容量缺口、洗消复核）。
- `src/chemical_repository.py`：接收台、洗消池、化学批次与审计的SQLite持久化。
- `src/chemical_service.py`：分区接收台用例编排、权限检查与乐观并发。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面（含分区接收台完整流程演示）。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8323
```

默认端口为`8323`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 化学事故分区接收台

批次登记后**只进待安排区**，不占床位、呼吸机和洗消池。红色（重伤）伤员与其他批次一样，必须先洗消、复核通过后才能正式收治。

分区与状态：

| 状态 | 分区 | 资源 |
| --- | --- | --- |
| `pending_arrangement` | 待安排区 | 不占用任何资源；安排失败、池位故障、复核未过都退回这里，缺口写入`gaps` |
| `reserved` | 洗消等候区 | 预占床位/呼吸机，锁定池位时段，不得收治 |
| `decontaminating` | 洗消区 | 正在洗消；红色伤员同样必须进入 |
| `admitted` | 收治区 | 去污复核通过后预占转正式占用，池位释放 |
| `cancelled` | 已取消 | 预占释放 |

接口（角色见`X-Role`：`incident_commander`/`transport_coordinator`/`decon_officer`/`hospital_liaison`/`admin`）：

- `GET /api/chem/stations` / `POST /api/chem/stations`：查询或配置分区接收台容量（`beds_total`、`ventilators_total`；默认分区`MAIN`，50床/12呼吸机）。
- `POST /api/chem/pools`：登记洗消池，请求体`{"data":{"code":"P1","station":"MAIN","name":"1号池"}}`。
- `GET /api/chem/pools?station=&status=`：池位列表。
- `POST /api/chem/pools/{id}/fault`、`POST /api/chem/pools/{id}/repair`：标记故障/修复；故障时该池上所有预占和洗消中批次自动退回待安排区、释放预占并记缺口。
- `POST /api/chem/batches`：批次登记，数据含`contamination_level`(heavy/moderate/light)、`triage`(red/yellow/green/black)、`casualty_count`、`required_beds`、`required_ventilators`。
- `GET /api/chem/batches?state=&station=&date=YYYY-MM-DD&limit=`：批次查询，`date`按登记日期过滤，第二天可继续查询。
- `GET /api/chem/batches/{id}`、`GET /api/chem/batches/{id}/audit`：批次详情与完整经过（预占、收治、退回均有审计）。
- 批次动作（`POST /api/chem/batches/{id}/actions/{action}`，需`expected_version`）：
  - `arrange`（incident_commander）：`{"pool_id":1,"scheduled_at":"ISO8601","estimated_minutes":30}`。同一池位同一时段只接一批（区间重叠即冲突）；池位故障或床位/呼吸机容量不足时不预占，批次留在待安排区，`gaps`写清缺口（原因、池位、床位/呼吸机缺数、时段）。
  - `start_decontamination`（decon_officer）：开始洗消。
  - `review`（decon_officer/hospital_liaison）：`{"decontamination_passed":true,"review_note":"..."}`。通过则正式收治；未过则释放预占、退回待安排区并记缺口，可重新安排。
  - `cancel`（incident_commander）：取消并释放预占。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
