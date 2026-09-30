# 关系查询溯源与反事实解释 API

基于 **Python + FastAPI + SQLite** 的受限关系代数服务。客户端在一次请求中提交**带主键的多张关系表快照**和一份**受限关系代数计划**，服务端执行查询，并为**每一条输出**给出：

- **全部极小来源行见证集**（minimal witness sets）：哪些源行组合足以产生该输出；
- **布尔溯源表达式**（Boolean provenance，DNF）：见证集的析取形式；
- 对指定输出，给出**令该输出消失的全部最小源行删除集**（最小反事实干预），以及在每个删除集上**删除后重算**的完整结果。

计划只能引用已提交的表与字段；重复主键、非法引用、类型不符、聚合空组等均返回**结构化错误**。请求由「规范化计划 + 表快照 + 反事实选择器」指纹与**幂等键**共同绑定，重复请求返回**字节完全一致**的 JSON（SQLite 持久化幂等账本）。

---

## 1. 快速开始（Docker Compose 一键启动）

需要 Docker 20.10+ 与 Docker Compose v2。

```bash
docker compose up --build -d
```

服务监听 `http://localhost:8000`，幂等账本持久化在命名卷 `provenance-data`（容器内 `/data/idempotency.sqlite3`）。

健康检查：

```bash
curl -s http://localhost:8000/health
```

```json
{"status":"ok","service":"关系查询溯源与反事实解释 API"}
```

停止 / 清理：

```bash
docker compose down            # 停止
docker compose down -v         # 停止并删除幂等账本数据卷
```

### 本地（无 Docker）运行

```bash
python3 -m pip install -r requirements.txt
export IDEMPOTENCY_DB=./data/idempotency.sqlite3   # 缺省为 /data/idempotency.sqlite3
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

交互式文档：启动后访问 `http://localhost:8000/docs`（Swagger）与 `/redoc`。

---

## 2. 受限关系代数计划

计划是一棵 JSON 树，节点必须带 `type`，字段一律使用 `别名.列` 的**限定名**：

| 节点 | 形态 | 说明 |
|------|------|------|
| `scan` | `{"type":"scan","table":"orders","alias":"o"}` | 扫描已提交表；`alias` 缺省取表名 |
| `filter` | `{"type":"filter","input":…,"predicate":{…}}` | 受限谓词过滤 |
| `join` | `{"type":"join","left":…,"right":…,"on":[["c.cid","o.cid"]]}` | 等值内连接，`on` 必须非空，两侧类型须一致 |
| `project` | `{"type":"project","input":…,"fields":["o.oid","o.amount"]}` | 列投影（去重，集合语义）。投影分组聚合结果时必须保留**全部** `GROUP BY` 分组键，避免不同分组塌缩成同一输出行 |
| `group` | `{"type":"group","input":…,"by":[…],"aggregates":[…]}` | 分组 + `count`/`sum`/`min`/`max` |

聚合描述：`{"name":"total","func":"sum","field":"o.amount"}`；`count` 的 `field` 可省略（`count(*)`）。`sum/min/max` 只作用于 `integer`/`real` 列。不支持嵌套聚合（`group` 的子树中不能再出现 `group`）。

谓词只允许**列与常量**比较（无子查询、无跨列、无函数），支持：

```text
{"op":"and"|"or","args":[…]}            {"op":"not","arg":{…}}
{"op":"eq|ne|lt|lte|gt|gte","field":"o.amount","value":100}
{"op":"is_null"|"not_null","field":"o.amount"}
```

列类型：`integer` / `real` / `text` / `bool`；`null` 对任意列合法，参与等值连接或比较时按 SQL 风格处理（`null` 永不连接、比较不为真；判空请用 `is_null/not_null`）。

---

## 3. HTTP API

### `POST /query`

请求信封：

```json
{
  "idempotency_key": "客户端生成的唯一幂等键（必填，非空字符串）",
  "tables": [ { "name": ..., "columns": [{"name":...,"type":...}],
                "primary_key": [...], "rows": [{...}] } ],
  "plan": { /* 受限关系代数计划 */ },
  "counterfactual": { "output_index": 0 }   // 可选
}
```

响应（成功）顶层字段：

| 字段 | 含义 |
|------|------|
| `ok` | 恒为 `true` |
| `idempotency_key` / `request_fingerprint` | 回显幂等键与规范化请求内容的 SHA-256 |
| `output.columns` / `output.row_count` / `output.rows` | 输出模式与行 |
| `output.rows[].values` | 该行各列的值 |
| `output.rows[].witness_sets` | **全部极小见证集**，每个见证是源变量列表 |
| `output.rows[].witness_count` | 极小见证集数量 |
| `output.rows[].provenance_expr` | 布尔溯源表达式（DNF：项内 `*`，项间 `+`） |
| `source_variables` | 本次输出引用到的全部来源变量 |
| `counterfactual` | 仅当请求带该字段时出现，见下 |

反事实块：`target` 为被解释输出；`minimal_deletion_set_count` 为最小删除集数量；`scenarios[]` 中每个场景给出 `deleted_source_rows`（一个最小删除集）与 `result`（在删除这些源行后**重算**得到的模式、行数与行）。

`output_index` 指向**排序后**的输出行（输出按全行动态 JSON 升序，保证确定性）。

### 幂等与字节一致性

- 幂等缓存键 = `idempotency_key` + 指纹；指纹 = `SHA256(canonical(plan, tables, counterfactual))`，规范化即递归排序对象键、紧凑分隔符、UTF-8 原样中文。
- 完全相同的请求（含错误请求）重复提交 → 返回首次响应的**原始字节**，响应头带 `X-Idempotent-Replay: true`。
- 同一 `idempotency_key` 绑定**不同**内容 → `409 IDEMPOTENCY_KEY_CONFLICT`，响应中同时返回两次内容的指纹。

### 结构化错误

错误响应统一为 `{"ok":false,"error":{"code","message","details"?}}`：

| HTTP | code | 触发条件 |
|------|------|----------|
| 422 | `INVALID_REQUEST` / `INVALID_PLAN` | 信封或计划结构不合法 |
| 422 | `UNKNOWN_TABLE` / `UNKNOWN_FIELD` | 引用未提交的表 / 不在作用域内的字段 |
| 422 | `TYPE_MISMATCH` | 谓词或聚合的类型不符（如对文本列求和） |
| 422 | `AGGREGATE_EMPTY_GROUP` | 无 `GROUP BY` 的聚合收到 0 行输入 |
| 422 | `TARGET_NOT_FOUND` | 反事实 `output_index` 越界 |
| 422 | `PROVENANCE_TOO_LARGE` | 穷举规模超过上限（见 §6） |
| 409 | `DUPLICATE_PRIMARY_KEY` | 表快照内出现重复主键 |
| 409 | `IDEMPOTENCY_KEY_CONFLICT` | 幂等键绑定了不同内容 |

注意：**带 `GROUP BY` 的空输入返回 0 行（200）**；只有**无 `GROUP BY` 的整体聚合**在空输入时报 `AGGREGATE_EMPTY_GROUP`（对应 SQL 中无 GROUP BY 的聚合恒返回一行，空表时该行为不产出可溯源行）。

---

## 4. 实际请求 / 响应示例

示例文件位于 [`examples/`](examples/)：`spj_query.json`、`aggregate_query.json`。

### 4.1 SPJ 查询 + 反事实

查询：`customers ⋈ orders`（cid 等值连接）→ 过滤 `status='paid'` → 投影，并解释第 0 行（Alice 的订单 oid=10）。

```bash
curl -s -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  --data-binary @examples/spj_query.json
```

真实响应（已格式化；线上实际为键排序、无空白的紧凑 JSON）：

```json
{
  "counterfactual": {
    "minimal_deletion_set_count": 2,
    "scenarios": [
      {
        "deleted_source_rows": ["customers[cid=1]"],
        "result": {
          "columns": ["c.cname", "c.region", "o.oid", "o.amount"],
          "row_count": 1,
          "rows": [["Carol", "north", 13, 300]]
        }
      },
      {
        "deleted_source_rows": ["orders[oid=10]"],
        "result": {
          "columns": ["c.cname", "c.region", "o.oid", "o.amount"],
          "row_count": 2,
          "rows": [["Alice", "north", 11, 200], ["Carol", "north", 13, 300]]
        }
      }
    ],
    "target": {
      "output_index": 0,
      "values": {"c.cname": "Alice", "c.region": "north", "o.oid": 10, "o.amount": 100}
    }
  },
  "idempotency_key": "demo-spj-paid-orders-v1",
  "ok": true,
  "output": {
    "columns": ["c.cname", "c.region", "o.oid", "o.amount"],
    "row_count": 3,
    "rows": [
      {
        "output_index": 0,
        "provenance_expr": "(customers[cid=1] * orders[oid=10])",
        "values": {"c.cname": "Alice", "c.region": "north", "o.amount": 100, "o.oid": 10},
        "witness_count": 1,
        "witness_sets": [["customers[cid=1]", "orders[oid=10]"]]
      }
      // … 另外两行：Alice/oid=11、Carol/oid=13（完整内容见 examples/spj_response.json）
    ]
  },
  "request_fingerprint": "…",
  "source_variables": [ /* customers[cid=1..3]、orders[oid=10..13] … */ ]
}
```

读法：

- 目标行只有一个见证 `customers[cid=1] * orders[oid=10]`（该客户行与该订单行**同时存在**才产生输出）。
- 让它消失的**全部最小删除集**有两个：删除 `orders[oid=10]`（重算剩 2 行），或删除 `customers[cid=1]`（连接级联，Alice 的 oid 10/11 同时消失，重算只剩 Carol 1 行）。
- 对 SPJ 计划，最小删除集恰好是该行全部极小见证集的**极小击中集**。

字节一致重放（第二次提交响应头带 `X-Idempotent-Replay: true`，两次 body 字节相同）：

```bash
curl -s -D - -o /tmp/a.json -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' --data-binary @examples/spj_query.json
curl -s -D - -o /tmp/b.json -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' --data-binary @examples/spj_query.json
cmp /tmp/a.json /tmp/b.json && echo "byte-identical"
```

### 4.2 分组聚合 + 反事实

按地区分组，计算 `count / sum / min / max`，并解释第 0 行（north 区域）。

```bash
curl -s -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  --data-binary @examples/aggregate_query.json
```

north 分组的真实输出（节选）：

```json
{
  "output_index": 0,
  "provenance_expr": "(customers[cid=1] * customers[cid=3] * orders[oid=10] * orders[oid=11] * orders[oid=13])",
  "values": {"c.region": "north", "order_count": 3, "total_amount": 600,
             "min_amount": 100, "max_amount": 300},
  "witness_count": 1,
  "witness_sets": [["customers[cid=1]", "customers[cid=3]",
                    "orders[oid=10]", "orders[oid=11]", "orders[oid=13]"]]
}
```

由于带 `count(*)`，要让 `order_count=3` 保持原值必须**三笔订单配对全部存活**，因此唯一极小见证是该组的全部 5 个来源行。令该聚合输出消失（消失或聚合值改变）的最小删除集共 **5** 个：删除任一北部客户行（级联删除其订单），或删除任一笔北部订单行；每个场景的 `result` 都给出删除后重算的完整两张分组。例如删除 `orders[oid=10]` 后，north 重算为 `count=2, sum=500, min=200, max=300`。完整输出见 [`examples/aggregate_response.json`](examples/aggregate_response.json)。

聚合见证的语义：`count` 要求保留全部配对绑定；`min/max` 只需保留任一取到极值的行；`sum` 取“保留行之和仍等于原总和”的极小子集（当存在 0 贡献或正负抵消时会出现多个极小见证）。多聚合时取各聚合所需绑定的并集再极小化。

### 4.3 结构化错误

```bash
# 非法字段引用 -> 422 UNKNOWN_FIELD
curl -s -X POST http://localhost:8000/query -H 'Content-Type: application/json' -d '{
  "idempotency_key":"demo-err-unknown-field",
  "tables":[{"name":"orders","columns":[{"name":"oid","type":"integer"},{"name":"amount","type":"integer"}],"primary_key":["oid"],"rows":[{"oid":1,"amount":10}]}],
  "plan":{"type":"filter","predicate":{"op":"eq","field":"o.ghost","value":1},
          "input":{"type":"scan","table":"orders","alias":"o"}}}'
# {"ok":false,"error":{"code":"UNKNOWN_FIELD",
#   "message":"字段 'o.ghost' 不存在或不在当前作用域内。",
#   "details":{"field":"o.ghost","scope":"o.amount,o.oid"}}}

# 无 GROUP BY 的聚合收到空输入 -> 422 AGGREGATE_EMPTY_GROUP
curl -s -X POST http://localhost:8000/query -H 'Content-Type: application/json' -d '{
  "idempotency_key":"demo-err-empty-agg",
  "tables":[{"name":"orders","columns":[{"name":"oid","type":"integer"},{"name":"status","type":"text"}],"primary_key":["oid"],"rows":[{"oid":1,"status":"paid"}]}],
  "plan":{"type":"group","by":[],"aggregates":[{"name":"n","func":"count"}],
          "input":{"type":"filter","predicate":{"op":"eq","field":"o.status","value":"X"},
                   "input":{"type":"scan","table":"orders","alias":"o"}}}}'
# {"ok":false,"error":{"code":"AGGREGATE_EMPTY_GROUP",
#   "message":"无 GROUP BY 的聚合节点收到了 0 行输入；SQL 语义下该查询不产出任何分组。",
#   "details":{"aggregates":["n"],"node":"group"}}}
```

重复主键返回 `409 DUPLICATE_PRIMARY_KEY`；同幂等键不同内容返回 `409 IDEMPOTENCY_KEY_CONFLICT`（带两份指纹）。

---

## 5. 溯源与反事实的形式化说明

- **来源变量**：`表名[主键列=值,…]`，如 `orders[oid=10]`；复合主键按声明顺序拼接。自连接中同一物理行在不同别名下仍是同一个变量（删除该行同时影响所有别名）。
- **见证（witness）**：一组来源变量，它们的绑定足以让输出行以原值出现。服务对每条输出枚举**全部极小见证**（极小 = 不含另一见证为其真子集），其析取即布尔溯源 DNF。
- **反事实删除集**：一组来源变量，删除对应物理行使目标输出**不再以原值存在**（行消失，或聚合值改变）。返回的是按集合包含关系极小的**全部**删除集。
  - SPJ：目标存活 ⇔ 至少一个见证绑定存活 ⇒ 最小删除集 = 极小见证族的极小击中集（分支定界精确枚举）。
  - 聚合：在目标分组的全部来源变量上做逐绑定存活判定（与全量重算等价），按删除集大小升序枚举、剪除已知最小解的超集，保证结果**完备且极小**（`sum` 含正负值的非单调情形也成立）；每个删除集再跑一次真实计划得到 `result`。

---

## 6. 规模上限

反事实与聚合见证的穷举是精确算法（最坏情况下界为 #P 难），服务对单次枚举的**不同来源变量数**设上限 `18`、对返回的最小删除集数量设上限 `256`（常量定义于 `app/counterfactual.py` 与 `app/engine.py`）。超限时返回 `422 PROVENANCE_TOO_LARGE` 并在 `details` 中给出实际规模，而不是静默截断或超时。请用更严格的选择/连接条件缩小相关来源行集合。

---

## 7. 项目结构与测试

```text
app/
  canonical.py       规范化 JSON、请求指纹、来源变量编解码
  models.py          Table / Row、表快照校验（类型、主键、重复主键）
  predicates.py      受限谓词的静态校验与求值
  engine.py          计划校验/schema 推导、关系代数执行、极小见证
  counterfactual.py  极小击中集、聚合删除集枚举、删除后重算
  response.py        输出排序、DNF 渲染
  storage.py         SQLite 幂等账本
  main.py            FastAPI 路由与请求/错误装配
examples/            可直接 curl 的请求与真实响应
tests/test_api.py    端到端测试（16 个用例）
```

运行测试：

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest            # 16 passed
```

测试覆盖：SPJ 执行与 DNF、击中集反事实与级联删除、`count/sum/min/max` 聚合溯源、多对多连接计数、空组与空 GROUP BY、重复主键、非法表/字段/类型、目标越界、字节一致重放、幂等键冲突、错误响应幂等。
