# 非遗项目版权到期巡检

面向新学期排期场景的版权合规服务端：集中保存权利主体、许可范围与期限、替代素材，
以及素材 → 教案 → 场次的引用关系；支持按可控时间窗批量巡检生成风险案件、
拦截不合规发布、以版本链承载续期/撤销/临时停用/批量替换，并冻结已完成活动的
当时依据，巡检可安全重跑，每项权利变化都能列清受影响的未来安排。

## 领域模型

- **权利主体 / 素材**：素材归属权利主体，可登记替代素材；公有领域素材可标记 `license_free`。
- **许可版本链**：首登（grant）→ 续期（renewal）/ 撤销（revocation）/ 临时停用（suspension）/ 提前恢复（resumption），
  每版带有序号、前序摘要与自身 SHA-256 摘要，只追加、不可改。
- **教案引用版本链**：创建（create）→ 批量替换（replace），同样为哈希链；
  替换只能选用已登记的替代素材，一条指令可跨多份教案、整批原子生效。
- **场次**：scheduled → published → completed，可 cancel；发布前逐素材、逐时段校验许可覆盖。
- **许可依据快照**：场次完成时冻结每个素材实际命中的许可版本与摘要，事后撤销/到期不追溯。
- **巡检批次 / 风险案件**：案件以（场次, 素材）为自然键，同批次重跑幂等，
  风险未消除前不重复开案，续期/替换/取消/完成后由新批次消解并留痕。

## 关键不变量

1. 许可必须覆盖场次的**完整时段**与所需使用方式（含地域），否则发布被拦截（422）。
2. 续期、撤销、临时停用、恢复、批量替换只能追加版本链；`POST /chains/verify` 可检出任何篡改。
3. 巡检使用显式 `as_of`（知识截止点）与 `horizon`（窗口终点），相同 `batch_id` 重跑返回同一结果。
4. 已完成场次冻结当时依据，后续变化不改变其状态与结论。
5. 影响查询返回变化前后合规对比（`risk_cleared` / `risk_introduced` / 不变）与完整引用路径。

## 目录

- `domain/contract.json`：领域角色、状态、不变量、实体与策略契约。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/copyright_patrol/`：服务端实现
  - `coverage.py`：许可时段/停用区间/范围判定（按版本边界逐段评估，可按 cutoff 回放）
  - `service.py`：领域服务（登记、版本链、发布闸门、批量巡检、影响分析）
  - `repository.py`：只追加存储与 JSON 快照（原子写入）
  - `api.py` / `serve.py`：标准库 HTTP 接口与启动入口
- `tools/check_contract.py`：契约摘要检查。
- `tests/`：契约测试、领域场景测试（20 例）、HTTP 集成测试。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 全部 21 个测试
python3 -m compileall -q src tools tests     # 编译检查
python3 tools/check_contract.py domain/contract.json
```

## 启动

```bash
PYTHONPATH=src python3 -m copyright_patrol.serve --port 8080 --store data/store.json
```

## 接口一览（请求/响应均为 JSON，时间用 ISO-8601）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/holders` | 登记权利主体 |
| POST | `/materials` | 登记素材（可带 `alternative_ids`、`license_free`） |
| POST | `/materials/{id}/alternatives` | 补充替代关系 |
| POST | `/licenses` | 登记许可（首版授权） |
| POST | `/licenses/{id}/renew` | 续期，追加新版本 |
| POST | `/licenses/{id}/revoke` | 撤销（不可重复） |
| POST | `/licenses/{id}/suspend` | 临时停用（区间） |
| POST | `/licenses/{id}/resume` | 提前恢复 |
| GET  | `/licenses/{id}` | 查看完整版本链 |
| GET  | `/licenses/{id}/versions/{seq}/impact` | 该权利变化影响的未来安排 |
| POST | `/plans` | 登记教案及其素材引用与所需使用方式 |
| POST | `/replacements` | 批量替换（整批原子，返回指令号） |
| GET  | `/replacements/{cmd}/impact` | 替换指令的影响与前后合规对比 |
| POST | `/sessions` | 排期场次 |
| POST | `/sessions/{id}/publish` | 发布闸门，不合规返回 422 与逐段违规 |
| POST | `/sessions/{id}/complete` | 完成并冻结许可依据 |
| POST | `/sessions/{id}/cancel` | 取消场次 |
| POST | `/patrols` | 按 `as_of`/`horizon` 批量巡检（`batch_id` 幂等） |
| GET  | `/patrols/{batch_id}` | 查询巡检批次结果 |
| GET  | `/cases` / `/cases/{id}` | 开放风险案件列表 / 案件详情与处置历史 |
| GET  | `/materials/{id}/impact` | 素材关联的全部未来场次 |
| POST | `/chains/verify` | 重算全部哈希链，校验历史完整性 |

### 典型流程

```bash
# 九月内有效的音乐许可
curl -s -X POST localhost:8080/licenses -H 'Content-Type: application/json' -d '{
  "license_id":"L1","material_id":"M1","scope":["演出权","录制权"],
  "valid_from":"2026-09-01T00:00:00+00:00",
  "valid_until":"2026-09-30T00:00:00+00:00"}'

# 十月场次发布 → 422，violations 中给出 license_expired 与违规时段
# 指定时间窗批量巡检 → 生成风险案件；同 batch_id 重跑返回 idempotent_replay
# 续期或批量替换后再次巡检 → 旧案件以 renewed/material_replaced 消解
```
