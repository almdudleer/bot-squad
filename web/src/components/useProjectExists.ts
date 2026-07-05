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
