# 跨来源事件对齐 API（FastAPI + SQLite）

客户端提交**两组**带时间戳、来源键和载荷摘要的事件（A 侧 / B 侧），服务会：

1. 在容差窗口 `tolerance_seconds` 内为 `|Δt| <= 容差` 的事件对建立**候选边**；
2. 求出一组**一对一**匹配，使得匹配数最多（最大基数），并在这些方案中让
   **总绝对时间差最小**；
3. 当存在多个等优方案时，按**事件 ID 稳定决胜**（输入相同则输出永远相同，
   ID 更小的事件在等费用可互换时优先配对）；
4. **未匹配事件全部保留**，与匹配结果一起返回；
5. 每个匹配对附带逐对的**差值解释**（带符号差 `B - A`、绝对差、谁早谁晚）。
6. 每次任务（原始事件 + 匹配结果）持久化到 SQLite，可随时按 ID 取回。

算法分两阶段（实现见 [`app/matching.py`](app/matching.py)）：

1. 把问题建成 **|A| 行 ×（|B| + |A|）列矩形指派**：真实 B 为普通列，
   每行额外有一个私有"空置列"表示该 A 未匹配；候选格费用为整数微秒级
   `|Δt|`，非候选格/空置格用分层惩罚常数，保证最优解先最大化匹配数、再
   最小化总时间差。用矩形匈牙利（O(|A|²·(|A|+|B|))）求解，并取对偶变量
   得到所有"紧边"（reduced cost = 0，即可能出现在某个最优解里的边）。
2. 只在紧边子图上，按 A 的 ID 顺序用残量增广路把每个事件换成它能取得的
   最小列（真实 B 按 ID 升序，未匹配列排最后），由此取**字典序最小**的
   最优方案——这就是平局时的稳定决胜。

所有费用以**整数微秒**表示（`datetime` 的内部分辨率），避免浮点相等性
误差；每侧上限 250 个事件，250×250 约 0.3 秒返回。

## 一键启动

需要 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

服务监听 **端口 `8000`**，数据保存在 Docker 卷 `alignment-data`（容器内
`/data/alignment.db`），重启不丢数据。

停止并清理：

```bash
docker compose down            # 停止
docker compose down -v         # 停止并删除数据卷
```

- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

## 本地（无 Docker）运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ALIGN_DB_PATH=./alignment.db uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
python -m pytest -q
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET`  | `/health` | 健康检查 |
| `POST` | `/alignments` | 提交两组事件并对齐（结果落库，返回 201） |
| `GET`  | `/alignments` | 历史任务列表 |
| `GET`  | `/alignments/{job_id}` | 取回某次任务的完整结果 |

### 请求体

```json
{
  "events_a": [
    {"id": "a1", "timestamp": "2026-09-22T10:00:00Z", "source_key": "sensor-a", "payload_summary": "door open"}
  ],
  "events_b": [
    {"id": "b1", "timestamp": "2026-09-22T10:00:02Z", "source_key": "camera-b", "payload_summary": "motion"}
  ],
  "tolerance_seconds": 5
}
```

说明：

- 时间戳支持任意 RFC 3339 / ISO 8601 格式；**强烈建议带时区**（如 `Z` 或
  `+08:00`）。不带时区的朴素时间戳按 UTC 解释。
- 每一侧的 `id` 必须在该侧唯一；每侧最多 250 个事件。
- `tolerance_seconds >= 0`；边界值（`|Δt| == 容差`）也算候选边。

### 响应字段

- `pairs[]`：匹配对，含双方事件、`delta_seconds`（带符号，B − A）、
  `absolute_delta_seconds`（绝对差）和 `explanation`（人类可读解释）。
- `unmatched_a` / `unmatched_b`：两侧未匹配事件完整清单（保留事件全部字段）。
- `candidate_edges`：容差窗口内建立的候选边数量。
- `total_absolute_delta_seconds`：所选匹配的总绝对时间差（被最小化的目标值）。
- `counts`：各类计数。

## 示例请求

```bash
curl -s -X POST http://localhost:8000/alignments \
  -H 'Content-Type: application/json' \
  -d @examples/request.json | python3 -m json.tool
```

或手写：

```bash
curl -s -X POST http://localhost:8000/alignments \
  -H 'Content-Type: application/json' \
  -d '{
    "events_a": [
      {"id": "a1", "timestamp": "2026-09-22T10:00:00Z", "source_key": "sensor-a", "payload_summary": "door open"},
      {"id": "a2", "timestamp": "2026-09-22T10:00:10Z", "source_key": "sensor-a", "payload_summary": "door close"},
      {"id": "a3", "timestamp": "2026-09-22T10:05:00Z", "source_key": "sensor-a", "payload_summary": "lonely event"}
    ],
    "events_b": [
      {"id": "b1", "timestamp": "2026-09-22T10:00:02Z", "source_key": "camera-b", "payload_summary": "motion near door"},
      {"id": "b2", "timestamp": "2026-09-22T10:00:09Z", "source_key": "camera-b", "payload_summary": "motion ends"}
    ],
    "tolerance_seconds": 5
  }' | python3 -m json.tool
```

预期：`a1↔b1`（Δ=2s）、`a2↔b2`（Δ=−1s），`a3` 进入 `unmatched_a`
（`b` 侧数量少且 `a3` 远离任何候选边）。

取回历史结果：

```bash
curl -s http://localhost:8000/alignments | python3 -m json.tool
curl -s http://localhost:8000/alignments/<job_id> | python3 -m json.tool
```

## 匹配语义示例（平局决胜）

设容差为 10s：

```
A: a1 @ 00:00:00, a2 @ 00:00:05
B: b1 @ 00:00:05, b2 @ 00:00:10
```

可行的两对方案总时间差都为 10s（`a1-b1/a2-b2` 与 `a1-b2/a2-b1` 均为 10s）。
服务确定性地选择 `a1↔b1`、`a2↔b2`——最大基数前提下总差最小，平局由
事件 ID 稳定决胜。

## 项目结构

```
app/
  __init__.py
  main.py        # FastAPI 路由与请求校验
  matching.py    # 核心：候选边 + 最小费用最大流一对一匹配
  schemas.py     # Pydantic 请求/响应模型
  db.py          # SQLite 建表、落库与结果重建
tests/
  test_matching.py
examples/
  request.json
Dockerfile
docker-compose.yml
requirements.txt
```
