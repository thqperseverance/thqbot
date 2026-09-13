# ithqbot Engine 运行与操作手册

本手册指导如何启动并运行 **ithqbot (Agent Engine)** 环境。ithqbot 是一个完全解耦的 AI 工作单元，它从 Kafka 消费任务并返回回复。

## 1. 部署模式 (Stateless Mode)

ithqbot 采用无状态设计。企业版默认将长期记忆与 Cron 任务持久化到 **PostgreSQL**，会话可按需使用 **Redis/PostgreSQL**，文件存储在外部对象存储（如 **MinIO**），因此适合 Docker/Kubernetes 水平扩展。

## 2. 启动步骤

### 第一步：环境套件 (Infrastructure)
确保已启动 Kafka、PostgreSQL 和 MinIO。ithqbot 依赖这些中间件来实现消息总线、持久化和附件托管。

### 第二步：配置环境
ithqbot 通过环境变量或 `--config` 参数指定配置文件（默认读取 `~/.ithqbot/config.json`）。

生产环境建议额外开启以下护栏：
- 设置 `ITHQBOT_STRICT_CONFIG=1`，在启动时拒绝关键敏感配置缺失。
- 仅通过 `observability.adminAccountIds` 或环境变量 `ITHQBOT_TRACE_ADMIN_ACCOUNTS` / `ITHQBOT_OBSERVABILITY_ADMIN_ACCOUNTS` 授予 trace 管理权限。
- 对 `sessionStoreUri` / `memoryStoreUri` 明确选择降级策略：允许降级时保留默认重试后回退；不允许降级时将 `requireExternalSessionStore` / `requireExternalMemoryStore` 设为 `true`。

**建议配置文件结构 (`config.json`)：**
> [!NOTE]
> 从 v0.1.4.post6 开始，支持引导配置 (Bootstrap Config)。
> 引导配置允许 `config.json` 仅作为入口，真实的机器人配置存储在 Redis 或 SQL 数据库中。
```json
{
  "agents": {
    "defaults": {
      "sessionStoreUri": "redis://localhost:6379/1",
      "memoryStoreUri": "postgresql://postgres:postgres@127.0.0.1:5432/ithqbot",
      "cronStoreUri": "postgresql://postgres:postgres@127.0.0.1:5432/ithqbot",
      "requireExternalSessionStore": false,
      "requireExternalMemoryStore": true,
      "requireExternalCronStore": true
    }
  },
  "infrastructure": {
    "kafka": {
      "servers": "localhost:9092",
      "inboundTopic": "icatmsg_inbound",
      "outboundTopic": "icatmsg_outbound"
    }
  },
  "channels": {
    "icatmsg": {
      "enabled": true,
      "bot_id": "finance_bot"
    }
  }
}
```

关键说明：
- `ITHQBOT_STRICT_CONFIG=1` 时，会校验当前生效模型对应 provider 的 `apiKey`、MinIO 凭据完整性，以及 Kafka SASL 用户名/密码完整性；校验失败直接拒绝启动。
- `observability.adminAccountIds` 用于控制跨租户 trace 查询管理员白名单，避免仅凭请求参数提升权限。
- `sessionStoreUri` 与 `memoryStoreUri` 初始化失败时会有限重试，并在允许降级的前提下回退到本地实现。

### 第三步：运行 Runtime
ithqbot 主工作模式是 `runtime`，它会持续监听 Kafka 总线。`gateway` 仍可用，但仅作为兼容别名保留。
```bash
# 进入项目根目录并挂载必要的 Python 路径
PYTHONPATH=. .venv/bin/python -m ithqbot.cli.commands runtime --config ./config.json
```

## 3. 多实例扩展 (Consumer Group)

对于高并发场景，可以启动多个具有相同 `bot_id` 和 `group_id` 的 ithqbot 副本：
- **负载均衡**：Kafka 自动将入站消息分配给不同的副本处理。
- **配置要点**：确保所有副本指向同一个 Redis/MinIO 实例。

## 4. Cron 企业方案 B（PostgreSQL）

当前已实现“调度与执行解耦”并落地 PostgreSQL：
- 调度层：多实例竞争 `scheduler` 租约，只有租约持有者负责将到期任务入队。
- 执行层：Worker 通过数据库行锁领取待执行事件并处理，支持并行执行。
- 幂等层：事件 `dedupe_key=job_id:scheduled_at_ms` 防止重复消费。
- 失败恢复：事件状态 `pending/processing/done/dead`，失败自动退避重试。

推荐配置：

```json
{
  "agents": {
    "defaults": {
      "cronStoreUri": "postgresql://postgres:postgres@127.0.0.1:5432/ithqbot",
      "requireExternalCronStore": true
    }
  }
}
```

说明：
- 企业版默认 `memoryStoreUri` 与 `cronStoreUri` 均为 PostgreSQL URI，且 `requireExternalMemoryStore/requireExternalCronStore=true`。
- Cron 不再支持本地 `jobs.json` 文件回退；未配置可用外部存储时会直接失败。
- `cronStoreUri` 仍支持 `postgresql://`、`postgres://`、`postgresql+psycopg://`；未来接入其他数据库时只需新增对应 URI scheme 的后端工厂实现。

## 5. 安全与护栏 (Bot Guardrails)

通过 `config.json` 中的 `botGuardrails` 部分来约束机器人的能力：
- `toolAllowlist`：该机器人仅允许调用的工具列表。
- `toolDenylist`：显式禁用的高风险操作（如 `exec` shell）。
- `sensitiveWords`：出参消息在返回网关前，如果命中敏感词，将被自动拦截并替换为预设文案。
- 生产模式建议同时开启 `ITHQBOT_STRICT_CONFIG=1`，让敏感配置问题在启动阶段暴露，而不是等到运行期请求才失败。

## 6. 远程配置存储 (Remote Configuration Store)

为了支持多机器人管理和动态配置更新，`ithqbot` 支持将配置存储在 Redis 或 SQL (PostgreSQL) 中。

### 6.1 引导配置 (Bootstrap Config)

本地 `config.json` 或环境变量现在支持“引导”模式：

```json
{
  "configStoreType": "redis",
  "configStoreUri": "redis://localhost:6379/0",
  "botId": "bot_A"
}
```

或者使用环境变量：
- `ITHQBOT_BOOTSTRAP_CONFIG_STORE_TYPE=redis`
- `ITHQBOT_BOOTSTRAP_CONFIG_STORE_URI=redis://localhost:6379/0`
- `ITHQBOT_BOOTSTRAP_BOT_ID=bot_A`

### 6.2 配置同步与迁移

可以使用 CLI 工具将本地配置同步到远程存储：

```bash
# 同步到 Redis
ithqbot config sync --to-redis redis://localhost:6379/0

# 同步并指定特定的 Bot ID
ithqbot config sync --target-bot-id my_special_bot --to-sql postgresql://...
```

### 6.3 运行特定机器人的实例

启动时可以通过 `-b` 参数覆盖引导配置中的 `bot_id`：

```bash
# 启动 bot_B 的实例，配置将从远程存储加载
ithqbot runtime -b bot_B
```

## 7. 配置加密存储 (Configuration Encryption)

为了满足企业级安全要求，`ithqbot` 支持对存储在文件、Redis 或 SQL 中的配置进行全量加密。

### 7.1 加密算法

支持以下加密算法：
- **AES-256 (default)**：国际标准。
- **SM4 (Guomi)**：中国国家密码标准。

### 7.2 开启加密

在引导配置（Bootstrap Config）中设置加密参数：

```json
{
  "encryptionEnabled": true,
  "encryptionAlgorithm": "sm4",
  "encryptionKey": "YOUR_MASTER_KEY"
}
```

或者通过环境变量：
- `ITHQBOT_BOOTSTRAP_ENCRYPTION_ENABLED=true`
- `ITHQBOT_BOOTSTRAP_ENCRYPTION_ALGORITHM=sm4`
- `ITHQBOT_BOOTSTRAP_ENCRYPTION_KEY=YOUR_MASTER_KEY`

### 7.3 UI 编辑支持

未来通过插件平台界面修改配置时，管理平台后端需要具备相同的 `encryptionKey` 即可对配置进行实时解密和重加密。`ithqbot` 内部使用了标准的 SM4-CBC 补齐模式，确保了跨语言/跨平台的兼容性。

## 8. 两阶段模型路由 (Model Routing)

为了优化成本和速度，建议开启 `agents.routing`：
- 第一阶段：使用小模型（如 `gpt-4o-mini`）快速分类任务意图。
- 第二阶段：根据分类结果（Small/Medium/Large）路由到主模型执行。
- 所有的模型 Key 均在 `providers` 中统一配置，与 `routing` 逻辑解耦。

## 7. 常见问题排查

### 7.1 Kafka 连接超时
- 查看 `infrastructure.kafka.servers` 是否可达。
- 确认 `saslMechanism` 和鉴权账号是否正确。

### 7.2 工具调用失败
- 检查 `ithqbot.context` 中的 `tenant_id` 是否传入。
- 确认该 `bot_id` 是否在 `toolAllowlist` 中涵盖了所需的技能。
- 确认外部依赖项（如 Brave Search API Key）是否在 `providers` 中定义。
