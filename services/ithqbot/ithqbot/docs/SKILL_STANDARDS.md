# 技能与工具统一标准（Skill & Tool Unified Standard）

本文档是 ithqbot 技能体系的唯一权威标准，统一了原 `SKILL_STANDARDS.md` 与 `SKILL_SPEC.md` 的规范内容。

适用对象：
- 新建或改造所有 `skills/*` 能力
- Prompt Skill、Executable Skill 与 Composite Skill
- 内置技能与工作区覆盖技能

## 1. 规范定位

- `SKILL_STANDARDS.md`：规范主文档（必须遵循）
- `SKILL_SPEC.md`：兼容性与快速索引文档（说明如何与 Claude/OpenClaw 风格对齐）

所有实现细节、约束条款、质量门槛以本文档为准。

## 2. Skill 分类

### 2.1 Prompt Skill
- 只依赖 `SKILL.md` 提供策略与流程约束
- 可不包含可执行代码
- 适合流程编排、输出格式约束、工具调用策略

### 2.2 Executable Skill
- 通过 `tool/tool.py` 或 `runtime/implementation.py` 承载执行逻辑
- 推荐同时提供契约定义（`schema.json` 或 `tool/tool_def.json`）
- 适合文件处理、系统集成、长流程任务

### 2.3 Composite Skill
- 由多个已有 Skill 通过 DAG 组合而成
- 目录内提供 `graph.yaml` 或 `graph.yml` 作为流程定义
- 可像普通 Skill 一样被 `graph_id` 触发，也可被 Planner 视为高层能力节点
- 适合“周报生成”“审批流”“工单诊断”等稳定多步骤流程

## 3. 统一目录结构

```text
skills/
└── <skill_name>/
    ├── SKILL.md                # 必须，技能入口与语义规范
    ├── schema.json             # 推荐，输入输出契约
    ├── graph.yaml              # Composite Skill 推荐，流程定义
    ├── tool/                   # 可选，Tool 实现层
    │   ├── tool_def.json
    │   └── tool.py
    ├── runtime/                # 可选，本地执行层
    │   └── implementation.py
    ├── config/                 # 可选，依赖与插件配置
    │   ├── requirements.txt
    │   └── plugin.json
    ├── examples/               # 可选，few-shot 示例
    │   └── examples.json
    └── tests/                  # 推荐，最少 happy/error 用例
```

约束：
- `SKILL.md` 缺失则该目录不视为有效 Skill
- Executable Skill: `tool/tool.py` 与 `runtime/implementation.py` 至少存在一个
- Composite Skill: `graph.yaml|graph.yml` 至少存在一个
- 若两者同时存在，运行时优先 `tool/tool.py`
- skills存放位置： `ithqbot/skills/<skill_name>`

## 4. SKILL.md 标准

### 4.1 Frontmatter 最小格式

```yaml
---
name: "<skill_name>"
description: "<做什么 + 何时触发>"
metadata:
  ithqbot:
    capability: ["document", "analysis"]
    tags: ["summary", "nlp"]
    level: "atomic" # atomic | composite
    planner:
      input_from: ["document_parse"]
      output_to: ["task_generate"]
      incompatible_with: ["raw_llm"]
      preferred_after: ["document_parse"]
    idempotent: true
    retryable: true
    cost:
      level: "low" # low | medium | high
    latency:
      expected_ms: 1200
---
```

要求：
- `name` 与目录名一致，只允许小写字母、数字、下划线、中划线
- `description` 同时描述能力与触发条件
- `metadata` 可选，支持 `ithqbot` 与 `openclaw` 命名空间
- `metadata.ithqbot.capability/tags` 建议声明，供 Router/Planner 做能力选择与召回
- `metadata.ithqbot.level` 建议显式声明 `atomic|composite`
- `metadata.ithqbot.planner` 用于约束自动编排边界
- `metadata.ithqbot.idempotent/retryable/cost/latency` 用于声明执行语义与成本特征

### 4.2 正文推荐结构

1. `# <Skill Title>`
2. `## 目标`
3. `## 何时使用`
4. `## 输入约束`
5. `## 执行步骤`
6. `## 输出约定`
7. `## 示例`
8. `## 风险与边界`

## 5. 可执行契约标准

### 5.1 Tool 类实现要求

所有 Agent 可调用工具必须继承 `ithqbot.agent.tools.base.Tool`，并实现：
- `name` 属性
- `description` 属性
- `parameters` 属性（JSON Schema object）
- `async def execute(self, **kwargs)` 方法

### 5.2 契约文件要求

可执行 Skill 推荐提供 `schema.json` 或 `tool/tool_def.json`，并满足：
- `name` 与运行时工具名一致
- `description` 为 API 契约描述，不仅是业务文案
- `input_schema` 使用 JSON Schema object 结构
- `output_schema` 建议使用 JSON Schema object 结构，便于 Planner 自动推导下游
- `semantic.produces[]` 与 `semantic.consumes[]` 建议声明输出/输入语义标签

说明：
- `schema.json` 可以描述 Skill 自身输入输出契约，也可以承载 Capability 引用关系
- 当 Skill 只是编排层而非直接执行层时，推荐在 Skill 契约中使用 `capability_ref[]` 引用底层 Capability，而不是把 API 路由细节直接写进 Skill
- 面向执行层的 HTTP/MCP 路由、权限、SLA、成本等信息，应统一下沉到 Capability Schema

推荐示例：

```json
{
  "name": "meeting_summary",
  "description": "生成会议纪要并输出行动项",
  "input_schema": {
    "type": "object",
    "properties": {
      "transcript": {"type": "string"}
    },
    "required": ["transcript"]
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "summary": {"type": "string"},
      "action_items": {"type": "array"}
    }
  },
  "semantic": {
    "produces": ["meeting_summary", "action_items"],
    "consumes": ["meeting_transcript", "text"]
  }
}
```

推荐的 Skill 契约扩展示例：

```json
{
  "type": "skill",
  "name": "meeting_summary",
  "description": "生成会议纪要，并在需要时调用知识检索能力补充背景信息",
  "capability_ref": ["knowledge.retrieve"],
  "input_schema": {
    "type": "object",
    "properties": {
      "transcript": {"type": "string"}
    },
    "required": ["transcript"]
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "summary": {"type": "string"},
      "action_items": {"type": "array"}
    }
  }
}
```

## 6. Capability Schema 标准（插件能力描述规范）

本章节定义“Capability”的统一描述规范，用于把底层 API、MCP 服务或插件执行入口抽象为 AI 可理解、可组合、可调度的能力单元。

### 6.1 能力模型定义

- `Capability` = 可被 Skill、Agent 或 Planner 调用的最小能力单元
- `Skill` 负责流程编排、上下文组织和交互策略
- `Capability` 负责声明“能做什么、需要什么、产出什么、如何路由、有什么约束”
- `API/MCP` 是 Capability 的具体实现，不直接等同于 Capability

分层关系：

```text
Skill（编排）
   ↓
Capability（能力抽象）
   ↓
Plugin（执行载体）
   ↓
API / MCP（实现）
```

约束：
- 一个 Capability 应只表达一个稳定、可复用、可独立调度的能力
- Capability 名称全局唯一，不得与 Skill 名称空间混淆
- API 路径、SDK 客户端、MCP transport 只是 Capability 的执行细节，不应上升为 Skill 的核心语义

### 6.2 标准结构

Capability Schema 推荐采用如下结构：

```json
{
  "name": "billing.get_bill",
  "description": "获取用户账单信息",
  "version": "1.0.0",
  "namespace": "billing",
  "category": "atomic",
  "tags": ["billing", "finance"],
  "deprecated": false,
  "input_schema": {
    "type": "object",
    "properties": {
      "user_id": {
        "type": "string",
        "examples": ["u_12345"]
      }
    },
    "required": ["user_id"]
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "amount": {"type": "number"},
      "items": {"type": "array"}
    }
  },
  "semantic": {
    "produces": [
      {
        "name": "billing_data",
        "schema_ref": "#/output_schema",
        "description": "用户账单数据"
      }
    ],
    "consumes": [
      {
        "name": "user_id",
        "source": "input.user_id",
        "schema_ref": "#/input_schema/properties/user_id",
        "description": "账单归属用户 ID"
      }
    ]
  },
  "routing": {
    "type": "http",
    "service": "billing-service",
    "method": "POST",
    "endpoint": "/get_bill",
    "environment": "prod",
    "timeout_ms": 3000
  },
  "dependencies": [],
  "auth": {
    "required": true,
    "roles": ["user", "admin"]
  },
  "sla": {
    "latency_ms": 200,
    "availability": "99.9%"
  },
  "cost": {
    "level": "low"
  },
  "effects": {
    "type": "read",
    "resources": ["billing_db"]
  },
  "quality": {
    "score": 0.9,
    "source": "manual"
  },
  "idempotent": true,
  "retryable": true
}
```

必选字段：
- `name`
- `description`
- `version`
- `namespace`
- `input_schema`
- `output_schema`
- `semantic`
- `routing`

推荐字段：
- `category`
- `tags`
- `deprecated`
- `auth`
- `sla`
- `cost`
- `dependencies`
- `effects`
- `quality`
- `idempotent`
- `retryable`

`category` 推荐值：
- `atomic`
- `composite`
- `ai`

配套模板文件：
- 推荐直接复用 [capability.schema.json](file:///home/winlmp/code/ai-atomic-platform/services/ithqbot/ithqbot/docs/capability.schema.json) 作为 Capability 声明模板或 CI 校验基线
- 模板中的 `semantic` 已升级为 typed semantic，`routing` 已支持服务发现字段，适合作为 Planner/Registry 的标准输入
- `input_schema/output_schema` 推荐补充 `examples`、`default`、`enum` 等信息，以提升 LLM 理解能力与契约可读性

### 6.3 字段说明

#### 6.3.1 `name`

能力唯一标识格式：

```text
<namespace>.<action>
```

示例：
- `billing.get_bill`
- `knowledge.retrieve`
- `template.parse`

要求：
- 必须全局唯一
- 使用稳定动词短语，避免随实现细节频繁变化
- 不应包含环境名、协议名、供应商名，例如 `billing.get_bill_http`、`billing.get_bill_mcp` 都不推荐

#### 6.3.2 `semantic`

```json
{
  "semantic": {
    "produces": [
      {
        "name": "billing_data",
        "schema_ref": "#/output_schema",
        "description": "用户账单数据"
      }
    ],
    "consumes": [
      {
        "name": "user_id",
        "source": "input.user_id"
      }
    ]
  }
}
```

`semantic` 是 Capability 对 AI 最关键的声明层，推荐使用 typed semantic，而不是只有字符串 tag。

用于：
- Planner 自动拼接能力链
- Skill 自动补全下游依赖
- Router 按输入输出语义做能力匹配
- 为 Graph/Planner 提供可解释的调度依据
- 为 Capability Graph 提供类型化边信息
- 支持能力间输入输出的结构校验

要求：
- `produces[]` 与 `consumes[]` 的元素应为对象，而不是裸字符串
- `produces[].name` 描述稳定产出语义，不写实现细节
- `produces[].schema_ref` 推荐指向 `#/output_schema` 或其子路径
- `consumes[].name` 描述依赖的输入语义
- `consumes[].source` 推荐标明值来源，例如 `input.user_id`、`context.tenant_id`
- `consumes[].schema_ref` 可选，用于绑定输入结构位置
- 推荐使用领域词汇，而不是业务接口名

#### 6.3.3 `routing`

```json
{
  "routing": {
    "type": "http",
    "service": "billing-service",
    "method": "POST",
    "endpoint": "/get_bill",
    "environment": "prod",
    "timeout_ms": 3000
  }
}
```

`routing` 只描述执行路径，不定义能力本身。

要求：
- `type` 标识路由类型，如 `http`、`mcp`、`internal`
- `service` 推荐作为服务发现主键，用于与具体环境、注册中心、网关配置解耦
- `endpoint`、`method`、`server`、`tool`、`timeout_ms` 等字段由执行网关或运行时消费
- `environment` 可作为可选提示字段，用于表达 `dev/test/prod` 等环境语义，但环境切换逻辑不应硬编码到 Skill
- Skill/Planner 依赖的是 Capability 的语义与约束，不直接依赖 `routing`

HTTP 推荐示例：

```json
{
  "routing": {
    "type": "http",
    "service": "billing-service",
    "endpoint": "/get_bill",
    "method": "POST"
  }
}
```

MCP 推荐示例：

```json
{
  "routing": {
    "type": "mcp",
    "server": "billing-mcp",
    "tool": "get_bill"
  }
}
```

#### 6.3.4 `auth`

```json
{
  "auth": {
    "required": true,
    "roles": ["admin"]
  }
}
```

用于描述能力级权限边界，控制：
- 哪些 Skill 或运行角色可以调用
- 哪些用户身份可以触发
- 是否必须携带认证信息进入执行层

#### 6.3.5 `sla` 与 `cost`

```json
{
  "sla": {
    "latency_ms": 200,
    "availability": "99.9%"
  },
  "cost": {
    "level": "low"
  }
}
```

用于调度优化：
- Planner 可优先选择低延迟 Capability
- Router 可在语义等价时做成本控制
- 运维侧可据此设定降级与回退策略

#### 6.3.6 `dependencies`

当 `category=composite` 时，推荐显式声明依赖的上游 Capability。

```json
{
  "dependencies": [
    "billing.get_bill",
    "user.get_profile"
  ]
}
```

用途：
- 让 Composite Capability 可被静态解析
- 让 Planner 或构建器可做拓扑排序
- 为自动生成实现、依赖分析、循环依赖检测提供基础

要求：
- `composite` 类型 Capability 建议必须声明 `dependencies[]`
- `dependencies[]` 中的值应指向真实存在的 Capability 名称

#### 6.3.7 `effects`

```json
{
  "effects": {
    "type": "read",
    "resources": ["billing_db"]
  }
}
```

用于声明副作用和外部影响范围。

用途：
- Planner 避免危险或不必要的写操作
- Sandbox 控制外部资源访问
- 权限系统对高风险能力做更细粒度控制

要求：
- `type` 推荐为 `read|write|external`
- `resources[]` 应指向会被读取、写入或影响的资源
- `idempotent` 不能替代 `effects`，两者语义不同

#### 6.3.8 `quality`

```json
{
  "quality": {
    "score": 0.9,
    "source": "manual"
  }
}
```

用于给 Planner、Router 或 Registry 提供选优信号。

用途：
- 多个同类 Capability 并存时自动选优
- 支持 A/B 测试与灰度切换
- 支持根据指标或 AI 评估持续优化调度策略

要求：
- `score` 建议取值 `0~1`
- `source` 建议取值 `manual|metrics|ai_eval`

#### 6.3.9 `tags` 与 `deprecated`

```json
{
  "tags": ["billing", "finance"],
  "deprecated": false
}
```

用途：
- `tags[]` 用于检索、聚类、UI 展示
- `deprecated` 用于版本治理、平滑迁移和 Planner 降权

#### 6.3.10 `idempotent` 与 `retryable`

- `idempotent=true` 表示同样输入重复执行不会产生额外副作用
- `retryable=true` 表示运行时可在失败后自动重试
- 两者应基于真实语义声明，不得为了“调度方便”随意标记

### 6.4 命名规范与最佳实践

Capability 的命名应优先服务于“稳定语义”和“可检索性”，而不是迁就某次实现。

#### 6.4.1 `namespace` 命名规则

`namespace` 表示业务域，不表示协议、环境、部署单元或供应商。

要求：
- 使用单个领域名词，采用 `snake_case`
- 只表达“这是什么业务域”，不表达“部署在哪里”
- 应长期稳定，避免随组织架构、服务拆分、底层品牌变化而变更

推荐：
- `billing`
- `knowledge`
- `template`
- `user_profile`

不推荐：
- `billing_service`
- `prod_billing`
- `mcp_billing`
- `openai_report`

判断原则：
- 如果名字回答的是“属于哪个业务域”，通常是合格的 `namespace`
- 如果名字回答的是“跑在哪个系统里”“由哪个团队实现”“调用哪个供应商”，通常不应进入 `namespace`

#### 6.4.2 `action` 命名规则

`action` 表示能力行为，推荐使用稳定、可理解的动宾短语。

要求：
- 使用小写 `snake_case`
- 优先使用通用动作词，如 `get`、`list`、`create`、`update`、`delete`、`parse`、`retrieve`、`generate`、`analyze`
- 动作要表达用户或 Planner 关心的业务语义，而不是底层技术动作
- 一个 `action` 应只对应一个核心结果，不要塞入多个并列动作

推荐：
- `get_bill`
- `list_items`
- `retrieve`
- `generate_report`
- `analyze_risk`

不推荐：
- `do_request`
- `invoke_api`
- `run_pipeline`
- `get_and_parse_and_save`
- `call_openai`

拆分原则：
- 如果一个名字里出现多个并列动词，通常说明该 Capability 过大，应拆分
- 如果动作名离开当前实现仍然成立，通常说明命名较稳定

#### 6.4.3 `name` 组合规则

Capability 全名格式固定为：

```text
<namespace>.<action>
```

要求：
- `name` 必须与 `namespace` 语义一致
- `name` 必须避免品牌名、环境名、协议名、版本号
- `name` 不能把实现策略写进去，例如缓存、网关、MCP、HTTP、异步等

推荐：
- `billing.get_bill`
- `knowledge.retrieve`
- `report.generate`

不推荐：
- `billing_v2.get_bill`
- `knowledge.retrieve_http`
- `report.generate_openai`
- `prod_billing.get_bill`

#### 6.4.4 `semantic` 命名规则

`semantic.produces[].name` 与 `semantic.consumes[].name` 使用“稳定语义标签”，不是字段名镜像，也不是接口参数名堆砌。

要求：
- 使用小写 `snake_case`
- 优先使用领域对象、业务结果、通用输入语义
- 语义标签应能跨多个 Capability 复用
- 尽量避免把字段层细节、传输协议、实现品牌写入语义标签

推荐：
- `billing_data`
- `user_profile`
- `meeting_transcript`
- `document_text`
- `risk_analysis`

不推荐：
- `response_json`
- `post_body`
- `openai_result`
- `billing_get_bill_response`
- `request_v2`

区分原则：
- `input_schema` 解决“结构上需要什么字段”
- `semantic.consumes[]` 解决“语义上依赖什么输入”
- `output_schema` 解决“返回结果长什么样”
- `semantic.produces[]` 解决“这个能力产出什么业务语义”

#### 6.4.5 命名反模式

以下情况应视为命名质量问题：
- 把底层实现写进 `name`，例如 `knowledge.retrieve_http`
- 把供应商写进 `namespace` 或 `action`，例如 `openai.generate_report`
- 把多个能力拼成一个名字，例如 `billing.get_and_export_and_notify`
- 用模糊动作词命名，例 `do_task`、`handle_data`
- 用接口返回格式命名语义标签，例 `json_result`、`http_response`

#### 6.4.6 推荐校验规则

Capability 命名建议在评审或 CI 中至少校验：
- `namespace` 匹配 `^[a-z][a-z0-9_]*$`
- `action` 匹配 `^[a-z][a-z0-9_]*$`
- `name` 匹配 `^<namespace>\\.<action>$` 的组合语义
- `semantic.produces[].name` 与 `semantic.consumes[].name` 均使用 `snake_case`
- `composite` 类型 Capability 必须声明 `dependencies[]`
- 不允许出现 `http|mcp|api|sdk|openai|anthropic|prod|staging|test` 等实现或环境词，除非该词本身就是业务域词汇

### 6.5 与 Skill 契约的关系

Skill 与 Capability 的职责边界如下：

- Skill 是流程语言，负责编排和交互
- Capability 是能力语言，负责声明可调用能力
- Plugin 是运行时载体，负责承接一个或多个 Capability

当前常见 Skill 契约：

```json
{
  "name": "meeting_summary",
  "input_schema": {}
}
```

推荐升级为：

```json
{
  "type": "skill",
  "name": "meeting_summary",
  "capability_ref": ["knowledge.retrieve"],
  "input_schema": {},
  "output_schema": {}
}
```

原则：
- Skill 不再直接绑定某个 API 或网关路径
- Skill 通过 `capability_ref[]` 引用一个或多个 Capability
- 底层路由切换时，优先修改 Capability，而不是修改上层 Skill

### 6.6 Capability Registry

Capability 应统一注册到注册表，而不是只散落在插件目录中。

推荐目录结构：

```text
registries/
└── capability-registry/
    ├── billing.get_bill.json
    ├── knowledge.retrieve.json
    └── template.parse.json
```

注册表示例：

```json
{
  "name": "billing.get_bill",
  "plugin": "billing-plugin",
  "version": "1.0.0",
  "status": "active",
  "capability": {
    "name": "billing.get_bill",
    "description": "获取用户账单信息",
    "version": "1.0.0",
    "namespace": "billing",
    "category": "atomic",
    "tags": ["billing", "finance"],
    "deprecated": false,
    "semantic": {
      "produces": ["billing_data"],
      "consumes": ["user_id"]
    },
    "routing": {
      "type": "http",
      "service": "billing-service",
      "endpoint": "/get_bill",
      "method": "POST",
      "environment": "prod",
      "timeout_ms": 3000
    },
    "effects": {
      "type": "read",
      "resources": ["billing_db"]
    },
    "quality": {
      "score": 0.9,
      "source": "manual"
    },
    "cost": {
      "level": "low"
    },
    "sla": {
      "latency_ms": 200,
      "availability": "99.9%"
    },
    "idempotent": true,
    "retryable": true
  },
  "source": {
    "schema_file": "registries/capability-registry/billing.get_bill.json"
  }
}
```

要求：
- 注册表至少能映射 `Capability -> Plugin`
- `status` 建议支持 `active|deprecated|disabled`
- Registry 建议保存“Capability 的可索引摘要”，而不是把 Planner 每次都回源到原始 Capability 文件
- `capability.semantic` 推荐只保留可检索名称，例如 `produces/consumes[].name` 的归一化结果
- `capability.routing` 推荐保留 `type/service/server/tool/endpoint/method` 等调度必要字段
- `capability.effects`、`capability.quality`、`capability.deprecated` 应进入索引层，供 Planner/Router 直接过滤和排序
- `source.schema_file` 应指向权威 Capability 描述文件，供运行时回源获取完整 typed semantic 与完整契约
- 注册表应支持按 `name`、`namespace`、`semantic`、`effects.type`、`quality.score`、`deprecated` 检索

配套模板文件：
- 推荐直接复用 [capability-registry.schema.json](file:///home/winlmp/code/ai-atomic-platform/services/ithqbot/ithqbot/docs/capability-registry.schema.json) 作为 Registry entry 模板或索引构建校验基线
- Registry 自动构建规则见 [registry-build-rules.md](file:///home/winlmp/code/ai-atomic-platform/services/ithqbot/ithqbot/docs/registry-build-rules.md)
- 当前落地示例见 `services/ithqbot/ithqbot/registries/capability-registry/`

设计原则：
- `capability.schema.json` 是权威能力声明，强调完整契约与 typed semantic
- `capability-registry.schema.json` 是注册索引声明，强调发现、过滤、排序和状态管理
- Registry 中的 `capability.semantic` 建议使用名称级摘要，避免把完整 typed semantic 重复存储多份
- 运行时若需要 `schema_ref/source/description` 等完整语义信息，应通过 `source.schema_file` 回源原始 Capability 文件

### 6.7 调用方式

不推荐：

```python
call_plugin("billing.get_bill")
```

推荐：

```python
await ctx.call_capability("billing.get_bill", {"user_id": user_id})
```

内部执行链路：

```text
Capability -> routing -> Plugin/MCP/API
```

原则：
- Skill 只调用 Capability，不直接耦合 Plugin 实现名
- Capability 解析与路由下沉到统一运行时
- 这样可以在不改 Skill 的前提下替换底层 API/MCP 实现

### 6.8 Planner 使用方式

Planner 选择 Capability 的核心依据不是插件名，而是语义、约束与调度指标。

示例逻辑：

```python
def find_capability(intent):
    return search_by_semantic(intent)
```

示例：
- 用户意图：`查账单`
- Planner 匹配：`semantic.produces[].name = billing_data`
- 最终选择：`billing.get_bill`

在多个候选 Capability 并存时，Planner 还可结合以下信号排序：
- `quality.score`
- `sla.latency_ms`
- `cost.level`
- `effects.type`
- `deprecated`

### 6.9 Capability 分类

#### 6.9.1 Atomic Capability

- 对应单一 MCP/API 或内部原子操作
- 例如：`billing.get_bill`、`user.get_profile`

#### 6.9.2 Composite Capability

- 对多个 Capability 做轻量封装
- 对 Planner 来说仍表现为一个可独立调度的能力节点

#### 6.9.3 AI Capability

- 包含 LLM 推理、分析或生成逻辑
- 例如：`report.generate`、`work.analyze`

### 6.10 与 Plugin 的关系

定义：
- `Plugin` = Capability 的运行载体
- `Capability` = Plugin 对外暴露的能力声明

示例：

```yaml
plugin: billing-plugin
capabilities:
  - billing.get_bill
  - billing.list_items
```

结论：
- 一个 Plugin 可以提供多个 Capability
- 一个 Capability 在同一时刻应只由一个生效实现负责路由
- 插件替换、灰度、迁移不应改变 Capability 的语义标识

### 6.11 向后兼容与迁移策略

现有系统无需整体推翻，推荐按以下顺序渐进升级：

1. 为现有插件补充 Capability 描述文件
2. 为现有 Skill 增加 `capability_ref[]`
3. 把执行入口从“直接调插件/API”迁移为“调用 Capability”
4. 增加 Capability Registry 支持发现、状态管理与检索

迁移后体系：

```text
ithqbot（决策）
   ↓
Skill（编排）
   ↓
Capability（能力抽象）
   ↓
Plugin（执行载体）
   ↓
MCP / API Gateway
   ↓
微服务
```

最终认知边界：
- Skill 是流程语言
- Capability 是能力语言
- Plugin 是运行时

## 7. SkillContext 与资源注入

### 7.1 自动注入资源

加载阶段（构造函数可接收）：
- `workspace` (`Path`)
- `config` (`Config`)

执行阶段（`execute` 自动注入）：
- `context` (`SkillContext`)

### 7.2 必须透传的上下文字段

Skill 运行过程中必须保留以下身份与追踪字段：
- `tenant_id`
- `account_id`
- `channel_user_id`
- `chat_id`
- `bot_id`
- `client_id`
- `trace_id`
- `request_msg_id`
- `priority`
- `skill_name`
- `metadata`

`metadata` 是统一透传容器，不得因 Skill 执行而丢失原始关键信息。

## 8. 进度、交互与可观测

### 8.1 进度上报

优先使用 `context.emit_progress(percent, stage, message, **kwargs)` 上报进度；若无 `context`，降级使用 `record_trace_event`。

Tool/Skill 进度上报应包含内部状态追踪字段（至少其一）：
- `progress_stage`
- `status_details`
- `tool_name`
- `skill_name`
- `call_type`

若使用 `record_trace_event`，`details` 中至少应包含 `stage` 与 `status_text`。

推荐最小载荷：

```python
await context.emit_progress(
    35,
    "processing",
    "正在处理",
    progress_stage="skill_call",
    call_type="skill",
    tool_name="your_tool_name",
    skill_name="your_skill_name",
    status_details={"execution": {"step": "parse_input"}}
)
```

若当前 Skill 正运行在 Graph 节点内，推荐同时透传以下链路字段，便于上层汇总成可视化调用树：

- `status_details.graph.graph_id`
- `status_details.graph.run_id`
- `status_details.node.node_id`
- `context.parent_run_id`

对于需要参与 Planner / Graph 级统计的能力，建议在 `details` 或 `status_details.execution` 中显式暴露：

- `idempotent`
- `retryable`

这样 Trace 汇总页可以直接统计“哪些请求主要由可重试/幂等能力组成”，也能在失败复盘时快速判断是否适合自动重放。

约束：
- `percent` 必须在 `0~100` 区间
- 阶段应单调推进，不应出现明显回退（如 finalizing 后再回到 initializing）
- `status_details` 不得包含密钥、令牌、口令、完整证件号等敏感数据
- `status_details` 应聚焦“执行状态”，避免存放大体积原文内容

推荐阶段：
- `initializing`
- `searching`
- `processing`
- `analyzing`
- `finalizing`

### 8.2 交互式能力

需二次确认/补充信息时，统一使用 `message.interaction` 结构。

字段约束：
- `type` 允许 `select|otp|confirm|form|table|file_upload|graph|rich_text`
- `version` 固定 `v1`
- `interaction_id` 必填，用于客户端回填
- `sensitive=true` 时必须按敏感输入处理

### 8.3 Graph/Planner 场景补充规范

当 Skill 被 Graph 节点调用，或参与 Planner 生成-执行链路时，除 8.1 的通用进度字段外，还应补充图运行可追踪信息。

详细设计、触发入口与运行流程见 `GRAPH_SKILL_DESIGN.md`。

推荐要求：
- Graph 节点上下文应保留 `parent_run_id`，其值建议等于当前 `graph.run_id`
- Graph 节点上下文的 `metadata.graph` 应至少包含 `graph_id`、`run_id`、`node_id`
- `status_details` 中包含 `graph.run_id`（当前图运行 ID）
- `status_details` 中包含 `graph.graph_id`（图 ID）与 `graph.node_id`（当前节点）
- 节点执行开始/结束/失败时均有可观测事件，失败事件需包含可读失败原因
- 若节点返回“等待用户输入”态，需显式上报 `waiting_for_input`（或等价阶段）并带可恢复标识
- 并行节点中若仅部分节点进入等待态，不应无条件中断其他可继续执行的独立分支

推荐最小载荷（Graph 节点执行）：

```python
await context.emit_progress(
    60,
    "processing",
    "图节点执行中",
    progress_stage="graph_node_running",
    call_type="graph",
    tool_name="your_tool_name",
    skill_name="your_skill_name",
    status_details={
        "graph": {
            "run_id": "graph-xxx",
            "graph_id": "approval_flow",
            "node_id": "step2",
            "node_status": "running",
        }
    },
)
```

说明：
- `GraphExecutor` 若已负责节点上下文封装，Skill 实现应直接复用注入的 `context.parent_run_id` 与 `context.metadata["graph"]`
- 若节点进入等待态，推荐使用 `graph_waiting` 或等价阶段；恢复执行后再回到 `graph_running`

## 9. 文件与对象存储标准

运行原则：
- Skill 保持无状态，不依赖长期本地 workspace
- 临时文件使用 `tempfile` 系列
- 需持久化/回传的文件统一写对象存储（MinIO、S3 或兼容实现）
- Skill/Tool 不得直接在业务代码中自行实例化底层 SDK 客户端，必须统一通过 `ithqbot.storage` 提供的对象存储接口或工厂获取客户端
- Skill/Tool 不得把某个具体品牌对象存储当作能力边界；`MinIO/S3` 只是底层后端，业务层只依赖统一的对象存储抽象

返回结构建议：
- `status`
- `message`
- `data`
- `files[]`

`files[]` 建议包含：
- `name`
- `mime`
- `size`
- `rel_path`
- `storage_uri`
- `storage_backend`
- `storage_bucket`
- `storage`

兼容策略：
- 新消费方优先读取 `files[]`
- `storage_uri` 作为统一对象存储地址，推荐使用 `minio://` 或 `s3://` scheme
- `minio_uri`、`s3_uri` 可作为兼容字段保留，但新实现不应只暴露品牌特定字段
- `file_meta` 仅作为单文件兼容字段保留

## 10. 引用回复链路

Skill 的进度、交互、最终结果必须绑定触发消息：
- 首选 `request_msg_id`
- 兼容 `reply_to`
- Kafka Header 使用 `parent_msg_id`

同一问题线程内必须保证：
- 进度/交互/结果按 `request_msg_id` 聚合
- 最终结果支持去重并保留最新版本

## 11. 模型使用与安全要求

- 不得硬编码 API Key
- 不得硬编码固定模型名
- 模型必须经 `context.call_llm(task=...)` 调用
- 可读取 `metadata["_routing"]` 做观测，不得覆写路由决策关键字段
- 不得破坏保留元数据字段（如 `attachments/files/file_meta/request_msg_id`）

## 12. 质量门槛（发布前必过）

- 至少 1 个 happy path 测试
- 至少 1 个 error path 测试
- 属于某个 Skill 实现细节的单元测试，优先放在 `ithqbot/skills/<skill_name>/tests/`
- 跨 Skill、跨 Agent 或跨渠道的集成测试，保留在公共 `ithqbot/tests/`
- 错误返回可读，不向终端用户暴露裸异常堆栈
- 若 Skill 存在非 LLM HTTP 调用，需复用共享错误分类器（如 `ithqbot.utils.http_client.classify_http_client_error()`）或输出等价的统一错误结构，避免每个 Skill 各自定义超时/限流/5xx 语义
- 文件结果可下载或可定位
- `request_msg_id/reply_to/metadata` 链路完整
- 额外依赖需在 `config/requirements.txt` 或 `config/plugin.json` 显式声明
- 必须通过代码缺陷静态检查（如未受控异常吞噬、危险反射执行、弱加密算法）
- 必须通过基础安全风险检查（如硬编码密钥、命令注入风险、TLS 校验被关闭、JWT 关闭签名校验、不安全反序列化）
- 必须通过外部链接安全检查（如明文 HTTP 外链、IP 直连外链、未评审硬编码第三方链接）
- 必须满足工具内部状态可追踪要求（进度上报含 `progress_stage/status_details/tool_name/skill_name/call_type` 或等价字段）
- 若 Skill 以压缩包交付，压缩包内不得包含路径穿越、软链接/硬链接文件

判定建议：
- `strict=false`：缺少内部状态字段按 `WARN` 处理，可发布但需整改排期
- `strict=true`：缺少内部状态字段按 `FAIL` 处理，阻塞发布

## 13. 兼容性要求（Claude/OpenClaw）

ithqbot Skill 标准是 Claude/OpenClaw 风格的工程化超集：
- 保留 `SKILL.md` 作为入口
- 支持 YAML frontmatter
- 支持 `metadata.openclaw` 与 `metadata.ithqbot`
- 保持目录结构可渐进演进，不阻断已有 Skill 的迁移

## 14. 执行优先级建议

新开发默认遵循：
1. 先写 `SKILL.md` 明确语义与边界
2. 再定义 `schema.json` 或 `tool/tool_def.json`
3. 最后实现 `tool/tool.py` 并补测试，优先就近放到 `skills/<skill_name>/tests/`

旧 Skill 改造时优先补：
1. `request_msg_id` 链路
2. `context.emit_progress` 进度
3. `files[]` 标准化输出

## 15. 重点实践样例（必须掌握）

以下三类样例是 Skill 开发的高频错误点与必备基线，必须优先按本节实现。

### 15.1 使用透传模型，不自行配置 API_KEY

错误方式：
- Skill 内直接读取环境变量 API_KEY
- Skill 内硬编码模型名和 SDK 客户端

正确方式：
- 只通过 `context.call_llm(task=...)` 调用模型
- 模型映射由系统配置 `agents.purposes` 与 `agents.defaults.model` 决定

模型配置样例（`~/.ithqbot/config.json`）：

```json
{
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5"
    },
    "purposes": {
      "reasoning": "anthropic/claude-3-5-sonnet",
      "extraction": "openai/gpt-4o-mini",
      "vision": "openai/gpt-4o",
      "embedding": "openai/text-embedding-3-small",
      "tool_calling": "openai/gpt-4o"
    }
  }
}
```

Tool 代码样例：

```python
from typing import Any, Optional, TYPE_CHECKING
from pydantic import BaseModel
from ithqbot.agent.tools.base import Tool

if TYPE_CHECKING:
    from ithqbot.agent.skills.base import SkillContext

class RiskSummary(BaseModel):
    summary: str
    risks: list[str]

class ContractReviewTool(Tool):
    @property
    def name(self) -> str:
        return "contract_review"

    @property
    def description(self) -> str:
        return "审核合同并输出风险摘要"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string"}
            },
            "required": ["content"]
        }

    async def execute(self, content: str, context: Optional["SkillContext"] = None, **kwargs: Any) -> dict[str, Any]:
        if context is None:
            raise RuntimeError("context is required")
        answer = await context.call_llm(
            task="reasoning",
            system_prompt="你是法务助手，请输出风险点清单。",
            prompt_template=(
                "请分析以下合同内容并输出 JSON："
                '{"summary":"...","risks":["..."]}\\n\\n合同内容:\\n{content}'
            ),
            template_vars={"content": content},
            output_model=RiskSummary,
            retries=2,
        )
        return {
            "status": "success",
            "message": "ok",
            "data": {"summary": answer.summary, "risks": answer.risks},
        }
```

### 15.2 共享对象存储文件，统一 `files[]` 返回

目标：
- Skill 产物可被前端、下游服务、消息通道复用
- 结果不依赖本地临时路径

推荐返回载荷样例：

```json
{
  "status": "success",
  "message": "报告已生成",
  "data": {
    "report_id": "cmp-20260411-001"
  },
  "files": [
    {
      "name": "diff_report.md",
      "mime": "text/markdown",
      "size": 18342,
      "rel_path": "u100/c200/reports/cmp-20260411-001/diff_report.md",
      "storage_uri": "minio://ithqbot-storage/u100/c200/reports/cmp-20260411-001/diff_report.md",
      "storage_backend": "minio",
      "storage_bucket": "ithqbot-storage",
      "minio_uri": "minio://ithqbot-storage/u100/c200/reports/cmp-20260411-001/diff_report.md",
      "download_url": "https://example.com/download/cmp-20260411-001",
      "storage": {
        "backend": "minio",
        "bucket": "ithqbot-storage",
        "path": "u100/c200/reports/cmp-20260411-001/diff_report.md"
      }
    }
  ]
}
```

实现要点：
- 持久化文件后返回 `files[]`
- 同时提供 `rel_path` 与 `storage_uri`
- 若需兼容旧链路，可附带 `minio_uri` 或 `s3_uri`
- 兼容消费方可读取 `storage`，新接入优先读取顶层字段

### 15.3 用户与会话信息透传，不丢字段

目标：
- Skill 结果可审计、可追踪、可正确归并到原问题线程
- 多租户、多渠道场景保持上下文一致

Tool 代码样例：

```python
from typing import Any, Optional, TYPE_CHECKING
from ithqbot.agent.tools.base import Tool

if TYPE_CHECKING:
    from ithqbot.agent.skills.base import SkillContext

class TicketSyncTool(Tool):
    @property
    def name(self) -> str:
        return "ticket_sync"

    @property
    def description(self) -> str:
        return "同步工单并回传线程绑定结果"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"}
            },
            "required": ["ticket_id"]
        }

    async def execute(self, ticket_id: str, context: Optional["SkillContext"] = None, **kwargs: Any) -> dict[str, Any]:
        if context is None:
            raise RuntimeError("context is required")
        identity = {
            "tenant_id": context.tenant_id,
            "account_id": context.account_id,
            "chat_id": context.chat_id,
            "bot_id": context.bot_id,
            "client_id": context.client_id,
            "trace_id": context.trace_id,
            "request_msg_id": context.request_msg_id
        }
        metadata = dict(context.metadata or {})
        await context.emit_progress(20, "processing", "正在同步工单", content_preview=ticket_id)
        return {
            "status": "success",
            "message": "同步完成",
            "data": {
                "ticket_id": ticket_id,
                "identity": identity,
                "metadata": metadata
            }
        }
```

必须遵循：
- 返回结果中保留 `request_msg_id` 关联语义
- 不删除或覆盖上游透传 `metadata` 关键字段
- 所有进度/交互/结果事件使用同一线程标识归并
