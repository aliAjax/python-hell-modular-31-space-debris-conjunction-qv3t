# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突、状态机和排队优先级。
- `src/repository.py`：SQLite、事务、乐观版本、卫星燃料台账、方案调度和审计链。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 调度账

服务把接近事件、规避方案、卫星燃料和轨道修订接成一张调度账：

- **排队与占用燃料**：方案按风险等级（high→low）和交会时刻（TCA）排队，批准时占用目标卫星的燃料余量。余量不足时拒绝（`fuel_budget_exceeded`，409）并在 `reject_reason` 中给出未排原因、当前队列和可用余量。
- **并发提交**：批准动作带乐观版本（`expected_version`）。同一方案被两人同时提交时，晚到的变更按版本校验拒绝（`version_conflict`，409），先到的占用保留。
- **轨道修订失效重算**：`report_revision` 提交新轨道数据后，依赖旧数据的未执行方案立即作废（`voided`）、释放燃料，事件回到待评估状态重新排队；已执行指令保留原记录，燃料记为已消耗。
- **越权审计**：所有角色校验在事务内完成，越权变更直接拒绝并追加 `authorization_denied` 审计事件，不推进业务版本。

卫星燃料台账可通过 `POST /api/satellites` 显式登记；创建接近事件时若目标卫星尚无台账，会按事件给定预算自动开立。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/schedule`、`GET /api/satellites`、`POST /api/satellites`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和 `GET /api/items/<id>/audit`。身份使用 `X-User-Id`、`X-Role` 请求头。

动作（`POST /api/items/<id>/actions`，body 含 `action` 与 `expected_version`）：`assess`、`record_opinion`、`approve`、`execute`、`resolve`、`cancel`、`report_revision`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道、运营方意见冲突、燃料跨事件占用与拒绝、同一方案并发提交、轨道修订失效重算、已执行记录保留和越权审计。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
