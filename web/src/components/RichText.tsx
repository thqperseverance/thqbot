/** Markdown 渲染：把 markdown.js 解析出的结构渲染成 React 元素（不使用 innerHTML）。 */
import { useCallback, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { parseInline, parseMarkdown } from "../markdown.js";
import { CheckIcon, CopyIcon } from "./Icons";

/** markdown.js 是纯 JS，这里补上渲染需要的形状。 */
type InlineSpan = {
  type: "text" | "code" | "strong" | "em" | "strike" | "link";
  value: string;
  href?: string;
};

type Block =
  | { type: "paragraph"; text: string }
  | { type: "heading"; level: number; text: string }
  | { type: "code"; lang: string; content: string }
  | { type: "list"; ordered: boolean; items: Array<{ text: string; depth: number }> }
  | { type: "quote"; text: string }
  | { type: "table"; header: string[]; rows: string[][] }
  | { type: "hr" };

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const spans = parseInline(text) as InlineSpan[];
  const nodes: ReactNode[] = [];
  spans.forEach((span, index) => {
    const key = `${keyPrefix}-${index}`;
    switch (span.type) {
      case "code":
        nodes.push(
          <code className="md-code" key={key}>
            {span.value}
          </code>,
        );
        break;
      case "strong":
        nodes.push(<strong key={key}>{span.value}</strong>);
        break;
      case "em":
        nodes.push(<em key={key}>{span.value}</em>);
        break;
      case "strike":
        nodes.push(<del key={key}>{span.value}</del>);
        break;
      case "link":
        nodes.push(
          <a key={key} href={span.href} target="_blank" rel="noreferrer noopener">
            {span.value}
          </a>,
        );
        break;
      default:
        // 纯文本直接返回字符串，避免多包一层无意义的 <span>
        nodes.push(span.value);
    }
  });
  return nodes;
}

/** 段落内保留换行。 */
function renderParagraph(text: string, keyPrefix: string): ReactNode {
  const lines = text.split("\n");
  return lines.map((line, index) => (
    <span key={`${keyPrefix}-l${index}`}>
      {index > 0 && <br />}
      {renderInline(line, `${keyPrefix}-l${index}`)}
    </span>
  ));
}

function CodeBlock({ lang, content }: { lang: string; content: string }) {
  const [copied, setCopied] = useState(false);

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }, [content]);

  return (
    <div className="md-pre">
      <div className="md-pre-head">
        <span className="md-pre-lang">{lang || "text"}</span>
        <button className="md-pre-copy" onClick={() => void copy()} title="复制代码">
          {copied ? <CheckIcon size={13} /> : <CopyIcon size={13} />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre>
        <code>{content}</code>
      </pre>
    </div>
  );
}

interface ListItem {
  text: string;
  children: ListItem[];
}

/** 把扁平的 (depth, text) 序列还原成树，保证嵌套列表嵌在父 <li> 内（而不是 ul > ul）。 */
function buildListTree(items: Array<{ text: string; depth: number }>): ListItem[] {
  const roots: ListItem[] = [];
  let current: ListItem | null = null;
  for (const item of items) {
    if (item.depth > 0 && current) {
      current.children.push({ text: item.text, children: [] });
      continue;
    }
    current = { text: item.text, children: [] };
    roots.push(current);
  }
  return roots;
}

function renderListItems(nodes: ListItem[], ordered: boolean, keyPrefix: string): ReactNode {
  const Tag = ordered ? "ol" : "ul";
  return (
    <Tag className="md-list">
      {nodes.map((node, index) => (
        <li key={`${keyPrefix}-${index}`}>
          {renderInline(node.text, `${keyPrefix}-${index}`)}
          {node.children.length > 0 && renderListItems(node.children, false, `${keyPrefix}-${index}-sub`)}
        </li>
      ))}
    </Tag>
  );
}

function ListBlock({ ordered, items }: { ordered: boolean; items: Array<{ text: string; depth: number }> }) {
  return <>{renderListItems(buildListTree(items), ordered, "li")}</>;
}

export default function RichText({ content, className }: { content: string; className?: string }) {
  const blocks = useMemo(() => parseMarkdown(content) as Block[], [content]);

  return (
    <div className={className ? `md ${className}` : "md"}>
      {blocks.map((block, index) => {
        const key = `b${index}`;
        switch (block.type) {
          case "heading": {
            const level = Math.min(6, Math.max(1, block.level));
            const Tag = `h${level}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
            return (
              <Tag className={`md-h md-h${level}`} key={key}>
                {renderInline(block.text, key)}
              </Tag>
            );
          }
          case "code":
            return <CodeBlock key={key} lang={block.lang} content={block.content} />;
          case "list":
            return <ListBlock key={key} ordered={block.ordered} items={block.items} />;
          case "quote":
            return (
              <blockquote className="md-quote" key={key}>
                {renderParagraph(block.text, key)}
              </blockquote>
            );
          case "table":
            return (
              <div className="md-table-wrap" key={key}>
                <table className="md-table">
                  <thead>
                    <tr>
                      {block.header.map((cell, cellIndex) => (
                        <th key={`th-${cellIndex}`}>{renderInline(cell, `th-${cellIndex}`)}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, rowIndex) => (
                      <tr key={`tr-${rowIndex}`}>
                        {row.map((cell, cellIndex) => (
                          <td key={`td-${rowIndex}-${cellIndex}`}>
                            {renderInline(cell, `td-${rowIndex}-${cellIndex}`)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          case "hr":
            return <hr className="md-hr" key={key} />;
          default:
            return (
              <p className="md-p" key={key}>
                {renderParagraph(block.text, key)}
              </p>
            );
        }
      })}
    </div>
  );
}
