# 盘后决策卡

## 职责

本文件说明盘后决策卡的定位、入口、规则来源和已知限制。它是独立于 ReviewRun 正式对象链的轻量入口，只回答四个问题：**当前市场情绪、明天能不能买、买什么、买多少**。

## 与 ReviewRun 正式链的关系

| 维度 | ReviewRun 正式链 | 盘后决策卡 |
| --- | --- | --- |
| 目标 | 可追溯、不可变的正式 publication | 当日可用的决策结论 |
| 对象 | RunManifest/EvidenceBundle/RevisionBundle/ActionSet | 无，单次运行直接产出 Markdown |
| 确认 | 两次完整对象确认 + 哈希链 | 一次运行，无需确认 |
| 当前状态 | 生产出全缺口报告，尚未真实验收 | 已跑通真实数据 |

决策卡**不写 `data/review_run.sqlite`、不创建 publication、不进入 `reports/daily/`**，因此它不会与正式链产生同名产物冲突，也不能被表述为正式复盘。

> 定位已确认（2026-10-09）：决策卡作为**独立决策入口**保留，不并入正式对象链。它与 `PROJECT_RULES.md` 红线 2 的关系是：决策卡不被表述为"平行正式流程"——其产物不进 `reports/daily/`、不写 `review_run.sqlite`、不产生 publication。**若后续验证其结论长期可用，再考虑提升为主链**；那需要正式修改红线 2，不在此处自行变更。

## 入口

```powershell
.\.venv\Scripts\python.exe -m stock_review.decision --date 2026-10-09
```

可选参数：`--lookback-days`（情绪序列回看交易日数，默认 5）、`--report-dir`、`--data-dir`、`--stdout`。

输出：

| 产物 | 默认位置 |
| --- | --- |
| 决策卡（Markdown） | `reports/decision/<trade-date>.md` |
| 输入与结论快照（JSON） | `data/decision/<trade-date>.json` |

## 通达信连接器接入

通达信数据走 MCP 通道，只能由 Agent 调用，无法被 Python 进程直接请求。因此接入方式是**快照解耦**：

1. Agent 通过通达信连接器采集当日事实，落成 `data/decision/tdx/<trade-date>.json`；
2. 决策卡运行时用 `src/stock_review/decision/tdx_snapshot.py` 读取该快照，作为一等输入并入判定与渲染。

快照缺失或 JSON 非法时一律视为「本次未接入通达信」，返回 `None`，**绝不用默认值或臆造数据冒充通达信证据**。

自校验（每个交易日采集后必须执行）：

```powershell
.\.venv\Scripts\python.exe -m stock_review.decision.tdx_snapshot --date 2026-10-09
```

它回答快照是否合法、各字段采到几条、有无条目被静默跳过；`ok=false` 时必须先修好快照再重跑决策卡。逐日采集的固定步骤见 [`DECISION_TDX_COLLECTION.md`](DECISION_TDX_COLLECTION.md)。

| 快照字段 | 用途 | 落地位置 |
| --- | --- | --- |
| `code_lookup` | 名称 → 代码解析，补全板块领涨股代码 | 第五节候选对象 |
| `board_quotes` | 概念/行业板块当日行情，补全 akshare 缺失的概念维度 | 第四节板块主线 |
| `stock_themes` | 个股涨停主题与原因揭秘 | 第五节「涨停逻辑（通达信）」 |
| `news` | 当日资讯标题与摘要 | 第六节消息面 |

逐日采集的固定步骤、连接器调用与字段映射见 [`DECISION_TDX_COLLECTION.md`](DECISION_TDX_COLLECTION.md)。

## 四个问题与规则来源

所有阈值集中在 `src/stock_review/decision/read_market_state.py` 顶部，判定为有序规则，先命中者优先。

| 问题 | 判定方式 | 规则来源 |
| --- | --- | --- |
| 市场情绪 | 涨停/跌停/炸板率/最高连板 + 全市场涨跌家数，按 冰点 → 退潮 → 主升 → 上升 → 混沌 顺序命中；另给 0–100 情绪温度作参考 | `stock-review.md` STEP 3 |
| 指数阶段 | 上证指数 5 日涨跌幅 + 10 日收盘箱体位置 + MA5/MA10，分为主升/下降/震荡 | STEP 1.1 |
| 量能档位 | 两市 A 股成交额分 1 万亿以下 / 1 万亿–2 万亿 / 2 万亿以上，并提示环比急剧变化 | STEP 1.2 |
| 参与倾向 | 指数阶段 × 情绪阶段矩阵，输出 可参与 / 谨慎参与 / 不参与 / 待确认 | STEP 1.1 + STEP 3 |
| 买什么 | 板块内涨停家数聚集度（主证据）+ 概念/行业涨幅榜 + 连板梯队 + 板块领涨股 | STEP 4、STEP 6 |
| 买多少 | 量能档位基线 × 参与倾向 × 指数阶段修正，给出总仓位区间、单票上限与标的上限 | STEP 1.1 / 1.2 推导 |

仓位区间是**框架推导建议，不是执行指令**；决策卡正文中每次都显式声明这一点。

## 数据来源

| 用途 | 来源 |
| --- | --- |
| 指数日线与两市成交额 | `akshare:stock_zh_index_daily_em`、`akshare:stock_sse_deal_daily+stock_szse_summary` |
| 涨停/炸板/跌停池、连板高度 | `akshare:stock_zt_pool_em`、`stock_zt_pool_zbgc_em`、`stock_zt_pool_dtgc_em` |
| 概念与行业板块榜 | `akshare:stock_board_concept_name_em`、`stock_board_industry_name_em` |
| 全市场涨跌家数与活跃度 | `akshare:stock_market_activity_legu` |
| 交易日历 | `akshare:tool_trade_date_hist_sina` |
| 概念/行业板块行情、个股涨停逻辑、代码解析、当日资讯 | `tdx-connector`（经 `data/decision/tdx/<trade-date>.json` 快照） |

采集复用 `src/stock_review/evidence/akshare_source.py` 的适配器，不新建第二套外部调用；通达信数据只经快照文件进入，与采集通道解耦。

## 已知限制

1. **概念板块受东财接口稳定性影响。** 环境代理阻断 `push2.eastmoney.com` 时，板块采集会退化到同花顺行业口径。此时若通达信快照提供 `board_quotes`，概念维度改由通达信板块行情补充；两者都没有才记入缺口。
2. **指数历史不含逐日成交额。** 指数日线接口不返回成交额，因此量能序列单独通过交易所每日概况采集，缺失的交易日保留空值并登记缺口。
3. **板块领涨股代码优先用通达信解析。** 板块字段只提供领涨股名称；名称未出现在当日涨停池时，先查通达信 `code_lookup`，仍无法确认才标记为待确认，不做猜测。
4. **情绪温度是参考指标**，用于横向比较当日强弱，不构成框架事实，也不进入参与倾向判定。
5. **12 模式买点尚未纳入。** STEP 9 的十二模式需要分时、竞价与次日反馈证据，当前不在决策卡范围内。
6. **通达信快照由每交易日盘后任务自动采集。** 采集步骤固定在 [`DECISION_TDX_COLLECTION.md`](DECISION_TDX_COLLECTION.md)；当日未采集或连接器不可用时，决策卡仍照常产出并在第六节写明消息面缺口。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_decision_card.py
```

测试全部使用合成输入，不访问网络、真实数据库或正式报告目录。
