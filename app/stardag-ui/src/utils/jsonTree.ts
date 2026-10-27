/**
 * The pure half of the JSON tree view: indexing a value into a flat node
 * list, deciding which nodes are visible, and searching.
 *
 * Everything the component needs per keystroke or per toggle is a linear
 * pass over the index, which is built once per value. The index is in
 * preorder and every node records where its subtree ends, so skipping a
 * collapsed subtree is a jump rather than a walk.
 */

import { identityOf, qualifiedName } from "./instances";

export type JsonKind = "object" | "array" | "string" | "number" | "boolean" | "null";

export interface JsonNode {
  /** The node's JSON path (`$.a[0]["b c"]`): its identity, and what "copy path" copies. */
  id: string;
  /** The object key or array index under the parent; `null` for the root. */
  key: string | number | null;
  /** The root is 0. */
  depth: number;
  kind: JsonKind;
  value: unknown;
  /** Number of children; 0 for a scalar. */
  size: number;
  /** Index of the parent in the node list; -1 for the root. */
  parent: number;
  /** Exclusive index of the end of this node's subtree. */
  end: number;
  /** 1-based position among its siblings, and their count (for `aria-posinset`/`aria-setsize`). */
  posInSet: number;
  setSize: number;
  /** `namespace.Name` when the node is an object carrying a task body's discriminators. */
  taskLabel: string | null;
  /** Lower-cased key (object keys only) and scalar text, precomputed for search. */
  keyLower: string | null;
  valueLower: string | null;
}

const IDENTIFIER = /^[A-Za-z_$][\w$]*$/;

/** The path segment for a key: `.name`, `[0]`, or `["odd key"]`. */
export function pathSegment(key: string | number): string {
  if (typeof key === "number") return `[${key}]`;
  return IDENTIFIER.test(key) ? `.${key}` : `[${JSON.stringify(key)}]`;
}

export function kindOf(value: unknown): JsonKind {
  if (value === null || value === undefined) return "null";
  if (Array.isArray(value)) return "array";
  switch (typeof value) {
    case "object":
      return "object";
    case "string":
      return "string";
    case "number":
    case "bigint":
      return "number";
    case "boolean":
      return "boolean";
    default:
      return "string";
  }
}

export function isContainer(node: JsonNode): boolean {
  return node.kind === "object" || node.kind === "array";
}

/** The text a scalar is drawn with (and searched by): strings unquoted. */
export function scalarText(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "string") return value;
  return String(value);
}

function taskLabelOf(value: Record<string, unknown>): string | null {
  const { namespace, name } = identityOf(value);
  return name ? qualifiedName(namespace, name) : null;
}

/** Index a JSON value into a preorder node list. The root is node 0. */
export function indexJson(root: unknown): JsonNode[] {
  const nodes: JsonNode[] = [];

  function visit(
    value: unknown,
    key: string | number | null,
    id: string,
    depth: number,
    parent: number,
    posInSet: number,
    setSize: number,
  ): void {
    const kind = kindOf(value);
    const index = nodes.length;
    const entries: [string | number, unknown][] =
      kind === "array"
        ? (value as unknown[]).map((v, i) => [i, v])
        : kind === "object"
          ? Object.entries(value as Record<string, unknown>)
          : [];
    const node: JsonNode = {
      id,
      key,
      depth,
      kind,
      value,
      size: entries.length,
      parent,
      end: index + 1,
      posInSet,
      setSize,
      taskLabel:
        kind === "object" ? taskLabelOf(value as Record<string, unknown>) : null,
      keyLower: typeof key === "string" ? key.toLowerCase() : null,
      valueLower:
        kind === "object" || kind === "array" ? null : scalarText(value).toLowerCase(),
    };
    nodes.push(node);
    entries.forEach(([childKey, childValue], i) =>
      visit(
        childValue,
        childKey,
        id + pathSegment(childKey),
        depth + 1,
        index,
        i + 1,
        entries.length,
      ),
    );
    node.end = nodes.length;
  }

  visit(root, null, "$", 0, -1, 1, 1);
  return nodes;
}

/**
 * The indices of the visible rows, in display order. The root itself is
 * not drawn — its children are the top level — and is always expanded.
 */
export function visibleRows(
  nodes: JsonNode[],
  expanded: ReadonlySet<string>,
): number[] {
  const rows: number[] = [];
  let i = 1;
  while (i < nodes.length) {
    const node = nodes[i];
    rows.push(i);
    i = isContainer(node) && !expanded.has(node.id) ? node.end : i + 1;
  }
  return rows;
}

/** Every container's id: "expand all". */
export function allContainerIds(nodes: JsonNode[]): Set<string> {
  return new Set(nodes.filter(isContainer).map((node) => node.id));
}

/**
 * The initially expanded containers: every level shallower than
 * `maxDepth` (the root is depth 0), stopping early once opening the next
 * level would show more than `budget` rows. The top level is always
 * shown, whatever its size.
 */
export function defaultExpanded(
  nodes: JsonNode[],
  maxDepth = 2,
  budget = 200,
): Set<string> {
  const expanded = new Set<string>();
  if (nodes.length === 0) return expanded;
  expanded.add(nodes[0].id);
  let shown = nodes[0].size;
  for (let depth = 1; depth < maxDepth; depth++) {
    const level = nodes.filter((node) => node.depth === depth && isContainer(node));
    const added = level.reduce((sum, node) => sum + node.size, 0);
    if (level.length === 0 || shown + added > budget) break;
    for (const node of level) expanded.add(node.id);
    shown += added;
  }
  return expanded;
}

export interface JsonMatch {
  /** Index into the node list. */
  index: number;
  inKey: boolean;
  inValue: boolean;
}

/**
 * The nodes whose key or scalar value contains `query`, case-insensitively,
 * in display order. Array indices and container values are not searched:
 * an index is a position, not content, and a container's content is its
 * children's. An empty or whitespace-only query matches nothing.
 */
export function findMatches(nodes: JsonNode[], query: string): JsonMatch[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const matches: JsonMatch[] = [];
  for (let i = 1; i < nodes.length; i++) {
    const node = nodes[i];
    const inKey = node.keyLower !== null && node.keyLower.includes(q);
    const inValue = node.valueLower !== null && node.valueLower.includes(q);
    if (inKey || inValue) matches.push({ index: i, inKey, inValue });
  }
  return matches;
}

/** The ids of every ancestor of the given nodes: what to expand to reveal them. */
export function ancestorIds(nodes: JsonNode[], indices: Iterable<number>): Set<string> {
  const ids = new Set<string>();
  for (const index of indices) {
    let parent = nodes[index]?.parent ?? -1;
    while (parent >= 0 && !ids.has(nodes[parent].id)) {
      ids.add(nodes[parent].id);
      parent = nodes[parent].parent;
    }
  }
  return ids;
}

/** `a` plus everything in `b`; `a` itself when nothing is added, so a state setter can bail out. */
export function union(
  a: ReadonlySet<string>,
  b: Iterable<string>,
): ReadonlySet<string> {
  let out: Set<string> | null = null;
  for (const id of b) {
    if (a.has(id)) continue;
    out ??= new Set(a);
    out.add(id);
  }
  return out ?? a;
}

export interface Segment {
  text: string;
  match: boolean;
}

/** `text` split into runs that do and do not contain `query`, case-insensitively. */
export function highlightSegments(text: string, query: string): Segment[] {
  const q = query.trim().toLowerCase();
  if (!q) return [{ text, match: false }];
  const lower = text.toLowerCase();
  const segments: Segment[] = [];
  let from = 0;
  let at = lower.indexOf(q);
  while (at !== -1) {
    if (at > from) segments.push({ text: text.slice(from, at), match: false });
    segments.push({ text: text.slice(at, at + q.length), match: true });
    from = at + q.length;
    at = lower.indexOf(q, from);
  }
  if (from < text.length) segments.push({ text: text.slice(from), match: false });
  return segments;
}

/** What "copy value" puts on the clipboard: a string as-is, anything else as JSON. */
export function copyValueText(node: JsonNode): string {
  if (node.kind === "string") return node.value as string;
  if (isContainer(node)) return JSON.stringify(node.value, null, 2);
  return scalarText(node.value);
}

/** A collapsed container's summary: `3 keys`, `1 item`. */
export function sizeLabel(node: JsonNode): string {
  if (node.kind === "array") return `${node.size} item${node.size === 1 ? "" : "s"}`;
  return `${node.size} key${node.size === 1 ? "" : "s"}`;
}
