"""Generate every thqbot brand asset (SVG) plus a geometry manifest.

Run:  python gen_all.py <repo-root>
Outputs into <repo-root>/docs and <repo-root>/docs/assets, and the manifests
into this folder for check_svg.py to verify against real Chrome metrics.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from svgkit import (  # noqa: E402
    ACCENT,
    ACCENT_LINE,
    ACCENT_SOFT,
    BG,
    DIM,
    Doc,
    LAYER2,
    LAYER3,
    LINE1,
    LINE2,
    MUTED,
    OK_GREEN,
    PANEL,
    TEXT,
    WARN,
    glow_defs,
    mark,
    text_width,
)

REPO = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(".").resolve()
DOCS = REPO / "docs"
ASSETS = DOCS / "assets"
MANIFEST = Path(__file__).parent / "boxes"

GITHUB = "github.com/thqperseverance/thqbot"


# --------------------------------------------------------------------- helpers
def stacked_card(
    d: Doc,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    icon: str,
    title: str,
    lines: list[str],
    title_size: float = 15,
    body_size: float = 12,
    tag: str | None = None,
    fill: str = LAYER2,
    accent_icon: bool = False,
) -> None:
    """Icon on top, then title, then body lines (used inside architecture bands)."""
    pad = 16
    icon_size = 26
    d.rect(x, y, w, h, rx=12, fill=fill, stroke=LINE1)
    d.icon_tile(
        x + pad,
        y + pad,
        icon_size,
        icon,
        fill=ACCENT_SOFT if accent_icon else LAYER3,
        color=ACCENT if accent_icon else MUTED,
    )
    if tag:
        d.text(
            x + w - pad,
            y + pad + icon_size * 0.7,
            tag,
            size=10,
            weight=500,
            fill=ACCENT,
            anchor="end",
            allow=(x + w * 0.45, y + pad, x + w - pad, y + pad + icon_size),
        )
    ty = y + pad + icon_size + 20
    d.text(
        x + pad,
        ty,
        title,
        size=title_size,
        weight=600,
        allow=(x + pad, y + pad + icon_size + 4, x + w - pad, ty + title_size * 0.28),
        max_w=w - pad * 2,
    )
    for index, line in enumerate(lines):
        base = ty + 20 + index * (body_size + 6)
        d.text(
            x + pad,
            base,
            line,
            size=body_size,
            fill=MUTED,
            allow=(x + pad, base - body_size * 0.9 - 4, x + w - pad, base + body_size * 0.3),
            max_w=w - pad * 2,
        )


def band(
    d: Doc,
    y: float,
    h: float,
    *,
    label: str,
    sub: str,
    cards_x: float = 300.0,
    width: float = 1440.0,
    x: float = 80.0,
    rail_w: float = 200.0,
    fill: str = PANEL,
) -> None:
    d.rect(x, y, width, h, rx=16, fill=fill, stroke=LINE1)
    # label rail
    d.rect(x, y, rail_w, h, rx=16, fill=LAYER3, stroke=None)
    d.rect(x + rail_w - 16, y, 16, h, rx=0, fill=LAYER3, stroke=None)
    d.text(x + 24, y + 34, label, size=15, weight=600, allow=(x + 20, y + 18, x + rail_w - 20, y + 44))
    d.text(x + 24, y + 58, sub, size=11, fill=MUTED, allow=(x + 20, y + 46, x + rail_w - 20, y + 70))
    d.line(x + rail_w, y + 18, x + rail_w, y + h - 18, stroke=LINE2)


def bullet_row(
    d: Doc,
    x: float,
    y: float,
    w: float,
    text: str,
    *,
    ok: bool = True,
    size: float = 13,
) -> None:
    color = OK_GREEN if ok else WARN
    d.circle(x + 6, y - size * 0.3, 3.2, fill=color)
    d.text(x + 20, y, text, size=size, fill=TEXT, allow=(x + 20, y - size, x + w, y + size * 0.35), max_w=w - 20)


# ----------------------------------------------------------------------- logo
def gen_logo() -> None:
    d = Doc(128, 128, "thqbot logomark", bg="none")
    glow_defs(d)
    mark(d, 0, 0, 128)
    d.save(ASSETS / "logo.svg")

    w = 128 + 24 + 300
    d2 = Doc(int(w), 128, "thqbot wordmark", bg="none")
    glow_defs(d2)
    mark(d2, 0, 0, 128)
    d2.text(152, 90, "thqbot", size=76, weight=700, fill=TEXT, allow=(152, 8, w, 128))
    d2.save(ASSETS / "logo-wordmark.svg")

    # favicon: same mark, square, slightly padded for browser chrome
    d3 = Doc(64, 64, "thqbot favicon", bg="none")
    glow_defs(d3)
    mark(d3, 0, 0, 64)
    d3.save(ASSETS / "favicon.svg")


# --------------------------------------------------------------------- poster
def gen_poster() -> None:
    W, H = 1200, 1600
    d = Doc(W, H, "thqbot 宣传海报 — 开源 Agent 平台底座")
    glow_defs(d)
    d.add('<ellipse cx="120" cy="60" rx="700" ry="560" fill="url(#g-glow)"/>')
    d.add('<ellipse cx="1160" cy="1580" rx="560" ry="440" fill="url(#g-glow)" opacity="0.65"/>')
    d.rect(0, 0, W, 4, rx=0, fill=ACCENT, stroke=None)

    M, RIGHT, CW = 88.0, 1112.0, 1024.0

    # ---- header
    mark(d, M, 84, 64)
    d.text(170, 132, "thqbot", size=44, weight=700, allow=(164, 88, 740, 152))
    pill = "Apache-2.0 · 开源核心"
    pw = text_width(pill, 14) + 34
    d.rect(RIGHT - pw, 98, pw, 36, rx=18, fill=LAYER2, stroke=LINE1)
    d.text(RIGHT - pw / 2, 98 + 18 + 5, pill, size=14, fill=MUTED, anchor="middle", max_w=pw - 26)

    # ---- eyebrow + headline
    d.text(M, 250, "AGENT PLATFORM KIT", size=14, weight=600, fill=ACCENT, spacing=4.2, max_w=700)
    d.text(M, 348, "开源 Agent 平台底座", size=68, weight=700, max_w=CW)
    d.block(
        M,
        414,
        [
            ("Web 多轮对话 · 技能即插即用 · Kafka 异步编排", 0),
            ("PostgreSQL 事实来源 · Redis 实时推送 · MinIO 附件直通", 0),
        ],
        size=22,
        line_height=38,
        allow=(M, 388, RIGHT, 478),
    )
    d.line(M, 516, RIGHT, 516, stroke=LINE2)

    # ---- architecture strip
    d.text(M, 570, "架构一览", size=13, weight=600, fill=DIM, spacing=3, allow=(M, 552, M + 400, 582))
    top, ph = 590.0, 340.0
    d.rect(M, top, CW, ph, rx=16, fill=PANEL, stroke=LINE1)

    nodes = [
        ("浏览器", "React 18 + SSE", "dot"),
        ("网关 BFF", "登录 · 会话 · SSE", "grid"),
        ("Kafka", "信封 v1.2 · 3 分区", "bars"),
        ("Agent 运行时", "循环 · 技能 · LLM", "bolt"),
        ("技能与工具", "text_stats · 文件", "ring"),
    ]
    nw, nh, ny = 160.0, 96.0, top + 34
    xs = [120 + i * 198 for i in range(5)]
    for index, (title, sub, glyph) in enumerate(nodes):
        x = xs[index]
        d.rect(x, ny, nw, nh, rx=12, fill=LAYER2, stroke=LINE1)
        d.icon_tile(x + 16, ny + 16, 24, glyph, fill=ACCENT_SOFT, color=ACCENT)
        d.text(x + 16, ny + 58, title, size=14, weight=600, allow=(x + 14, ny + 40, x + nw - 12, ny + 74), max_w=nw - 26)
        d.text(x + 16, ny + 78, sub, size=11, fill=MUTED, allow=(x + 14, ny + 66, x + nw - 12, ny + 92), max_w=nw - 26)
        if index < 4:
            d.line(x + nw + 7, ny + nh / 2, xs[index + 1] - 7, ny + nh / 2, stroke=ACCENT_LINE, sw=1.6, marker="ar")

    d.text(120, top + 168, "状态与数据", size=12, weight=600, fill=DIM, spacing=1.6, allow=(118, top + 152, 420, top + 182))
    chips = [("PostgreSQL", "唯一事实来源"), ("Redis", "实时推送 · 缓存 · 锁"), ("MinIO", "附件直通技能")]
    cw, ch, cy = 288.0, 54.0, top + 186
    for index, (name, desc) in enumerate(chips):
        x = 144 + index * 312
        d.rect(x, cy, cw, ch, rx=10, fill=LAYER3, stroke=LINE1)
        d.text(x + 18, cy + ch / 2 - 2, name, size=13, weight=600, allow=(x + 16, cy + 6, x + cw - 16, cy + 30))
        d.text(x + 18, cy + ch / 2 + 17, desc, size=11, fill=MUTED, allow=(x + 16, cy + 30, x + cw - 16, cy + ch - 6))
    d.text(
        W / 2,
        top + ph - 22,
        "信封契约 v1.2 · 分区键 tenant|bot|account|chat · 出站消费幂等",
        size=13,
        mono=True,
        fill=MUTED,
        anchor="middle",
        allow=(M, top + ph - 44, RIGHT, top + ph - 6),
    )

    # ---- feature cards
    d.text(M, 990, "开箱即得", size=13, weight=600, fill=DIM, spacing=3, allow=(M, 972, M + 400, 1002))
    cards = [
        ("grid", "单一后端 BFF", ["登录 · 会话 · SSE · 静态托管", "一个进程跑完整个后端"]),
        ("bolt", "Kafka 异步编排", ["与 agent 运行时彻底解耦", "出站消费幂等，重启不丢回复"]),
        ("bars", "技能调用可观测", ["阶段推进与技能/工具名实时推送", "落到消息 meta，可复盘"]),
        ("dot", "真实用量与耗时", ["回合级 token 用量与延迟", "逐消息展示，不是估算"]),
        ("ring", "附件直通技能", ["上传即入 MinIO", "技能用 minio:// 直接读取"]),
        ("lock", "隔离式测试", ["106 网关用例 + 35 前端用例", "不依赖数据库与消息队列"]),
    ]
    cw2, ch2 = 326.0, 132.0
    for index, (glyph, title, lines) in enumerate(cards):
        col, row = index % 3, index // 3
        x = M + col * (cw2 + 23)
        y = 1014 + row * (ch2 + 22)
        d.card(x, y, cw2, ch2, title=title, lines=lines, glyph=glyph, title_size=16, body_size=13, icon_size=30)

    # ---- footer
    d.line(M, 1356, RIGHT, 1356, stroke=LINE2)
    d.text(M, 1402, GITHUB, size=16, mono=True, fill=ACCENT, allow=(M, 1380, 700, 1420))
    d.text(RIGHT, 1402, "Apache-2.0 · v0.1.0", size=14, fill=MUTED, anchor="end", allow=(800, 1380, RIGHT, 1420))
    d.text(W / 2, 1478, "让每一次技能调用都看得见", size=26, weight=600, fill=TEXT, anchor="middle", allow=(M, 1444, RIGHT, 1506))
    d.line(W / 2 - 60, 1512, W / 2 + 60, 1512, stroke=ACCENT_LINE, sw=2)
    d.text(
        W / 2,
        1552,
        "Web 对话 · 技能编排 · 异步总线 · 可观测轨迹",
        size=14,
        fill=DIM,
        anchor="middle",
        allow=(M, 1530, RIGHT, 1574),
    )

    d.save(DOCS / "poster.svg", manifest_dir=MANIFEST)


# ------------------------------------------------------------- social preview
def gen_social() -> None:
    W, H = 1280, 640
    d = Doc(W, H, "thqbot — 开源 Agent 平台底座")
    glow_defs(d)
    d.add('<ellipse cx="180" cy="80" rx="620" ry="420" fill="url(#g-glow)"/>')
    d.add('<ellipse cx="1220" cy="600" rx="420" ry="300" fill="url(#g-glow)" opacity="0.6"/>')
    d.rect(0, 0, W, 4, rx=0, fill=ACCENT, stroke=None)

    # header
    mark(d, 80, 72, 68)
    d.text(168, 124, "thqbot", size=44, weight=700, allow=(160, 80, 640, 146))
    pill = "开源核心 · Apache-2.0"
    pw = text_width(pill, 13) + 30
    d.rect(1200 - pw, 88, pw, 34, rx=17, fill=LAYER2, stroke=LINE1)
    d.text(1200 - pw / 2, 88 + 17 + 4.5, pill, size=13, fill=MUTED, anchor="middle", max_w=pw - 24)

    # headline
    d.text(80, 254, "把对话、技能与异步编排", size=46, weight=700, allow=(80, 208, 780, 272))
    d.text(80, 314, "装进一个开源底座", size=46, weight=700, fill=ACCENT, allow=(80, 268, 780, 332))
    d.block(
        80,
        376,
        [
            ("Web 工作台 · Kafka 编排 · Agent 运行时", 0),
            ("技能调用全过程可观测，token 用量真实可查", 0),
        ],
        size=18,
        line_height=32,
        allow=(80, 352, 790, 424),
    )

    # chips
    x = 80.0
    for label in ("多轮对话", "技能编排", "异步总线", "可观测"):
        x += d.chip(x, 452, label, size=13, pad=13, h=32, fill=LAYER2) + 10

    # footer
    d.text(80, 560, GITHUB, size=15, mono=True, fill=ACCENT, allow=(80, 538, 700, 582))

    # right panel: vertical data flow
    px, py, pw2, ph2 = 790.0, 88.0, 410.0, 464.0
    d.rect(px, py, pw2, ph2, rx=16, fill=PANEL, stroke=LINE1)
    d.text(px + 24, py + 38, "一条消息的旅程", size=13, weight=600, fill=DIM, spacing=2, allow=(px + 22, py + 20, px + pw2 - 22, py + 50))
    rows = [
        ("浏览器", "React 18 · SSE"),
        ("网关 BFF", "鉴权 · 落库 · 推送"),
        ("Kafka", "icatmsg_inbound"),
        ("Agent 运行时", "AgentLoop · 技能"),
        ("数据与存储", "PG · Redis · MinIO"),
    ]
    rw, rh = 362.0, 56.0
    rx0 = px + 24
    ry0 = py + 64
    for index, (name, sub) in enumerate(rows):
        y = ry0 + index * (rh + 16)
        d.rect(rx0, y, rw, rh, rx=10, fill=LAYER2, stroke=LINE1)
        d.text(rx0 + 16, y + 24, name, size=14, weight=600, allow=(rx0 + 14, y + 8, rx0 + rw - 14, y + 34))
        d.text(rx0 + 16, y + 43, sub, size=11, fill=MUTED, allow=(rx0 + 14, y + 32, rx0 + rw - 14, y + 54))
        if index < len(rows) - 1:
            d.line(rx0 + 26, y + rh + 3, rx0 + 26, y + rh + 13, stroke=ACCENT_LINE, sw=1.4, marker="ar")

    d.save(DOCS / "social-preview.svg", manifest_dir=MANIFEST)


# ----------------------------------------------------------- product architecture
def gen_arch_product() -> None:
    W, H = 1600, 1120
    d = Doc(W, H, "thqbot 产品架构图")
    glow_defs(d)
    d.add('<ellipse cx="100" cy="40" rx="700" ry="420" fill="url(#g-glow)"/>')
    d.rect(0, 0, W, 4, rx=0, fill=ACCENT, stroke=None)
    d.title_block(
        80,
        96,
        "thqbot 产品架构",
        "从用户场景到交付形态，以及开源核心与企业版保留之间的边界",
        width=1100,
    )

    # ---- A 用户与场景
    band(d, 150, 176, label="用户与场景", sub="谁在用、解决什么")
    personas = [
        ("dot", "一线业务同学", ["不写代码也能调起技能", "对话式完成文档与数据任务"]),
        ("grid", "平台开发者", ["接 REST + SSE 扩展前端", "按 SKILL.md 规范贡献技能"]),
        ("bars", "运维与集成", ["docker compose 一键起全套", "Kafka 信封对接既有系统"]),
    ]
    for index, (glyph, title, lines) in enumerate(personas):
        stacked_card(d, 300 + index * 413, 172, 390, 132, icon=glyph, title=title, lines=lines, accent_icon=True)

    # ---- B 开源核心能力
    band(d, 346, 196, label="开源核心能力", sub="本仓库已实现并测试")
    caps = [
        ("grid", "Web 工作台", ["对话 / 轨迹双视图", "附件、设置、模型切换"]),
        ("bolt", "对话与编排", ["多轮会话落 PostgreSQL", "Kafka 异步解耦"]),
        ("ring", "技能与工具", ["SKILL.md 加载与注册", "read_file 沙箱"]),
        ("bars", "实时与可观测", ["SSE 进度推送", "回合级用量与耗时"]),
        ("lock", "附件与知识", ["MinIO 上传 / 下载", "技能直读对象存储"]),
    ]
    for index, (glyph, title, lines) in enumerate(caps):
        stacked_card(d, 300 + index * 246, 368, 230, 152, icon=glyph, title=title, lines=lines, accent_icon=True)

    # ---- C 平台支撑
    band(d, 562, 176, label="平台支撑", sub="同一套实现即架构")
    infra = [
        ("bolt", "消息骨干", ["Kafka 3 分区", "信封契约 v1.2"]),
        ("grid", "数据与状态", ["PostgreSQL 事实来源", "Redis 缓存 / 锁 / 通道"]),
        ("ring", "对象存储", ["MinIO bucket", "键含 user_id 隔离"]),
        ("bars", "Agent 运行时", ["AgentLoop + 技能", "多 provider 可换模型"]),
    ]
    for index, (glyph, title, lines) in enumerate(infra):
        stacked_card(d, 300 + index * 308, 584, 292, 132, icon=glyph, title=title, lines=lines)

    # ---- D 开源边界
    band(d, 758, 290, label="开源边界", sub="什么开源、什么保留", rail_w=200)
    left_x, right_x, panel_w, panel_h = 300.0, 904.0, 588.0, 246.0
    top = 780.0
    d.rect(left_x, top, panel_w, panel_h, rx=12, fill=LAYER2, stroke=ACCENT_LINE)
    d.text(left_x + 20, top + 34, "本仓库开源（Apache-2.0）", size=15, weight=600, fill=ACCENT, allow=(left_x + 18, top + 16, left_x + panel_w - 18, top + 44))
    for index, line in enumerate(
        [
            "网关 BFF：鉴权 / 会话 / 附件 / SSE / 静态托管",
            "Kafka 编排与信封契约、出站消费幂等",
            "Agent 运行时、技能加载、工具注册与沙箱",
            "PostgreSQL schema 与 Alembic 迁移",
            "对齐 DSH 的深色工作台前端",
            "隔离式测试与端到端脚本",
        ]
    ):
        bullet_row(d, left_x + 22, top + 68 + index * 28, panel_w - 44, line, ok=True)

    d.rect(right_x, top, panel_w, panel_h, rx=12, fill=LAYER2, stroke=LINE1)
    d.text(right_x + 20, top + 34, "企业版保留（不开源）", size=15, weight=600, fill=WARN, allow=(right_x + 18, top + 16, right_x + panel_w - 18, top + 44))
    for index, line in enumerate(
        [
            "多租户隔离、RBAC 与审计日志",
            "技能市场：审核流水线、版本与灰度",
            "全链路可观测：OTel、成本看板、告警",
            "高可用部署：多副本消费、Helm、灰度",
            "企业连接器：SSO / OIDC、IM、知识库",
            "配额与计费",
        ]
    ):
        bullet_row(d, right_x + 22, top + 68 + index * 28, panel_w - 44, line, ok=False)

    d.text(80, 1092, "开源的是「平台底座」；企业版补的是「规模、治理与合规」。", size=13, fill=MUTED, allow=(80, 1072, 1520, 1104))

    d.save(DOCS / "architecture-product.svg", manifest_dir=MANIFEST)


# ------------------------------------------------------------ tech architecture
def gen_arch_tech() -> None:
    W, H = 1600, 1120
    d = Doc(W, H, "thqbot 技术架构图")
    glow_defs(d)
    d.add('<ellipse cx="100" cy="40" rx="700" ry="420" fill="url(#g-glow)"/>')
    d.rect(0, 0, W, 4, rx=0, fill=ACCENT, stroke=None)
    d.title_block(80, 96, "thqbot 技术架构", "真实组件、协议与数据流 —— 与仓库里跑的这套完全一致", width=1100)

    lx, lw = 80.0, 860.0

    def lane(y: float, h: float, label: str, stack: str) -> None:
        d.rect(lx, y, lw, h, rx=14, fill=PANEL, stroke=LINE1)
        d.text(lx + 22, y + 34, label, size=15, weight=600, allow=(lx + 20, y + 16, lx + 460, y + 46))
        d.text(lx + lw - 22, y + 34, stack, size=12, mono=True, fill=MUTED, anchor="end", allow=(lx + 460, y + 16, lx + lw - 20, y + 46))

    def box(x: float, y: float, w: float, h: float, title: str, sub: str, *, fill: str = LAYER2, accent: bool = False) -> None:
        d.rect(x, y, w, h, rx=10, fill=fill, stroke=ACCENT_LINE if accent else LINE1)
        tx, tw = x + 14, w - 28
        if not sub:
            base = y + h / 2 + 4.5
            d.text(tx, base, title, size=13, weight=600, fill=ACCENT if accent else TEXT,
                   allow=(x + 12, y + 6, x + w - 12, y + h - 6), max_w=tw)
            return
        title_base = y + h / 2 - 5
        sub_base = y + h / 2 + 15
        d.text(tx, title_base, title, size=13, weight=600, fill=ACCENT if accent else TEXT,
               allow=(x + 12, y + 6, x + w - 12, title_base + 4), max_w=tw)
        d.text(tx, sub_base, sub, size=11, fill=MUTED,
               allow=(x + 12, sub_base - 12, x + w - 12, y + h - 4), max_w=tw)

    # L1 客户端
    lane(140, 124, "① 客户端", "browser")
    box(lx + 22, 196, 380, 56, "React 18 + Vite 6 工作台", "无第三方 UI 库 · 自托管 Inter")
    box(lx + 422, 196, 416, 56, "传输", "同源 Cookie 鉴权 · SSE 事件流 · REST")

    # L2 网关
    lane(284, 228, "② 网关 BFF（FastAPI + uvicorn）", "python 3.11")
    mods = [
        ("auth", "PBKDF2 + 签名 Cookie"),
        ("chat API", "会话 / 消息 / 配置"),
        ("files API", "上传 · 下载鉴权"),
        ("SSE stream", "Redis Pub/Sub 扇出"),
        ("orchestrator", "信封构造 · 出站消费"),
    ]
    for index, (name, sub) in enumerate(mods):
        box(lx + 22 + index * 166, 330, 152, 66, name, sub)
    box(lx + 22, 414, 300, 58, "repository + Alembic", "SQLAlchemy · schema 由迁移拥有", accent=True)
    box(lx + 338, 414, 300, 58, "realtime", "去重 · 状态缓存 · 分布式锁")
    box(lx + 654, 414, 184, 58, "object_store", "S3 兼容 · 取流 / 删除")

    # L3 总线
    lane(532, 124, "③ 消息骨干", "Kafka 3.9 · KRaft")
    box(lx + 22, 588, 380, 56, "icatmsg_inbound", "分区键 tenant|bot|account|chat")
    box(lx + 422, 588, 200, 56, "icatmsg_outbound", "3 分区")
    box(lx + 638, 588, 200, 56, "消费组", "thqbot-gateway")

    # L4 Agent
    lane(676, 184, "④ Agent 运行时", "ithqbot 0.1.4")
    runtime = [
        ("AgentLoop", "推理 · 工具循环"),
        ("技能加载", "SKILL.md 规范"),
        ("工具注册", "沙箱 roots"),
        ("LLM Provider", "OpenAI 兼容"),
        ("会话 / 记忆", "PG + 文件工作区"),
    ]
    for index, (name, sub) in enumerate(runtime):
        box(lx + 22 + index * 166, 722, 152, 62, name, sub)
    box(lx + 22, 800, 380, 44, "内置技能：text_stats · doc_compare · check_skill", "", fill=LAYER3)
    box(lx + 422, 800, 416, 44, "read_file 沙箱：工作区 + 内置技能目录，越界即拒", "", fill=LAYER3)

    # L5 存储
    lane(880, 124, "⑤ 数据与存储", "stateful")
    box(lx + 22, 932, 262, 56, "PostgreSQL", "唯一事实来源")
    box(lx + 300, 932, 262, 56, "Redis", "缓存 / 推送 / 锁")
    box(lx + 578, 932, 260, 56, "MinIO", "桶 ithqbot-storage")

    # connectors between lanes
    for y0, y1 in ((264, 284), (512, 532), (656, 676), (860, 880)):
        d.line(lx + lw / 2, y0 + 4, lx + lw / 2, y1 - 4, stroke=ACCENT_LINE, sw=1.4, marker="ar")

    # right column: journey
    rx0, rw2 = 980.0, 540.0
    d.rect(rx0, 140, rw2, 520, rx=14, fill=PANEL, stroke=LINE1)
    d.text(rx0 + 24, 176, "一条消息的完整旅程", size=15, weight=600, allow=(rx0 + 22, 158, rx0 + rw2 - 22, 188))
    steps = [
        "浏览器 POST /api/conversations/{id}/messages",
        "网关注入 user 行 → 组装信封 v1.2 → 投递 inbound",
        "ithqbot 消费 → AgentLoop 推理 → 调技能 / 工具",
        "每步 emit_progress → 回推 status.processing 事件",
        "网关把进度并入 Redis 状态缓存（跨事件做并集）",
        "回复落 PG → Redis Pub/Sub → SSE 推到浏览器",
        "前端按 SSE 事件累积出「轨迹」时间线",
        "回合结束写回 usage 与 latency_ms 到消息 meta",
    ]
    for index, step in enumerate(steps):
        y = 214 + index * 54
        d.circle(rx0 + 30, y - 4, 11, fill=LAYER3)
        d.text(rx0 + 30, y, str(index + 1), size=12, weight=600, fill=ACCENT, anchor="middle", allow=(rx0 + 18, y - 16, rx0 + 42, y + 6))
        d.text(rx0 + 52, y, step, size=12.5, fill=TEXT, allow=(rx0 + 52, y - 14, rx0 + rw2 - 22, y + 8), max_w=rw2 - 74)

    # right column: decisions
    d.rect(rx0, 684, rw2, 320, rx=14, fill=PANEL, stroke=LINE1)
    d.text(rx0 + 24, 720, "关键设计决策", size=15, weight=600, allow=(rx0 + 22, 702, rx0 + rw2 - 22, 732))
    decisions = [
        "PostgreSQL 是唯一事实来源；Redis 只做缓存 / 通道 / 锁",
        "前后端通过 Kafka 解耦，出站消费按 external_id 幂等",
        "schema 由 Alembic 拥有，不靠 ORM 隐式建表",
        "技能产出文件走同一 MinIO 桶，键含 user_id 做隔离",
        "进度元数据跨事件做并集，避免 finalizing 抹掉技能名",
        "附件下载按 PG 记录校验归属，越权一律 404",
    ]
    for index, line in enumerate(decisions):
        y = 760 + index * 40
        d.circle(rx0 + 30, y - 4, 3.4, fill=ACCENT)
        d.text(rx0 + 44, y, line, size=12.5, fill=TEXT, allow=(rx0 + 44, y - 16, rx0 + rw2 - 22, y + 8), max_w=rw2 - 80)

    d.text(80, 1092, "端口：网关 8080（宿主 8090）· worker 健康检查 9002 · Kafka 9092（宿主 29092）· MinIO 9000", size=12, mono=True, fill=MUTED, allow=(80, 1072, 1520, 1104))

    d.save(DOCS / "architecture-tech.svg", manifest_dir=MANIFEST)


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    gen_logo()
    gen_poster()
    gen_social()
    gen_arch_product()
    gen_arch_tech()
    print("\nall assets generated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
