import { useEffect, useState } from "react";
import { api, cachedProjects } from "../api";

/**
 * T-0602 (T-0588d, N3) — does `slug` name a project this user can see?
 *
 * Returns `null` while unresolved, then true/false against the user-scoped
 * /api/projects list. Seeds from the T-0365 module cache for an instant
 * answer, then revalidates once per slug change. A list-fetch FAILURE keeps
 * the current value — never claim not-found on a network error.
 *
 * Shared by the /p/:slug route guard (App.tsx — swaps the body for a
 * not-found panel) and the Shell (suppresses the project nav rail), so both
 * ride one cached fetch.
 *
 * T-0763: "one fetch" was a claim this file could not keep on its own. Two
 * instances mount in the same tick and each runs its own effect, so /p/:slug
 * issued /api/projects TWICE on every load (measured on staging; it is never
 * polled, so the second was pure waste). The dedupe belongs in `api.projects()`
 * — it is the only place that can see both callers — and that is where it now
 * lives; the module cache below still handles the synchronous first paint.
 * Deliberately NOT fixed by deleting a consumer: the guard and the rail need
 * the same answer independently, and hoisting it into a shared context would
 * be the data-fetching redesign this P3 ticket explicitly excludes.
 */
export function useProjectExists(
  slug: string | null | undefined,
): boolean | null {
  const inList = (rows: { slug: string }[] | null): boolean | null =>
    slug && rows ? rows.some((p) => p.slug === slug) : null;
  const [exists, setExists] = useState<boolean | null>(() =>
    inList(cachedProjects()),
  );

  useEffect(() => {
    if (!slug) {
      setExists(null);
      return;
    }
    let cancelled = false;
    setExists(inList(cachedProjects()));
    api
      .projects()
      .then((rows) => {
        if (!cancelled) setExists(rows.some((p) => p.slug === slug));
      })
      .catch(() => {
        /* keep the current answer — see docstring */
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  return slug ? exists : null;
}
