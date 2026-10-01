# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突和状态机。
- `src/repository.py`：SQLite、事务、乐观版本和审计链。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。
- `src/ledger/`：**调度账**——把接近事件、规避方案、卫星燃料和轨道修订接成一张账：
  - `models.py`：角色/授权范围、风险评定、方案状态机、风险+TCA 排队键。
  - `repository.py`：`ledger_` 前缀的 SQLite 表、事务、乐观版本、燃料流水、独立哈希审计链。
  - `service.py`：排队占用燃料、余量不足拒绝、并发版本校验、轨道修订失效重算、授权代办。
  - `http.py`：挂在 `/api/ledger` 下的路由。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources` 和 `POST /api/items/<id>/actions`。身份使用 `X-User-Id`、`X-Role` 请求头。

### 调度账 API（`/api/ledger`）

| 方法/路径 | 作用 |
| --- | --- |
| `POST /actors`、`POST /satellites` | 注册人员（含角色与授权卫星范围）、卫星（含燃料容量） |
| `POST /conjunctions` | 录入接近事件，按脱靶量/协方差/距交会时间评定风险 |
| `POST /conjunctions/<id>/revisions` | 轨道修订：升版本、级联失效未执行占用、立即重算 |
| `POST /plans` | 提交/变更方案（变更需 `expected_version` 乐观锁） |
| `POST /plans/<id>/approve` | 值班员批准，进入风险+TCA 排队账占用燃料 |
| `POST /plans/<id>/execute` | 执行已排队方案，真实扣减燃料，记录永久保留 |
| `POST /plans/<id>/cancel`、`/reevaluate` | 取消释放占用；失效方案重新挂到最新轨道数据 |
| `GET /book?satellite_id=`、`GET /fuel/<sat>` | 调度账（燃料余量+队列）、燃料流水 |
| `GET /conjunctions`、`GET /conjunctions/<id>` | 事件与修订报告（已执行保留 / 失效待重评清单） |
| `GET /audit`、`GET /audit/verify` | 拒绝也留痕的哈希审计链及完整性校验 |

调度账规则：批准/执行/取消均按**风险等级高者优先、同级交会时刻早者优先**排队占用燃料；
余量不足直接拒绝或挂起并写明未排原因，燃料余额绝不为负。两人并发改同一方案时晚到的
版本过期变更被拒、先到占用保留。轨道数据更新后依赖旧数据的未执行批准与占用立即失效、
释放并重算，已执行指令原样保留。调度员只能在授权卫星范围内代办，越权（角色/范围）直接
拒绝并留 `result=denied` 审计。可运行 `python3 demo_ledger.py` 看完整演示。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道和运营方意见冲突；
`tests/test_ledger.py` 另覆盖调度账的风险/TCA 排队与燃料防负、并发版本冲突、轨道修订失效
重算与已执行保留、授权范围与越权拒绝留痕。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
