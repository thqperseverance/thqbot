# ithqbot 企业内部 Skill 场景建议

针对企业版 ithqbot，除了基础的对话与搜索外，以下是一些高度适配企业内部流程、建议优先开发或集成的 Skill 场景。

## 1. 运维与研发 (DevOps & SRE)

*   **日志智能分析 (Log Analyzer)**:
    *   **功能**: 接收一段 ELK/Loki 里的错误日志，自动分析堆栈，给出故障原因及可能的修复建议。
    *   **Purposes**: `reasoning` (分析原因), `extraction` (提取错误等级/关键字)。
*   **工单状态同步 (Ticket Assistant)**:
    *   **功能**: 与 Jira/Notion/GitHub 集成。用户在飞书里输入“查看我的待办工单”，Skill 自动拉取并生成摘要。
    *   **Purposes**: `extraction` (整理状态), `reasoning` (任务优先级分类)。
*   **数据库查询助手 (DB Copilot)**:
    *   **功能**: (慎用，需只读权限) 用户输入自然语言描述，Skill 生成符合公司规范的 SQL 并执行（带结果限制），直接反馈表格或图表。
    *   **Purposes**: `reasoning` (SQL 生成), `extraction` (结果摘要)。

## 2. 行政与 HR (Admin & HR)

*   **员工入职引导 (Onboarding Guide)**:
    *   **功能**: 针对新员工，解答关于福利、网络配置、办公区分布等 FAQ。
    *   **Purposes**: `extraction` (从内部 PDF 规范中提取精准答案)。
*   **会议摘要与待办提取 (Meeting Minutes)**:
    *   **功能**: 接收一段语音或文字会议记录，自动生成“決议事项”与“待办任务 (Action Items)”，并可一键推送到团队空间。
    *   **Purposes**: `reasoning` (润色总结), `extraction` (提取待办)。

## 3. 财务与合规 (Finance & Compliance)

*   **报销辅助预审 (Reimbursement Pre-check)**:
    *   **功能**: 用户上传发票图片，Skill 自动提取金额、日期、税号，并校验是否符合公司“非餐饮消费不报销”等逻辑。
    *   **Purposes**: `vision` (图片识别), `extraction` (数据抽取)。
*   **合同关键条款比对 (Contract Review)**:
    *   **功能**: 上传新旧两份合同 PDF，对比金额、违约责任、交付周期等差异，标记出高风险点。
    *   **Purposes**: `reasoning` (风险评估), `extraction` (差异比对)。

## 4. 知识库与数据分析 (BI & Knowledge)

*   **长文档/研报专家 (RAG Skill)**:
    *   **功能**: 针对公司内部的千万级 WIKI/知识库，实现跨文档的综合问答。
    *   **Purposes**: `embedding` (向量化), `reasoning` (多视角综合回答)。
*   **数据清洗与转换 (Data Cleaner)**:
    *   **功能**: 用户上传一个凌乱的 Excel，Skill 按照其描述（如“把日期全部转为 YYYY-MM-DD，并去重”）进行转换并提供下载链接。
    *   **Purposes**: `extraction` (定义转换规则)。

## 5. 开源 Skill 集成建议

*   **LangChain / LangGraph Skills**: 利用开源社区成熟的 RAG chain 或多步骤工作流，封装为 ithqbot 的 Python Skill。
*   **CrewAI / AutoGPT 概念集成**: 将“代理协作”逻辑封装在 Skill 内部，例如一个“市场调研 Skill”，其内部会启动多个小 sub-agents 协作。
*   **MCP (Model Context Protocol) Servers**: 
    *   虽然 ithqbot 已经支持 MCP 工具，但你可以将一些常用的 MCP Server (如 Google Maps, SQL, CLI) 组合成一个更懂业务的 **Compound Skill**。

## 6. 开发建议

*   **安全性**: 绝大多数企业 Skill 应当优先使用 `context.call_llm(task="extraction", ...)` 来降低成本和提升速度，只有在处理复杂逻辑分析时才使用 `reasoning`。
*   **隐私**: 处理敏感数据（如薪资、个人身份）的 Skill 应当增加额外的 `BotGuardrails` 策略或独立审计日志。
