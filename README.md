# 证券结算净额批次与托管对账

纯 Python 标准库实现的证券结算后端原型：日终净额轧差、确认冻结、公司行动版本化、托管回执对账与交收拦截。使用 SQLite 持久化，HTTP 接口由 `http.server` 提供。

## 业务规则

1. **日终净额批次**：结算主管把未交收的结算指令按 **账户 + 币种 + 交收日** 归组；买卖在证券腿（数量）与资金腿（金额）两侧分别相抵，得到净收/净付方向与净额。批次号形如 `NB-{账户}-{币种}-{交收日}-{序号}`。
2. **确认即冻结**：确认时把每笔净额与当时的公司行动版本写入冻结快照（`frozen_snapshot`）。确认后指令再增删改、比例再变，已确认批次照旧。
3. **公司行动版本**：比例参数实际变化才产生新版本；一经发布，所有含该证券腿的**未确认（open）批次立即作废（void）并留痕**，下一次日终净额重算。已确认批次继续使用冻结的版本与比例。
4. **并发确认**：确认采用 `state='open' AND version=?` 条件更新，两个主管同时提交只有先到者成功，后者得到 409 冲突。写入遇到瞬时错误时按**原批次号**重试。
5. **托管回执**：
   - 回执可以晚到（交收尝试时未到则拦截，到了再对账）；
   - `receipt_no` 唯一，重复回执只记一次，直接返回首次结果；
   - 引用不存在的批次 → 登记 `orphan_receipt` 差异；
   - 证券腿数量/资金金额/交收日对不上 → 登记 `receipt_mismatch` 差异；
   - 存在未处理差异、或没有对账一致（matched）的回执时，交收被 409 拦截。

## 角色

- `trader`：录入/撤销结算指令
- `corporate_actions`：发布公司行动版本
- `settlement_officer`：净额重算、确认、交收、差异处理
- `custodian`：提交托管回执
- `admin`：全部权限

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：身份、错误类型与输入校验（金额/整数/日期/代码格式）。
- `src/rules.py`：净额轧差、公司行动比例调整、回执对账、角色与状态机（纯函数）。
- `src/repository.py`：SQLite 建表、事务、条件更新与查询。
- `src/service.py`：用例编排、权限、幂等与审计。
- `src/http_api.py`：HTTP 路由与统一错误响应。
- `src/audit.py`：审计时间线（只读）。
- `tests/`：规则计算、端到端流程、并发与失败场景。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

服务启动时自动建表。除 `/health` 和 `/` 外，请求需带 `X-User-Id`、`X-Role`，可选 `X-Org`。

## 主要接口

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /api/instructions` | trader | 录入指令，体：`{"reference":"TRD-1","data":{账户,币种,交收日,证券,side,quantity,price,fees}}` |
| `POST /api/instructions/{id}/cancel` | trader | 撤销未冻结指令，未确认批次同步作废 |
| `GET /api/instructions`、`/{id}`、`/{id}/audit` | 全部 | 查询与审计 |
| `POST /api/corporate-actions` | corporate_actions | 发布公司行动版本（比例变更即作废旧批次） |
| `GET /api/corporate-actions` | 全部 | 版本列表 |
| `POST /api/netting/run` | settlement_officer | 日终净额重算，返回新建 open 批次 |
| `GET /api/batches`、`/api/batches/{batchNo}`、`.../audit` | 全部 | 批次查询（含冻结快照与成员指令） |
| `POST /api/batches/{batchNo}/confirm` | settlement_officer | 确认冻结，体：`{"expected_version":1}` |
| `POST /api/batches/{batchNo}/settle` | settlement_officer | 交收（差异未清或无 matched 回执时 409） |
| `POST /api/receipts` | custodian | 提交回执，体：`{"data":{receipt_no,batch_no,settlement_date,cash_amount,legs[]}}` |
| `GET /api/receipts` | 全部 | 回执列表 |
| `GET /api/discrepancies` | 全部 | 差异列表（可带 `?state=open`） |
| `POST /api/discrepancies/{id}/resolve` | settlement_officer | 处理差异，体：`{"data":{"resolution":"..."}}` |
| `GET /api/stats` | 全部 | 各实体状态计数 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：买卖两腿轧平、按账户/币种/交收日分组、拆股比例调整、确认冻结快照、比例变更作废旧批次而确认批次照旧、并发确认只放行先到者、写入失败按原批次号重试、回执重复幂等/引用不存在/数量金额不符/迟到、差异未清与缺回执拦截交收。
