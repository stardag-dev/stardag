/**
 * `/task-diff`: which two task instances it compares, and how that lives
 * in the URL.
 *
 * The page's own parameters are `left`/`right` (task ids) and
 * `leftInstance`/`rightInstance` (instance ids; the newest when absent).
 * Every other parameter is the right-hand picker's search, in `/tasks`'s
 * own names (`q`, `namespace`, `name`, `tag`, `status`), so the search
 * part of a diff URL reads like a `/tasks` URL.
 */

import type { TaskInstance } from "../types/task";
import { EMPTY_SEARCH, formatSearch, type TaskSearchState } from "./taskSearch";

export interface TaskDiffSelection {
  left: string | null;
  leftInstance: string | null;
  right: string | null;
  rightInstance: string | null;
}

/** The page's own query parameters; the rest belong to the search. */
export const TASK_DIFF_PARAMS = [
  "left",
  "leftInstance",
  "right",
  "rightInstance",
] as const;

export const NO_SELECTION: TaskDiffSelection = {
  left: null,
  leftInstance: null,
  right: null,
  rightInstance: null,
};

export function parseTaskDiff(search: string): TaskDiffSelection {
  const params = new URLSearchParams(search);
  const get = (name: string) => params.get(name)?.trim() || null;
  return {
    left: get("left"),
    leftInstance: get("leftInstance"),
    right: get("right"),
    rightInstance: get("rightInstance"),
  };
}

/**
 * `current` with the selection written over the page's own parameters,
 * which come first; the search's are kept as they are. With `?`, or `""`.
 */
export function withSelection(current: string, selection: TaskDiffSelection): string {
  const from = new URLSearchParams(current);
  const out = new URLSearchParams();
  for (const name of TASK_DIFF_PARAMS) {
    const value = selection[name];
    if (value) out.set(name, value);
  }
  for (const [name, value] of from) {
    if (!(TASK_DIFF_PARAMS as readonly string[]).includes(name))
      out.append(name, value);
  }
  const query = out.toString();
  return query ? `?${query}` : "";
}

/** The query string of a diff: the selection, then the picker's search. */
export function taskDiffQuery(
  selection: TaskDiffSelection,
  search: TaskSearchState = EMPTY_SEARCH,
): string {
  return withSelection(formatSearch(search), selection);
}

/** The search the right-hand picker starts from: the left task's type. */
export function sameTypeSearch(namespace: string, name: string): TaskSearchState {
  return { ...EMPTY_SEARCH, namespace, name };
}

/** What the "Compare" action opens: this task (and instance) on the left. */
export interface CompareTarget {
  taskId: string;
  instanceId: string | null;
  namespace: string;
  name: string;
}

/** The query string the "Compare" action navigates to. */
export function compareQuery(target: CompareTarget): string {
  return taskDiffQuery(
    { ...NO_SELECTION, left: target.taskId, leftInstance: target.instanceId },
    sameTypeSearch(target.namespace, target.name),
  );
}

/** Whether a search is the left task's type, exactly: the pre-filter, unloosened. */
export function isSameTypeSearch(
  search: TaskSearchState,
  namespace: string,
  name: string,
): boolean {
  return search.namespace === namespace && search.name === name;
}

/** The instance a side shows: the one asked for, else the newest. */
export function pickInstance(
  instances: TaskInstance[],
  instanceId: string | null,
): TaskInstance | null {
  return instances.find((i) => i.id === instanceId) ?? instances[0] ?? null;
}

/**
 * The instance the right side shows when it is the left side's task and
 * no instance is asked for: the newest other than the left's, so "compare
 * with another instance of this task" compares two different ones.
 */
export function pickOtherInstance(
  instances: TaskInstance[],
  instanceId: string | null,
  avoid: string | null,
): TaskInstance | null {
  if (instanceId) return pickInstance(instances, instanceId);
  return instances.find((i) => i.id !== avoid) ?? instances[0] ?? null;
}
