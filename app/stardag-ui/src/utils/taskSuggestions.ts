/**
 * Autocomplete for the `/tasks` search, derived client-side: the registry
 * serves no suggestion route, so names, namespaces and tags are harvested
 * from the task pages the browser has already read, and recent searches are
 * remembered. Kept per environment in `localStorage`, bounded.
 */

import type { TaskSummary } from "../types/task";
import { qualifiedName } from "./instances";
import {
  compareCodePoints,
  formatSearch,
  isEmptySearch,
  parseSearch,
  type TaskSearchState,
} from "./taskSearch";

const STORAGE_PREFIX = "stardag.taskSearch.v1.";
const MAX_NAMES = 300;
const MAX_TAGS = 300;
const MAX_RECENT = 8;
export const MAX_SUGGESTIONS = 8;

interface KnownName {
  namespace: string;
  name: string;
}

export interface SuggestionStore {
  // Most recently seen first.
  names: KnownName[];
  tags: string[];
  // Query strings (`formatSearch`), most recent first.
  recent: string[];
}

export type Suggestion =
  | { kind: "name"; namespace: string; name: string; label: string }
  | { kind: "namespace"; namespace: string; label: string }
  | { kind: "tag"; tag: string; label: string }
  | { kind: "recent"; search: TaskSearchState; label: string };

function emptyStore(): SuggestionStore {
  return { names: [], tags: [], recent: [] };
}

function key(environmentId: string): string {
  return `${STORAGE_PREFIX}${environmentId}`;
}

export function loadStore(environmentId: string): SuggestionStore {
  try {
    const raw = localStorage.getItem(key(environmentId));
    if (!raw) return emptyStore();
    const parsed = JSON.parse(raw) as Partial<SuggestionStore>;
    return {
      names: Array.isArray(parsed.names)
        ? parsed.names.filter(
            (n): n is KnownName =>
              !!n && typeof n.name === "string" && typeof n.namespace === "string",
          )
        : [],
      tags: Array.isArray(parsed.tags)
        ? parsed.tags.filter((t): t is string => typeof t === "string")
        : [],
      recent: Array.isArray(parsed.recent)
        ? parsed.recent.filter((r): r is string => typeof r === "string")
        : [],
    };
  } catch {
    return emptyStore();
  }
}

function saveStore(environmentId: string, store: SuggestionStore): void {
  try {
    localStorage.setItem(key(environmentId), JSON.stringify(store));
  } catch {
    // Full or unavailable storage only loses suggestions.
  }
}

/** Fold the names and tags of a page of tasks into the store. */
export function harvest(store: SuggestionStore, tasks: TaskSummary[]): SuggestionStore {
  const names = [...store.names];
  const seen = new Set(names.map((n) => `${n.namespace}\u0000${n.name}`));
  for (const task of tasks) {
    const id = `${task.task_namespace}\u0000${task.task_name}`;
    if (!seen.has(id)) {
      seen.add(id);
      names.push({ namespace: task.task_namespace, name: task.task_name });
    }
  }
  const tags = new Set(store.tags);
  for (const task of tasks) for (const tag of task.tags ?? []) tags.add(tag);
  return {
    ...store,
    names: names.slice(-MAX_NAMES),
    tags: Array.from(tags).sort(compareCodePoints).slice(0, MAX_TAGS),
  };
}

export function harvestInto(environmentId: string, tasks: TaskSummary[]): void {
  if (tasks.length === 0) return;
  saveStore(environmentId, harvest(loadStore(environmentId), tasks));
}

/** Remember a search (most recent first, deduplicated, bounded). */
export function remember(
  store: SuggestionStore,
  search: TaskSearchState,
): SuggestionStore {
  if (isEmptySearch(search)) return store;
  const query = formatSearch(search);
  return {
    ...store,
    recent: [query, ...store.recent.filter((r) => r !== query)].slice(0, MAX_RECENT),
  };
}

export function rememberSearch(environmentId: string, search: TaskSearchState): void {
  saveStore(environmentId, remember(loadStore(environmentId), search));
}

/** A one-line summary of a search, for a "recent" suggestion. */
export function describeSearch(search: TaskSearchState): string {
  const parts: string[] = [];
  if (search.text.trim()) parts.push(`“${search.text.trim()}”`);
  if (search.namespace !== null && search.name) {
    parts.push(`name: ${qualifiedName(search.namespace, search.name)}`);
  } else {
    if (search.namespace !== null) {
      parts.push(`namespace: ${search.namespace || "(root)"}`);
    }
    if (search.name) parts.push(`name: ${search.name}`);
  }
  for (const tag of search.tags) parts.push(`#${tag}`);
  if (search.status) parts.push(`status: ${search.status}`);
  return parts.join("  ");
}

function rank(haystack: string, needle: string): number | null {
  const h = haystack.toLowerCase();
  const index = h.indexOf(needle);
  if (index < 0) return null;
  // Prefix first, then a match at a word/segment start, then anywhere.
  if (index === 0) return 0;
  if (/[._\-\s]/.test(h[index - 1])) return 1;
  return 2;
}

/**
 * Suggestions for the search box. With no text: the recent searches. With
 * text: known names, namespaces and tags containing it (prefix matches
 * first), skipping what the search already filters on.
 */
export function suggest(
  store: SuggestionStore,
  text: string,
  current: TaskSearchState,
): Suggestion[] {
  const needle = text.trim().toLowerCase();
  if (!needle) {
    return store.recent
      .map((query) => parseSearch(query))
      .filter((search) => formatSearch(search) !== formatSearch(current))
      .map((search) => ({
        kind: "recent" as const,
        search,
        label: describeSearch(search),
      }))
      .filter((s) => s.label)
      .slice(0, MAX_SUGGESTIONS);
  }
  const scored: { score: number; suggestion: Suggestion }[] = [];
  const namespaces = new Set<string>();
  for (const { namespace, name } of store.names) {
    if (namespace) namespaces.add(namespace);
    if (current.name === name && current.namespace === namespace) continue;
    const qualified = qualifiedName(namespace, name);
    const r = Math.min(rank(name, needle) ?? 9, rank(qualified, needle) ?? 9);
    if (r < 9) {
      scored.push({
        score: r,
        suggestion: { kind: "name", namespace, name, label: qualified },
      });
    }
  }
  for (const namespace of namespaces) {
    if (current.namespace === namespace) continue;
    const r = rank(namespace, needle);
    if (r !== null) {
      scored.push({
        score: r + 0.1,
        suggestion: { kind: "namespace", namespace, label: namespace },
      });
    }
  }
  for (const tag of store.tags) {
    if (current.tags.includes(tag)) continue;
    const r = rank(tag, needle);
    if (r !== null) {
      scored.push({ score: r + 0.2, suggestion: { kind: "tag", tag, label: tag } });
    }
  }
  return scored
    .sort(
      (a, b) =>
        a.score - b.score || compareCodePoints(a.suggestion.label, b.suggestion.label),
    )
    .slice(0, MAX_SUGGESTIONS)
    .map((s) => s.suggestion);
}
