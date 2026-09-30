# 物流管理报价系统（Logistics Quotation System）

> **资产说明**：`/docs` 和 `/data` 以及其他 SQL 均涉及公司资产，需另外管理，不在此项目中体现。本项目只展示系统技术骨架。
>
> 同理，业务执行手册（`/agents` 下的 SKILL.md / README.md）、`AGENTS.md`、`/tests` 也涉及公司业务口径与数据，不在本仓库体现，仅保留空目录占位。

---

## 一、项目简介

本项目是某公司的**物流管理报价系统**——一个对话式智能体（Agent）应用：用户以自然语言描述业务诉求，系统通过多 Agent 协同编排，结合企业数据库与外部地图服务，输出结构化的决策建议（如报价参考区间）。

本仓库**仅展示技术骨架**（分层架构、编排引擎、工具接入、配置治理），**不含**具体业务逻辑、数据库结构、企业数据与业务文档。

---

## 二、技术栈

| 层 | 选型 | 说明 |
|----|------|------|
| 编排框架 | **LangGraph** | 确定性状态图 + HITL 中断 + Checkpoint 持久化 |
| 后端 API | **FastAPI** | 异步（ASGI）+ WebSocket 流式 |
| 前端 | **Chainlit** | 纯 Python 对话界面（历史会话 + 流式输出）|
| LLM | **OpenAI 兼容接口** | 裸 base_url + 薄抽象，可切换不同 provider |
| 数据库 | **SQL Server** | pyodbc 直连（只读账户，完整 T-SQL 支持）|
| 外部地图 | **高德地图 MCP** | 地理编码（maps_geo）|
| 记忆 | **mem0** | 用户偏好长期记忆 |
| 知识库 | **WeKnora** | RAG 检索，预留接口，默认不启用 |
| 持久化 | **LangGraph Checkpointer** | SQLite（起步）→ PostgreSQL（多用户）|

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
├── agents/       # 子 Agent 节点实现
├── llm/          # LLM 薄抽象（OpenAI 兼容，可切换 provider）
├── memory/       # mem0 记忆集成
├── knowledge/    # WeKnora 知识库预留接口
├── tools/        # 工具层（pyodbc DB 客户端 + 高德 MCP 客户端）
├── sql/          # SQL 加载与参数注入
└── main.py       # 入口

# 预留（P7 阶段建）：api/（FastAPI）· web/（Chainlit）· persistence/（Checkpointer）
```

> `/docs`、`/data`、`/agents`（业务手册与 SQL）、`/tests`、`AGENTS.md` 涉及公司资产，不在本仓库体现（保留空目录占位）。

---

## 五、运行环境

- Python 3.11+
- ODBC Driver for SQL Server（微软官方驱动）
- 安装依赖：`pip install -r requirements.txt`
- 配置：复制 `.env.example` 为 `.env`，填入实际的数据库 / LLM / 地图服务凭据

---

## 六、开发进度

技术骨架分阶段搭建：**P0** 配置层 → **P1** 工具层（已完成）→ **P2** LLM 薄抽象 → **P3** 子 Agent 节点 → **P4** LangGraph 编排 → **P5** 记忆 / 知识库 → **P6** 端到端 + 评估 → **P7** 界面 / API / 持久化 → **P8** 界面深化 / 多用户。
