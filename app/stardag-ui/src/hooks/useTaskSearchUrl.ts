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
const LOCATION_CHANGE = "stardag:locationchange";

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

/**
 * The `/tasks` search, kept in the query string. The router tracks the
 * pathname only, so this owns `location.search` on the page: it reads it,
 * writes it with `history.pushState`/`replaceState`, and follows
 * `popstate`.
 */
export function useTaskSearchUrl(): TaskSearchUrl {
  const [search, setState] = useState(() => parseSearch(window.location.search));
  const [navigationKey, setNavigationKey] = useState(0);

  useEffect(() => {
    rememberLastQuery(formatSearch(search));
  }, [search]);

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
      const query = formatSearch(next);
      const url = `${window.location.pathname}${query}${window.location.hash}`;
      if (query !== window.location.search) {
        if (options.replace) window.history.replaceState(window.history.state, "", url);
        else window.history.pushState({}, "", url);
      }
      setState(parseSearch(query));
    },
    [],
  );

  return { search, setSearch, navigationKey };
}
