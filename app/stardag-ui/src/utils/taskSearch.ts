/**
 * The `/tasks` search: its state, how that state lives in the URL, and how
 * it maps onto `GET /tasks` filters.
 *
 * The URL is the source of truth, so a search is shareable and the back
 * button walks through searches. The parameter names are the API's own
 * (`q`, `namespace`, `name`, repeatable `tag`, `status`), so a URL reads
 * the way the request it makes does.
 */

import type { TaskFilters } from "../api/registry";
import type { TaskStatus } from "../types/task";

export const TASK_STATUSES: readonly TaskStatus[] = [
  "pending",
  "running",
  "suspended",
  "interrupted",
  "completed",
  "failed",
  "skipped",
  "cancelled",
];

export interface TaskSearchState {
  // The search box, as typed: free text, or a task-id prefix (see
  // `classifyQuery`).
  text: string;
  // Exact namespace; `""` is the root namespace, `null` is no filter.
  namespace: string | null;
  // Exact task name; `null` is no filter.
  name: string | null;
  // All of them (normalised, sorted, unique).
  tags: string[];
  status: TaskStatus | null;
}

export const EMPTY_SEARCH: TaskSearchState = {
  text: "",
  namespace: null,
  name: null,
  tags: [],
  status: null,
};

function isStatus(value: string | null): value is TaskStatus {
  return value !== null && (TASK_STATUSES as readonly string[]).includes(value);
}

/** The search a query string describes; unknown or invalid values drop out. */
export function parseSearch(search: string): TaskSearchState {
  const params = new URLSearchParams(search);
  const status = params.get("status");
  const tags = Array.from(
    new Set(
      params
        .getAll("tag")
        .map((t) => t.trim())
        .filter(Boolean),
    ),
  ).sort(compareCodePoints);
  const name = params.get("name");
  return {
    text: params.get("q") ?? "",
    // Present-but-empty is the root namespace.
    namespace: params.has("namespace") ? params.get("namespace") ?? "" : null,
    name: name ? name : null,
    tags,
    status: isStatus(status) ? status : null,
  };
}

/** The query string (with `?`, or `""` for the empty search). */
export function formatSearch(state: TaskSearchState): string {
  const params = new URLSearchParams();
  if (state.text.trim()) params.set("q", state.text.trim());
  if (state.namespace !== null) params.set("namespace", state.namespace);
  if (state.name) params.set("name", state.name);
  for (const tag of state.tags) params.append("tag", tag);
  if (state.status) params.set("status", state.status);
  const query = params.toString();
  return query ? `?${query}` : "";
}

export function isEmptySearch(state: TaskSearchState): boolean {
  return formatSearch(state) === "";
}

/** Python's `sorted()` order on strings: by code point, not UTF-16 unit. */
export function compareCodePoints(a: string, b: string): number {
  const ca = Array.from(a);
  const cb = Array.from(b);
  for (let i = 0; i < Math.min(ca.length, cb.length); i++) {
    const d = (ca[i].codePointAt(0) ?? 0) - (cb[i].codePointAt(0) ?? 0);
    if (d !== 0) return d;
  }
  return ca.length - cb.length;
}

// ---- The search box: free text or a task-id prefix ----

/** The shortest input taken as an id prefix: shorter is too ambiguous. */
export const MIN_ID_PREFIX_LENGTH = 4;

/** An explicit prefix that forces either reading. */
const ID_PREFIX_MARKER = /^id:\s*/i;

/**
 * The shortest free text sent as `q`. The server refuses a shorter one
 * (422): its trigram index cannot narrow a pattern under three characters.
 * Counted in code points after trimming, as the server counts it.
 */
export const MIN_TEXT_QUERY_LENGTH = 3;

export type QueryKind =
  | { kind: "empty" }
  | { kind: "text"; q: string }
  // Free text too short to send: suggestions only, no `q`.
  | { kind: "short"; text: string }
  | { kind: "id"; idPrefix: string };

/**
 * A task-id prefix in the canonical form task ids are stored in
 * (lowercase, dashed 8-4-4-4-12), so an id pasted without dashes or in
 * upper case still matches. `null` if it cannot be the start of a task id.
 */
export function normaliseIdPrefix(raw: string): string | null {
  const value = raw.trim().toLowerCase();
  if (!/^[0-9a-f-]+$/.test(value)) return null;
  const hex = value.replace(/-/g, "");
  if (hex.length === 0 || hex.length > 32) return null;
  // Re-dash from the hex digits: the only form the server stores.
  const groups = [8, 4, 4, 4, 12];
  const parts: string[] = [];
  let rest = hex;
  for (const size of groups) {
    if (!rest) break;
    parts.push(rest.slice(0, size));
    rest = rest.slice(size);
  }
  let dashed = parts.join("-");
  // A trailing dash the user typed at a group boundary narrows nothing,
  // but keep it when it is exactly where the stored form has one.
  if (value.endsWith("-") && hex.length < 32 && [8, 12, 16, 20].includes(hex.length)) {
    dashed += "-";
  }
  return dashed;
}

/**
 * What the search box asks for. Something that looks like the start of a
 * task id (hex, at least {@link MIN_ID_PREFIX_LENGTH} digits, with a digit
 * or a dash in it so words like "face" stay text, or any dashed uuid
 * shape) searches `id_prefix`; anything else is free text (`q`), once it
 * is at least {@link MIN_TEXT_QUERY_LENGTH} characters long — shorter text
 * is `short` and filters nothing. `id:` forces the id reading.
 */
export function classifyQuery(text: string): QueryKind {
  const trimmed = text.trim();
  if (!trimmed) return { kind: "empty" };
  if (ID_PREFIX_MARKER.test(trimmed)) {
    const prefix = normaliseIdPrefix(trimmed.replace(ID_PREFIX_MARKER, ""));
    return prefix ? { kind: "id", idPrefix: prefix } : { kind: "empty" };
  }
  const hex = trimmed.replace(/-/g, "");
  const idLike =
    /^[0-9a-f-]+$/i.test(trimmed) &&
    hex.length >= MIN_ID_PREFIX_LENGTH &&
    (/[0-9]/.test(hex) || trimmed.includes("-") || hex.length >= 8);
  if (idLike) {
    const prefix = normaliseIdPrefix(trimmed);
    if (prefix) return { kind: "id", idPrefix: prefix };
  }
  if (Array.from(trimmed).length < MIN_TEXT_QUERY_LENGTH) {
    return { kind: "short", text: trimmed };
  }
  return { kind: "text", q: trimmed };
}

/** A whole task id (UUID shaped), as typed or pasted. */
export function isFullTaskId(text: string): boolean {
  const prefix = normaliseIdPrefix(text.replace(ID_PREFIX_MARKER, ""));
  return prefix !== null && prefix.replace(/-/g, "").length === 32;
}

/**
 * The `GET /tasks` filters a search reads with (without paging). Text too
 * short to send contributes nothing, so it never reaches the server.
 */
export function toTaskFilters(state: TaskSearchState): TaskFilters {
  const filters: TaskFilters = {};
  const query = classifyQuery(state.text);
  if (query.kind === "text") filters.q = query.q;
  if (query.kind === "id") filters.idPrefix = query.idPrefix;
  if (state.namespace !== null) filters.namespace = state.namespace;
  if (state.name) filters.name = state.name;
  if (state.tags.length) filters.tags = [...state.tags];
  if (state.status) filters.status = state.status;
  return filters;
}

// ---- Edits to a search ----

export function withTag(state: TaskSearchState, tag: string): TaskSearchState {
  const t = tag.trim();
  if (!t || state.tags.includes(t)) return state;
  return { ...state, tags: [...state.tags, t].sort(compareCodePoints) };
}

export function withoutTag(state: TaskSearchState, tag: string): TaskSearchState {
  return { ...state, tags: state.tags.filter((t) => t !== tag) };
}
