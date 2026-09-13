/**
 * markdown.js 的解析测试（Node 原生跑，无需任何依赖）：
 *   node web/test/markdown.test.mjs
 */
import assert from "node:assert/strict";

import { parseInline, parseMarkdown, sanitizeHref } from "../src/markdown.js";

let passed = 0;
const failures = [];

/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (error) {
    failures.push(`${name}\n    ${error.message}`);
  }
}

/** @param {string} source */
const types = (source) => parseMarkdown(source).map((block) => block.type);

test("空输入返回空数组", () => {
  assert.deepEqual(parseMarkdown(""), []);
  assert.deepEqual(parseMarkdown(null), []);
  assert.deepEqual(parseMarkdown("   \n\n  "), []);
});

test("段落与换行保留", () => {
  const blocks = parseMarkdown("第一行\n第二行\n\n新段落");
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].text, "第一行\n第二行");
  assert.equal(blocks[1].text, "新段落");
});

test("ATX 标题 1-6 级", () => {
  const blocks = parseMarkdown("# 一级\n## 二级\n###### 六级");
  assert.deepEqual(
    blocks.map((b) => [b.type, b.level, b.text]),
    [
      ["heading", 1, "一级"],
      ["heading", 2, "二级"],
      ["heading", 6, "六级"],
    ],
  );
});

test("围栏代码块保留内容与语言", () => {
  const blocks = parseMarkdown("说明：\n\n```python\nprint('hi')\n# 井号不是标题\n```\n\n结束");
  assert.deepEqual(types("```python\nprint('hi')\n```"), ["code"]);
  assert.equal(blocks[1].type, "code");
  assert.equal(blocks[1].lang, "python");
  assert.equal(blocks[1].content, "print('hi')\n# 井号不是标题");
  assert.equal(blocks[2].text, "结束");
});

test("未闭合的代码块也能收尾", () => {
  const blocks = parseMarkdown("```\nno closing fence");
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].type, "code");
  assert.equal(blocks[0].content, "no closing fence");
});

test("无序列表与嵌套层级", () => {
  const blocks = parseMarkdown("- 一\n- 二\n  - 二之一\n- 三");
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].type, "list");
  assert.equal(blocks[0].ordered, false);
  assert.deepEqual(
    blocks[0].items.map((item) => [item.depth, item.text]),
    [
      [0, "一"],
      [0, "二"],
      [1, "二之一"],
      [0, "三"],
    ],
  );
});

test("有序列表识别", () => {
  const blocks = parseMarkdown("1. 甲\n2. 乙");
  assert.equal(blocks[0].type, "list");
  assert.equal(blocks[0].ordered, true);
  assert.equal(blocks[0].items.length, 2);
});

test("分隔线不与列表混淆", () => {
  assert.deepEqual(types("---"), ["hr"]);
  assert.deepEqual(types("***"), ["hr"]);
  assert.deepEqual(types("- 项目"), ["list"]);
});

test("引用块合并连续行", () => {
  const blocks = parseMarkdown("> 第一行\n> 第二行\n\n正文");
  assert.equal(blocks[0].type, "quote");
  assert.equal(blocks[0].text, "第一行\n第二行");
  assert.equal(blocks[1].type, "paragraph");
});

test("表格解析表头与数据行", () => {
  const blocks = parseMarkdown("| 名称 | 说明 |\n| --- | --- |\n| a | 甲 |\n| b | 乙 |");
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].type, "table");
  assert.deepEqual(blocks[0].header, ["名称", "说明"]);
  assert.deepEqual(blocks[0].rows, [
    ["a", "甲"],
    ["b", "乙"],
  ]);
});

test("含竖线的普通段落不会被误判成表格", () => {
  assert.deepEqual(types("a | b 只是文本"), ["paragraph"]);
});

test("行内代码优先于强调", () => {
  const spans = parseInline("用 `**不是粗体**` 表示");
  assert.deepEqual(spans, [
    { type: "text", value: "用 " },
    { type: "code", value: "**不是粗体**" },
    { type: "text", value: " 表示" },
  ]);
});

test("粗体、斜体、删除线", () => {
  assert.deepEqual(parseInline("**粗**"), [{ type: "strong", value: "粗" }]);
  assert.deepEqual(parseInline("__粗__"), [{ type: "strong", value: "粗" }]);
  assert.deepEqual(parseInline("*斜*"), [{ type: "em", value: "斜" }]);
  assert.deepEqual(parseInline("~~删~~"), [{ type: "strike", value: "删" }]);
});

test("链接解析并清洗 href", () => {
  assert.deepEqual(parseInline("[站点](https://example.com)"), [
    { type: "link", value: "站点", href: "https://example.com" },
  ]);
  assert.deepEqual(parseInline("[相对](/api/files/1)"), [
    { type: "link", value: "相对", href: "/api/files/1" },
  ]);
});

test("危险协议被剥离为纯文本", () => {
  assert.equal(sanitizeHref("javascript:alert(1)"), "");
  assert.equal(sanitizeHref("data:text/html,x"), "");
  assert.equal(sanitizeHref("vbscript:x"), "");
  assert.deepEqual(parseInline("[点我](javascript:alert(1))"), [{ type: "text", value: "点我" }]);
});

test("混合行内标记", () => {
  const spans = parseInline("请 **注意** `sse` 与 [文档](/docs)");
  assert.deepEqual(
    spans.map((span) => span.type),
    ["text", "strong", "text", "code", "text", "link"],
  );
});

test("纯文本原样返回单个 span", () => {
  assert.deepEqual(parseInline("没有标记"), [{ type: "text", value: "没有标记" }]);
  assert.deepEqual(parseInline(""), [{ type: "text", value: "" }]);
});

test("真实 agent 回复片段可解析", () => {
  const source = [
    "统计完成 ✅",
    "",
    "**文本统计结果**",
    "- 总字符数：45",
    "- 英文词数：4",
    "",
    "1. 先看规模",
    "2. 再抽关键词",
    "",
    "> 提示：中文按字计词",
    "",
    "```json",
    '{"chars_total": 45}',
    "```",
  ].join("\n");
  const blocks = parseMarkdown(source);
  assert.deepEqual(
    blocks.map((block) => block.type),
    ["paragraph", "paragraph", "list", "list", "quote", "code"],
  );
  assert.equal(blocks[2].ordered, false);
  assert.equal(blocks[3].ordered, true);
  assert.equal(blocks[5].lang, "json");
});

console.log(`markdown.test: ${passed} passed, ${failures.length} failed`);
if (failures.length > 0) {
  console.error("\n失败用例：\n- " + failures.join("\n- "));
  process.exit(1);
}
