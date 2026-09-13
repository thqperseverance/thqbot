# Capability Registry Build Rules

本文档定义如何从权威 Capability 描述自动生成 Capability Registry 索引。

目标：
- 统一 Capability 到 Registry 的构建规则
- 支持从 `capability.schema.json` 直接生成索引
- 兼容当前仓库中仍存在的 `schema.json` 与 `tool/tool_def.json`
- 保证 Planner、Router、Registry UI 使用的索引字段稳定一致

## 1. 输入源优先级

Registry 构建器按以下优先级选择输入源：

1. 专用 Capability 文件
   - 符合 [capability.schema.json](file:///home/winlmp/code/ai-atomic-platform/services/ithqbot/ithqbot/docs/capability.schema.json) 的能力声明文件
2. Skill 契约文件
   - `skills/<skill_name>/schema.json`
   - `skills/<skill_name>/tool/tool_def.json`
3. 其他兼容来源
   - 后续如引入 `capability.yaml` 或数据库注册中心，可扩展到本规则之外

原则：
- 若同一能力同时存在专用 Capability 文件与旧 Skill 契约文件，以专用 Capability 文件为准
- 若只有旧 Skill 契约文件，允许生成“兼容索引记录”，但应在后续逐步迁移为正式 Capability 文件

## 2. 输出目标

每个 Capability 生成一个 Registry entry：

```text
registries/
└── capability-registry/
    └── <capability_name>.json
```

输出结构必须符合 [capability-registry.schema.json](file:///home/winlmp/code/ai-atomic-platform/services/ithqbot/ithqbot/docs/capability-registry.schema.json)。

## 3. 基本映射规则

### 3.1 顶层字段映射

| Registry 字段 | 来源 | 规则 |
|---|---|---|
| `name` | Capability `name` | 直接复制 |
| `plugin` | Capability 所属插件/Skill 目录名 | 若无独立 Plugin 概念，使用当前 Skill 目录名 |
| `version` | Capability `version` | 直接复制；若旧文件缺失，默认 `1.0.0` |
| `status` | Registry 生命周期状态 | 默认 `active`；若 `deprecated=true` 可置为 `deprecated` |
| `updated_at` | 构建时间或源文件更新时间 | 推荐使用 RFC 3339 时间 |
| `source.schema_file` | 权威源文件路径 | 记录构建输入文件路径 |
| `source.checksum` | 文件内容哈希 | 推荐使用 `sha256:<hex>` |

### 3.2 `capability` 索引摘要映射

Registry 中的 `capability` 是面向检索与调度的摘要层，不要求重复存储完整 typed semantic。

必须保留：
- `name`
- `description`
- `version`
- `namespace`
- `semantic`
- `routing`

推荐保留：
- `category`
- `tags`
- `deprecated`
- `dependencies`
- `auth`
- `sla`
- `cost`
- `effects`
- `quality`
- `idempotent`
- `retryable`

## 4. Capability 到 Registry 的具体转换

### 4.1 直接复制字段

以下字段可直接复制到 `capability` 摘要层：

- `name`
- `description`
- `version`
- `namespace`
- `category`
- `tags`
- `deprecated`
- `dependencies`
- `auth`
- `sla`
- `cost`
- `effects`
- `quality`
- `idempotent`
- `retryable`

### 4.2 `semantic` 降维规则

权威 Capability 文件中的 `semantic` 为 typed semantic，例如：

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

写入 Registry 时，应降维为名称级摘要：

```json
{
  "semantic": {
    "produces": ["billing_data"],
    "consumes": ["user_id"]
  }
}
```

规则：
- `semantic.produces[]` 提取每个对象的 `name`
- `semantic.consumes[]` 提取每个对象的 `name`
- 去重后写入索引
- 保留顺序，优先保持源文件语义顺序

原因：
- Registry 主要用于检索和排序，不需要完整 typed semantic
- 完整 `schema_ref/source/description` 应通过 `source.schema_file` 回源获取

### 4.3 `routing` 摘要规则

Registry 中保留调度必要字段：

- `type`
- `service`
- `endpoint`
- `method`
- `server`
- `tool`
- `environment`
- `timeout_ms`

规则：
- 仅保留运行时调度需要的字段
- 若源文件中包含更细节的网关、认证透传配置，不建议直接写入 Registry

## 5. 兼容旧 Skill 契约的构建规则

当前仓库仍有大量 `schema.json` / `tool/tool_def.json`。构建器应支持兼容映射。

### 5.1 `name` 规范化

旧 Skill 契约通常类似：

```json
{
  "name": "retrieve_knowledge"
}
```

构建 Registry 时，推荐将其规范化为标准 Capability 名称，例如：

- `retrieve_knowledge` -> `knowledge.retrieve`
- `file_to_markdown` -> `document.to_markdown`
- `doc_compare` -> `document.compare`

规则：
- 优先读取显式 Capability 映射配置
- 若无映射配置，可采用人工维护的命名映射表
- 不建议仅靠字符串切分自动猜测 `namespace/action`，因为误判成本高

### 5.2 `version` 回填

若旧 Skill 契约中无 `version`：
- 默认写入 `1.0.0`
- 同时建议在迁移任务中补充正式 Capability 文件

### 5.3 `namespace` 推断

若旧 Skill 契约中无 `namespace`：
- 优先根据映射表推断
- 若无映射表，可根据能力名人工指定

### 5.4 `semantic` 兼容

若旧 Skill 契约中 `semantic` 仍是字符串数组：

```json
{
  "semantic": {
    "produces": ["knowledge_answer", "knowledge_hits"],
    "consumes": ["user_query", "knowledge_domain"]
  }
}
```

则可直接写入 Registry 的名称级摘要层，无需再次降维。

### 5.5 `routing` 兼容

若旧 Skill 契约没有独立 `routing`：
- 可写入最小内部路由摘要，例如：

```json
{
  "routing": {
    "type": "internal"
  }
}
```

- 若已知该 Skill 实际走某个服务或 MCP，也可在构建配置中补充 `service/server/tool`

### 5.6 `effects` 与 `quality` 缺省策略

若旧 Skill 契约中缺失这些字段：
- `effects` 不自动猜测，除非已有明确规则
- `quality` 不自动伪造，除非已有人工配置或质量指标来源
- 缺省时可不写入索引

原则：
- Registry 可以缺少部分增强字段
- 但不应生成不真实的副作用或评分数据

## 6. 状态计算规则

推荐规则：

- `deprecated=true` 且未显式关闭时，`status=deprecated`
- 显式禁用时，`status=disabled`
- 默认 `status=active`

状态优先级：

```text
disabled > deprecated > active
```

## 7. 校验规则

构建产物至少应通过以下检查：

1. 输入文件可被对应 Schema 解析
2. 产出的 Registry entry 必须通过 [capability-registry.schema.json](file:///home/winlmp/code/ai-atomic-platform/services/ithqbot/ithqbot/docs/capability-registry.schema.json)
3. `name` 必须等于输出文件名去掉 `.json`
4. `source.schema_file` 必须存在且可读取
5. 若 `category=composite`，`dependencies[]` 至少 1 个
6. 若存在 `quality.score`，必须在 `0~1`
7. 若存在 `effects.type`，必须同时提供 `effects.resources[]`

## 8. 构建步骤建议

推荐流程：

1. 扫描输入源
2. 解析权威 Capability 文件或旧 Skill 契约
3. 做命名映射与兼容归一化
4. 提取 Registry 索引摘要
5. 生成 `source.schema_file` 与 `checksum`
6. 写出到 `registries/capability-registry/<name>.json`
7. 对产物运行 Schema 校验

当前仓库参考实现：

```bash
python3 services/ithqbot/ithqbot/scripts/build_capability_registry.py
```

该脚本会：
- 扫描 `skills/*/schema.json` 与 `skills/*/tool/tool_def.json`
- 按命名映射和兼容默认值生成 Registry entry
- 使用 `capability-registry.schema.json` 做校验
- 写出到 `services/ithqbot/ithqbot/registries/capability-registry/`

若需要补齐正式 Capability 文件，可执行：

```bash
python3 services/ithqbot/ithqbot/scripts/build_capability_registry.py --write-capabilities
```

该模式会：
- 基于旧 Skill 契约生成 `skills/<skill_name>/capability.json`
- 先使用 `capability.schema.json` 校验正式声明
- 再优先以 `capability.json` 为权威来源生成 Registry entry

## 9. 示例映射

### 9.1 `knowledge_retrieval`

源文件：
- `skills/knowledge_retrieval/tool/tool_def.json`

推荐映射：
- 旧名：`retrieve_knowledge`
- 新 Capability 名：`knowledge.retrieve`
- Plugin：`knowledge_retrieval`

### 9.2 `doc_compare`

源文件：
- `skills/doc_compare/schema.json`

推荐映射：
- 旧名：`doc_compare`
- 新 Capability 名：`document.compare`
- Plugin：`doc_compare`

## 10. 迁移建议

推荐按以下三个阶段推进，而不是一次性切换：

### 阶段 1：先建立兼容索引层

目标：
- 先让现有 Skill 可以被 Registry 统一发现、检索和调度
- 在不改现有 Skill 执行逻辑的前提下，快速产出可用的 Registry entry

执行方式：
- 从 `schema.json` 或 `tool/tool_def.json` 生成兼容 Registry entry
- 通过命名映射表把旧 Skill 名归一化为标准 Capability 名
- 缺失的 `version/status/routing` 使用本规范定义的兼容默认值

阶段完成标准：
- 核心 Skill 已有对应 Registry entry
- 所有 Registry entry 通过 `capability-registry.schema.json` 校验
- Planner/Router 已可基于 Registry 做基础发现和过滤

### 阶段 2：逐步补齐正式 Capability 文件

目标：
- 把兼容索引逐步升级为“由正式 Capability 声明驱动”的索引体系
- 让 `semantic/routing/effects/quality/dependencies` 等字段从临时兼容值转为权威声明

执行方式：
- 为高频或高价值 Skill 优先补齐正式 Capability 文件
- 将 typed semantic、服务发现路由、副作用、质量评分等字段写入权威 Capability
- Registry 构建时优先读取正式 Capability 文件，旧 Skill 契约仅作为兜底来源

阶段完成标准：
- 核心高价值能力已有正式 Capability 文件
- 新增能力默认不再只写 Skill 契约，而是同步提供 Capability 声明
- Registry 中大部分关键字段已不再依赖兼容猜测或默认回填

### 阶段 3：切换为 Capability-only 构建

目标：
- 让 Registry 完全由正式 Capability 文件驱动
- 彻底收敛 Skill 契约与 Registry 索引之间的语义漂移

执行方式：
- 停止从纯 `schema.json/tool_def.json` 直接生成新的 Registry entry
- 对未迁移能力设置告警或阻塞策略
- 将命名映射表从“主路径”降级为“迁移遗留兼容层”

进入条件：
- 绝大多数在线能力已具备正式 Capability 文件
- 兼容构建产物占比已降到可接受范围
- 构建链路、校验链路和运行时回源链路均已稳定

阶段完成标准：
- Registry 只从正式 Capability 文件构建
- 旧 Skill 契约不再承担 Registry 主数据源角色
- Capability 成为 Planner、Router、Registry、Plugin 之间的唯一能力事实来源

这样可以在不阻断现有系统的前提下，把迁移过程拆成“先可用、再规范、后收敛”的三段式演进路径。
