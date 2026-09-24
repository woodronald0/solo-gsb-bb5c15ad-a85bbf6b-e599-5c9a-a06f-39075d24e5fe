# 冷链周转箱事件接收 API

基于 **Python 3.11 + FastAPI + SQLAlchemy 2.0 + SQLite** 的设备温度事件接收服务。
设备会**重试**或**延迟/乱序**上报，服务按以下规则处理：

- **相同箱号 + 相同序号 + 相同内容**（温度、事件时间）→ 判定为设备重试的重复上报，
  幂等返回已有记录（HTTP `200`，`status=duplicate`），**不改变任何状态**。
- **相同箱号 + 相同序号但内容不同** → 拒绝（HTTP `409 Conflict`）。
- **乱序/延迟事件**（序号先到 3 后到 1、中间跳号等）→ 正常入库；
  查询“最新状态”时始终取每个箱子**序号最大**的一条，与到达顺序无关。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── config.py          # 环境变量配置
│   ├── database.py        # 引擎/会话，启动建表
│   ├── models.py          # BoxEvent 模型，(box_code, seq) 唯一约束
│   ├── schemas.py         # 请求/响应模型与时间归一化
│   ├── services.py        # 幂等去重、冲突判定、最新状态查询、保序回归时钟校正
│   └── main.py            # FastAPI 路由
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh   # 容器启动：初始化建表 -> 启动服务
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

启动过程会自动完成：

1. 基于 `requirements.txt` 在镜像内安装全部依赖；
2. 容器入口脚本 `docker-entrypoint.sh` 创建数据目录并执行建表初始化（幂等）；
3. 启动 uvicorn。

数据持久化在命名卷 `coldchain-data`（容器内 `/app/data`），重启/重建容器不丢数据。

常用命令：

```bash
docker compose logs -f        # 查看日志
docker compose down           # 停止
docker compose down -v        # 停止并删除数据卷（清空数据）
```

## 本地直接运行（不用 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

首次请求前/启动时会自动创建 SQLite 表（`./data/coldchain.db`）。

## 配置

通过环境变量配置（均有默认值）：

| 环境变量       | 默认值                          | 说明                                       |
| -------------- | ------------------------------- | ------------------------------------------ |
| `DATABASE_URL` | `sqlite:///./data/coldchain.db` | 数据库连接串（SQLAlchemy 格式）            |
| `APP_HOST`     | `0.0.0.0`                       | 监听地址                                   |
| `APP_PORT`     | `8000`                          | 监听端口（Compose 左侧映射端口同样取它）   |

Compose 下可用 `.env` 或临时变量覆盖，例如改宿主端口：

```bash
APP_PORT=9000 docker compose up -d --build
# 此时宿主访问端口为 9000
```

## API 地址与端口

- 服务地址：`http://localhost:8000`（按上面的端口配置调整）
- Swagger 交互文档：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>

## 接口一览

| 方法 | 路径                          | 说明                                   |
| ---- | ----------------------------- | -------------------------------------- |
| GET  | `/health`                     | 健康检查                               |
| POST | `/events`                     | 上报一个事件（幂等，支持设备重试）     |
| GET  | `/states`                     | 所有箱子的最新状态（按最大序号）       |
| GET  | `/boxes`                      | 所有上报过的箱号                       |
| GET  | `/boxes/{box_code}/state`     | 单个箱子的最新状态                     |
| GET  | `/boxes/{box_code}/events`    | 单个箱子的全部事件（按序号升序）       |
| GET  | `/boxes/{box_code}/clock-audit` | 区间事件的设备时钟回拨校正建议（保序回归，不改原始记录） |
| GET  | `/boxes/{box_code}/heat-exposure` | 区间内温度超过上限的精确暴露量积分（摄氏度·秒，保序校正时间，不改原始记录） |

### 上报事件

`POST /events`

请求体：

```json
{
  "box_code": "BOX-001",
  "seq": 0,
  "temperature": -4.5,
  "event_time": "2026-09-23T10:00:00+08:00"
}
```

字段说明：

- `box_code`：箱号，非空字符串（≤64 字符）。
- `seq`：该箱递增的事件序号，非负整数；不要求连续，乱序/补报均可。
- `temperature`：摄氏温度，范围 `-100 ~ 100`。
- `event_time`：ISO 8601 时间；**建议带时区**。服务统一换算为 UTC 存储与比较，
  不带时区的时间按 UTC 处理。

响应：

- 首次入库：`201 Created`，`status = "accepted"`
- 重复上报（同序号且温度、事件时间完全一致）：`200 OK`，`status = "duplicate"`
- 同序号内容不一致：`409 Conflict`，响应体带库中已有值与本次值，便于排查
- 参数非法：`422 Unprocessable Entity`
- 查询不存在的箱子：`404 Not Found`

## 使用示例

```bash
# 上报 seq=1
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-001","seq":1,"temperature":-4.5,
       "event_time":"2026-09-23T10:01:00+08:00"}'

# 设备重试：原样重发 -> 200 duplicate，状态不变
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-001","seq":1,"temperature":-4.5,
       "event_time":"2026-09-23T10:01:00+08:00"}'

# 同序号改温度 -> 409 拒绝
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-001","seq":1,"temperature":9.9,
       "event_time":"2026-09-23T10:01:00+08:00"}'

# 乱序补报 seq=0 / seq=2 ... 最新状态始终取最大 seq
curl http://localhost:8000/boxes/BOX-001/state
curl http://localhost:8000/states
```

## 时钟回拨校正（clock-audit）

设备 RTC 发生回拨时，后发生的事件其 `event_time` 反而更早，物理时间序列会出现“倒退”。
`GET /boxes/{box_code}/clock-audit?seq_from=&seq_to=` 对**指定序号区间（含端点）**
即时生成校正建议，**不会改动任何原始记录**；乱序补报后再次查询会基于最新数据重新计算。

算法：按 `seq` 升序取出区间内事件，将 UTC 事件时间换算为**整数微秒**，做
**等权保序回归**（isotonic regression，PAVA）——在建议时间非递减的约束下，
最小化各事件时间偏移的平方和；实现方式即把相邻的违规块不断合并、块内取算术均值，
均值四舍五入到微秒。例如连续两个时间分别为第 10 秒、第 0 秒时，两条都会建议为
第 5 秒（前面的往后拨、后面的往前调），而不是只把后者改成 10 秒。

查询参数：

- `seq_from` / `seq_to`：序号区间，**均为整数且含端点**，必须 `>= 0` 且 `seq_from <= seq_to`。

返回：数组，每个元素对应一条区间内事件（按 seq 升序）；区间内无事件返回 `[]`。

| 字段             | 说明                                                       |
| ---------------- | ---------------------------------------------------------- |
| `seq`            | 事件序号                                                   |
| `original_time`  | 原始 UTC 事件时间（库中记录，未被修改）                    |
| `suggested_time` | 保序回归后的建议 UTC 时间                                  |
| `offset_micros`  | 建议时间 − 原时间，单位微秒（正=向前修正，负=回拨修正）    |
| `block_seq_from` / `block_seq_to` | 该事件所属合并块的序号范围（同一块内建议时间相同） |

错误情况：箱号不存在返回 `404`；缺少参数、参数非整数、负数或 `seq_from > seq_to`
返回 `422`。

```bash
# 先造一组带时钟回拨的事件（UTC）：seq=0 在第 10 秒，seq=1 倒退到第 0 秒
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-AUDIT","seq":0,"temperature":-4.5,
       "event_time":"2026-09-23T02:00:10+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-AUDIT","seq":1,"temperature":-4.6,
       "event_time":"2026-09-23T02:00:00+00:00"}'
# 再补一条正常推进的 seq=2
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-AUDIT","seq":2,"temperature":-4.7,
       "event_time":"2026-09-23T02:00:20+00:00"}'

# 查询 0..2 的校正建议
curl 'http://localhost:8000/boxes/BOX-AUDIT/clock-audit?seq_from=0&seq_to=2'
```

响应（seq=0、seq=1 合并为一个块，都建议为 02:00:05；seq=2 独立成块）：

```json
[
  {
    "seq": 0,
    "original_time": "2026-09-23T02:00:10",
    "suggested_time": "2026-09-23T02:00:05",
    "offset_micros": -5000000,
    "block_seq_from": 0,
    "block_seq_to": 1
  },
  {
    "seq": 1,
    "original_time": "2026-09-23T02:00:00",
    "suggested_time": "2026-09-23T02:00:05",
    "offset_micros": 5000000,
    "block_seq_from": 0,
    "block_seq_to": 1
  },
  {
    "seq": 2,
    "original_time": "2026-09-23T02:00:20",
    "suggested_time": "2026-09-23T02:00:20",
    "offset_micros": 0,
    "block_seq_from": 2,
    "block_seq_to": 2
  }
]
```

```bash
# 箱号存在但区间内无事件 -> 200，空数组
curl 'http://localhost:8000/boxes/BOX-AUDIT/clock-audit?seq_from=100&seq_to=200'
# []

# 箱号不存在 -> 404
curl -i 'http://localhost:8000/boxes/NO-SUCH-BOX/clock-audit?seq_from=0&seq_to=1'

# 范围非法（seq_from > seq_to）-> 422
curl -i 'http://localhost:8000/boxes/BOX-AUDIT/clock-audit?seq_from=5&seq_to=2'
```

## 高温暴露积分（heat-exposure）

`GET /boxes/{box_code}/heat-exposure` 计算指定序号区间内，温度**超过 `upper_limit`**
部分对时间的精确积分（单位：**摄氏度·秒，°C·s**）。与 clock-audit 一样在查询时
即时计算，**不改动任何原始记录**；补报后再次查询会基于最新数据重新计算。

计算规则：

1. 按 `seq` 升序取出区间内（含端点）实际存在的事件，**缺号不自动补点**；
2. 事件时间沿用 clock-audit 的**等权保序回归**（PAVA）校正为非递减序列；
3. 相邻事件之间温度视为**线性变化**，对超过 `upper_limit` 的部分做精确积分
   （内部用有理数运算，杜绝浮点误差）——两端都超限时取梯形面积，仅一端超限时
   按线性过零点切出三角形面积；
4. 校正后间隔**为零**或**超过 `max_gap_seconds`** 的段不计入积分（明细中
   `counted=false` 并给出 `excluded_reason`），其余段逐段返回并累加总计。

查询参数（均必填）：

| 参数              | 约束                         | 说明                                   |
| ----------------- | ---------------------------- | -------------------------------------- |
| `seq_from`        | 非负整数，`<= seq_to`        | 起始序号（含端点）                     |
| `seq_to`          | 非负整数                     | 结束序号（含端点）                     |
| `upper_limit`     | 有限数值                     | 温度上限（°C），仅积分超出部分         |
| `max_gap_seconds` | **正数**、有限数值           | 相邻事件校正后间隔超过该秒数则不计入   |

返回字段：

| 字段                              | 说明                                                       |
| --------------------------------- | ---------------------------------------------------------- |
| `box_code` / `seq_from` / `seq_to` | 回显查询条件                                              |
| `upper_limit` / `max_gap_seconds` | 回显参数                                                   |
| `event_count`                     | 区间内实际取到的事件条数（缺号不补）                       |
| `total_degree_seconds`            | 所有计入段的积分总和（摄氏度·秒）                          |
| `segments[]`                      | 逐段明细（相邻事件两两成段，见下表）；区间无事件时为 `[]`  |

`segments[]` 每段字段：

| 字段                   | 说明                                                                 |
| ---------------------- | -------------------------------------------------------------------- |
| `seq_from` / `seq_to`  | 该段两端事件序号（缺号时二者不相邻）                                 |
| `corrected_time_from` / `corrected_time_to` | 保序校正后的两端 UTC 时间（不改动原始记录）          |
| `interval_seconds`     | 校正后的段时长（秒），保序回归保证其非负                             |
| `temperature_from` / `temperature_to` | 两端温度（°C）                                              |
| `excess_degree_seconds` | 该段超限部分的积分（未计入段为 0）                                  |
| `counted`              | 是否计入总计                                                         |
| `excluded_reason`      | `zero_interval`（校正后间隔为零）/ `gap_too_large`（超过最大间隔）/ `null` |

错误情况：箱号不存在返回 `404`；箱号存在但区间内无事件返回 `200` 零值报告
（`total_degree_seconds=0`、`segments=[]`）；缺少参数、参数非数值、
`max_gap_seconds <= 0`、`NaN/Infinity` 或 `seq_from > seq_to` 返回 `422`。

```bash
# 一组事件（UTC）：seq=0 为 4°C，seq=1 升到 8°C，seq=2 回落到 5°C，每分钟一条
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-HEAT","seq":0,"temperature":4.0,
       "event_time":"2026-09-23T02:00:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-HEAT","seq":1,"temperature":8.0,
       "event_time":"2026-09-23T02:01:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-HEAT","seq":2,"temperature":5.0,
       "event_time":"2026-09-23T02:02:00+00:00"}'

# 上限 5°C、最大允许间隔 120 秒
curl 'http://localhost:8000/boxes/BOX-HEAT/heat-exposure?seq_from=0&seq_to=2&upper_limit=5&max_gap_seconds=120'
```

响应（seq 0→1：从 4°C 线性升至 8°C，第 15 秒越过 5°C，随后 45 秒超限高度
由 0 线性升到 3°C，三角形面积 3×45/2=67.5；seq 1→2：从 8°C 线性回落至 5°C，
三角形面积 3×60/2=90；总计 157.5 摄氏度·秒）：

```json
{
  "box_code": "BOX-HEAT",
  "seq_from": 0,
  "seq_to": 2,
  "upper_limit": 5.0,
  "max_gap_seconds": 120.0,
  "event_count": 3,
  "total_degree_seconds": 157.5,
  "segments": [
    {
      "seq_from": 0,
      "seq_to": 1,
      "corrected_time_from": "2026-09-23T02:00:00",
      "corrected_time_to": "2026-09-23T02:01:00",
      "interval_seconds": 60.0,
      "temperature_from": 4.0,
      "temperature_to": 8.0,
      "excess_degree_seconds": 67.5,
      "counted": true,
      "excluded_reason": null
    },
    {
      "seq_from": 1,
      "seq_to": 2,
      "corrected_time_from": "2026-09-23T02:01:00",
      "corrected_time_to": "2026-09-23T02:02:00",
      "interval_seconds": 60.0,
      "temperature_from": 8.0,
      "temperature_to": 5.0,
      "excess_degree_seconds": 90.0,
      "counted": true,
      "excluded_reason": null
    }
  ]
}
```

```bash
# 箱号存在但区间无事件 -> 200 零值报告
curl 'http://localhost:8000/boxes/BOX-HEAT/heat-exposure?seq_from=100&seq_to=200&upper_limit=5&max_gap_seconds=120'
# {"box_code":"BOX-HEAT", ..., "event_count":0, "total_degree_seconds":0.0, "segments":[]}

# 间隔超过 max_gap_seconds（或缺号导致的长间隔）-> 该段 counted=false，不计入总计
# max_gap_seconds 不是正数 -> 422
curl -i 'http://localhost:8000/boxes/BOX-HEAT/heat-exposure?seq_from=0&seq_to=2&upper_limit=5&max_gap_seconds=0'

# 箱号不存在 -> 404
curl -i 'http://localhost:8000/boxes/NO-SUCH-BOX/heat-exposure?seq_from=0&seq_to=1&upper_limit=5&max_gap_seconds=120'
```

## 设计说明

- **去重键**：数据库对 `(box_code, seq)` 建唯一约束（`uq_box_seq`）。
  应用层先查后插，且捕获并发下的 `IntegrityError` 重新判定，
  因此设备并发重试也不会产生重复行或错误覆盖。
- **内容相等**：比较 `temperature` 与已归一化为 UTC 的 `event_time`。
  重试通常原样重发 JSON，浮点解析结果一致，可直接判等。
- **乱序安全**：最新状态通过“每箱 `MAX(seq)` 关联回原行”的子查询得到，
  不依赖插入先后，也不要求序号连续。
- **时钟校正只读**：`clock-audit` 在查询时即时计算，不写库、不改动原始事件；
  乱序补报后重查即自动重算。时间先精确换算为整数微秒（整数运算，无浮点误差），
  再用 PAVA 做等权保序回归：相邻块出现“前块均值 > 后块均值”时向前合并、
  块内取算术均值并四舍五入到微秒，天然得到非递减的建议序列，
  且最小化各事件偏移的平方和。
- **暴露积分只读且精确**：`heat-exposure` 复用同一套保序校正时间，
  同样查询时即时计算、不写库、不改动原始事件，补报后重查即重算。
  相邻事件间温度按线性处理，仅对超过 `upper_limit` 的部分积分；
  段时长由校正后的整数微秒时间确定，温度与上限以十进制有理数（`Fraction`）
  参与运算，分段积分结果精确，最后输出时才转为浮点。校正后间隔为零或超过
  `max_gap_seconds` 的段只在明细中标记、不计入总计；缺号不补点。
- **持久化**：默认 SQLite + WAL，卷挂载于 `/app/data`；
  如需 PostgreSQL/MySQL，只需将 `DATABASE_URL` 换成对应 SQLAlchemy 连接串
  （并安装相应驱动），建表逻辑通用。
