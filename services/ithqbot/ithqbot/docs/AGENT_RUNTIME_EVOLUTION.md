# ithqbot / iCatMsg Human-in-the-Loop Runtime Audit And Evolution

## 1. Current Audit

### 1.1 Current execution model

Current message path:

```text
iCatMsg Kafka Consumer
  -> ICatMsgChannel._handle_contract_event()
  -> MessageBus.publish_inbound()
  -> AgentLoop.run()
  -> AgentLoop._dispatch()
  -> AgentLoop._session_worker()
  -> AgentLoop._process_message_internal()
  -> AgentLoop._run_agent_loop()
  -> AgentPlanner.plan_iteration()
  -> ToolCallExecutor.execute_plan()
  -> Tool.invoke()/SkillContext
  -> MessageBus.publish_outbound()
  -> ICatMsgChannel.send()
```

Lifecycle:

1. Kafka 消费线程直接把消息送到本进程内 `MessageBus`.
2. `AgentLoop` 为每个 session 建本地 queue，单进程内串行处理。
3. 执行态主要由 `_run_agent_loop()` 的局部变量承载。
4. 结束后把会话历史写入 `SessionManager`，trace 事件写入 observability / gateway runtime store。

State storage points:

- 持久化:
  - `session/*store.py`: 会话消息历史
  - `graph/state_manager.py`: graph run / node state
  - `icatmsg-core-gateway/runtime_store.py`: 前端消息列表、trace、上传文件
- 进程内:
  - `AgentLoop._active_tasks`
  - `AgentLoop._session_queues`
  - `AgentLoop._session_locks`
  - `_run_agent_loop()` 的 `messages / iteration / active_model / planner_state / executor_state`
  - tool 正在执行时的 call stack / coroutine state

Unrecoverable today:

- planner iteration 中间态
- 当前 plan 的 node cursor
- tool outputs 的中间缓存
- cancellation 状态
- waiting interaction 的统一 runtime 状态
- task 与 coroutine 引用

### 1.2 Current blocking points

- 人工中断:
  - 现在 `/stop` 主要是 `task.cancel()`，不是协作式取消。
  - Tool/Skill 没有统一 cancellation token。
- 等待人工输入:
  - skill 可以发 `interaction`，但主 runtime 不进入正式 `WAITING_HUMAN` 状态。
  - 没有统一 checkpoint 恢复点。
- 跨实例恢复:
  - `AgentLoop` 的执行游标只在内存。
  - `MessageBus` 是进程内 `asyncio.Queue`，不能迁移。
  - session 虽然可落库，但 runtime stack 不可恢复。

### 1.3 Current coupling points

- Kafka consumer 与 runtime:
  - 逻辑上经过 `MessageBus`，但物理上仍是同进程同步衔接。
  - consumer 并未只做 ingress，仍承担 runtime 启动触发。
- session 生命周期:
  - 当前被 `_session_worker` 本地 actor 和 distributed lock 共同管理。
  - 同一 session 的 ownership 没有独立 scheduler 抽象。
- tool execution:
  - 绑定当前 asyncio event loop 和当前 worker 进程。
- trace/context:
  - `ithqbot/context.py` 用 `ContextVar`，优于 thread local。
  - 但 runtime resume 后不会自动恢复完整执行上下文，只能恢复 message metadata。

## 2. Target Runtime

### 2.1 New architecture

```text
Kafka
  -> ingress adapter
  -> runtime scheduler
  -> stateless execution worker
  -> checkpoint store
  -> event bus / outbound publisher
  -> iCatMsg gateway
```

Principles:

- consumer 只做 contract validation, dedupe tag, ingress publish
- scheduler 负责 session ownership 与 resume trigger
- worker 无状态，只消费 checkpoint + command
- checkpoint store 是唯一恢复真相源
- 所有 human interaction 走统一 contract

### 2.2 Runtime state machine

```text
CREATED -> RUNNING
RUNNING -> WAITING_HUMAN
RUNNING -> WAITING_TOOL
RUNNING -> PAUSED
RUNNING -> CANCELLING
RUNNING -> FAILED
RUNNING -> COMPLETED
RUNNING -> EXPIRED

WAITING_HUMAN -> RUNNING
WAITING_HUMAN -> PAUSED
WAITING_HUMAN -> CANCELLING
WAITING_HUMAN -> CANCELLED
WAITING_HUMAN -> EXPIRED

WAITING_TOOL -> RUNNING
WAITING_TOOL -> CANCELLING
WAITING_TOOL -> FAILED
WAITING_TOOL -> EXPIRED

PAUSED -> RUNNING
PAUSED -> CANCELLING
PAUSED -> CANCELLED
PAUSED -> EXPIRED

CANCELLING -> CANCELLED
CANCELLING -> FAILED
CANCELLING -> EXPIRED
```

Idempotency rules:

- 相同状态可重复写入
- 恢复前必须比较 checkpoint version
- 同 session 只能有一个有效 lease owner

## 3. Human Interaction Contract

Unified request:

```python
class HumanInteractionRequest:
    interaction_id: str
    session_id: str
    type: Literal["text_input", "confirm", "approval", "form", "otp", "file_upload"]
    prompt: str
    schema: dict | None
    timeout: int
```

Unified response:

```python
class HumanInteractionResponse:
    interaction_id: str
    data: Any
    session_id: str | None
```

Rules:

- skill/tool 只能通过 runtime contract 发起等待
- 禁止 skill 私自维护 websocket await
- interaction response 必须回到 runtime scheduler，再触发 resume

## 4. Runtime Checkpoint

Implemented schema:

```python
class RuntimeCheckpoint:
    schema_version: str
    checkpoint_id: str
    session_id: str
    run_id: str
    status: RuntimeStatus
    version: int
    trace_id: str | None
    route_metadata: dict
    plan: dict | None
    cursor: RuntimeCursor
    tool_outputs: dict
    memory_snapshot: dict
    variables: dict
    retry_state: dict
    cancellation: CancellationSnapshot | None
    pending_interaction: HumanInteractionRequest | None
    last_interaction_response: HumanInteractionResponse | None
    messages_snapshot: list[dict]
    errors: list[dict]
    metadata: dict
```

Store backends:

- file
- redis
- postgresql

Concurrency:

- optimistic version bump
- lease-based ownership hooks

## 5. Interrupt / Cancellation Model

Implemented baseline:

- `CancellationToken`
- `CancellationTokenSource`
- parent/child token chain
- timeout-ready snapshot format
- Tool.invoke 支持 `cancellation_token`
- SkillContext 注入 `cancellation_token`
- `/stop` 先 cooperative cancel，再 fallback task cancel

Required next step for full rollout:

- 所有 builtin tool / python skill 周期性 `throw_if_cancelled()`
- 外部 MCP / HTTP / subprocess 工具接入 nested token

## 6. Resume Model

Now implemented incrementally:

- runtime interaction 会落 checkpoint
- interaction response 会尝试加载 `WAITING_HUMAN` checkpoint
- resume message 会把 interaction response 重新注入到 messages snapshot

Next step:

- scheduler 按 checkpoint 恢复，而不是入口线程直接恢复
- tool 幂等键与 compensation policy 进入 checkpoint

## 7. Multi-instance Consistency

### 7.1 Session lock

- 继续保留现有 distributed session lock
- 新增 runtime lease，避免多 worker 同时 resume 同一 checkpoint

### 7.2 Deduplication

- Kafka ingress 应使用 `request_msg_id` 作为主幂等键
- checkpoint `run_id + version` 防重复恢复

### 7.3 Ordered resume

- interaction response 必须带 `interaction_id`
- 只允许匹配当前 pending interaction
- 更新 checkpoint version 后再进入 RUNNING

### 7.4 Long running tasks

- worker 不持有长事务
- 等待态写 checkpoint 后立即释放 worker

## 8. Performance Notes

Avoided:

- 不做全量 memory dump 作为唯一 checkpoint
- 不把大 tool result 无限制写入 checkpoint

Remaining risks:

- `messages_snapshot` 仍是当前渐进方案的主要体积来源
- 还没有做真正的 checkpoint delta compaction
- 进程内 `MessageBus` 仍限制了 execution worker 的完全无状态化

## 9. Progressive Migration Plan

### Phase 1

- 引入统一 runtime status / checkpoint / interaction contract
- 引入 cooperative cancellation
- interaction 进入 `WAITING_HUMAN`

### Phase 2

- 把 Kafka consumer 改成 ingress only
- 增加 runtime scheduler topic
- worker 改成按 checkpoint 执行

### Phase 3

- resume/retry/delay/timeout 全部事件化
- trace 与 OpenTelemetry span 按 checkpoint/version 关联

### Phase 4

- tool idempotency registry
- compensation policy
- actor-like session ownership

## 10. Files Added / Changed In This Step

- `agent/runtime/cancellation.py`
- `agent/runtime/human.py`
- `agent/runtime/state_machine.py`
- `agent/runtime/checkpoint.py`
- `agent/runtime/__init__.py`
- `agent/tools/base.py`
- `agent/skills/base.py`
- `agent/runtime/executor.py`
- `agent/loop.py`

## 11. What Is Still Not Fully Done

- Kafka ingress / scheduler / worker 仍未物理拆分
- WAITING_TOOL 状态尚未被外部 async tool 普遍使用
- checkpoint 还未做增量压缩
- 所有技能尚未完成 token-aware 改造
- OpenTelemetry / Kafka trace propagation 仍需补事件级实现

This is a compatibility-first evolution, not a rewrite.
