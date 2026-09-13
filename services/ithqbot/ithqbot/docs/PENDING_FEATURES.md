# ithqbot 待实现功能清单

## 1. 目标
- 记录“已设计但未完全落地”的能力，避免后续迭代遗漏。
- 为每项功能提供最小验收标准，便于排期与回归验证。

## 2. 功能清单

### 2.0 已实现状态速览（2026-03-24）
- **已完成**：2.2 Feishu 凭据自检与告警、2.10 MinIO 双向文件传递、2.25 健康检查与监控。
- **部分完成**：2.3、2.5、2.8、2.11、2.12、2.13、2.19、2.20、2.21、2.22、2.23。
- **未完成**：2.1、2.4、2.6、2.7、2.9、2.14、2.15、2.16、2.17、2.18、2.26、2.27、2.28。

### 2.1 渠道身份绑定服务落地（高优先级）
- **现状**: 文档已定义 `channel_user_id -> account_id`，代码侧仍以部分渠道原始 ID 直传为主。
- **阶段进展（2026-03）**:
  - Feishu 已支持按 Bot 配置临时 `account_id` 映射（`feishu_bots.json`）。
  - 入站 metadata 已补充 `channel_user_id`、`feishu_app_id`，可用于后续正式绑定服务切换。
  - 当前仍属于过渡方案，尚未形成统一持久化绑定与查询接口实现。
- **待实现**:
  - 实现 `GET/PUT/PATCH/POST` 绑定接口对应的持久化存储。
  - 入站链路接入绑定查询，替换“直接使用渠道用户 ID”。
  - 增加绑定缓存与失效策略。
- **验收标准**:
  - 同一用户从 Feishu 与 icatmsg 进入时，均落到同一 `account_id`。
  - 绑定停用后入站请求可被识别并返回标准错误码。

### 2.2 Feishu 凭据自检与告警（高优先级）
- **现状**: 已完成启动阶段凭据预校验与渠道健康状态暴露。
- **已实现**:
  - 启动阶段执行 Feishu app_id/app_secret 预校验并记录结构化结果。
  - 将 `app_id invalid`、token 获取失败归类为结构化健康状态并附建议动作。
  - 已暴露 `/health/channels` 状态接口，返回 Feishu 聚合健康度与分 bot 明细。
- **验收标准**:
  - 配置错误时 30 秒内可见明确错误状态与建议动作。
  - 配置正确时状态恢复为 healthy。

### 2.3 跨渠道共享会话灰度开关（中优先级）
- **现状**: 已支持 `session_scope` 配置，但缺少租户/机器人粒度灰度能力。
- **待实现**:
  - 支持按 `tenant_id`、`bot_id` 配置 `session_scope`。
  - 提供运行期切换策略并保证会话一致性。
- **验收标准**:
  - 可在同一集群内同时运行“隔离模式”和“共享模式”。
  - 切换后新会话按新策略生效，旧会话行为可追踪。

### 2.4 多租户强约束策略收敛（中优先级）
- **现状**: `tenant_id` 允许兼容为空。
- **待实现**:
  - 提供 `strict_tenant_mode` 配置。
  - 启用后拒绝缺失 `tenant_id` 的多租户请求。
- **验收标准**:
  - 严格模式下，缺失 `tenant_id` 返回 `INVALID_ARGUMENT`。
  - 单租户模式可继续兼容空值。

### 2.5 回包可靠性与重试（中优先级）
- **现状**: 已落地 Feishu 渠道回包重试与死信兜底，其他渠道待扩展。
- **已实现（2026-03）**:
  - 出站失败进入统一重试队列，按指数退避进行重试。
  - 超过最大尝试次数后转入死信列表，保留 `msg_id`、尝试次数、最后错误。
  - 提供 `/delivery/reliability` 查询重试历史与死信记录。
- **待实现**:
  - 将重试/死信策略扩展到更多渠道并支持外部持久化存储。
- **验收标准**:
  - 临时失败可自动重试恢复。
  - 超限失败可被查询并人工补偿。

### 2.6 MCP 异步模式（高优先级）
- **现状**: 工具调用以同步链路为主，长耗时任务容易阻塞主对话。
- **待实现**:
  - 为 MCP 调用引入异步任务模型（提交、查询、取消）。
  - 提供任务状态机（待执行/执行中/成功/失败/已取消）和结果回填机制。
  - 增加超时与重试策略，避免长任务拖垮会话。
- **验收标准**:
  - 长耗时 MCP 任务可异步执行，不阻塞主消息处理。
  - 用户可查询任务进度并在完成后收到结果通知。

### 2.7 MCP 支持场景网关与金库（高优先级）
- **现状**: 场景化路由与敏感配置管理尚未形成统一 MCP 入口。
- **待实现**:
  - 落地“场景网关”用于按机器人/租户/业务场景分发 MCP 请求。
  - 接入“金库”用于管理 API Key、凭据与动态密钥轮转。
  - 建立权限策略，限制不同场景可调用的 MCP 能力边界。
- **验收标准**:
  - 同一 MCP 服务可按场景路由到不同策略集。
  - 敏感凭据不落盘明文，支持审计追踪。

### 2.8 机器人差异化技能与安全护栏（高优先级）
- **现状**: 已支持在 `~/.ithqbot/config.json` 按 `bot_id` 配置差异化技能与安全护栏，不依赖 icatmsg 服务端配置。
- **已实现（2026-03）**:
  - 按 `bot_id` 配置工具白名单/黑名单，运行时动态限制可调用能力。
  - 支持指令越权拦截（`blockedInstructionPatterns`）与输出敏感词审查（`sensitiveWords`）。
  - 输出元数据新增策略命中字段（`_bot_guardrails_turn_hits` / `_bot_guardrails_last_hit` / `_bot_guardrails_blocked`）用于可观测与审计。
- **待实现**:
  - 将策略命中记录落地到外部可查询存储（当前为进程内历史）。
- **验收标准**:
  - 不同 `bot_id` 可呈现不同能力集合与安全策略。
  - 越权请求可被拦截并产生日志证据。

### 2.9 安全沙箱环境（高优先级）
- **现状**: 工具执行隔离能力有限，资源边界控制待增强。
- **待实现**:
  - 构建沙箱执行环境（CPU/内存/网络/文件系统访问限制）。
  - 为高风险工具启用强隔离与最小权限运行。
  - 支持沙箱审计日志与异常熔断。
- **验收标准**:
  - 高风险执行不能越权访问宿主关键资源。
  - 沙箱超限可自动中断并上报。

### 2.10 MinIO 双向文件传递（中优先级）
- **现状**: 主链路已完成并上线，支持“上传 -> 处理 -> 回传 -> 下载”闭环。
- **已实现**:
  - `/upload` 与 `/download` 支持 MinIO 优先、本地回退，并保持统一 `rel_path={account_id}/{chat_id}/{file_id}{ext}`。
  - Kafka `attachments` 支持字符串路径、结构化对象、`minio://bucket/path` 三种形态并统一标准化。
  - Agent `minio_push` 上传后自动把结构化附件注入出站消息；`/receive` 同步返回 `files[]` 与兼容字段 `file_meta`。
  - Web 端收包支持 `files/file_meta/attachments` 三路兼容，结果文件可直接下载。
- **验收标准**:
  - 支持多文件输入并稳定返回结果文件引用。
  - 已完成账号级路径隔离下载校验；大小限制、过期清理、扫描策略继续按 2.16 推进。

### 2.11 企业多用户多实例记忆持久化（高优先级）
- **现状**: 已完成 PostgreSQL 持久化主链路，支持多实例共享记忆读写。
- **已实现**:
  - 新增 `memoryStoreUri` PostgreSQL 后端（支持 `postgresql://`、`postgres://` 与 `postgresql+driver://` 变体）。
  - 企业默认 `memoryStoreUri=postgresql://postgres:postgres@127.0.0.1:5432/ithqbot`，并启用 `requireExternalMemoryStore=true`。
  - 记忆存储按 `tenant_id/account_id/bot_id` 三维主键隔离，避免跨租户串记忆。
  - 自动建表并支持失败重连重试，适配多实例并发场景。
- **待实现**:
  - 租户级配额、归档、清理策略与对应治理指标。
- **验收标准**:
  - 多实例并发下记忆不丢失、不串会话。
  - 支持按租户和账号维度检索与治理。

### 2.12 icatmsg 通过 Kafka 透传用户信息增强（中优先级）
- 该条目已拆分到 `icatmsg/docs/PENDING_FEATURES.md` 维护，详见同名条目。
- 协议字段定义迁移至 `icatmsg/docs/PROTOCOL_V1_2.md`。

### 2.13 Cron 企业级多用户方案评估与落地（高优先级）
- **现状**: 方案 B 已落地 PostgreSQL 版本，保留后端工厂以支持未来扩展到其他数据库。
- **待实现（方案 A）**:
  - 引入 `APScheduler + SQLAlchemy JobStore + 分布式锁`（保留现有 cron 接口语义）。
  - 持续完善数据库任务治理（审计、归档、压缩策略），增加租户与账号维度索引。
  - 增加 leader/锁机制，避免多副本重复触发。
- **已实现（方案 B，2026-04）**:
  - 已采用 `CronService Timer + Worker Loop` 的 Beat/Worker 解耦模型，语义等价于 `Celery Beat + Celery Worker`。
  - 任务数据与事件队列已统一落 PostgreSQL（`ithqbot_cron_jobs/ithqbot_cron_events/ithqbot_cron_leases`）。
  - 已通过 `dedupe_key=job_id:scheduled_at_ms` 实现幂等去重，避免重复投递。
  - 已提供失败重试与死信状态流转（`pending/processing/done/dead`），支持失败补偿。
  - 已补齐租户与账号维度索引（含 `tenant_id/account_id` 复合索引），用于隔离治理与范围查询。
- **实现说明（当前）**:
  - 调度侧通过租约表抢占 `scheduler` 主节点，仅主节点负责到点入队。
  - 执行侧通过 `FOR UPDATE SKIP LOCKED` 领取任务，天然支持多实例并行消费。
  - 企业默认 `cronStoreUri=postgresql://postgres:postgres@127.0.0.1:5432/ithqbot`，且 `requireExternalCronStore=true`。
  - 企业版不再支持本地 `jobs.json` 文件配置，Cron 任务统一存储在数据库。
- **待实现（方案 C）**:
  - 引入 `Temporal` 作为工作流与定时任务引擎。
  - 使用 Workflow/Activity 统一处理定时、重试、超时、补偿。
  - 提供可观测性与审计链路，满足企业级 SLA 场景。
- **验收标准**:
  - 多实例部署下不重复触发、不漏触发。
  - 节点故障时任务可恢复并可追踪执行历史。
  - 支持按 `tenant_id/account_id` 做任务隔离与治理。

### 2.14 icatmsg Server 认证与密钥治理收敛（高优先级）
- 该条目已拆分到 `icatmsg/docs/PENDING_FEATURES.md` 维护，详见同名条目。

### 2.15 CORS 与边界安全策略治理（中优先级）
- **现状**: CORS 当前为 `allow_origins=["*"]`，边界策略过宽。
- **待实现**:
  - 按环境配置 CORS 白名单（dev/staging/prod 分离）。
  - 增加预检与跨域失败可观测日志，避免“伪网络错误”难排查。
  - 补齐基础安全响应头与反向代理层限流策略。
- **验收标准**:
  - 仅白名单域名可跨域访问。
  - 跨域失败可在日志中定位到 origin、method、path。
  - 高峰流量下无明显恶意跨域滥用风险。

### 2.16 文件链路安全与资源隔离增强（高优先级）
- **现状**: 上传/下载缺少统一大小限制、类型策略与路径级权限校验。
- **待实现**:
  - 增加上传大小上限、MIME/扩展名白名单与恶意文件扫描挂点。
  - 下载链路增加“路径归属校验”，确保仅访问当前用户命名空间对象。
  - 支持签名下载 URL、访问有效期与审计日志。
- **验收标准**:
  - 超限或非法类型文件被拒绝并返回明确错误码。
  - 任意构造 path 参数无法越权下载他人文件。
  - 文件访问具备可追踪审计记录。

### 2.17 icatmsg Server 状态外置与多实例一致性（高优先级）
- 该条目已拆分到 `icatmsg/docs/PENDING_FEATURES.md` 维护，详见同名条目。

### 2.18 网关内存队列背压与削峰治理（中优先级）
- **现状**: `MessageBus` 使用无界 `asyncio.Queue`，高峰时存在内存增长与雪崩风险。
- **待实现**:
  - 为 inbound/outbound 队列设置容量上限与背压策略。
  - 增加队列长度、等待时延、丢弃数等指标并接入告警。
  - 对慢消费链路引入降级策略（限流、优先级、重试队列）。
- **验收标准**:
  - 压测下队列长度可控，不出现无界增长。
  - 队列拥塞可自动告警并触发预设降级行为。
  - 系统在突发流量下保持可恢复。

### 2.19 icatmsg Channel 适配层解耦（中优先级）
- 该条目已拆分到 `icatmsg/docs/PENDING_FEATURES.md` 维护，详见同名条目。

### 2.20 icatmsg ↔ ithqbot 协议契约版本化与解耦（高优先级）
- 该条目已拆分到 `icatmsg/docs/PENDING_FEATURES.md` 维护，详见同名条目。
- 协议正文迁移至 `icatmsg/docs/PROTOCOL_V1_2.md`。

### 2.21 系统多模型分层路由治理（高优先级）
- **现状**: 已落地两阶段模型路由主链路，支持规则优先、分类升档与失败回退；当前可观测与治理能力仍偏基础。
- **已实现（2026-03）**:
  - 路由配置统一在 `~/.ithqbot/config.json` 的 `agents.routing`，不依赖 icatmsg 服务端配置。
  - 支持 `small/medium/large` 三档模型映射（`tiers`），并可配置 `fallbacks` 回退链。
  - 支持规则优先（`rules`）与分类模型（`routerModel`）组合决策，低置信度或高风险自动升档。
  - 主模型失败时按回退链切换模型继续执行，避免单模型故障导致整轮失败。
- **待实现**:
  - 补齐出站 `metadata` 的路由可观测字段（当前主要依赖日志中的 route/fallback 记录）。
  - 增加按 `bot_id/tenant_id` 的路由策略覆盖能力（当前以全局 `agents.routing` 为主）。
  - 增加按档位的成本、延迟、失败率指标，支持告警与自动调参。
- **验收标准**:
  - 路由决策与回退过程可在链路维度被查询与追踪。
  - 不同 `bot_id` 可配置差异化模型路由策略并稳定生效。
  - 发生模型异常时可自动回退并保留审计证据。

### 2.22 Redis/Kafka/Minio 多套配置快速切换（中优先级）
- **现状**: 已完成配置模型与运行时接线，支持“多套配置 + active profile”一键切换。
- **已实现（2026-03）**:
  - ithqbot 配置支持 `infrastructure.kafkaProfiles/activeKafkaProfile`、`infrastructure.redisProfiles/activeRedisProfile`、`tools.minioProfiles/activeMinioProfile`。
  - 会话存储 URI 已支持按 active Redis profile 解析，支持向后回退到旧字段与环境变量。
  - icatmsg Server 已支持 `ICATMSG_PROFILE_FILE`、`ICATMSG_ACTIVE_*_PROFILE` 与 JSON 注入方式选择 Kafka/Minio profile。
- **结论**: 该条目后续不再单独实现新能力；生产上通过不同配置文件切换环境即可满足需求。
- **处理策略**:
  - 保留现有 active profile 与配置文件切换能力，不新增专门的“切换审计/告警”实现项。
  - 需要差异化治理时通过部署层配置管理（配置仓、发布流程、变更审批）控制。
- **验收标准**:
  - 切换 active profile 后无需改代码即可完成环境切换。
  - 通过替换配置文件可稳定完成环境切换并保持回退能力。

### 2.23 ithqbot 多实例 Kafka 消费区分（中优先级）
- **现状**: 已完成按 `bot_id + group_id` 的消费区分机制与文档指引。
- **已实现（2026-03）**:
  - 消费组维度由 `channels.icatmsg.group_id` 决定，同组实例自动负载均衡。
  - 业务路由维度由 `payload.bot_id` 与 `channels.icatmsg.bot_id` 匹配决定，避免跨机器人误处理。
  - 配置与运行手册已补“同 bot 扩容 / 多 bot 并行”推荐命名与风险说明。
- **待实现**:
  - 增加重复消费风险的运行时检测与指标告警（同 bot 配置多个 group_id）。
  - 增加按 `bot_id/group_id` 的消费与回包统计看板。
- **验收标准**:
  - 同 bot 多实例可稳定负载均衡且无重复回包。
  - 多 bot 并行时消息可按 `payload.bot_id` 准确路由。

### 2.24 Skill Graph 执行引擎与 Graph Planner（高优先级）
- **现状**: 已完成基础 DAG 执行引擎、状态持久化与 Agent 调用链接入，Graph Planner 已具备首版提示词、解析、校验与执行闭环。
- **已实现（2026-04）**:
  - `ithqbot.graph` 已支持 DAG 校验、条件边、并行调度、节点状态流转、交互中断恢复与全局状态传递。
  - 图运行状态已支持持久化存储，并可通过 `graph_id` 从工作区 `graphs/` 目录加载执行。
  - Agent 已支持 `run_graph_by_id`、`handle_request_via_graph` 与 `run_skill` 适配层，Graph 节点可复用现有工具注册链路。
  - `ithqbot.planner` 已提供技能索引、few-shot 示例、Prompt 构建、LLM 输出解析、DAG 校验与安全重试。
  - Planner 与图执行链路已补充状态更新透传、`icatmsg` 处理进度通知，以及 `TRACE_OBSERVABILITY` 所需的 `run_id`、节点耗时、失败原因与等待恢复事件。
  - 已补充 Planner 重试与图执行状态事件的自动化测试，并覆盖 `icatmsg` 状态详情透传。
  - 已在 `SESSION_STORE_GUIDE.md` 收敛 PostgreSQL/Redis 状态存储配置说明，补充 Graph 状态持久化选择规则与生产部署约束。
  - Agent 已补充更明确的 Planner 入口协议与用户指令识别策略，支持 `/graph plan`、`/graph preview`、`/graph run <graph_id>` 与显式技能图意图短语直达规划链路。
  - 已补充 Planner few-shot 条件分支示例，以及复杂条件分支与汇聚节点场景的集成测试。
  - Graph 节点执行上下文已自动注入 `parent_run_id` 以及 `metadata.graph.graph_id/run_id/node_id`，便于 Skill 内统一上报图级状态与恢复链路。
  - 并行节点执行已优化为“局部等待不阻断全部分支”：当某一分支进入等待用户输入态时，其它独立可运行分支仍可继续完成并持久化结果。
- **待实现**: 暂无，后续新增需求再补充。
- **验收标准**:
  - 用户请求可被自动规划为合法 DAG，并通过现有工具链稳定执行。
  - 图执行支持恢复、条件分支、状态映射与并行节点调度。
  - Planner 输出在技能不存在、图有环、节点孤立等异常场景下可被稳定拦截并重试。
  - 至少具备覆盖 planner 生成、graph-by-id 执行与 Agent 集成链路的自动化测试。

### 2.25 健康检查与监控 (Health Monitoring)（高优先级）
- **现状**: 已落地的网关健康检查端点，支持 Docker/K8s 探测。
- **已实现（2026-04）**:
  - 增加了一个微型的 HTTP 侦听器，提供 `/health` (存活) 和 `/ready` (就绪) 接口。
  - `/ready` 接口可反馈 Kafka (icatmsg channel) 连接状态和 Redis (Observability) 连通性。
  - 已支持通过 `--health-port` (默认 9002) 自定义监听端口。
- **验收标准**:
  - `curl http://localhost:9002/health` 返回 200 OK。
  - 基础设施异常时 `curl http://localhost:9002/ready` 返回 503。

### 2.26 业务指标导出 (Metrics Export)（中优先级）
- **现状**: 虽然有 Trace 日志，但缺乏聚合的业务指标。
- **建议**: 集成 Prometheus 导出器，追踪实时活跃会话数、LLM 响应耗时 (P99/P95) 以及各技能的调用频次。
- **验收标准**:
  - 提供 `/metrics` 接口输出标准 Prometheus 格式指标。
  - 可在 Grafana 中绘制核心业务看板。

### 2.27 长短文本联动优化 (Memory & RAG)（中优先级）
- **现状**: 目前依靠 `MemoryConsolidator` 进行内存压缩。
- **建议**: 随着对话增长，可以考虑将 RAG (检索增强生成) 能力深入整合到 Loop 内部，而不是仅作为一个工具存在，以提高大上下文下的检索效率。
- **验收标准**:
  - 长对话下检索相关上下文的准确度与响应速度有显著提升。
  - 支持自动化的知识沉淀与召回。

### 2.28 多智能体协同 (Multi-Agent Orchestration)（高优先级）
- **现状**: 目前主要处于“单 Bot，多技能”模式。
- **建议**: 增加内置的“主管智能体 (Supervisor)”路由能力，支持在同一个会话中调用多个垂直领域的小助手进行协同办公。
- **验收标准**:
  - 支持多 Bot 在同一 Context 下无缝切换与协作。
  - 具备清晰的任务分发与结果聚合机制。

## 3. 里程碑建议
- **M1**: 2.1 + 2.2（先解决账号归一与可观测性）
- **M2**: 2.3 + 2.4（治理策略能力）
- **M3**: 2.5（可靠性完善）
- **M4**: 2.6 + 2.7 + 2.8（MCP 能力与策略体系）
- **M5**: 2.9 + 2.10（执行安全与文件闭环）
- **M6**: 2.11 + 2.12（企业级持久化与链路透传增强）
- **M7**: 2.13（Cron 企业级多用户方案评估与落地）
- **M8**: 2.14 + 2.15（认证、密钥与边界安全基线）
- **M9**: 2.16 + 2.17（文件链路治理与服务状态外置）
- **M10**: 2.18 + 2.19（背压治理与渠道适配解耦）
- **M11**: 2.20（跨服务消息契约版本化与兼容治理）
- **M12**: 2.21（多模型路由治理与可观测增强）
- **M13**: 2.22 + 2.23（多套配置切换治理与多实例消费可观测）
- **M14**: 2.24（Skill Graph 自动规划与执行闭环）
- **M15**: 2.25 + 2.26（健康监控与指标体系）
- **M16**: 2.27 + 2.28（RAG 深度集成与多智能体协同）

## 4. 变更维护约定
- 每次上线前检查本清单，确认是否触发对应验收项。
- 已完成项需在发布后标记“完成版本 + 日期 + 负责人”。
- 当某个待实现项落地后，从“2. 功能清单”迁移到“5. 已实现功能记录”，并补全实现细节与证据链接。
- 若仅部分实现，保留在“2. 功能清单”，并在该条目下新增“已落地范围/剩余范围”说明。

## 5. 已实现功能记录（持续补充）

### 5.1 记录规范
- 每条已实现功能必须包含：来源条目、上线版本、完成日期、负责人、实现摘要、影响范围、验证证据、遗留事项。
- 验证证据建议包含：PR 链接、测试用例、关键日志、接口回归结果。
- 若后续回滚或重构，需在对应条目追加“变更历史”。

### 5.2 条目模板
- **来源条目**: 2.x
- **状态**: 已完成 / 部分完成
- **上线版本**: vX.Y.Z
- **完成日期**: YYYY-MM-DD
- **负责人**: 
- **实现摘要**:
  - 
- **影响范围**:
  - 服务/模块:
  - 接口/配置:
- **验证证据**:
  - 测试:
  - 日志/监控:
  - 回归结论:
- **遗留事项**:
  - 

### 5.3 已实现清单

#### 5.3.1 来源条目 2.3（部分完成）
- **来源条目**: 2.3 跨渠道共享会话灰度开关
- **状态**: 部分完成
- **上线版本**: 待补
- **完成日期**: 待补
- **负责人**: 待补
- **实现摘要**:
  - icatmsg Channel 已支持 `session_scope` 配置并在运行时生成不同 session key（`account_chat` 共享/`channel` 隔离）。
- **影响范围**:
  - 服务/模块: `ithqbot/channels/icatmsg.py`
  - 接口/配置: `channels.icatmsg.session_scope`
- **验证证据**:
  - 测试: `tests/test_icatmsg_channel.py` 覆盖 `session_key_override` 行为；当默认配置时为 `icatmsg:user123:chat456`。
  - 日志/监控: Channel 启动日志可见 Kafka topic/group 与运行态。
  - 回归结论: 会话键策略已可配置，但尚未实现 tenant/bot 维度灰度开关。
- **遗留事项**:
  - 按 `tenant_id/bot_id` 的细粒度灰度策略与运行期切换能力未落地。

#### 5.3.2 来源条目 2.2（已完成）
- **来源条目**: 2.2 Feishu 凭据自检与告警
- **状态**: 已完成
- **上线版本**: 待补
- **完成日期**: 待补
- **负责人**: 待补
- **实现摘要**:
  - 启动阶段已执行 Feishu app_id/app_secret 凭据预校验并输出结构化健康状态。
  - 已提供 `/health/channels` 聚合健康接口，返回 Feishu 渠道总体状态与分 bot 详情。
- **影响范围**:
  - 服务/模块: `icatmsg/server/app.py`
  - 接口/配置: `/health/channels`、`feishu_bots.json`
- **验证证据**:
  - 测试: `tests/test_icatmsg_channel.py::test_channels_health_endpoint_returns_feishu_structured_status`
  - 日志/监控: 启动日志与健康接口可见 app_id invalid/token 获取失败等结构化状态。
  - 回归结论: 凭据异常可被快速识别并提供建议动作。
- **遗留事项**:
  - 无（后续增强可按 2.14 统一纳入认证治理）

#### 5.3.3 来源条目 2.10（已完成）
- **来源条目**: 2.10 MinIO 双向文件传递
- **状态**: 已完成
- **上线版本**: 待补
- **完成日期**: 待补
- **负责人**: 待补
- **实现摘要**:
  - 已打通“用户上传 -> Agent 下载处理 -> Agent 上传结果 -> 服务端回传 -> 用户下载”双向闭环。
  - 出站附件支持 `minio://bucket/path` 与结构化 `storage` 协议，服务端回包统一产出 `files[]` 与兼容 `file_meta`。
- **影响范围**:
  - 服务/模块: `icatmsg/server/app.py`、`ithqbot/channels/icatmsg.py`、`ithqbot/agent/tools/minio.py`、`icatmsg/web/src/hooks/useChat.ts`
  - 接口/配置: `/upload`、`/download`、`/send`、`/receive` 与 Kafka `attachments/file_meta/files`
- **验证证据**:
  - 测试: `tests/test_icatmsg_channel.py`、`tests/test_context_propagation.py` 覆盖上传回退、附件规范化、`minio_push` 自动附件注入。
  - 日志/监控: 服务日志包含 upload 存储后端、回包类型与附件透传信息。
  - 回归结论: 双向主链路稳定可用。
- **遗留事项**:
  - 文件安全治理项继续按 2.16 推进。

#### 5.3.4 来源条目 2.5（部分完成）
- **来源条目**: 2.5 回包可靠性与重试
- **状态**: 部分完成
- **上线版本**: 待补
- **完成日期**: 待补
- **负责人**: 待补
- **实现摘要**:
  - 已落地出站失败重试与死信兜底机制，支持指数退避重试。
  - 已提供 `/delivery/reliability` 查询重试历史与死信记录。
- **影响范围**:
  - 服务/模块: `icatmsg/server/app.py`
  - 接口/配置: `/delivery/reliability`
- **验证证据**:
  - 测试: 待补 `/delivery/reliability` 专项自动化用例（当前以接口回归与运行日志验证为主）。
  - 日志/监控: 服务日志可见重试尝试、失败原因与 dead_letter 入列记录。
  - 回归结论: Feishu 回包链路已具备可恢复能力。
- **遗留事项**:
  - 重试/死信策略尚未扩展到更多渠道并接入外部持久化存储。

#### 5.3.5 来源条目 2.8（部分完成）
- **来源条目**: 2.8 机器人差异化技能与安全护栏
- **状态**: 部分完成
- **上线版本**: 待补
- **完成日期**: 待补
- **负责人**: 待补
- **实现摘要**:
  - 已支持按 `bot_id` 配置工具白名单/黑名单、越权指令拦截、输出敏感词审查。
  - 回包 metadata 已补策略命中字段用于审计和排障。
- **影响范围**:
  - 服务/模块: `ithqbot/agent/loop.py`、`ithqbot/config/schema.py`
  - 接口/配置: `~/.ithqbot/config.json` `botGuardrails.*`
- **验证证据**:
  - 测试: `tests/test_message_tool_suppress.py`
  - 日志/监控: 回包 metadata 可见 `_bot_guardrails_turn_hits/_bot_guardrails_last_hit/_bot_guardrails_blocked`。
  - 回归结论: 差异化策略与阻断链路已生效。
- **遗留事项**:
  - 策略命中记录当前主要为进程内历史，外部可查询存储待补。

#### 5.3.6 来源条目 2.12（部分完成）
- 详细记录已迁移至 `icatmsg/docs/PENDING_FEATURES.md` 中对应条目。

#### 5.3.7 来源条目 2.13（部分完成）
- **来源条目**: 2.13 Cron 企业级多用户方案评估与落地
- **状态**: 部分完成
- **上线版本**: 待补
- **完成日期**: 待补
- **负责人**: 待补
- **实现摘要**:
  - `CronTool` 已支持 `every_seconds` 的 one-time 推断与显式覆盖（`one_time=False` 强制循环）。
  - 非周期语义默认转一次性任务（`schedule.kind=at` + `delete_after_run=True`），避免误触发无限循环提醒。
  - Cron 作业已支持 `owner_id` 过滤与删除权限约束（list/remove 时按 owner 过滤）。
  - 已新增外部存储后端工厂（`register_cron_store_backend`），当前默认接入 PostgreSQL，未来可扩展其他数据库。
  - 已新增 PostgreSQL 三表模型：任务表（`ithqbot_cron_jobs`）、事件表（`ithqbot_cron_events`）、租约表（`ithqbot_cron_leases`）。
  - 已实现“调度入队 + Worker 消费”解耦执行链路，支持幂等键、重试退避与死信状态。
  - 已支持按 `tenant_id/account_id` 写入任务作用域并建立索引。
- **影响范围**:
  - 服务/模块: `ithqbot/agent/tools/cron.py`、`ithqbot/cron/service.py`、`ithqbot/config/schema.py`、`ithqbot/cli/commands.py`
  - 接口/配置: `cron add/list/remove`、`every_seconds/one_time`、`agents.defaults.cronStoreUri`、`requireExternalCronStore`
- **验证证据**:
  - 测试: `tests/test_cron_service.py` 覆盖 one-time 默认行为、强制 recurring、租户/账号作用域写入、PostgreSQL URI scheme 兼容。
  - 测试: `tests/test_config_migration.py::test_cron_store_uri_prefers_explicit_then_falls_back_to_memory_store` 覆盖配置回退链路。
  - 日志/监控: Cron 服务执行日志包含 add/execute/remove 轨迹。
  - 回归结论: 方案 B 的 PostgreSQL 版本主链路已可用，多实例调度一致性具备基础保障。
- **遗留事项**:
  - 与标准 Celery Beat/Worker + Redis/RabbitMQ 组件化部署仍有差异，后续可按运行环境切换执行引擎。
  - 审计看板、告警指标、任务治理后台待补。

#### 5.3.8 来源条目 2.20（部分完成）
- 详细记录已迁移至 `icatmsg/docs/PENDING_FEATURES.md` 中对应条目。

#### 5.3.9 来源条目 2.19（部分完成）
- 详细记录已迁移至 `icatmsg/docs/PENDING_FEATURES.md` 中对应条目。

#### 5.3.10 来源条目 2.21（部分完成）
- **来源条目**: 2.21 系统多模型分层路由治理
- **状态**: 部分完成
- **上线版本**: 待补
- **完成日期**: 待补
- **负责人**: 待补
- **实现摘要**:
  - 已落地 `agents.routing` 两阶段路由（规则优先 + 分类升档）与主模型失败回退链。
  - 已支持 `small/medium/large` 三档模型映射和 `fallbacks` 配置。
- **影响范围**:
  - 服务/模块: `ithqbot/agent/model_router.py`、`ithqbot/agent/loop.py`
  - 接口/配置: `~/.ithqbot/config.json` `agents.routing`
- **验证证据**:
  - 测试: `tests/test_model_router.py`
  - 日志/监控: 路由与 fallback 过程可在运行日志中观测。
  - 回归结论: 主链路可用，路由治理与指标能力待完善。
- **遗留事项**:
  - 出站 metadata 路由字段标准化、按 bot/tenant 覆盖与指标告警待补。

#### 5.3.11 来源条目 2.22（部分完成）
- **来源条目**: 2.22 Redis/Kafka/Minio 多套配置快速切换
- **状态**: 部分完成
- **上线版本**: 待补
- **完成日期**: 待补
- **负责人**: 待补
- **实现摘要**:
  - 已支持 Kafka/Redis/Minio profile 结构与 active profile 选择，运行时按 active 配置接线。
  - 会话存储 URI 已支持从 active Redis profile 自动解析并兼容旧配置回退。
  - icatmsg Server 已支持 profile 文件与环境变量 active profile 组合选择。
- **影响范围**:
  - 服务/模块: `ithqbot/config/schema.py`、`ithqbot/channels/manager.py`、`ithqbot/session/factory.py`、`icatmsg/server/app.py`
  - 接口/配置: `infrastructure.*Profiles`、`tools.minioProfiles`、`ICATMSG_ACTIVE_*_PROFILE`
- **验证证据**:
  - 测试: `tests/test_config_migration.py::test_active_profiles_select_kafka_minio_and_redis_uri`。
  - 日志/监控: 启动日志可观测当前 Kafka 连接与消费组；profile 未命中时走回退路径。
  - 回归结论: 快速切换主链路已可用，治理与审计能力待补。
- **遗留事项**:
  - profile 切换审计、字段完整性校验、变更告警待补齐。

#### 5.3.12 来源条目 2.23（部分完成）
- **来源条目**: 2.23 ithqbot 多实例 Kafka 消费区分
- **状态**: 部分完成
- **上线版本**: 待补
- **完成日期**: 待补
- **负责人**: 待补
- **实现摘要**:
  - 已支持 `channels.icatmsg.group_id` 控制同 bot 多实例负载均衡。
  - 已支持 `payload.bot_id` 与 `channels.icatmsg.bot_id` 过滤匹配，避免跨 bot 误消费。
  - 配置模板与运行手册已补充多实例推荐命名、扩容模式与风险提示。
- **影响范围**:
  - 服务/模块: `ithqbot/channels/icatmsg.py`、`icatmsg/docs/PROTOCOL_V1_2.md`、`docs/ithqbot/OPERATION_GUIDE.md`
  - 接口/配置: `channels.icatmsg.bot_id`、`channels.icatmsg.group_id`、Kafka `payload.bot_id`
- **验证证据**:
  - 测试: `tests/test_icatmsg_channel.py` 覆盖 bot_id 过滤行为与消费链路回归。
  - 日志/监控: 消费日志可见 `bot_id/group_id` 维度的收包与处理轨迹。
  - 回归结论: 区分机制已落地，重复消费风险告警与指标面板待完善。
- **遗留事项**:
  - 增加同 bot 多 group 的冲突检测与告警；补齐按 bot/group 的可观测统计。
