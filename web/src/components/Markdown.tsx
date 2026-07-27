import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Link } from "react-router-dom";

import { classifyDocHref, docRoute, EMPTY_DOC_INDEX } from "./docLinks";
import { Mermaid } from "./Mermaid";
import { useDocIndex } from "./useDocIndex";

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

// T-0756: does this body carry a relative link at all? Gates the docs-index
// fetch so only the bodies that need it pay for it. Deliberately loose — a
// false positive costs one cached GET, a false negative silently reinstates
// the bug.
const RELATIVE_LINK_RE = /\]\(\s*(?!#)(?![a-z][a-z0-9+.-]*:)(?!\/)[^)\s]/i;

export function Markdown({
  source,
  slug,
  // T-0756: the category dir of the doc being rendered, so `./x.md` and
  // `../design/x.md` resolve the way they do on disk. Only the Docs pane knows
  // it; every other surface renders bodies that live outside the docs tree and
  // omits it, which falls the resolver back to the tree-wide stem index.
  baseCategory,
}: {
  source: string;
  slug?: string;
  baseCategory?: string;
}) {
  const body = stripFrontmatter(source ?? "");
  const docIndex = useDocIndex(slug, RELATIVE_LINK_RE.test(body));
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
            // T-0756: a relative href in a doc body is a link into the file
            // tree, not a URL. Left alone it resolves against the PATH — and
            // because docs route by query param (`?doc=<id>`), the param is
            // dropped and the reader lands silently on the project board.
            // Resolve it to the docs route instead, and when nothing is behind
            // it, say so loudly rather than navigating somewhere plausible.
            const relative = href
              ? classifyDocHref(href, docIndex ?? EMPTY_DOC_INDEX, baseCategory)
              : null;
            if (relative && relative.kind !== "external" && relative.kind !== "fragment") {
              if (docIndex === null) {
                // Index still loading (or its fetch failed) — we do not yet
                // know whether the target exists. Inert text beats both a wrong
                // navigation and a wrong "broken" accusation.
                return <span className="mc-doclink-pending">{children}</span>;
              }
              if (relative.kind === "doc" && slug) {
                return (
                  <Link to={docRoute(slug, relative.id, relative.fragment)}>{children}</Link>
                );
              }
              const target = relative.kind === "missing" ? relative.target : href;
              return (
                <span
                  className="mc-doclink-broken"
                  title={`No document matches ${target} — this link is broken.`}
                >
                  {children}
                  <span aria-hidden="true"> ⚠</span>
                </span>
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
