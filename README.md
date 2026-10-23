# 物流管理报价系统（Logistics Quotation System）

> **资产说明**：`/docs` 和 `/data` 以及其他 SQL 均涉及公司资产，需另外管理，不在此项目中体现。本项目只展示系统技术骨架。（**月度账单规格 `docs/bill.md` 涉及公司敏感信息——真实表名/口径/数据，同样不入库**）
>
> 同理，业务执行手册（`/agents` 下的 SKILL.md / README.md）、`AGENTS.md`、`/tests`、以及 `src/agents/`（子 Agent 业务节点实现）也涉及公司业务口径与数据，不在本仓库体现，仅保留空目录占位。

---

## 一、项目简介

本项目是某公司的**物流管理报价系统**——一个对话式智能体（Agent）应用：用户以自然语言描述业务诉求，系统通过多 Agent 协同编排，结合企业数据库与外部地图服务，输出结构化的决策建议（如报价参考区间）。

本仓库**仅展示技术骨架**（分层架构、编排引擎、工具接入、配置治理），**不含**具体业务逻辑、数据库结构、企业数据与业务文档。

---

## 二、技术栈

| 层 | 选型 | 说明 |
|----|------|------|
| 编排框架 | **LangGraph** | 确定性状态图 + HITL 中断 + Checkpoint 持久化 |
| 前端 | **Chainlit** | 纯 Python 对话界面；P8 起降为纯前端，经 WebSocket 调后端（登录 / 历史会话 / 流式 / HITL）|
| 后端 API | **FastAPI** | P8 批1 已拆出：独立进程跑编排图 + REST（会话）+ WS（流式 / HITL）+ JWT 鉴权 |
| LLM | **OpenAI 兼容接口** | 裸 base_url + 薄抽象，可切换不同 provider |
| 数据库 | **SQL Server** | pyodbc 直连（只读账户，完整 T-SQL 支持）|
| 外部地图 | **高德地图 MCP** | 地理编码（maps_geo）|
| 记忆 | **mem0** | 用户偏好长期记忆（向量库 Qdrant：local embedded / server 双模式）|
| 知识库 | **WeKnora** | RAG 检索，预留接口，默认不启用 |
| 持久化 | **Checkpointer + 会话/用户库** | provider 双后端：SQLite（默认单机）/ PostgreSQL（多用户），`.env` 一改即切、无需动代码 |

---

## 三、架构亮点

- **确定性编排**：LangGraph 状态图定义主流程（意图解析 → 参数补全 → 名称转码 → 子 Agent 调度 → 结果汇总），流程由图结构决定而非 LLM 自主发挥，保证可控、可复现。
- **HITL（人工在环）**：原生 `interrupt` 支持关键节点人工介入（如缺失数据的人工回填），Checkpoint 保存状态、支持断点续跑。
- **harness 五层治理**：启动协议 / 执行铁律 Checklist / 产出 DoD 自检 / 记忆治理 / 单一事实源审计——一套让多 Agent 系统"言行一致"的可靠性工程范式。
- **工具层混合接入**：数据库用 pyodbc 直连（完整 T-SQL / CTE 支持），外部地图用 MCP，均为代码确定性调用（非 LLM Tool Use），行为可预测。
- **配置分层与密钥隔离**：`config.yaml`（结构，可入库）+ `.env`（敏感，gitignore）+ `${ENV}` 展开——密钥绝不进版本库。
- **可移植 / 反厂商锁定**：基于 agentskills.io + AGENTS.md 开放标准，能力资产可跨工具迁移。

---

## 四、目录结构（技术骨架）

```
src/
├── config/       # 配置层（pydantic 模型 + ${ENV} 展开）
├── graph/        # LangGraph 编排图 + 节点定义（含 nodes/）
├── agents/       # 子 Agent 节点实现（业务逻辑，不上传，仅空目录占位）
├── llm/          # LLM 薄抽象（OpenAI 兼容，可切换 provider）
├── memory/       # mem0 记忆集成（Qdrant local / server）
├── knowledge/    # WeKnora 知识库预留接口
├── tools/        # 工具层（pyodbc DB 客户端 + 高德 MCP 客户端）
├── sql/          # SQL 加载与参数注入
├── persistence/  # Checkpointer + 会话元数据（SQLite / Postgres 双后端）
├── auth/         # 用户认证（PBKDF2 密码哈希 + JWT + create-user CLI）
├── api/          # FastAPI 后端（跑图 + REST 会话 + WS 流式/HITL + 鉴权）
├── web/          # Chainlit 前端（经 WS 调后端 + data layer 代理 REST）
└── main.py       # 入口
```

> `/docs`、`/data`、`/agents`（业务手册与 SQL）、`/tests`、`AGENTS.md`、`src/agents/`（子 Agent 业务节点）涉及公司资产，不在本仓库体现（保留空目录占位）。

---

## 五、运行环境与启动

**环境准备**

- Python 3.11+
- ODBC Driver for SQL Server（微软官方驱动）
- 安装依赖：`pip install -r requirements.txt`
- 配置：复制 `.env.example` 为 `.env`，填入实际的数据库 / LLM / 嵌入 / 地图服务凭据，以及两个 JWT 密钥 `CHAINLIT_AUTH_SECRET`（前端会话）+ `AUTH_JWT_SECRET`（后端 API 鉴权）

**启动**（手动分步；默认 SQLite 无需 Docker，Postgres / Qdrant server 模式见步骤 1 与文末「多用户生产」）

```bash
# 0) 激活虚拟环境
#    PowerShell: .\.venv\Scripts\Activate.ps1    |    bash/macOS: source .venv/bin/activate

# 1) 起数据库容器（仅 Postgres / Qdrant server 模式需要；默认 SQLite 可跳过）
#    数据卷挂宿主机 ./data/，持久化、不落容器内
docker compose up -d

# 2) 建首个登录账户（密码从 stdin 隐藏输入，不进 shell 历史）
#    按 .env 的 PERSISTENCE_PROVIDER 建到 SQLite 或 Postgres
python -m src.auth.cli create-user <用户名>

# 3) 起后端（FastAPI Agent 服务：跑图 + REST/WS + 鉴权，默认 http://localhost:8000）
#    必须用 python -m src.api.app —— 其 __main__ 在 Windows 用 asyncio.run(loop_factory=SelectorEventLoop)；
#    uvicorn CLI 会强制 ProactorEventLoop，provider=postgres 时 psycopg async 会崩
python -m src.api.app

# 4) 另开一个终端（同样激活 venv），起前端（Chainlit，http://localhost:8001）
chainlit run src/web/chainlit_app.py --port 8001
```

- 浏览器打开 http://localhost:8001，用步骤 2 的账户登录（真实 auth）。
- 前端经 WebSocket 连后端跑图；会话历史 / 恢复走后端 `/sessions` REST。后端健康检查：http://localhost:8000/healthz。
- 停止：后端 / 前端各终端 `Ctrl+C`；起过容器的话再 `docker compose down`。
- 两个终端都要能访问 venv。若提示 `chainlit` / `python` 不是可运行的命令 → venv 未激活：先跑步骤 0 的 `Activate.ps1`（Windows）/ `source`（macOS/Linux）；或直接用完整路径 `.venv\Scripts\chainlit.exe`（Windows）/ `.venv/bin/chainlit`（macOS/Linux），后端换 `.venv\Scripts\python.exe -m src.api.app`。若 `Activate.ps1` 被执行策略拦住，完整路径方式可绕过。

**本地存储**（默认 SQLite，均在 `data/`、gitignore、重启不丢）：`langgraph.db`（图状态 / 断点续跑）· `sessions.db`（会话元数据 + 消息历史）· `auth.db`（用户表，密码仅存 PBKDF2 哈希）· `qdrant/`（mem0 长期记忆）。多用户 / 多进程生产可切 Postgres + Qdrant server（`persistence.provider` 双后端，`.env` 一改即切、无需动代码；起容器 `docker compose up -d`）。

---

## 六、功能模块：估价 Agent 与 月度账单（Bill Agent）

系统规划两类对话能力，**共用同一交互入口**（Chainlit 对话界面 → FastAPI 后端 → LangGraph 编排）：

- **估价 Agent**（事前）：对话式集装箱运价估价——协议价 / 历史成交价 / 理论报价 三价对比。
- **月度账单 Bill Agent**（事后）：按货主按月出账，产出**三件套**——月度账单 .xls、税率表 .xls、收费凭证集合（每提单一份动态生成的收费清单 + 归集的进项凭证）。

Bill Agent 已作为子流程**编排进现有 LangGraph**（与估价 Agent 同一状态图 / 入口，按意图路由：`domain` + `bill_action` 二级分流，**出账 / 账期查询 / 封账三条链路均已打通**）。样板货主见本地模板配置（`agents/bill/shippers.yaml`，不入库），业务规则以**可替换模板**外置（换货主 = 换配置）。

- **利润分析 Profit Agent**（事后复盘）：按月份区间统计每趟利润 / 货主贡献 / 公里分档 / 盈亏平衡，产出 Excel（桌面 `利润分析/`）。已编排进同一状态图（`domain=profit` → 区间确认 → 生成 → 摘要 / 文件指引）；业务实现（`src/profit/`、`agents/profit/`）与口径属公司资产，不在本仓库体现。

> 需求、数据口径、字段映射与实现说明详见 **`docs/bill.md`**（本地文档，**涉及公司敏感信息，不入库**）。
> 注：`docs/bill.md`、账单相关的 Excel 产物、`账单_tmp/`、以及含真实数据的凭证文件夹均属公司财产，已在 `.gitignore` 中排除，不入库。
> 本仓库**只展示技术骨架**：与估价 Agent 一样，Bill Agent 的业务手册、三件套产物与后续代码整合均属公司内部资产，不在本仓库体现；本节仅说明其定位与接入方式。
