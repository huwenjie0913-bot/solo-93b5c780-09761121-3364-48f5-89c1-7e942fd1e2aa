# 冷库温度越界归因 API

接收带时间戳的**温度采样、库门开合、压缩机状态、化霜记录**，自动识别温度越界区段，
并结合多源证据为每个区段归因：**开门作业/换货、制冷能力不足、探头异常、化霜升温**，
输出主因、置信等级与证据明细，明确标记**数据不足**与**证据冲突**。
规则可版本化修改，按时间范围重算，保留历史分析版本，支持版本间结论 diff 与 JSON 报告导出。

**库区设备拓扑**：同一站点多个库区共用上报通道时，可建立库区并绑定各自的
探头/库门/压缩机；每次调整生成不可变拓扑版本并记录生效时段，设备归属重叠的绑定请求
返回冲突区间且不写入。分析任务指定库区与拓扑版本后只读取本库区绑定设备的数据，
相邻探头比较限定同区，范围内的未绑定数据仅作为告警列出、不参与打分；
分析版本、区段、diff 与 JSON 报告均回显库区/拓扑版本与被排除数据摘要。

技术栈：Python 3.11 · FastAPI · SQLite（标准库 sqlite3）· Pydantic v2

## 快速开始

```bash
pip install -r requirements.txt
uvicorn coldchain.main:app --reload --port 8000
# 交互文档: http://127.0.0.1:8000/docs
pytest tests/ -q          # 运行测试
```

数据库文件默认为 `./coldchain.db`，可用环境变量 `COLDCHAIN_DB` 覆盖。

## 工作流程

```
上报数据(可乱序/可重复) ──▶ 字段校验 + 入库去重 ──▶ 读取时按 ts 排序整理
        │
        ▼
创建规则集(温度上限/持续时长/采样缺口/各归因窗口) 
        │
        ▼
POST /analysis/runs 按时间范围重算 ──▶ 越界区段识别 ──▶ 事件时间线对齐 ──▶ 逐区段归因
        │
        ▼
版本保留 ──▶ /analysis/diff 结论对比 ──▶ /analysis/runs/{id}/report 导出 JSON
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/ingest/temperatures` | 批量温度采样 `{"samples":[{probe_id, ts, value}]}` |
| POST | `/ingest/door-events` | 库门事件 `{"events":[{door_id, ts, state: open/closed}]}` |
| POST | `/ingest/compressor-status` | 压缩机启停 `{"events":[{compressor_id, ts, state: on/off}]}` |
| POST | `/ingest/defrost` | 化霜记录 `{"records":[{zone_id, start_ts, end_ts}]}` |
| POST | `/rules` / GET `/rules` | 创建 / 查询规则集 |
| POST | `/zones` · GET `/zones` · GET `/zones/{code}` | 创建 / 查询库区 |
| POST | `/zones/{code}/topology-versions` | 调整绑定，生成不可变拓扑版本（重叠返回 409 冲突区间） |
| GET | `/topology/versions` · `/topology/versions/{id}` | 拓扑版本（可按 zone 过滤）与版本快照 |
| POST | `/analysis/runs` | 按规则集+时间范围（+库区/拓扑版本）重算，生成新分析版本 |
| GET | `/analysis/runs` · `/analysis/runs/{id}` · `/analysis/runs/{id}/segments` | 版本与区段结论查询 |
| GET | `/analysis/diff?run_a=&run_b=&tolerance_s=` | 两版本结论差异（回显双方库区/拓扑/排除摘要） |
| GET | `/analysis/runs/{id}/report?download=true` | 导出 JSON 归因报告（含拓扑快照与排除数据告警） |

- 时间戳同时接受 **Unix 秒** 与 **ISO 8601** 字符串；
- 上报逐条校验，合法条目正常入库，非法条目在响应 `errors` 中逐条说明（部分接受）；
- 温度采样按 `(probe_id, ts)` 去重，重复上报幂等。

## 库区设备拓扑

多个库区共用一套上报通道时，先建库区再绑定设备：

```bash
# 1. 建库区
curl -X POST localhost:8000/zones -H 'Content-Type: application/json' \
  -d '{"code":"ZA","name":"一号冷冻库"}'

# 2. 绑定探头/库门/压缩机，生成不可变拓扑版本 v1
curl -X POST localhost:8000/zones/ZA/topology-versions -H 'Content-Type: application/json' -d '{
  "probes": ["P1", "P2"], "doors": ["D1"], "compressors": ["C1"],
  "effective_from": "2026-09-14T00:00:00Z"
}'

# 3. 调整绑定 → 生成 v2，同时自动闭合 v1 的生效时段（快照不变）
curl -X POST localhost:8000/zones/ZA/topology-versions -H 'Content-Type: application/json' \
  -d '{"probes":["P1","P3"],"doors":["D1"],"compressors":["C1"],"note":"更换探头"}'
```

- 每次绑定是**全量快照**：版本一旦生成不可修改，仅新版本生效时闭合旧版本的 `effective_to`；
- 设备在重叠时段只能归属一个库区。与**其它库区有效版本**冲突时返回 `409` 及逐条冲突区间
  （对方版本、冲突开始时间、`conflict_end=null` 表示持续至今），**整笔请求不写入**；
  已闭合且完全早于新生效时间的旧归属允许设备跨区流转；
- 同库区重复绑定同一设备按去重处理；绑定设备全部为空返回 422。

创建分析任务时指定库区与拓扑版本（缺省取该库区最新版本）：

```bash
curl -X POST localhost:8000/analysis/runs -H 'Content-Type: application/json' -d '{
  "rule_set_id": 1,
  "range_start": 1789340000, "range_end": 1789350000,
  "zone": "ZA", "topology_version_id": 1
}'
```

- 温度区段只识别本库区绑定探头；开门/压缩机/化霜证据只来自绑定设备，相邻探头比较限定同区；
- 分析范围内的**未绑定数据**（含属于其它库区的设备）不参与打分，单独落库为
  `excluded_alerts`，响应、详情与报告中的 `excluded_summary` 给出按类型计数；
- 分析版本固化 `zone_id` / `topology_version_id`；不传 `zone` 的旧版任务仍按全量数据复现；
- 旧 SQLite 库启动时自动迁移（`PRAGMA user_version`），历史数据绑定后即可参与区分分析。

## 区段识别规则（RuleConfig）

| 字段 | 默认 | 含义 |
|---|---|---|
| `temp_upper` | -18.0 | 温度上限 ℃，超过即进入越界 |
| `min_duration_s` | 300 | 越界持续时长阈值，短于此不计入区段 |
| `max_gap_s` | 600 | 采样缺口阈值，超过则切断区段并标记 |
| `door_lead_s` | 900 | 开门事件与区段起点的关联窗口 |
| `recovery_window_s` | 1800 | 关门后恢复时间窗口 |
| `slope_window_s` | 600 | 开门后升温斜率计算窗口 |
| `peer_deviation_c` | 2.0 | 相邻探头偏差阈值 ℃ |
| `compressor_duty_high/low` | 0.8 / 0.3 | 压缩机占空高/低阈值 |

## 归因逻辑（证据加权打分）

| 证据 | 支持主因 | 权重 |
|---|---|---|
| 区段与化霜记录重叠 | 化霜升温 | +3.0 |
| 关联窗口内存在开门事件 | 开门作业 | +2.0 |
| 开门后升温斜率 > 0.05 ℃/min | 开门作业 | +1.0 |
| 关门后恢复窗口内回落到上限以下 | 开门作业 | +1.5 |
| 关门后超过恢复窗口仍未恢复 | 制冷能力不足 | +1.0 |
| 压缩机占空 ≥ 高阈值仍压不住 | 制冷能力不足 | +2.0 |
| 压缩机占空 ≤ 低阈值（疑似停机） | 制冷能力不足 | +1.5 |
| 相邻探头均正常且偏差超阈值 | 探头异常 | +3.0 |

- 化霜期间相邻探头偏差不纳入判定（化霜只作用于本机组）；
- **置信等级**：得分 ≥4 且领先 ≥2 且无数据缺口 → `high`；得分 ≥2 → `medium`；否则 `low`；
- **数据不足**（`insufficient_data`）：缺压缩机数据 / 缺相邻探头 / 采样缺口 / 样本过少；
- **证据冲突**（`conflicting_evidence`）：前两名候选主因得分接近（如开门后长期不恢复且压缩机满载）；
- 无任何有效证据时主因为 `unknown`。

## 示例

```bash
# 1. 上报（允许乱序，ts 支持 ISO 字符串）
curl -X POST localhost:8000/ingest/temperatures -H 'Content-Type: application/json' -d '{
  "samples": [
    {"probe_id":"P1","ts":"2026-09-14T00:11:00Z","value":-17.5},
    {"probe_id":"P1","ts":1789343700,"value":-20.0}
  ]}'
curl -X POST localhost:8000/ingest/door-events -d '{"events":[{"door_id":"D1","ts":1789344600,"state":"open"},{"door_id":"D1","ts":1789344900,"state":"closed"}]}' -H 'Content-Type: application/json'

# 2. 规则 + 重算
curl -X POST localhost:8000/rules -d '{"name":"冷冻库","config":{"temp_upper":-18.0}}' -H 'Content-Type: application/json'
curl -X POST localhost:8000/analysis/runs -d '{"rule_set_id":1,"range_start":1789340000,"range_end":1789350000}' -H 'Content-Type: application/json'

# 3. 结论 / 版本对比 / 报告
curl localhost:8000/analysis/runs/1/segments
curl "localhost:8000/analysis/diff?run_a=1&run_b=2"
curl -OJ "localhost:8000/analysis/runs/2/report?download=true"
```

## 目录结构

```
coldchain/
├── main.py         # FastAPI 路由
├── schemas.py      # 字段校验与规则配置（Pydantic）
├── db.py           # SQLite 建表、迁移与连接
├── topology.py     # 库区、不可变拓扑版本与设备绑定（冲突区间检测）
├── detection.py    # 越界区段识别（上限/持续时长/采样缺口）
├── timeline.py     # 事件时间线对齐（开门配对/占空比/斜率/恢复时间）
├── attribution.py  # 证据打分归因引擎
└── analysis.py     # 分析版本、库区隔离、排除告警、diff、报告导出
tests/test_api.py            # 10 个端到端场景测试
tests/test_topology_api.py   # 8 个库区拓扑/迁移/复现场景测试
```
