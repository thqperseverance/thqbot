# ithqbot 测试文档

## 1. 测试目标
验证多账号隔离性、Kafka 消息可靠性及端到端联通性。

## 2. 测试用例

### TC-01: 多账号记忆隔离测试
1.  用户 A 发送：“请记住我的名字是张三”。
2.  用户 B 发送：“请记住我的名字是李四”。
3.  用户 A 发送：“我是谁？” -> 预期回答：“张三”。
4.  用户 B 发送：“我是谁？” -> 预期回答：“李四”。

### TC-02: Kafka 断联重连测试
1.  停止 ithqbot 进程。
2.  用户发送 3 条消息。
3.  启动 ithqbot 进程。
4.  预期：ithqbot 应自动拉取并处理这 3 条积压消息。

### TC-03: 模型路由规则优先测试
1.  配置 `agents.routing.enabled=true`，并设置规则：关键词 `bug` 命中 `large`。
2.  发送消息：“帮我修这个 bug，并给我重构建议”。
3.  预期：日志中路由来源为 `rule`，执行模型为 `large` 档位模型。

### TC-04: 模型路由低置信升级测试
1.  开启两阶段模型路由，并设置 `confidenceMin=0.72`。
2.  构造一条边界语义消息，使分类结果置信度低于阈值。
3.  预期：路由从 `small` 自动升级到 `medium`（或更高）。

### TC-05: 模型失败回退测试
1.  配置 `fallbacks.small=["medium","large"]`。
2.  人为使 `small` 模型不可用（错误 API key 或临时下线）。
3.  发送普通问答消息。
4.  预期：主模型失败后自动切换到 `medium`，最终仍可回复。

### TC-06: 协议解耦回归测试
1.  构造一条 `icatmsg_inbound` 的 `v1.2` 协议消息，仅包含 Kafka 契约字段。
2.  校验 `ithqbot/channels/icatmsg.py` 能正常解码并生成 `InboundMessage`。
3.  再构造 `OutboundMessage`，校验其能被编码为 `icatmsg_outbound` 协议消息。
4.  预期：测试不依赖 icatmsg Server 的内存结构、HTTP 路由或前端组件实现。

### TC-07: 服务端路由边界测试
1.  模拟 `message.reply`、`message.file`、`message.interaction`、`status.processing` 四类出站消息。
2.  校验 Server 仅根据协议字段完成客户端路由与 Feishu 下发，不依赖 ithqbot 内部对象。
3.  预期：服务端只消费 Kafka 协议，不直接调用 AgentLoop、Tool 或 Provider 实现。

## 3. 测试工具
- 协议样例自检：`icatmsg/tests/run_test.py`
- 协议与服务端回归：`tests/test_icatmsg_channel.py`
- 路由单测参考：`tests/test_model_router.py`
- 环境要求：安装 `aiokafka`（或使用内置 Mock 模式）。
- 如需端到端联调，可在启动 Kafka、icatmsg Server 与 ithqbot Gateway 后执行集成测试。
