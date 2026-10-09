# 通达信快照采集手册

## 职责

本文件说明**每个交易日如何采集并校验通达信快照** `data/decision/tdx/<交易日>.json`，供盘后决策卡读取。它把「哪个连接器调用产出哪个快照字段」固定成可重复步骤，使采集不依赖临场判断。

边界说明：通达信数据走 MCP 通道，只能在 Agent 侧调用，**不属于 `DEV_WORKFLOW.md` 的本地 shell 作业**；本手册因此单独成立，不写入 `DEV_WORKFLOW.md`。决策卡本身的定位、规则与限制见 [`DECISION_CARD.md`](DECISION_CARD.md)。

前置条件：连接器 `tdx-connector` 已授权并启用；工作目录为仓库根目录。

## 步骤 0：确定交易日

先判断当天是否为交易日。若不是交易日，本次**不采集、不出卡**，直接结束。

```powershell
.\.venv\Scripts\python.exe -c "import os,datetime as dt,akshare as ak; [os.environ.pop(k,None) for k in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy')]; d=dt.date.today().isoformat(); cal=ak.tool_trade_date_hist_sina(); days=set(map(str,cal['trade_date'])); print('today',d,'is_trading_day',d in days,'latest',sorted(x for x in days if x<=d)[-1])"
```

- `is_trading_day True` → `trade_date` 取今天。
- `is_trading_day False` → 不采集，结束；确需补做历史某日时，显式指定 `trade_date`。

## 步骤 1：先跑一次决策卡，拿到采集清单

```powershell
.\.venv\Scripts\python.exe -m stock_review.decision --date <trade_date>
```

这一步先以 akshare 单源出卡，同时写出 `data/decision/<trade_date>.json`。**采集清单从它里面读，不凭印象列**：

| 从快照读 | 用途 |
| --- | --- |
| `inputs.sectors[].name` | 需要取行情的板块名 |
| `inputs.sectors[].leading_stock` | 需要解析代码的领涨股名称 |
| `inputs.limit_up_stocks[]`（`board_count >= 2`） | 需要取涨停逻辑的连板个股 |

## 步骤 2：连接器采集

四个字段各自对应一类调用，缺一不可。

### 2.1 `code_lookup`：名称 → 代码

对步骤 1 得到的**领涨股名称**逐个解析；同时解析要取行情的**板块名**（板块名解析结果用于 2.2）。

| 连接器调用 | 说明 |
| --- | --- |
| `tdx_lookup_stock(query="<名称>")` | 同时支持个股与板块；板块返回形如「880710 通达信概念-种业」「881109 通达信行业-农林牧渔」 |

- `code` 与 `setcode` **原样记录连接器返回值**，不自行推断市场。
- 名称存在多个候选代码时，取首选并写 `note` 说明歧义（例：北交所 `920021` 与新三板 `834021`）。

### 2.2 `board_quotes`：概念/行业板块当日行情

先用 2.1 得到的板块代码取行情：

| 连接器调用 | 说明 |
| --- | --- |
| `tdx_quotes(code="<板块代码>", setcode="1", hasExtInfo="1")` | 返回涨跌幅、上涨/下跌家数、成交额 |

字段映射：

| 快照字段 | 取自 | 说明 |
| --- | --- | --- |
| `change_percent` | 涨跌幅 | 百分比数值 |
| `up_count` / `down_count` | 上涨/下跌家数 | 来自 `hasExtInfo` |
| `amount` | 成交额 | 原始值，不换算 |
| `kind` | 板块名标签 | 「通达信概念-」→ `concept`；「通达信行业-」→ `industry`，**只能取这两个值** |

**概念板块优先。** akshare 概念板块受环境代理影响可能缺失，通达信 `board_quotes` 是概念维度的主要补位来源；应至少覆盖当日主线相关的概念板块。

### 2.3 `stock_themes`：连板个股涨停逻辑

| 连接器调用 | 说明 |
| --- | --- |
| `tdx_api_data(entry="TdxSharePCCW.tdxf10_gg_jyds", fixedTag="ztfx", code="<个股代码>")` | F10 交易大事-涨停分析 |

| 快照字段 | 取自 |
| --- | --- |
| `theme` | 涨停主题标签（多个用 `+` 连接） |
| `reason` | 当日原因揭秘 |
| `first_limit_time` | 首次涨停时间 |
| `seal_amount` | 封板资金 |

### 2.4 `news`：当日市场资讯

| 连接器调用 | 说明 |
| --- | --- |
| `wenda_news_query(query="A股 市场主线 热点 涨停", bdate="<YYYYMMDD>", edate="<YYYYMMDD>")` | 当日复盘/资讯 |

优先取与当日收盘一致的**市场复盘**类资讯，`summary` 保留涨停家数、封板率、连板梯队、量能等可与决策卡数字互相印证的口径。

## 步骤 3：写入快照

写入 `data/decision/tdx/<trade_date>.json`，结构如下（`2026-10-09.json` 为真实样例）：

```json
{
  "trade_date": "2026-10-09",
  "provider": "tdx-connector",
  "generated_at": "2026-10-09T22:38:00+08:00",
  "code_lookup": {
    "捷成股份": {"code": "300182", "setcode": "0"},
    "流金科技": {"code": "920021", "setcode": "2", "note": "北交所；另有新三板代码 834021"}
  },
  "board_quotes": [
    {"code": "880710", "name": "种业", "kind": "concept", "change_percent": 6.89, "up_count": 13, "down_count": null, "amount": 7437987330}
  ],
  "stock_themes": {
    "600825": {"code": "600825", "theme": "出版业+资产重组+国企改革", "reason": "公司拟发行股份购买界面财联社 100% 股权", "first_limit_time": "09:25:00", "seal_amount": 142117.64}
  },
  "news": [
    {"title": "【焦点复盘】…", "source": "财联社", "published_at": "2026-10-09 17:40:05", "summary": "今日 69 股涨停…"}
  ]
}
```

字段规范：

- 数字用 JSON number（`6.89`），**不要写成字符串**（`"6.89"`）；缺值用 `null` 或直接省略。
- `kind` 只能是 `concept` 或 `industry`；其他取值会被解析器跳过。
- 非法条目（缺 `code`/`name`、`kind` 不合法、缺标题）会被解析器**静默跳过**，因此步骤 4 的自校验必做。

## 步骤 4：自校验（必做）

```powershell
.\.venv\Scripts\python.exe -m stock_review.decision.tdx_snapshot --date <trade_date>
```

判读：

| 字段 | 含义 | 处理 |
| --- | --- | --- |
| `ok` | 文件存在、可解析、且**无条目被跳过** | `false` 时必须修好快照再重跑，不得跳过 |
| `counts` | 各字段实际采到的条数 | 与步骤 1 清单核对，明显偏少要补采 |
| `dropped` | 被解析器跳过的非法条目 | 非空即为写错，按提示修字段类型或必填项 |
| `warnings` | 某字段为空（覆盖度提示） | 该字段确实采不到可接受，但须在汇报里说明 |

退出码 `0` 表示 `ok`，`1` 表示不通过。

## 步骤 5：重跑决策卡出终稿

```powershell
.\.venv\Scripts\python.exe -m stock_review.decision --date <trade_date>
```

重跑后核对决策卡：第四节应出现「通达信板块行情」（概念维度补齐）、第五节应出现「涨停逻辑（通达信）」、第六节应有消息面；数据缺口应为 0 条或只剩确实采不到的项。

## 失败与降级

- **连接器不可用或未授权**：跳过步骤 2-4，直接以 akshare 单源出卡；决策卡会写明「本次未接入通达信资讯」，这是合法状态。
- **个别字段采不到**：该字段留空或省略，**不得臆造**；自校验的 `warnings` 会提示该项为空，汇报时如实说明。
- **严禁**为了让快照看起来完整而填入猜测值、默认值或推算值。快照只承载已采到的事实。

## 相关文件

| 文件 | 作用 |
| --- | --- |
| `src/stock_review/decision/tdx_snapshot.py` | 快照读取、解析与自校验入口 |
| `data/decision/tdx/<trade_date>.json` | 每交易日快照，文件名即交易日 |
| `data/decision/<trade_date>.json` | 决策卡输入与结论快照，采集清单来源 |
| `reports/decision/<trade_date>.md` | 决策卡正文 |
