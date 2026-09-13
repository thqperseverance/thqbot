# iCatMsg Core Gateway

这个目录提供一个最小但正式可运行的 `icatmsg-core-gateway` 服务，用来承接：

- 宿主透传的用户上下文
- `icatmsg-client` 所需的 `/session/context`
- `trace-observability` 所需的 `/traces/summary`
- 基础健康检查

它现在被收敛为一个纯 BFF 层，只负责插件 API、宿主身份透传、配置聚合与依赖探活，
不再承担消息 consumer、渠道 runtime 或机器人执行入口的职责。

## 当前已包含

- `GET /health`
- `GET /ready`
- `GET /chat/session/context`
- `GET /chat/bots`
- `GET /chat/conversations`
- `GET /chat/conversations/{conversation_id}/messages`
- `POST /chat/messages/send`
- `GET /traces/summary`
- `GET /traces`
- `GET /traces/{trace_id}`
- `GET /traces/{trace_id}/events`

`/ready` 的语义：

- `ready`：BFF 所需 bot 目录、Redis、Kafka、MinIO、`ithqbot.observability` 均可用
- `ready` 响应会展示 bot catalog、chat API、trace API 各自依赖状态
- `503 not_ready`：任一核心依赖不可用，服务不会宣告插件链路可接流量

## 用户上下文来源

服务不自行登录，而是优先校验宿主代理注入的标准 Bearer JWT，并兼容读取可信头：

- `x-platform-user-id`
- `x-platform-username`
- `x-platform-user-email`
- `x-platform-user-display-name`
- `x-platform-user-roles`
- `x-platform-tenant-id`

兼容当前平台已有命名：

- `x-authenticated-user-id`
- `x-authenticated-username`
- `x-authenticated-user-email`
- `x-authenticated-user-display-name`
- `x-authenticated-user-roles`

## 启动

```bash
cd services/icatmsg-core-gateway
./scripts/dev.sh
```

基础环境变量：

```bash
export ICATMSG_ITHQBOT_REPO=/home/winlmp/code/ai-atomic-platform/services/ithqbot
export ICATMSG_REDIS_URI=redis://localhost:6379/0
export ICATMSG_KAFKA_BOOTSTRAP_SERVERS=localhost:9092
export ICATMSG_KAFKA_SECURITY_PROTOCOL=SASL_PLAINTEXT
export ICATMSG_KAFKA_USER=kafka
export ICATMSG_KAFKA_PASSWORD=your-password
export ICATMSG_KAFKA_SASL_MECHANISM=PLAIN
export ICATMSG_MINIO_ENDPOINT=localhost:9000
export ICATMSG_MINIO_BUCKET=ithqbot-storage
export ICATMSG_MINIO_ACCESS_KEY=minioadmin
export ICATMSG_MINIO_SECRET_KEY=minioadmin
export ATOMIC_PLATFORM_JWT_SECRET=atomic-platform-dev-secret
export ATOMIC_PLATFORM_JWT_ISSUER=atomic-platform
export ATOMIC_PLATFORM_JWT_AUDIENCE=icatmsg-client,trace-observability
export ATOMIC_PLATFORM_TRUST_PROXY_HEADERS=true
```

也可以直接参考 [`.env.example`](/home/winlmp/code/ai-atomic-platform/services/icatmsg-core-gateway/.env.example)。

说明：

- `ICATMSG_ITHQBOT_REPO` 可选配，默认指向当前仓库下的 `services/ithqbot`，用于读取 `config*.json` 里的机器人资料
- `ICATMSG_REDIS_URI` 为必填，用于保存会话、消息与上传文件索引
- `ICATMSG_KAFKA_BOOTSTRAP_SERVERS` / `ICATMSG_INBOUND_TOPIC` 为必填，用于将用户消息投递到生产消息链路
- Kafka 如需账号密码，额外配置 `ICATMSG_KAFKA_SECURITY_PROTOCOL`、`ICATMSG_KAFKA_USER`、`ICATMSG_KAFKA_PASSWORD`，默认 SASL 机制可用 `ICATMSG_KAFKA_SASL_MECHANISM=PLAIN`
- 兼容旧变量名 `KAFKA_SERVERS`、`KAFKA_USER`、`KAFKA_PASSWORD`、`KAFKA_SECURITY_PROTOCOL`、`KAFKA_SASL_MECHANISM`
- `ICATMSG_MINIO_ENDPOINT` / `ICATMSG_MINIO_BUCKET` / `ICATMSG_MINIO_ACCESS_KEY` / `ICATMSG_MINIO_SECRET_KEY` 为必填，用于附件上传下载
- `ATOMIC_PLATFORM_JWT_AUDIENCE` 支持逗号分隔多个插件 ID，供共享 BFF 同时服务多个插件
- 若未配置 JWT 校验参数且 `ATOMIC_PLATFORM_TRUST_PROXY_HEADERS=true`，服务会回退到可信代理头，便于本地迁移调试
- `ithqbot.observability` 为 trace 查询的唯一远端来源；BFF 不再依赖本地消息 consumer 维护 trace 状态

## 联调建议

推荐按这个顺序启动：

1. `services/icatmsg-core-gateway`
2. 平台后端
3. 平台前端
4. `plugins/icatmsg-client/frontend`
5. `plugins/trace-observability/frontend`

关键检查点：

- `GET http://localhost:9001/health`
- `GET http://localhost:9001/ready`
- `GET http://localhost:8000/api/plugins`

## 当前迁移状态

- trace 查询由 `app/services/trace_service.py` 负责，优先读取 `ithqbot.observability`
- bot、会话、消息、附件上传下载由 `app/services/chat_service.py` + `app/services/runtime_store.py` 负责
- 新版本默认走 BFF 模式：用户消息写入 Redis 后投递 Kafka，附件直接上传 MinIO
- `icatmsg-core-gateway` 不再启动任何消息 consumer 或渠道 runtime；bot 执行与外部 channel 对接由 `services/ithqbot` 自身负责
