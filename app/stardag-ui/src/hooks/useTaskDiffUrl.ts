import { useCallback, useEffect, useState } from "react";
import {
  parseTaskDiff,
  withSelection,
  type TaskDiffSelection,
} from "../utils/taskDiff";
import { LOCATION_CHANGE } from "./useTaskSearchUrl";

export interface TaskDiffUrl {
  selection: TaskDiffSelection;
  /**
   * Write a selection to the URL, keeping the search's parameters. A new
   * history entry unless `replace`, so Back returns to the previous pick.
   */
  select: (next: TaskDiffSelection, options?: { replace?: boolean }) => void;
}

/**
 * `/task-diff`'s two selections, kept in the query string next to the
 * picker's search (which `useTaskSearchUrl` owns); each writes only its
 * own parameters, and both follow `popstate`.
 */
export function useTaskDiffUrl(): TaskDiffUrl {
  const [selection, setSelection] = useState(() =>
    parseTaskDiff(window.location.search),
  );

  useEffect(() => {
    const follow = () => setSelection(parseTaskDiff(window.location.search));
    window.addEventListener("popstate", follow);
    window.addEventListener(LOCATION_CHANGE, follow);
    return () => {
      window.removeEventListener("popstate", follow);
      window.removeEventListener(LOCATION_CHANGE, follow);
    };
  }, []);

  const select = useCallback(
    (next: TaskDiffSelection, options: { replace?: boolean } = {}) => {
      const query = withSelection(window.location.search, next);
      const url = `${window.location.pathname}${query}${window.location.hash}`;
      if (query !== window.location.search) {
        if (options.replace) window.history.replaceState(window.history.state, "", url);
        else window.history.pushState({}, "", url);
      }
      setSelection(parseTaskDiff(query));
    },
    [],
  );

  return { selection, select };
}
