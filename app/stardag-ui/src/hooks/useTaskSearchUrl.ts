import { useCallback, useEffect, useState } from "react";
import { formatSearch, parseSearch, type TaskSearchState } from "../utils/taskSearch";

// The last `/tasks` query string, so "Tasks" in a task page's breadcrumb
// returns to the search the task was opened from. Session-scoped.
const LAST_SEARCH_KEY = "stardag.taskSearch.last";

export function lastTaskSearchQuery(): string {
  try {
    return sessionStorage.getItem(LAST_SEARCH_KEY) ?? "";
  } catch {
    return "";
  }
}

function rememberLastQuery(query: string): void {
  try {
    sessionStorage.setItem(LAST_SEARCH_KEY, query);
  } catch {
    // Storage unavailable: the breadcrumb returns to a fresh search.
  }
}

// Fired by the app's own navigation (`pushState` emits no `popstate`), so
// following a link to `/tasks` from `/tasks?q=x` resets the search.
export const LOCATION_CHANGE = "stardag:locationchange";

export function notifyLocationChange(): void {
  window.dispatchEvent(new Event(LOCATION_CHANGE));
}

export interface TaskSearchUrl {
  search: TaskSearchState;
  /**
   * Write a search to the URL. `replace` for edits that should not each
   * become a history entry (typing); otherwise a new entry, so Back undoes
   * a filter change.
   */
  setSearch: (next: TaskSearchState, options?: { replace?: boolean }) => void;
  // Bumped when the URL changed under the page (Back/Forward, or the app's
  // own navigation to the same path), so state
  // derived from the search — the text being typed — can re-sync.
  navigationKey: number;
}

export interface TaskSearchUrlOptions {
  /**
   * Query parameters that belong to the page rather than the search
   * (`/task-diff`'s selections): kept as they are when the search is
   * written. Every other parameter is the search's.
   */
  keep?: readonly string[];
  /**
   * Remember the search as the one "Tasks" in a breadcrumb returns to.
   * On for `/tasks` only; a search embedded in another page is not one.
   */
  remember?: boolean;
}

const NO_KEPT: readonly string[] = [];

/**
 * The search's query string with the page's own `keep` parameters from
 * `current` in front of it (with `?`, or `""` when there is neither).
 */
export function withKeptParams(
  current: string,
  searchQuery: string,
  keep: readonly string[],
): string {
  const from = new URLSearchParams(current);
  const out = new URLSearchParams();
  for (const name of keep) {
    for (const value of from.getAll(name)) out.append(name, value);
  }
  for (const [name, value] of new URLSearchParams(searchQuery)) {
    out.append(name, value);
  }
  const query = out.toString();
  return query ? `?${query}` : "";
}

/**
 * The `/tasks` search, kept in the query string. The router tracks the
 * pathname only, so this owns `location.search` on the page (all of it
 * but the `keep` parameters): it reads it, writes it with
 * `history.pushState`/`replaceState`, and follows `popstate`.
 */
export function useTaskSearchUrl({
  keep = NO_KEPT,
  remember = true,
}: TaskSearchUrlOptions = {}): TaskSearchUrl {
  const [search, setState] = useState(() => parseSearch(window.location.search));
  const [navigationKey, setNavigationKey] = useState(0);

  useEffect(() => {
    if (remember) rememberLastQuery(formatSearch(search));
  }, [search, remember]);

  useEffect(() => {
    const onPopState = () => {
      setState(parseSearch(window.location.search));
      setNavigationKey((k) => k + 1);
    };
    window.addEventListener("popstate", onPopState);
    window.addEventListener(LOCATION_CHANGE, onPopState);
    return () => {
      window.removeEventListener("popstate", onPopState);
      window.removeEventListener(LOCATION_CHANGE, onPopState);
    };
  }, []);

  const setSearch = useCallback(
    (next: TaskSearchState, options: { replace?: boolean } = {}) => {
      const query = withKeptParams(window.location.search, formatSearch(next), keep);
      const url = `${window.location.pathname}${query}${window.location.hash}`;
      if (query !== window.location.search) {
        if (options.replace) window.history.replaceState(window.history.state, "", url);
        else window.history.pushState({}, "", url);
      }
      setState(parseSearch(query));
    },
    [keep],
  );

  return { search, setSearch, navigationKey };
}
