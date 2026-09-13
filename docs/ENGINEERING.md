# thqbot 工程细节

> 这是本仓库的**工程侧**文档：测试、缺陷修复、部署偏差与逐项实现说明。
> 想先了解产品与架构，请回到 [README](../README.md) 与 [ARCHITECTURE.md](ARCHITECTURE.md)。

一个可运行的最小可用产品：**Web 端多轮对话（可调用 skills）+ Kafka + Redis + PostgreSQL + Agent 平台**。

本项目基于一份上游 agent 平台源码重建（内部代号 MVP），重建过程按一份**内部决策清单**逐条执行；
该清单属于工程管理文档，未随仓库发布。所有关键改动都配有单元测试。

---

## 1. 这是什么

| 需求 | 实现 |
|------|------|
| Web 端多轮对话 | 单页 React 应用 + FastAPI 后端；会话与消息持久化到 PostgreSQL |
| 调 skills | ithqbot AgentLoop + 12 个内置技能工具；技能调用过程实时可见 |
| Kafka | `icatmsg_inbound` / `icatmsg_outbound`（各 3 分区），网关生产 + 消费，agent 消费 + 生产 |
| Redis | 去重（带 TTL）、SSE 推送通道、进度状态缓存；**不再作为会话主存储** |
| PostgreSQL | 会话、消息、用户、附件元数据；以及 ithqbot 的 session / memory / cron / checkpoint |
| Agent 平台 | ithqbot runtime：AgentLoop、技能加载、工具注册、多 provider、MCP、cron |
| 附件（MinIO） | 上传 / 下载（归属校验）/ 列表 / 删除；随消息传给 agent，技能可直接从对象存储读取 |

**架构**

```
浏览器 (React)
   │  HTTP /api/*            SSE /api/stream
   ▼
gateway（单应用后端，FastAPI）
   ├─ 登录鉴权（PG 用户 + 签名 Cookie）
   ├─ 会话/消息 CRUD ──────────► PostgreSQL（唯一事实来源）
   ├─ 附件上传/下载 ───────────► MinIO（键含 user_id；归属按 PG 记录校验）
   ├─ 出站消费 ─► PG 落库 ─► Redis Pub/Sub ─► SSE
   └─ 入站生产（信封 v1.2，分区键 tenant|bot|account|chat，可带 attachments）
            │
            ▼  Kafka: icatmsg_inbound
     ithqbot runtime（agent worker）
       AgentLoop ─► 技能/工具 ─► LLM（OpenAI 兼容）
                     └─ 技能从同一个 MinIO bucket 直接读取附件
            │
            ▼  Kafka: icatmsg_outbound（status.processing / message.reply / …）
       回到 gateway 出站消费
```

---

## 2. 快速开始

### 前置条件

- Docker Desktop 已启动
- 宿主已有 Redis（默认 `127.0.0.1:6379`）——本项目复用，不重复部署
- 一个 OpenAI 兼容的模型端点（base_url / api_key / 模型名）
- MinIO 镜像已本地导入（`minio/minio:latest`；Docker Hub 直连在本网络被拒）

### 步骤

```bash
cp .env.example .env
# 编辑 .env：填 APP_LLM_BASE_URL / APP_LLM_API_KEY / APP_LLM_MODEL

docker compose up -d postgres kafka kafka-init minio   # 基础设施
python scripts/init_minio.py                           # 创建 bucket（幂等）
docker compose up -d --build                           # 构建并启动 gateway + ithqbot

# 浏览器打开 http://127.0.0.1:8090  （默认账号 admin / admin123456）
```

`scripts/init_minio.py` 需要能读到 `.env` 里的 `MINIO_*`（可用 `scripts/dev-local.ps1` 的环境加载方式，
或手工 export）。`.env.example` 里已给出全部变量说明。

### 验证

```bash
python scripts/e2e_chat.py            # 端到端冒烟：登录 → 发消息 → 等 agent 回复
python scripts/e2e_chat.py --message "用 text_stats 工具统计：你好世界"
python scripts/verify_attachment.py   # 附件往返：上传 → 下载比对字节 → 未认证被拒 → 删除后 404
```

### 本机调试模式（不用容器跑业务进程）

```powershell
docker compose up -d postgres kafka kafka-init minio
.\scripts\dev-local.ps1 -Target render     # 渲染 ithqbot 的 config.json
.\scripts\dev-local.ps1 -Target gateway    # 前台启动 gateway（:8090）
.\scripts\dev-local.ps1 -Target worker     # 另一个终端：启动 agent worker
```

---

## 3. 演示（4 个场景，均已实测通过）

| # | 提示词 | 预期 |
|---|--------|------|
| 1 | `用 text_stats 工具统计这句话：thqbot 是一个 agent 平台，支持多轮对话与技能调用。` | 返回真实统计结果；消息 `meta.progress.skills == ["text_stats"]` |
| 2 | `请用 check_skill 工具检查 text_stats 这个技能是否符合 SKILL_STANDARDS 标准` | 返回 PASS/WARN 合规报告；`progress.skills == ["check_skill"]` |
| 3 | `请用 read_file 读取 <技能 SKILL.md 的绝对路径>，然后用中文说明这个技能具备什么能力` | 准确复述 SKILL.md；`progress.tools == ["read_file"]` |
| 4 | 在界面上「＋ 附件」上传 `services/ithqbot/workspace/docs/spec-v1.md` 与 `spec-v2.md`，然后发：`请对比这两份文档的差异` | 技能**直接从 MinIO 读取两份文档**并输出逐点差异；`progress.tools == ["doc_compare"]` |

第 4 个场景也可用脚本一键复现：

```bash
python scripts/e2e_chat.py --timeout 240 \
  --attach services/ithqbot/workspace/docs/spec-v1.md \
  --attach services/ithqbot/workspace/docs/spec-v2.md \
  --message "请对比这两份文档的差异"
```

> 技能调用可见性的实现：ithqbot 的进度元数据带一等字段 `_tool_name` / `_skill_name` / `_call_type`，
> gateway 把它们累计写入 Redis 状态缓存（跨事件做并集，避免最后那个 `finalizing` 事件把技能名抹掉），
> 最终落到回复消息的 `meta.progress` 上，同时通过 SSE 实时推给前端。

### 附件链路怎么工作

1. 前端「＋ 附件」→ `POST /api/conversations/{id}/files`（multipart）；
2. 网关把文件写入 **与 ithqbot 共用的那个 bucket**，对象键为
   `uploads/{user_id}/{conversation_id}/{file_id}{ext}`，并把元数据登记到 PostgreSQL 的 `files` 表；
3. 发消息时带上 `file_ids`，网关把每条附件展开成规范描述（`name` / `mime` / `size` /
   `rel_path` / `storage_uri` / `storage{backend,bucket,path}`）放进信封的 `payload.attachments`
   （同时冗余到 `metadata.attachments`），ithqbot 收到后会提升为消息 metadata 的 `attachments`；
4. 技能侧无需任何额外拷贝 —— `doc_compare` 之类的技能直接用
   `minio://<bucket>/<object_path>` 去对象存储读取（走 `ithqbot.storage`）；
5. 下载走 `GET /api/files/{file_id}`：先按 `user_id + file_id` 查 PG 记录，再用记录里的
   对象路径取流。**不使用"路径前缀"这类弱校验**，越权一律 404（不区分"不存在"与"不属于你"）。

---

## 4. API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录（设置 HttpOnly + SameSite=Lax 签名 Cookie） |
| POST | `/api/auth/logout` | 登出 |
| GET | `/api/auth/me` | 当前用户 |
| GET | `/api/session/context` | 用户 / bot / 未读数（顺带确保有默认会话） |
| GET | `/api/config` | 前端运行配置：模型清单、租户、权限范围、附件上限（驱动模型下拉与权限提示） |
| GET | `/api/conversations` | 会话列表 |
| POST | `/api/conversations` | 新建会话 |
| GET | `/api/conversations/{id}/messages` | 消息分页（`limit` ≤200、`before_id` 游标） |
| POST | `/api/conversations/{id}/messages` | 发送消息（落库 → Kafka） |
| POST | `/api/conversations/{id}/read` | 清未读 |
| DELETE | `/api/conversations/{id}/messages` | 删除消息 |
| POST | `/api/conversations/{id}/files` | 上传附件（multipart `file`）→ MinIO，登记到 PG |
| GET | `/api/conversations/{id}/files` | 该会话的附件列表 |
| GET | `/api/files/{file_id}` | 下载附件（**按 PG 记录做归属校验**，越权 404） |
| DELETE | `/api/files/{file_id}` | 删除附件（同时删对象与元数据） |
| GET | `/api/stream` | **SSE**：`ready` / `message` / `status` 事件 |
| GET | `/health` `/ready` | 健康检查（`/ready` 逐项检查 PG / Redis / Kafka 消费者） |

---

## 5. 测试

```bash
# gateway（隔离式单测：临时 SQLite + 内存 Redis/对象存储假实现，不依赖任何外部服务）
cd services/gateway && ../../.venv/Scripts/python.exe -m pytest      # 106 passed

# ithqbot：本次新增/相关的测试
cd services/ithqbot
../../.venv/Scripts/python.exe -m pytest ithqbot/tests/test_read_file_tool.py \
    ithqbot/tests/test_skill_discovery.py \
    ithqbot/tests/test_custom_provider_no_proxy.py \
    ithqbot/skills/text_stats/tests/test_tool.py                     # 49 passed（连同上面三个文件）

# 前端：Markdown 解析用例 + 组件 SSR 渲染烟测（无需浏览器）
cd web && npm test                                                   # 18 + 17 passed
npm run build                                                        # tsc 类型检查 + 产物构建
```

覆盖点：口令哈希与会话令牌、PG 数据访问与归属校验、Kafka 信封契约、出站消费幂等、
进度累计与技能可见性、SSE 鉴权、运行配置（模型清单顺序/去重、附件上限换算）、
`read_file` 沙箱（越界/截断/行范围）、技能发现与工具注册、
NO_PROXY 清理、`text_stats` 统计逻辑、附件上传/下载/删除/越权拒绝/大小限制/文件名安全/
信封与消息 meta 传播、S3 客户端 region 兼容；前端侧覆盖 Markdown 解析（标题/列表嵌套/表格/
代码块/危险链接剥离）与组件真实渲染（列表嵌套结构、附件与技能 chips、文件变更卡片、
用量/耗时元信息行、功能胶囊、顶栏面包屑与 Tab、轨迹时间线、侧边栏与设置入口、危险协议不落地），
以及**服务端返回的 CSS 逐项断言**（设计令牌与关键几何值压缩后仍可正则匹配）。

---

## 6. 相对决策清单的偏差（均已确认或说明）

| 事项 | 实现与原始计划的偏差 |
|------|------|
| 基础设施由 compose 自建全套 | **部分**：PostgreSQL / Kafka / MinIO 自建；**Redis 刻意不自建**，复用宿主已有容器（容器内经 `host.docker.internal:6379` 访问）。MinIO bucket 由 `scripts/init_minio.py` 或网关启动时自动创建 |
| 用最简单的 2-3 个技能做演示 | **扩展为 4 个**：`text_stats`（自研，确定性）+ `check_skill`（存量）+ `read_file` + `doc_compare`（附件链路打通后已可演示） |
| 修复附件下载授权 | **以设计实现**：附件下载先按 `user_id + file_id` 查 PG 记录，再用记录中的对象路径取流；不依赖路径前缀，越权一律 404 |
| 网关接入 PostgreSQL | 按语义实现：**重写存储 + 编排层**（`repository` / `orchestrator`），保留 Kafka 适配层（`source_adapter.py`） |
| 演示环境不轮换密钥 | 遵守；`.env` 入 `.gitignore`，`services/ithqbot/config.json` 由模板渲染生成（同样不入库）。**上线前请务必替换 `APP_SESSION_SECRET` 与数据库口令** |

---

## 7. 本次修复的上游缺陷

重建过程中发现并修复了 6 个会直接阻断运行的问题，均已加测试或验证：

| # | 问题 | 影响 | 处理 |
|---|------|------|------|
| 1 | `agent/runtime/executor.py` 的 `_invoke_node_once` 使用未定义的 `cancellation_token`（只在 `_execute_node` 内定义） | **所有**工具/技能执行都抛 `NameError` —— "调 skills" 完全不可用 | 在该方法内从 `skill_context` 取回 token；`test_message_tool_suppress.py` 失败数从 20 → 10（其余 10 个在原始源码上同样失败） |
| 2 | ithqbot 的 `--config` 只当作 bootstrap，真正的配置由 `FileConfigStore(<目录>).load("default")` 读 `<目录>/config.json` | 渲染出的配置被静默忽略，进程连到错误的数据库 | 渲染结果直接写成 `config.json`；`render_config.py` 增加必填环境变量校验，缺失即报错而非产出空配置 |
| 3 | `session_scope=account_chat` 时必须提供 `sharedWorkspaceRoot` | worker 消费消息即报错，回复永远不产生 | 参数化 `${ITHQBOT_SHARED_WORKSPACE}` |
| 4 | httpx 无法解析 `NO_PROXY` 中的 `[::1]`（系统默认值含它） | **所有** LLM 调用失败：`InvalidURL: Invalid port: ':1]'` | 在 `CustomProvider` 构造客户端前就地清理 NO_PROXY；附 12 个单测 |
| 5 | `s3_compat.py` 把 endpoint 的 hostname 当作 AWS region（`region_name=parsed.hostname`） | 自建 MinIO 的 endpoint 基本都是 `IP:端口`，boto3 直接抛 `InvalidRegionError`，**附件功能完全不可用** | 改用合法的 `us-east-1` 缺省值并支持显式 `region`；附 11 个单测 |
| 6 | 字体文件被发成 `text/plain`（Windows 的 `mimetypes` 注册表不含 woff2） | 严格 CSP 或 `X-Content-Type-Options: nosniff` 下浏览器会**拒绝加载字体** | 启动时显式注册 `font/woff2` 等类型；附 3 个单测 |

另外修复了旧网关的两个已知问题：出站消费 `auto_offset_reset` 由 `latest` 改为 `earliest`（网关重启期间的回复不再丢）、
畸形消息的提交路径 bug（原本会卡住消费位点）。

---

## 8. 未完成 / 后续

- **附件的生产级增强**：目前是"网关流式转发下载"（每次都要带上会话 Cookie）。可选的后续：
  预签名 URL 直连 MinIO、按用户/会话的容量配额、类型白名单与病毒扫描、附件保留期清理。
  另外 ithqbot 自带的 `file_api.FileService`（按 tenant/account/bot/chat/request 分层的元数据布局）
  尚未接入——当前技能是通过 `minio://` 直接读对象，够用但少了那层文件索引。
- **token 级流式输出**（有意取舍）：当前是「进度实时 + 完整回复」，非逐字打字机。
- **无 RBAC / 多租户隔离**：演示为单租户。
- **Kafka 出站多副本消费**：已把出站主题扩到 3 分区，但消费组横向扩展尚未实测。
- **ithqbot 存量测试**：全量跑 `ithqbot/tests` 是 **534 passed / 14 failed / 3 skipped**。
  这 14 个失败**全部**在未经修改的原始源码上同样失败（已逐条对比：抽取受影响的 5 个文件，
  原始树 29 failed / 39 passed，本仓库 17 failed / 51 passed —— 本仓库的失败集合是原始树失败集合的子集，
  并且**额外修好了 12 个**，全部来自 `test_message_tool_suppress.py`）。剩余失败集中在
  附件/文件服务的路由分支、`config_migration` 的 `contextWindowTokens`、model router 的
  matrix 配置、以及一个时间敏感的并行加速用例，未逐一处理。

---

## 9. 目录结构

```
thqbot/
├── docker-compose.yml           # PG + Kafka + MinIO + gateway + ithqbot（Redis 复用宿主）
├── .env.example                 # 全部环境变量说明
├── services/
│   ├── gateway/                 # 单应用后端（含前端静态托管）
│   │   ├── app/
│   │   │   ├── main.py          # 入口：路由、生命周期、静态挂载
│   │   │   ├── config.py        # APP_* 配置
│   │   │   ├── models.py        # users / conversations / messages
│   │   │   ├── repository.py    # PG 数据访问（含归属校验）
│   │   │   ├── orchestrator.py  # 发消息 / 出站事件 → PG + Redis + SSE
│   │   │   ├── realtime.py      # Redis：去重 / Pub-Sub / 状态缓存
│   │   │   ├── object_store.py  # MinIO 封装：键布局 / 上传 / 取流 / 删除
│   │   │   ├── s3_compat.py     # boto3 S3 兼容客户端（已修 region 缺陷）
│   │   │   ├── source_adapter.py# Kafka 信封 / 生产 / 消费（保留的适配层）
│   │   │   ├── security.py      # PBKDF2 + 签名令牌
│   │   │   └── routes/          # auth / chat / files / stream / health
│   │   ├── alembic/             # schema 迁移（0001 users/conversations/messages、0002 files）
│   │   ├── tests/               # 106 个隔离式单测
│   │   └── web/                 # 前端构建产物（由 web/ 构建生成）
│   └── ithqbot/                 # agent 运行时
│       ├── config.template.json # 带 ${ENV} 占位符的配置模板
│       ├── render_config.py     # 渲染 + 必填校验
│       ├── entrypoint.sh        # 容器入口：渲染配置后启动
│       ├── workspace/           # agent 工作区（含演示文档 docs/）
│       └── ithqbot/
│           ├── agent/tools/files.py      # 新增：read_file 沙箱
│           ├── skills/text_stats/        # 新增：自研演示技能
│           └── providers/custom_provider.py  # 修复 NO_PROXY
├── web/                         # 前端源码（React 18 + Vite 6，仅 react/react-dom + 自托管 Inter）
│   ├── src/
│   │   ├── App.tsx              # 会话状态机：SSE、乐观发送、按天分组、轨迹累积、运行配置
│   │   ├── markdown.js          # 自研 Markdown 解析器（无依赖，输出结构化数据）
│   │   ├── format.ts            # 时间 / 大小 / 阶段文案
│   │   ├── presets.ts           # 顶部功能胶囊的提示词预设
│   │   ├── styles.css           # 对齐 DSH 的设计令牌 + 全部样式（深色、响应式、尊重减少动效）
│   │   └── components/
│   │       ├── LoginScreen.tsx  # 登录页
│   │       ├── Sidebar.tsx      # 左侧窄侧边栏（品牌、搜索、新建、会话列表、设置）
│   │       ├── TopBar.tsx       # 面包屑 + 状态胶囊 + 功能胶囊 + 对话/轨迹 Tab
│   │       ├── MessageBubble.tsx# 消息卡 + 文件变更卡 + 附件 + chips + 元信息/操作行
│   │       ├── TrajectoryPanel.tsx # 轨迹时间线（阶段 / 技能 / 工具 / 回复）
│   │       ├── RichText.tsx     # Markdown 渲染（含代码块复制按钮）
│   │       ├── Composer.tsx     # 输入区（自适应高度、附件暂存、权限提示、模型下拉）
│   │       ├── Welcome.tsx      # 空态欢迎 + 建议卡片
│   │       └── Icons.tsx        # 内联 SVG 图标（不引第三方图标库）
│   └── test/                    # Node 原生跑的解析与渲染烟测
└── scripts/
    ├── dev-local.ps1            # 本机调试（render / gateway / worker）
    ├── e2e_chat.py              # 端到端冒烟测试（支持 --attach 上传附件）
    ├── verify_attachment.py     # 附件真实往返校验（直连 MinIO）
    └── init_minio.py            # 创建 bucket（幂等）
```

---

## 10. 前端设计说明

界面**对齐 DeepSeek Harness（DSH）的信息密度与页面比例**：左侧固定窄侧边栏 + 右侧「面包屑 / Tab / 居中阅读列 / 底部输入卡」的工作台结构。
设计令牌与几何值来自 DSH 源码（`packages/client/ui-theme/src/styles/design-platform.css`、`base.css`、
`ui-conversation/src/client/{chat,skeleton}/*.module.css`、`ui-layout/src/client/AppFrame.module.css`、`ui-primitives/src/Pill.module.css`），
颜色按本次需求给定的色值覆盖。

### 10.1 设计令牌

| 项 | 值 | 来源 |
|----|-----|------|
| 页面背景 | `#0F0F11` | 需求指定 |
| 卡片 / 面板 | `#1A1A1F` | 需求指定 |
| 侧边栏底色 | `#16161A`（层级 2 `#202027`、层级 3 `#26262E`） | 需求指定 + DSH 层级关系 |
| 主文字 / 次文字 | `#EAEAEF` / `#8A8A98` | 需求指定 |
| 强调色（淡蓝） | `#679EFE` | DSH 暗色 `state-business-primary` = deepseek-400 |
| 边框 | `rgba(255,255,255,.06 / .12 / .16)` | DSH `border-l1/l2/l3` |
| hover 底色 | `rgba(255,255,255,.08)` | DSH `interactive-bg-hover` |
| 圆角 | 小卡片 `6px` / 消息卡 `8px` / 胶囊 `12px` | 需求指定（DSH 结构，圆角收敛） |
| 阴影 | `lv2 = 0 4px 12px rgba(0,0,0,.02) + 0 2px 8px rgba(0,0,0,.04)`、`lv3` 用于浮层 | DSH `shadow-lv1..lv3`（极克制，无发光） |
| 字体 | **Inter**（`@fontsource-variable/inter` 自托管，随产物发出）+ DSH 字体栈兜底；等宽用 DSH code 栈（**不带裸 `monospace`**，否则 Windows 中文回落 SimSun） | DSH `--dsw-font-family` / `--ds-font-family-code` |
| 字号 / 行高 | 正文 `16px/28px`、代码 `14px/22px`、元信息 `12px/20px`、UI `14px/22px` | DSH `--dsw-font-markdown-base` 等 |
| 动效曲线 | `cubic-bezier(0.4, 0, 0.2, 1)`；`0.1s / 0.2s / 0.3s` | DSH `--ds-ease-in-out` / `--ds-transition-duration*` |
| 布局比例 | 侧边栏 `260px`、**阅读列 `748px`**、输入卡上限 `748 + 32 = 780px`、左右留白 `16px` | DSH `--dsh-chat-content-width` / `--dsh-composer-card-max-width` / `--dsh-composer-side-clearance` |

### 10.2 页面结构（对齐 DSH 的比例与留白）

- **侧边栏**：品牌行（logo + `thqbot`）、搜索框、新建会话按钮、会话列表（标题 + 最近时间 + 预览 + 未读徽标，hover 高亮、选中态用层级 3 底色）、
  底部用户信息 + **设置**按钮 + 退出。列表项 `38px` 高、`6px` 圆角、hover 背景过渡。
- **顶栏**（DSH `ConversationSessionHeader`）：一行面包屑 `thqbot / <任务名>` + 状态胶囊（空闲 / 运行中 / 运行失败）+ 真实运行指标胶囊
  （botId、消息数、技能数、工具数）+ 顶部功能胶囊；下一行是 **`对话` / `轨迹` Tab**（`gap: 36px`、`13px/16px`、选中态为淡蓝文字 + `2px` 下划线压在分隔线上）。
  > DSH 头部左侧还有"子代理数量"胶囊。thqbot 是单 agent 运行时，**没有子代理数据**，因此没有伪造这个数字，
  > 换成了真实可得的指标（消息 / 技能 / 工具计数）。这是与原版唯一的结构性差异，已在代码注释里标注。
- **消息流**：`748px` 居中阅读列、左右 `32px`、消息之间 `16px` 节奏；用户消息右对齐气泡（`min(525px, 82%)`、`10px 16px` 内边距、`16px/24px`），
  Bot 消息按 DSH 走通栏文字流（无底板，仅 hover 给一层极淡背景），支持 Markdown 标题/列表/引用/**表格（单元格 hover 高亮）**/代码块（语言标签 + 复制）。
- **每消息页脚**：用量（`↑17.1k ↓185`，真实取自 ithqbot 回合结束写回的 `usage`）、调用次数、耗时（`latency_ms`）、时间，
  以及 hover 才出现的操作图标（点赞 / 点踩 / 复制 / **重试这一轮**）。
- **文件变更卡片**：技能产出的 `files[]` 渲染成 **60px 高、横向并排的卡片**（40px 图标块 + 文件名 + 描述 + 打开按钮），
  单个文件时自动单列 —— 取自 DSH 0.1.5 的 `dsh-client-ui-deliverables` 规格。
- **轨迹 Tab**：把 SSE 进度事件累积成时间线（阶段推进 / 技能调用 / 工具调用 / 回复），并会从历史消息里补齐既有轨迹。
- **输入区**（DSH `InputBar`）：浮动胶囊输入卡（圆角、`lv2` 阴影、focus 时描边转淡蓝）、多行自适应（上限 `200px`）、
  附件按钮（上传到 MinIO，暂存为 chips）、**权限提示**（`工作区受限 / 完全访问`，由 `/api/config` 驱动）、模型下拉（服务端只配一个模型时禁用）、圆形主色发送按钮。
- **设置面板**：`/api/config` + 会话上下文 + SSE 通道状态汇总（模型、租户、权限范围、附件上限、会话数、本地反馈条数）。

### 10.3 动画（需求指定，均已实现）

| # | 要求 | 实现 |
|---|------|------|
| 1 | 消息进入淡入 + 上移 `200ms ease-out` | `animation: msgIn 0.2s ease-out`（`opacity 0 → 1`、`translateY(6px) → 0`） |
| 2 | 列表 / 按钮 hover 背景过渡，不刺眼 | 全部 hover 只做 `background-color / color` 过渡（`0.2s`，hover 底色 `rgba(255,255,255,.08)`），**无发光、无位移** |
| 3 | Tab / 折叠区平滑过渡 | Tab 选中态走 `color` 过渡；展开/收起类控件只做 `transform`（chevron 旋转 `120ms`）—— 与 DSH 一致，**不做 height 动画** |
| 4 | 滚动条美化 + 全局平滑滚动 | 细滚动条（`10px` 轨道透明、`6px` 拇指、hover 提亮）、`overscroll-behavior: contain`、`scrollbar-gutter: stable`（切会话不抖动） |
| 5 | 运行中反馈 | 阶段文字走渐变 shimmer（`1.8s linear`，取自 DSH turn-status）+ 细进度条 + 技能/工具 chips |

### 10.4 工程取舍

- **不引第三方 UI / Markdown / 图标库**：只有 `react` + `react-dom`（+ 自托管字体）。Markdown 由 `markdown.js`
  解析成结构化数据再交给 React 渲染 —— 全程不使用 `innerHTML`，XSS 面为零；链接协议经
  `sanitizeHref()` 白名单过滤（`javascript:` / `data:` 降级为纯文本）。
- **真实数据优先**：token 用量与耗时都来自后端真实字段（`meta.usage` / `meta.latency_ms`），不是估算；
  点赞/点踩只写 `localStorage`（`thqbot:feedback`），设置面板里如实标注"仅存浏览器"。
- **可访问性**：`:focus-visible` 焦点环；`prefers-reduced-motion` 下关闭全部动画与过渡。
- **响应式**：`1280px` 以下隐藏顶部功能胶囊、`1080px` 以下隐藏指标胶囊（保留状态胶囊）、`900px` 以下侧栏变抽屉。
- **可验证性**：解析器是纯函数、组件可 SSR，因此**不需要浏览器**就能跑测试；服务端返回的 CSS 也已按
  「设计令牌 / 关键几何值」逐条断言（压缩后仍可正则匹配）。

> 改完前端后浏览器需要**硬刷新**一次：产物文件名带 hash，旧的 JS/CSS 已被清理。

