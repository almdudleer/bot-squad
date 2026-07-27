import { createContext, useContext, useEffect, useState } from "react";

import { api } from "../api";
import { buildDocIndex, DocIndex } from "./docLinks";

// T-0756: the docs index a markdown body needs to resolve its relative links.
//
// Fetched at most ONCE per slug for the whole page: every `<Markdown>` on
// screen shares one in-flight promise and one cached result, so a ticket page
// with a dozen rendered bodies still makes a single request.
//
// The fetch is OPT-IN (`enabled`). Only a body that actually contains a
// relative `.md` link needs the index, which is a docs-tree habit — ticket and
// initiative bodies almost never have one, and they must not start paying for
// a docs listing to render.

const cache = new Map<string, Promise<DocIndex>>();

export function loadDocIndex(slug: string): Promise<DocIndex> {
  const hit = cache.get(slug);
  if (hit) return hit;
  const p = api
    .docs(slug)
    .then(buildDocIndex)
    .catch((e) => {
      // Don't cache a failure: a transient error would otherwise mark every
      // cross-doc link on the page broken until a full reload.
      cache.delete(slug);
      throw e;
    });
  cache.set(slug, p);
  return p;
}

/** Test seam: render with a known index instead of fetching one. */
export const DocIndexContext = createContext<DocIndex | null>(null);

export function useDocIndex(slug: string | undefined, enabled: boolean): DocIndex | null {
  const injected = useContext(DocIndexContext);
  const [index, setIndex] = useState<DocIndex | null>(null);

  useEffect(() => {
    if (injected || !enabled || !slug) return;
    let live = true;
    // A rejected load leaves the index null, which renders cross-doc links as
    // inert text rather than as "this doc does not exist" — we don't know that
    // yet, and a wrong accusation is worse than a link that waits.
    loadDocIndex(slug).then(
      (i) => {
        if (live) setIndex(i);
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [slug, enabled, injected]);

  return injected ?? index;
}
