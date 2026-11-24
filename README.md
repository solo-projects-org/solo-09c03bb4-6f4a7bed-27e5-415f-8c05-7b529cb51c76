# 售后数仓 · 车型适配拆库服务

把车型适配明细拆成**备件表**与**工况表**两张表存入 SQLite，不留任何逐对
"备件 × 工况"配对明细；车间按车型把两表笛卡尔拼回时，保证不会出现不适用
的组合。FastAPI 提供装载、批量更正与取数接口，Docker Compose 一键启动。

## 核心规则

1. **按车型录入备件号与工况配对**，重复配对只算一条（服务端去重）。
2. 可入库的配对集合必须是完整矩形：`配对数 = 备件种数 × 工况种数`。
   只有两表能**原样拼回**这些配对时才入库；否则拒绝，并指出一对
   "两表会拼出、但拟入库数据中没有"的组合示例，已入库的原数据照常可查。
3. **入库后不留逐对明细**：持久层只有 `vehicle_models`、`model_parts`、
   `model_conditions` 三张表，配对关系由两表笛卡尔积隐式表达。
4. **批量更正**（plus 标"有"、minus 标"无"）按整批更正后的结果核定，
   通过才在同一事务内一起更新两张表；不通过整体回滚，两表保持原样。
5. **核定不遍历两表的所有配对**：核定只做 `COUNT` / `GROUP BY` 聚合；
   更正路径从不 JOIN 两张持久表，只在批次临时表上聚合（数据量 = 本批
   录入条数），复杂度与两表拼回的配对总数无关。反例仅在单个问题备件
   范围内 `LIMIT 1` 取证。
6. 取数一次返回该车型同时生效的两张表。

## 目录结构

```
app/
  main.py        FastAPI 路由与 Swagger 元数据
  service.py     装载 / 批量更正 / 取数业务逻辑（核定算法在此）
  database.py    SQLite 连接与建表
  schemas.py     请求/响应模型
tests/           15 个关键流程自测
Dockerfile
docker-compose.yml
requirements.txt
```

## 用 Docker Compose 启动

```bash
docker compose up --build
```

- API 地址：`http://localhost:8000`
- Swagger UI（在线试用）：`http://localhost:8000/docs`
- OpenAPI 文档：`http://localhost:8000/openapi.json`
- SQLite 数据保存在命名卷 `warehouse-data`（容器内 `/data/warehouse.db`），
  容器重建不丢数据。

停止：`docker compose down`（加 `-v` 会同时删除数据卷）。

## 本地无 Docker 运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
WAREHOUSE_DB=./data/warehouse.db .venv/bin/uvicorn app.main:app --port 8000
```

## Swagger 试用样例

### 1. 装载（`POST /api/v1/models/load`）

合法矩形（2 备件 × 2 工况 = 4 对，含一条重复配对，重复只算一条）：

```json
{
  "modelId": "SEDAN-A1",
  "pairs": [
    {"partNo": "P-1001", "conditionCode": "C-HOT"},
    {"partNo": "P-1001", "conditionCode": "C-COLD"},
    {"partNo": "P-1002", "conditionCode": "C-HOT"},
    {"partNo": "P-1002", "conditionCode": "C-COLD"},
    {"partNo": "P-1002", "conditionCode": "C-COLD"}
  ]
}
```

非矩形（缺 `P-1002 / C-COLD` 这一对）会返回 422，并给出反例：

```json
{
  "modelId": "SEDAN-A2",
  "pairs": [
    {"partNo": "P-1001", "conditionCode": "C-HOT"},
    {"partNo": "P-1001", "conditionCode": "C-COLD"},
    {"partNo": "P-1002", "conditionCode": "C-HOT"}
  ]
}
```

响应：

```json
{
  "detail": {
    "message": "配对集合不是完整矩形，拆成两表后会拼出拟入库数据中没有的组合",
    "extraCombinationExample": {"partNo": "P-1002", "conditionCode": "C-COLD"}
  }
}
```

### 2. 批量更正（`POST /api/v1/models/correct`）

整批：移除备件 P-1002 的两个工况，同时给 P-1001 增加新工况 C-DUST。
更正后为 `P-1001 × {C-HOT, C-COLD, C-DUST}` 完整矩形，整批通过：

```json
{
  "modelId": "SEDAN-A1",
  "plus":  [{"partNo": "P-1001", "conditionCode": "C-DUST"}],
  "minus": [
    {"partNo": "P-1002", "conditionCode": "C-HOT"},
    {"partNo": "P-1002", "conditionCode": "C-COLD"}
  ]
}
```

只删一对（剩余配对不是矩形）会被整批拒绝，两表保持原样：

```json
{
  "modelId": "SEDAN-A1",
  "plus": [],
  "minus": [{"partNo": "P-1001", "conditionCode": "C-HOT"}]
}
```

### 3. 取数（`GET /api/v1/models/SEDAN-A1`）

一次响应同时返回两张生效表：`parts`（备件表）与 `conditions`（工况表），
并附 `effectivePairCount`（两表拼回后的配对数）。

### 4. 车型清单（`GET /api/v1/models`）

## 错误码约定

| 状态码 | 含义 |
| --- | --- |
| 200 | 成功 |
| 404 | 车型不存在（取数/更正） |
| 409 | 车型已入库（重复装载，更正请走 correct 接口） |
| 422 | 入参为空、plus/minus 同批冲突，或核定不通过（含反例，两表不变） |

## 自测

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests/ -v
```

共 15 个用例，覆盖：矩形装载与去重、非矩形拒绝与反例、重复装载 409、
整批更正成功/失败回滚、两表一次取数、无逐对明细表、plus/minus 冲突、
404、200×4 大车型小批次更正的聚合性能、SQL 级"从不两表 JOIN"追踪、
新备件/新工况/幽灵成员等边界。
