# quant-agent

本地运行的多市场中低频量化决策面板：以用户自己的 ETF 候选池和参数为输入，生成美股、A 股与港股的个性化持仓建议、历史回测、基准对比和 AI 复盘。系统只提供研究与交易计划，不连接券商、不自动下单。

## 核心能力

- **三市场动态候选池**：A（美股）、B（A 股）、C（港股）均可在交易计划中维护 1～10 只 ETF；候选池是回测、推荐、行情快照、补充基准曲线和 AI 护栏的统一来源。
- **A / C 波动红利**：逐只计算自身 SMA 趋势、60 日风险调整动量和 40 日实现波动，在趋势合格者中选择 Top1，再按 `min(100%, 目标波动 / 实现波动)` 控制仓位；全部不合格时持有现金。
- **B 周度轮动**：风险候选先通过可调 SMA 资格过滤，再按“动量 / 波动率”选择 Top1；固定国债 ETF 为系统防御资产且不占 10 只额度。挑战者需取得超过 10% 的归一化分数优势才换仓。
- **无未来函数回测**：收盘生成信号，下一交易日执行；按调仓名义金额计入 15bp 成本。支持全历史、近 3 年、近 1 年、2026-06 以来及自定义起始日期。
- **多基准图表**：主基准分别为 TQQQ、沪深300和 7200.HK（不可用时 A/C 回退首个有效候选）；候选池 ETF 买入持有曲线默认隐藏，可通过图例切换。悬停可查看当日净值、目标持仓和现金比例。
- **交易计划与持仓**：动态展示完整候选池行情、资格、动量分数、失败原因和池外遗留持仓；持仓校验后原子写入，接口错误保持结构化 JSON。
- **AI 多模型复盘**：支持模型配置 CRUD、配置组和并行分析；仅加载用户选择市场的行情与新闻，新决策护栏只接受当前动态池资产及现金/防御资产。
- **参数与配置持久化**：候选池、波动率目标、SMA、动量窗口和持仓比例均可保存；旧配置会自动迁移，ETF 名称留空时自动尝试解析真实名称。

## 策略逻辑

### A · 美股动态 ETF 池

1. 用户维护 1～10 只美股 ETF。
2. 每只 ETF 使用自己的价格判断是否站上 SMA。
3. 合格资产按 60 日风险调整动量排序并选择 Top1。
4. 使用选中资产 40 日年化实现波动计算目标仓位；同一资产仓位变化不足 10 个百分点时不调仓。
5. 所有候选不合格时转现金。

### B · A 股周度动量轮动

1. 用户维护 1～10 只 A 股场内 ETF 作为风险候选；系统固定国债 ETF 独立作为防御资产。
2. 每周最后一个交易日收盘决策，次日执行。
3. 风险资产需站上当前 SMA 闸门；合格者按 `动量窗口涨幅 / 波动率 × √252` 排名。
4. 选择 Top1；挑战者相对在位资产的归一化分数优势超过 10% 才换仓。
5. 全部风险候选不合格时转入国债 ETF。

### C · 港股动态 ETF 池

逻辑与 A 相同，但候选为港股 ETF，并额外提示港股产品类型、每手股数、交易币种、价差和无涨跌停等执行风险。所有候选不合格时转现金。

## 行情、名称与缓存

- 美股历史行情：Yahoo query1 → Yahoo query2 → 新浪 → Stooq。
- 港股历史行情：Yahoo query1 → Yahoo query2 → 腾讯复权 K 线。
- A 股行情：腾讯 / 现有 A 股数据链路。
- 轻量快照与名称：腾讯优先，Yahoo 元数据兜底。
- 缓存 TTL 为 6 小时；在线源全部失败时可使用过期缓存救急。
- 单只风险 ETF 失败会被隔离并在页面展示原因；用户池全部失效或防御资产失效时，系统拒绝生成误导性策略。

免费或非正式接口可能延迟、限频、缺失或调整格式。生产或商业使用应接入具备授权和 SLA 的数据源。

## 快速启动

### Windows

双击 `start.bat`，默认打开：

```text
http://127.0.0.1:8643
```

停止服务可双击 `stop.bat`；指定端口可执行 `start.bat 8700`。更完整说明见 [README_STARTUP.md](README_STARTUP.md)。

### 命令行

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m uvicorn app:app --host 127.0.0.1 --port 8643
```

接口文档：`http://127.0.0.1:8643/docs`。

## 测试与 Allure

```bash
python -m pytest --alluredir=reports/allure-results --clean-alluredir
allure generate reports/allure-results -o reports/allure-report --clean
```

当前测试覆盖策略权重、动态候选池、配置迁移、行情回退、轻量快照、AI 市场隔离、持仓保存和主要 API。HTML 报告入口为 `reports/allure-report/index.html`。

## 项目结构

```text
app.py                    FastAPI 服务与 API
backend/engine.py         A/B/C 策略构建和图表数据
backend/strategies.py     策略信号与权重逻辑
backend/backtest.py       次日执行、成本和绩效指标
backend/data_feed.py      多行情源与缓存
backend/advisor.py        交易计划、快照与持仓
backend/ai_advisor.py     多模型 AI 上下文、护栏与审计
backend/settings_store.py 配置校验、迁移与原子持久化
backend/watchlist.py      通用代码规范化、名称与快照能力
backend/ext_strategy.py   扩展策略兼容能力（当前页面不展示）
static/                   前端页面
tests/                    pytest + Allure 测试
reports/                  Allure 与研究输出
lab_optimize.py           策略研究脚本
lab_b_sweep.py            B 策略参数扫描脚本
```

`/api/ext/*` 与 `backend/ext_strategy.py` 暂时保留用于后端兼容；前端已不再加载“扩展标的 · 多策略实验区”。

## 本地数据与安全

以下文件包含个人配置、密钥、持仓、缓存或审计数据，已由 `.gitignore` 排除，不应提交到 GitHub：

- `data_cache/ai_config.json`
- `data_cache/ai_decisions.json`
- `data_cache/quant_agent.db`
- `data_cache/*.csv`
- `strategy_settings.json`
- `holdings.json`
- `watchlist.json`
- `.env*`

分享仓库前仍建议运行 `git status --ignored` 和敏感信息扫描。API Key 只保存在本机配置中，不要粘贴到 Issue、日志或提交记录。

## 风险说明

动态候选池不等于自动完成 ETF 质量审核。添加标的前应核验代码、产品类型、流动性、上市历史、币种、杠杆/反向属性和数据完整性。趋势策略在震荡市可能反复触发，动量可能突然崩溃，波动率目标只能控制风险敞口而不能保证止损或锁定利润；参数敏感性、滑点、价差、税费、汇率、停牌、交易日历和样本外退化都会导致实盘偏离回测。

本项目仅用于量化研究与学习，不构成投资建议或收益承诺。历史收益不代表未来，所有交易须由用户自行核验并承担风险。
