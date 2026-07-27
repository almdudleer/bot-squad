import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Link } from "react-router-dom";

import { Mermaid } from "./Mermaid";

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
// - A ```mermaid fence renders as the DIAGRAM (T-0290 leg c). It lands here, in
//   the one renderer, rather than on any single page: docs, vision and ticket
//   bodies all reach the screen through this component (Docs -> Markdown;
//   Vision/TaskDetail -> MarkdownBody -> Markdown), so one change gives every
//   surface diagrams and no second copy exists to disagree later.

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

// hast (post-mdast) node shapes — what react-markdown hands the `components`
// renderers via `node`. Distinct from MdNode above, which is the mdast side the
// remark plugin walks.
interface HastNode {
  type: string;
  tagName?: string;
  value?: string;
  properties?: { className?: unknown };
  children?: HastNode[];
}

// The mermaid source of a ```mermaid fence, or null for every other <pre>.
//
// The interception point is `pre`, NOT `code`: a `code` renderer sees every
// fenced block AND every inline span, and swapping a <div> in there leaves the
// diagram nested inside the <pre> that react-markdown still emits (monospace,
// pre-wrapped SVG). Matching the whole <pre> whose single child is a
// language-mermaid <code> diverts exactly the diagram fences and leaves
// ```python and friends untouched.
function mermaidSource(node: HastNode | undefined): string | null {
  const kids = node?.children ?? [];
  const code = kids.length === 1 ? kids[0] : undefined;
  if (!code || code.type !== "element" || code.tagName !== "code") return null;
  const raw = code.properties?.className;
  const classes = Array.isArray(raw)
    ? raw.map(String)
    : typeof raw === "string"
      ? raw.split(/\s+/)
      : [];
  if (!classes.includes("language-mermaid")) return null;
  const text = (code.children ?? [])
    .filter((c) => c.type === "text")
    .map((c) => c.value ?? "")
    .join("");
  // An empty fence has no diagram to draw — fall through to the normal code
  // block rather than parking a "Rendering diagram…" spinner that never ends.
  return text.trim() ? text.replace(/\n$/, "") : null;
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
          // Mermaid itself is loaded lazily INSIDE <Mermaid> (a dynamic import
          // fired from its effect), so importing the component here costs a
          // page with no fence nothing — the multi-MB bundle is fetched only
          // once a diagram actually mounts.
          pre({ node, children, ...props }) {
            const code = mermaidSource(node as HastNode | undefined);
            if (code !== null) return <Mermaid code={code} />;
            return <pre {...props}>{children}</pre>;
          },
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
