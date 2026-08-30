# 量化 Agent · 安装与启动指南

> 一句话：这是一个本地运行的量化决策面板（FastAPI 后端 + 浏览器前端），双击 `start.bat` 即可使用。

---

## 1. 日常启动（推荐：一键启动）

**重启电脑后，只需双击项目文件夹里的 `start.bat`。**

- 服务地址：`http://127.0.0.1:8643`（启动约 2 秒后会自动打开浏览器）
- 关闭方式：直接关掉黑色命令行窗口；或双击 `stop.bat`
- 想换端口：打开命令行，进入项目目录后执行 `start.bat 8700`

```text
quant-agent\
├─ start.bat          ← 双击启动（入口）
├─ stop.bat           ← 双击停止（默认按 8643 端口找进程）
├─ app.py             ← FastAPI 主程序
├─ backend\           ← 策略 / 行情 / AI 决策逻辑
├─ static\            ← 前端页面
├─ data_cache\        ← 行情缓存 + AI 配置 + 决策审计数据库
├─ strategy_settings.json ← A/B/C 参数与动态 ETF 候选池
└─ holdings.json      ← 各市场持仓配置
```

---

## 2. 手动启动（命令行方式）

```bat
cd /d C:\Users\Administrator\WorkBuddy\2026-08-20-22-59-49\quant-agent

:: 方式 A：uvicorn 标准启动
C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8643

:: 方式 B：直接运行（端口可用环境变量 QUANT_AGENT_PORT 覆盖，默认 8643）
python app.py
```

启动后可访问：

| 地址 | 用途 |
|---|---|
| `http://127.0.0.1:8643/` | 面板首页 |
| `http://127.0.0.1:8643/docs` | FastAPI 自动接口文档 |
| `http://127.0.0.1:8643/api/ai/history` | 历史决策记录 |

---

## 3. 首次安装（新电脑 / 新环境）

本机当前已有一个可直接用的 Python 环境（WorkBuddy 托管）：

```text
C:\Users\Administrator\.workbuddy\binaries\python\envs\default
```

`start.bat` 会自动按以下顺序寻找 Python：

1. 项目内虚拟环境 `.venv\Scripts\python.exe`（如果存在，优先用它）
2. 上面的 WorkBuddy 托管环境

### 在全新电脑上从零安装

```bat
:: 1) 安装 Python 3.10 或更新版本（勾选 Add to PATH）

:: 2) 进入项目目录
cd /d C:\path\to\quant-agent

:: 3) 创建项目专属虚拟环境（可选但推荐）
python -m venv .venv

:: 4) 安装依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 国内网络慢可加清华镜像：
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

:: 5) 双击 start.bat 启动
```

依赖清单见 `requirements.txt`：fastapi、uvicorn、pydantic、pandas、numpy、requests。

---

## 4. 首次使用配置（面板内完成，无需改代码）

1. 打开面板，展开 **设置** 区；
2. 填写 AI 接口信息：
   - **API Base URL**（OpenAI 兼容接口，如 `https://api.deepseek.com/v1`）
   - **模型名**（如 `deepseek-chat`）
   - **API Key**
   - 可新增、编辑、删除多个命名模型配置，并用配置组并行生成分析；编辑时 API Key 留空会沿用原值；
3. 点击 **保存配置** → **测试连接**。
4. 在交易计划 A/B/C 中维护各市场 1～10 只 ETF；名称可留空，系统会尝试自动识别。修改候选池或策略参数后点击“应用并重跑”。

配置落盘位置：

| 文件 | 内容 | 注意事项 |
|---|---|---|
| `data_cache/ai_config.json` | 模型配置、配置组（含 API Key） | 含密钥，备份时注意权限 |
| `strategy_settings.json` | A/B/C 候选池及策略参数 | 本地个性化配置 |
| `holdings.json` | 各市场真实持仓比例及池外遗留持仓 | 本地个人数据 |

---

## 5. 数据存储与长期追溯（重要）

这是长期运行的项目，所有 AI 决策都可追溯：

- `data_cache/quant_agent.db` —— SQLite 审计库，每次 AI 生成都保存：
  `decision_id`、时间、所选市场、模型配置、数据源、分析视角、当时持仓、行情快照、新闻、LLM 原始响应、护栏修正结果。
- 前端“历史决策记录”里每条都有 **追溯** 按钮，可导出完整回放文本。
- 接口：`GET /api/ai/history`（列表）、`GET /api/ai/history/{decision_id}`（单条详情）。

### 定期备份清单

```text
data_cache/quant_agent.db      ← 决策审计（最核心）
data_cache/ai_config.json      ← 模型配置（含密钥）
strategy_settings.json         ← 动态 ETF 池与策略参数
holdings.json                  ← 持仓
```

行情 CSV 缓存在 `data_cache/*.csv`，丢失后系统会自动重新拉取，无需备份。

---

## 6. 常见问题（FAQ）

| 现象 | 处理办法 |
|---|---|
| 重启电脑后打不开面板 | 服务**不会开机自启**，重新双击 `start.bat` 即可 |
| 提示端口被占用 | 双击 `stop.bat` 杀掉旧进程，或换端口 `start.bat 8700` |
| `ModuleNotFoundError` | 依赖没装全：`python -m pip install -r requirements.txt` |
| 行情拉取失败 / 数据为空 | 检查代码、网络和代理；美股按 Yahoo query1/query2 → 新浪 → Stooq，港股按 Yahoo query1/query2 → 腾讯回退，最后尝试过期缓存；单只失败原因会显示在交易计划中 |
| AI 报鉴权错误 | 打开设置重新填写 API Key 并保存 |
| 页面能开但没有策略数据 | 首次加载需拉取行情，等待片刻或点击强制刷新 |

---

## 7. 安全边界

- 本项目只生成**交易建议**，不会自动下单，所有操作需人工确认后手动执行。
- AI 指令中的价格语义分两类：
  - “下一交易日开盘价执行” —— 实际成交价开盘后才产生；
  - “限价 / 区间” —— AI 给出的参考价位，附行情截至日期与触发条件。
