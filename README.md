# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：指令状态转换、交收完整性和公司行动调整、冲突检查。
- `src/netting.py`：净额批次、公司行动版本与托管回执规则。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 净额批次

- `POST /api/batches`：日终净额，按账户/币种/交收日归集未交收指令，券款两侧买卖相抵，请求体为`{"batch_no":"...","account":"...","currency":"CNY","settlement_day":2}`。批次号为幂等键，写入失败后可按原批次号重试；同组已存在活动批次则冲突。
- `GET /api/batches`：批次列表，可带`state`参数。
- `GET /api/batches/{id}`：批次详情（含逐笔净额快照）。
- `GET /api/batches/{id}/audit`：批次事件时间线。
- `POST /api/batches/{id}/actions/confirm`：确认批次并冻结净额与公司行动版本；两人同时提交只放行先到者，后到者收到409。
- `POST /api/batches/{id}/actions/resolve_discrepancy`：解除差异封锁，请求体`data.reason`为处理意见。

### 公司行动

- `POST /api/corporate-actions`：登记或调整公司行动比例，请求体为`{"instrument":"...","action_type":"split","ratio":2.0}`。比例变化时全局版本+1，未确认批次失效并按原分组重算（批次号派生`/R{n}`），已确认批次照旧。
- `GET /api/corporate-actions`：公司行动主数据与当前全局版本。

### 托管回执与差异

- `POST /api/receipts`：登记托管回执，请求体为`{"receipt_no":"...","batch_no":"...","delivered_quantity":600,"cash_paid":7520}`。回执号幂等去重，重复只记一次；引用不存在批次记为未匹配差异；金额不符留差异并把批次挡在`blocked`；相符则批次交收并联动成员指令置为已交收；迟到/失效/未确认批次的回执只记录不应用。
- `GET /api/receipts`：回执列表，可带`batch_no`参数。
- `GET /api/discrepancies`：差异列表，可带`state`参数。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。批次与确认操作角色为`settlement_officer`，公司行动维护为`corporate_actions`，回执登记为`custodian`或`settlement_officer`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、净额归集、并发确认、批次号幂等重试、公司行动失效重算、回执去重/迟到/不匹配、重复引用、权限拒绝和版本冲突。
