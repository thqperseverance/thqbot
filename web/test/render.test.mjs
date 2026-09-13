/**
 * 组件渲染烟测：用 esbuild 把 ssr-entry.tsx 打成 Node 可执行的 ESM，再真实渲染一遍。
 *
 * 价值：`tsc --noEmit` 只能证明类型正确，不能证明组件在运行时不炸（例如渲染期访问
 * 浏览器 API、导入路径写错、JSX 分支漏字段）。这里能真正跑出来。
 *
 *   node web/test/render.test.mjs
 */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { build } from "esbuild";

const testDir = resolve(import.meta.dirname);
const projectRoot = resolve(testDir, "..");

const passed = [];
const failures = [];

/** @param {string} name @param {() => void} fn */
function check(name, fn) {
  try {
    fn();
    passed.push(name);
  } catch (error) {
    failures.push(`${name}\n    ${error.message}`);
  }
}

const workDir = mkdtempSync(join(tmpdir(), "thqbot-ssr-"));
const outfile = join(workDir, "entry.mjs");

try {
  await build({
    entryPoints: [join(testDir, "ssr-entry.tsx")],
    outfile,
    bundle: true,
    platform: "node",
    format: "esm",
    jsx: "automatic",
    target: "node20",
    logLevel: "silent",
    absWorkingDir: projectRoot,
    loader: { ".css": "empty" },
    // react-dom/server 的 CJS 构建会 require Node 内置模块；把它注入 ESM 作用域
    banner: {
      js: "import { createRequire as __createRequire } from 'node:module'; const require = __createRequire(import.meta.url);",
    },
  });

  const { renderAll } = await import(pathToFileURL(outfile).href);
  const html = renderAll();

  check("RichText 渲染标题", () => {
    assert.match(html.rich, /<h2[^>]*md-h2[^>]*>结果<\/h2>/);
  });

  check("RichText 渲染粗体与行内代码", () => {
    assert.match(html.rich, /<strong>文本统计<\/strong>/);
  });

  check("RichText 渲染有序与无序列表（嵌套在父 li 内）", () => {
    assert.match(html.rich, /<ul class="md-list">/);
    assert.match(html.rich, /<ol class="md-list">/);
    // 合法结构：ul > li > ul > li，而不是 ul > ul
    assert.match(html.rich, /<li>总字符数：45<ul class="md-list">/);
    assert.ok(!html.rich.includes("md-list nested"), "不应再产生 ul > ul 的非法嵌套");
  });

  check("RichText 渲染引用块", () => {
    assert.match(html.rich, /<blockquote class="md-quote"/);
  });

  check("RichText 渲染表格", () => {
    assert.match(html.rich, /<table class="md-table"/);
    assert.match(html.rich, /<th>指标<\/th>/);
    assert.match(html.rich, /<td>45<\/td>/);
  });

  check("RichText 渲染代码块并带语言标签", () => {
    assert.match(html.rich, /class="md-pre-lang">json</);
    assert.match(html.rich, /chars_total/);
  });

  check("RichText 剥离危险链接但保留安全链接", () => {
    assert.ok(!html.rich.includes("javascript:"), "不应出现 javascript: 协议");
    assert.match(html.rich, /href="https:\/\/example\.com"/);
  });

  check("MessageBubble 渲染附件与技能 chips", () => {
    assert.match(html.bubble, /spec-v1\.md/);
    assert.match(html.bubble, /chip-skill/);
    assert.match(html.bubble, />text_stats</);
    assert.match(html.bubble, /chip-tool/);
    assert.match(html.bubble, /href="\/api\/files\/f-1"/);
  });

  check("MessageBubble 渲染文件变更卡片（文件名 + 描述 + 打开）", () => {
    assert.match(html.bubble, /class="file-cards"/);
    assert.match(html.bubble, /class="file-card"/);
    assert.match(html.bubble, /report\.md/);
    assert.match(html.bubble, /对比报告/);
  });

  check("MessageBubble 渲染用量/耗时/时间元信息行", () => {
    assert.match(html.bubble, /class="meta-row"/);
    assert.match(html.bubble, /↑6\.5k/);
    assert.match(html.bubble, /1\.2s/);
  });

  check("Welcome 渲染欢迎语与建议卡片", () => {
    assert.match(html.welcome, /开始一段对话/);
    assert.match(html.welcome, /统计一段文本/);
    assert.match(html.welcome, /对比两份文档/);
  });

  check("LoginScreen 渲染表单与默认提示", () => {
    assert.match(html.login, /进入工作台/);
    assert.match(html.login, /默认 admin123456/);
    assert.match(html.login, /Agent 平台 MVP/);
  });

  check("渲染结果不含未解析的 markdown 标记", () => {
    assert.ok(!html.rich.includes("**文本统计**"), "粗体不应残留星号");
    assert.ok(!html.rich.includes("| 指标 |"), "表格不应残留竖线原文");
  });

  check("QuickTags 渲染功能胶囊（含 doc_compare / otp）", () => {
    assert.match(html.tags, /class="capsules"/);
    assert.match(html.tags, /class="capsule"/);
    assert.match(html.tags, />doc_compare</);
    assert.match(html.tags, />otp</);
    assert.match(html.tags, />text_stats</);
    assert.match(html.tags, /title="对比两份文档（需先添加两个附件）"/);
  });

  check("TopBar 渲染面包屑 / 状态胶囊 / 运行指标 / 对话-轨迹 Tab", () => {
    assert.match(html.topbar, /class="breadcrumb"/);
    assert.match(html.topbar, /thqbot/);
    assert.match(html.topbar, /统计文本/);
    assert.match(html.topbar, /status-pill is-running/);
    assert.match(html.topbar, /运行中/);
    assert.match(html.topbar, /技能 (<!-- -->)?1/);
    assert.match(html.topbar, /工具 (<!-- -->)?2/);
    assert.match(html.topbar, /class="tabs"/);
    assert.match(html.topbar, /tab is-active"?>.*对话/s);
    assert.match(html.topbar, /轨迹/);
    assert.match(html.topbar, /class="tab-count">3</);
  });

  check("TrajectoryPanel 渲染轨迹时间线与事件类型", () => {
    assert.match(html.trajectory, /class="timeline"/);
    assert.match(html.trajectory, /tl-item is-run/);
    assert.match(html.trajectory, /tl-item is-skill/);
    assert.match(html.trajectory, /tl-item is-tool/);
    assert.match(html.trajectory, /text_stats/);
    // React SSR 会在相邻文本节点之间插入 <!-- --> 注释
    assert.match(html.trajectory, /共 (<!-- -->)?3(<!-- -->)? 条事件/);
  });

  check("Sidebar 渲染品牌 / 搜索 / 会话项 / 设置入口", () => {
    assert.match(html.sidebar, /class="brand"/);
    assert.match(html.sidebar, /thqbot/);
    assert.match(html.sidebar, /placeholder="搜索会话"/);
    assert.match(html.sidebar, /nav-item is-active/);
    assert.match(html.sidebar, /class="nav-badge">2</);
    assert.match(html.sidebar, /title="设置"/);
    assert.match(html.sidebar, /title="退出登录"/);
  });
} finally {
  rmSync(workDir, { recursive: true, force: true });
}

console.log(`render.test: ${passed.length} passed, ${failures.length} failed`);
if (failures.length > 0) {
  console.error("\n失败用例：\n- " + failures.join("\n- "));
  process.exit(1);
}
