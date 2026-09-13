/**
 * 轻量 Markdown 解析器（无第三方依赖、不产生 HTML —— 输出结构化数据，由 React 渲染，天然免疫 XSS）。
 *
 * 支持：
 *   块级：围栏代码、ATX 标题、有序/无序列表（含一层嵌套）、引用、表格、分隔线、段落
 *   行内：行内代码、粗体、斜体、删除线、链接
 *
 * @typedef {{ type: 'paragraph', text: string }} ParagraphBlock
 * @typedef {{ type: 'heading', level: number, text: string }} HeadingBlock
 * @typedef {{ type: 'code', lang: string, content: string }} CodeBlock
 * @typedef {{ type: 'list', ordered: boolean, items: Array<{ text: string, depth: number }> }} ListBlock
 * @typedef {{ type: 'quote', text: string }} QuoteBlock
 * @typedef {{ type: 'table', header: string[], rows: string[][] }} TableBlock
 * @typedef {{ type: 'hr' }} HrBlock
 * @typedef {ParagraphBlock|HeadingBlock|CodeBlock|ListBlock|QuoteBlock|TableBlock|HrBlock} Block
 *
 * @typedef {{ type: 'text', value: string }} TextSpan
 * @typedef {{ type: 'code', value: string }} CodeSpan
 * @typedef {{ type: 'strong', value: string }} StrongSpan
 * @typedef {{ type: 'em', value: string }} EmSpan
 * @typedef {{ type: 'strike', value: string }} StrikeSpan
 * @typedef {{ type: 'link', value: string, href: string }} LinkSpan
 * @typedef {TextSpan|CodeSpan|StrongSpan|EmSpan|StrikeSpan|LinkSpan} InlineSpan
 */

const FENCE_RE = /^\s*(```|~~~)\s*([A-Za-z0-9_+-]*)\s*$/;
const HEADING_RE = /^(#{1,6})\s+(.*)$/;
const HR_RE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const UL_RE = /^(\s*)[-*+]\s+(.*)$/;
const OL_RE = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;
const QUOTE_RE = /^\s*>\s?(.*)$/;
const TABLE_SEP_RE = /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/;

// 行内：用单个正则一次扫描，避免多次 replace 造成的嵌套错乱
// 链接的 href 允许一层嵌套括号（例如 javascript:alert(1) 这类要被识别后拒掉的写法）
const INLINE_RE =
  /(`+[^`]*?`+)|(\*\*[^*]+\*\*)|(__[^_]+__)|(~~[^~]+~~)|(\*[^*\n]+\*)|(_[^_\n]+_)|(\[[^\]\n]*\]\((?:[^()\s]|\([^()\s]*\))*\))/g;

/**
 * 只允许安全协议，杜绝 `javascript:` 之类的链接。
 * @param {string} href
 * @returns {string}
 */
export function sanitizeHref(href) {
  const value = String(href || "").trim();
  if (!value) return "";
  if (/^(https?:|mailto:|tel:)/i.test(value)) return value;
  if (value.startsWith("/") || value.startsWith("#")) return value;
  return "";
}

/**
 * @param {string} text
 * @returns {InlineSpan[]}
 */
export function parseInline(text) {
  const spans = [];
  const source = String(text ?? "");
  let lastIndex = 0;
  INLINE_RE.lastIndex = 0;

  /** @param {string} value */
  const pushText = (value) => {
    if (value) spans.push({ type: "text", value });
  };

  let match = INLINE_RE.exec(source);
  while (match !== null) {
    pushText(source.slice(lastIndex, match.index));
    const token = match[0];

    if (match[1]) {
      spans.push({ type: "code", value: token.replace(/^`+|`+$/g, "") });
    } else if (match[2] || match[3]) {
      spans.push({ type: "strong", value: token.slice(2, -2) });
    } else if (match[4]) {
      spans.push({ type: "strike", value: token.slice(2, -2) });
    } else if (match[5] || match[6]) {
      spans.push({ type: "em", value: token.slice(1, -1) });
    } else if (match[7]) {
      const splitAt = token.indexOf("](");
      const label = token.slice(1, splitAt);
      const href = sanitizeHref(token.slice(splitAt + 2, -1));
      if (href) spans.push({ type: "link", value: label, href });
      else pushText(label);
    } else {
      pushText(token);
    }

    lastIndex = match.index + token.length;
    match = INLINE_RE.exec(source);
  }

  pushText(source.slice(lastIndex));
  return spans.length > 0 ? spans : [{ type: "text", value: source }];
}

/**
 * @param {string} line
 * @returns {string[]|null}
 */
function splitTableRow(line) {
  const trimmed = String(line || "").trim();
  if (!trimmed.includes("|")) return null;
  let body = trimmed;
  if (body.startsWith("|")) body = body.slice(1);
  if (body.endsWith("|")) body = body.slice(0, -1);
  const cells = body.split("|").map((cell) => cell.trim());
  return cells.length >= 2 ? cells : null;
}

/**
 * @param {string} line
 * @returns {boolean}
 */
function isTableSeparator(line) {
  const cells = splitTableRow(line);
  if (!cells || !TABLE_SEP_RE.test(line)) return false;
  return cells.every((cell) => /^:?-{2,}:?$/.test(cell.replace(/\s+/g, "")));
}

/**
 * @param {string} source
 * @returns {Block[]}
 */
export function parseMarkdown(source) {
  /** @type {Block[]} */
  const blocks = [];
  const lines = String(source ?? "").replace(/\r\n?/g, "\n").split("\n");
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (!line.trim()) {
      index += 1;
      continue;
    }

    // 围栏代码块
    const fence = FENCE_RE.exec(line);
    if (fence) {
      const marker = fence[1];
      const lang = fence[2] || "";
      const body = [];
      index += 1;
      while (index < lines.length) {
        const candidate = lines[index];
        if (FENCE_RE.test(candidate) && candidate.trim().startsWith(marker)) {
          index += 1;
          break;
        }
        body.push(candidate);
        index += 1;
      }
      blocks.push({ type: "code", lang, content: body.join("\n") });
      continue;
    }

    // 标题
    const heading = HEADING_RE.exec(line);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, text: heading[2].trim() });
      index += 1;
      continue;
    }

    // 分隔线
    if (HR_RE.test(line)) {
      blocks.push({ type: "hr" });
      index += 1;
      continue;
    }

    // 引用
    if (QUOTE_RE.test(line)) {
      const collected = [];
      while (index < lines.length && QUOTE_RE.test(lines[index])) {
        collected.push(QUOTE_RE.exec(lines[index])[1]);
        index += 1;
      }
      blocks.push({ type: "quote", text: collected.join("\n").trim() });
      continue;
    }

    // 表格（表头 + 分隔行）
    if (line.includes("|") && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      const header = splitTableRow(line) || [];
      const rows = [];
      index += 2;
      while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
        const cells = splitTableRow(lines[index]);
        if (!cells) break;
        rows.push(cells);
        index += 1;
      }
      blocks.push({ type: "table", header, rows });
      continue;
    }

    // 列表
    if (UL_RE.test(line) || OL_RE.test(line)) {
      const ordered = OL_RE.test(line) && !UL_RE.test(line);
      /** @type {Array<{ text: string, depth: number }>} */
      const items = [];
      while (index < lines.length) {
        const candidate = lines[index];
        const unordered = UL_RE.exec(candidate);
        const numbered = OL_RE.exec(candidate);
        if (!unordered && !numbered) break;
        const indent = (unordered || numbered)[1].length;
        const text = unordered ? unordered[2] : numbered[3];
        items.push({ text, depth: indent >= 2 ? 1 : 0 });
        index += 1;
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }

    // 段落：直到空行或下一个块级起点
    const paragraph = [];
    while (index < lines.length) {
      const candidate = lines[index];
      if (!candidate.trim()) break;
      if (
        FENCE_RE.test(candidate) ||
        HEADING_RE.test(candidate) ||
        HR_RE.test(candidate) ||
        QUOTE_RE.test(candidate) ||
        UL_RE.test(candidate) ||
        OL_RE.test(candidate)
      ) {
        break;
      }
      paragraph.push(candidate.trim());
      index += 1;
    }
    if (paragraph.length > 0) {
      blocks.push({ type: "paragraph", text: paragraph.join("\n") });
    } else {
      // 兜底，避免死循环
      index += 1;
    }
  }

  return blocks;
}
