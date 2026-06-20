import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Link } from "react-router-dom";

// T-0275: shared markdown-body renderer for the artifact-reading surfaces
// (docs / user feedback / use cases). Replaces the old raw `<pre>` dumps.
//
// - Bodies stored on disk carry a leading YAML frontmatter block (id/title/…);
//   we strip it so the reader sees prose, not the `---` metadata.
// - Inline `T-NNNN` / `D-NNNN` mentions linkify to the ticket / doc (the
//   doc→ticket/doc half of the bidirectional mention story, T-0172) — but only
//   when a `slug` is supplied so we can build the in-app route.
// - Rendering goes through react-markdown (no raw/unsanitized HTML); HTML in the
//   source is treated as text, so this is XSS-safe by construction.

// Strip a single leading YAML frontmatter block ("---\n … \n---\n"). Idempotent
// for bodies that don't have one.
function stripFrontmatter(src: string): string {
  const m = src.match(/^﻿?---\r?\n[\s\S]*?\r?\n---\r?\n?/);
  return m ? src.slice(m[0].length) : src;
}

const MENTION_RE = /\b([TD]-\d{4,})\b/g;

// mdast node shapes we touch (kept loose — react-markdown re-types internally).
interface MdNode {
  type: string;
  value?: string;
  url?: string;
  children?: MdNode[];
}

// remark plugin: split text nodes on T-/D- mentions into link nodes carrying a
// `mention:` href that the custom <a> renderer resolves to an in-app route.
// Walks manually (no unist-util-visit dep). Mentions already inside a link or a
// code node are left alone.
function remarkMentions() {
  return (tree: MdNode) => walk(tree, false);
}

function walk(node: MdNode, inLink: boolean): void {
  if (!node.children) return;
  const out: MdNode[] = [];
  for (const child of node.children) {
    if (child.type === "text" && !inLink && typeof child.value === "string") {
      out.push(...splitMentions(child.value));
    } else {
      walk(child, inLink || child.type === "link");
      out.push(child);
    }
  }
  node.children = out;
}

function splitMentions(value: string): MdNode[] {
  const nodes: MdNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  MENTION_RE.lastIndex = 0;
  while ((m = MENTION_RE.exec(value)) !== null) {
    if (m.index > last) nodes.push({ type: "text", value: value.slice(last, m.index) });
    nodes.push({
      type: "link",
      url: `mention:${m[1]}`,
      children: [{ type: "text", value: m[1] }],
    });
    last = m.index + m[0].length;
  }
  if (nodes.length === 0) return [{ type: "text", value }];
  if (last < value.length) nodes.push({ type: "text", value: value.slice(last) });
  return nodes;
}

export function Markdown({ source, slug }: { source: string; slug?: string }) {
  const body = stripFrontmatter(source ?? "");
  return (
    <div className="mc-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMentions]}
        // Keep react-markdown's default URL sanitizer (drops javascript:/data:
        // etc. — XSS-safe) but let our synthetic `mention:` scheme through so
        // the <a> renderer below can turn it into an in-app route.
        urlTransform={(url) => (url.startsWith("mention:") ? url : defaultUrlTransform(url))}
        components={{
          a({ node: _node, href, children, ...props }) {
            const mention =
              href && href.startsWith("mention:") ? href.slice("mention:".length) : null;
            if (mention) {
              if (!slug) return <>{children}</>;
              const to = mention.startsWith("D-")
                ? `/p/${slug}/docs?doc=${mention}`
                : `/p/${slug}/t/${mention}`;
              return (
                <Link to={to} style={{ fontFamily: "var(--mc-mono)" }}>
                  {children}
                </Link>
              );
            }
            return (
              <a href={href} target="_blank" rel="noreferrer noopener" {...props}>
                {children}
              </a>
            );
          },
        }}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
}
