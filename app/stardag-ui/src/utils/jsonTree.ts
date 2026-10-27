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
      keyLower: typeof key === "string" ? foldCase(key) : null,
      valueLower:
        kind === "object" || kind === "array" ? null : foldCase(scalarText(value)),
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

/** How many children of a container are drawn at a time. */
export const PAGE_SIZE = 200;

/** The children of a container that are drawn: `[start, end)`, 0-based. */
export type Window = readonly [number, number];

/** Drawn windows by container id; a container not in it draws its first page. */
export type Windows = ReadonlyMap<string, Window>;

export type MoreDirection = "before" | "after";

/**
 * A row is a node index (>= 1), or a "show more" row standing for the
 * undrawn children of container `p` before or after its window, encoded
 * as `-(2p + 1)` and `-(2p + 2)`.
 */
export function moreRow(parent: number, direction: MoreDirection): number {
  return direction === "before" ? -(2 * parent + 1) : -(2 * parent + 2);
}

export function isMoreRow(row: number): boolean {
  return row < 0;
}

/** The container a "show more" row belongs to, and which side it is on. */
export function moreOf(row: number): { parent: number; direction: MoreDirection } {
  const n = -row - 1;
  return { parent: Math.floor(n / 2), direction: n % 2 === 0 ? "before" : "after" };
}

export function windowOf(nodes: JsonNode[], windows: Windows, parent: number): Window {
  const node = nodes[parent];
  return windows.get(node.id) ?? [0, Math.min(node.size, PAGE_SIZE)];
}

/**
 * The visible rows, in display order. The root itself is not drawn — its
 * children are the top level — and is always expanded.
 *
 * A container draws only the children in its window (its first page by
 * default), with a "show more" row on each side that has more, so a wide
 * array costs at most a page of rows however wide it is and however many
 * of its children a search matches.
 */
export function visibleRows(
  nodes: JsonNode[],
  expanded: ReadonlySet<string>,
  windows: Windows = new Map(),
): number[] {
  const rows: number[] = [];
  function walk(parent: number): void {
    const node = nodes[parent];
    const [start, end] = windowOf(nodes, windows, parent);
    if (start > 0) rows.push(moreRow(parent, "before"));
    let position = 0;
    for (let child = parent + 1; child < node.end; child = nodes[child].end) {
      if (position >= end) break;
      if (position++ < start) continue;
      rows.push(child);
      if (isContainer(nodes[child]) && expanded.has(nodes[child].id)) walk(child);
    }
    if (end < node.size) rows.push(moreRow(parent, "after"));
  }
  if (nodes.length > 0) walk(0);
  return rows;
}

/** The index of `parent`'s `n`th child (0-based), or -1. */
export function nthChild(nodes: JsonNode[], parent: number, n: number): number {
  let position = 0;
  for (let child = parent + 1; child < nodes[parent].end; child = nodes[child].end) {
    if (position === n) return child;
    position++;
  }
  return -1;
}

/** How many of `parent`'s children a "show more" row on that side stands for. */
export function hiddenCount(
  nodes: JsonNode[],
  windows: Windows,
  parent: number,
  direction: MoreDirection,
): number {
  const [start, end] = windowOf(nodes, windows, parent);
  return direction === "before" ? start : nodes[parent].size - end;
}

/** `windows` with `parent`'s window grown by one page on one side. */
export function showMore(
  nodes: JsonNode[],
  windows: Windows,
  parent: number,
  direction: MoreDirection,
): Windows {
  const [start, end] = windowOf(nodes, windows, parent);
  const next = new Map(windows);
  next.set(
    nodes[parent].id,
    direction === "before"
      ? [Math.max(0, start - PAGE_SIZE), end]
      : [start, Math.min(nodes[parent].size, end + PAGE_SIZE)],
  );
  return next;
}

/**
 * `windows` moved so that `index` and each of its ancestors is drawn: a
 * parent whose window misses the child gets the page holding it instead.
 * Moving rather than growing keeps a jump to the far end of a wide array
 * at one page of rows. Returns `windows` itself when nothing moves.
 */
export function revealInPages(
  nodes: JsonNode[],
  windows: Windows,
  index: number,
): Windows {
  let out = windows;
  for (let i = index; i > 0; i = nodes[i].parent) {
    const parent = nodes[i].parent;
    const position = nodes[i].posInSet - 1;
    const [start, end] = windowOf(nodes, out, parent);
    if (position >= start && position < end) continue;
    const pageStart = Math.floor(position / PAGE_SIZE) * PAGE_SIZE;
    const next = new Map(out);
    next.set(nodes[parent].id, [
      pageStart,
      Math.min(nodes[parent].size, pageStart + PAGE_SIZE),
    ]);
    out = next;
  }
  return out;
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
    const added = level.reduce((sum, node) => sum + Math.min(node.size, PAGE_SIZE), 0);
    if (level.length === 0 || shown + added > budget) break;
    for (const node of level) expanded.add(node.id);
    shown += added;
  }
  return expanded;
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

/** A character range `[start, end)` in an original (unfolded) string. */
export type Range = readonly [number, number];

export interface FuzzyMatch {
  score: number;
  /** The matched characters, as merged ranges into the original string. */
  ranges: Range[];
}

export interface JsonMatch {
  /** Index into the node list. */
  index: number;
  /** The better of the key's and the value's score. */
  score: number;
  /** Matched ranges in the key and in the scalar text; `null` where it did not match. */
  keyRanges: Range[] | null;
  valueRanges: Range[] | null;
}

// Scoring, in the spirit of fzf and VS Code's fuzzy filter: every matched
// character scores, more at the start of a word, more again when it
// continues the previous match, and a gap between matches costs per
// skipped character.
const SCORE_MATCH = 16;
const BONUS_START = 8; // the first character of the text
const BONUS_BOUNDARY = 7; // after a separator: `a_b`, `a.b`, `a b`
const BONUS_CAMEL = 6; // `aB`
const BONUS_DIGIT = 3; // letter/digit transition: `v2`
const BONUS_CONSECUTIVE = 12;
const FIRST_CHAR_MULTIPLIER = 2;
const GAP_OPEN = 5;
const GAP_EXTEND = 1;
/**
 * The longest gap allowed before a match that is not at a word start. A
 * longer gap may only land on a word start (`fl` in `first_..._last`), so
 * a short query cannot pick its characters out of a long value one here
 * and one there.
 */
export const MAX_MIDWORD_GAP = 8;
/**
 * The quality floor, per query character. A short query is a subsequence
 * of almost any long value; requiring this much per character rejects a
 * match whose characters are strewn far apart mid-word, while keeping
 * initials (`fb` in `foo_bar`) and small typos-by-omission (`unts`).
 */
export const MIN_SCORE_PER_CHAR = 12;
/**
 * Texts longer than this (in folded code units), and queries longer than
 * `MAX_FUZZY_QUERY`, are matched as a contiguous substring only: the
 * scoring is quadratic in neither, but its tables are `text × query`.
 */
export const MAX_FUZZY_TEXT = 1024;
const MAX_FUZZY_QUERY = 64;

/**
 * Case folding for search, one character at a time. Lower-casing a whole
 * string is context-sensitive (a final `Σ` becomes `ς`); per character it
 * is not, so the index, the query and the highlighting all agree.
 */
export function foldCase(text: string): string {
  let out = "";
  for (const ch of text) out += ch.toLowerCase();
  return out;
}

interface Folded {
  folded: string;
  /** For each folded code unit: the start and length of its original character. */
  origin: number[];
  width: number[];
}

function foldWithMap(text: string): Folded {
  let folded = "";
  const origin: number[] = [];
  const width: number[] = [];
  let at = 0;
  for (const ch of text) {
    const lower = ch.toLowerCase();
    for (let k = 0; k < lower.length; k++) {
      origin.push(at);
      width.push(ch.length);
    }
    folded += lower;
    at += ch.length;
  }
  return { folded, origin, width };
}

/** Whether `q` is a subsequence of `text`: the cheap filter before scoring. */
export function isSubsequence(q: string, text: string): boolean {
  let i = 0;
  for (let j = 0; j < text.length && i < q.length; j++) {
    if (text[j] === q[i]) i++;
  }
  return i === q.length;
}

const WORD = /[\p{L}\p{N}]/u;
const DIGIT = /\p{N}/u;

function isUpper(ch: string): boolean {
  return ch !== ch.toLowerCase() && ch === ch.toUpperCase();
}

function isLower(ch: string): boolean {
  return ch !== ch.toUpperCase() && ch === ch.toLowerCase();
}

/** The position bonus for each folded unit, read off the original characters. */
function bonuses(text: string, { origin, width }: Folded): number[] {
  const out = new Array<number>(origin.length);
  const charAt = (j: number) => text.slice(origin[j], origin[j] + width[j]);
  for (let j = 0; j < origin.length; j++) {
    if (j === 0) {
      out[j] = BONUS_START;
      continue;
    }
    if (origin[j] === origin[j - 1]) {
      out[j] = 0; // the second unit of one character
      continue;
    }
    const prev = charAt(j - 1);
    const cur = charAt(j);
    if (!WORD.test(prev) && WORD.test(cur)) out[j] = BONUS_BOUNDARY;
    else if (isLower(prev) && isUpper(cur)) out[j] = BONUS_CAMEL;
    else if (WORD.test(prev) && WORD.test(cur) && DIGIT.test(prev) !== DIGIT.test(cur))
      out[j] = BONUS_DIGIT;
    else out[j] = 0;
  }
  return out;
}

/** Folded positions to merged ranges of whole original characters. */
function toRanges({ origin, width }: Folded, positions: number[]): Range[] {
  const ranges: [number, number][] = [];
  for (const p of positions) {
    const start = origin[p];
    const end = origin[p] + width[p];
    const last = ranges.at(-1);
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else ranges.push([start, end]);
  }
  return ranges;
}

function substringMatch(f: Folded, q: string): FuzzyMatch | null {
  const at = f.folded.indexOf(q);
  if (at === -1) return null;
  const positions = Array.from({ length: q.length }, (_, k) => at + k);
  const score =
    q.length * SCORE_MATCH +
    (q.length - 1) * BONUS_CONSECUTIVE +
    (at === 0 ? BONUS_START : 0);
  return { score, ranges: toRanges(f, positions) };
}

/**
 * The best-scoring way to match folded query `q` against `text` as a
 * subsequence, or `null` when it is not one or scores below the floor.
 *
 * A dynamic programme over (query character, text position): `H[i][j]` is
 * the best score with query character `i` matched at text unit `j`. It is
 * reached either consecutively from `H[i-1][j-1]` or across a gap from the
 * best `H[i-1][k]`, `k < j - 1`, less the gap's cost. A gap longer than
 * `MAX_MIDWORD_GAP` may only end at a word start; that case is a running
 * maximum and the short gaps a bounded window, so the table costs
 * `|q| × |text| × MAX_MIDWORD_GAP` at most.
 */
function scoreFolded(text: string, f: Folded, q: string): FuzzyMatch | null {
  const n = f.folded.length;
  const m = q.length;
  if (m === 0 || m > n) return null;
  if (n > MAX_FUZZY_TEXT || m > MAX_FUZZY_QUERY) {
    const hit = substringMatch(f, q);
    return hit && hit.score >= m * MIN_SCORE_PER_CHAR ? hit : null;
  }
  const bonus = bonuses(text, f);
  const NONE = -Infinity;
  const H = Array.from({ length: m }, () => new Float64Array(n).fill(NONE));
  const back = Array.from({ length: m }, () => new Int32Array(n).fill(-1));
  // The first character only at a word start (see fuzzyMatch).
  for (let j = 0; j < n; j++) {
    if (f.folded[j] === q[0] && bonus[j] >= BONUS_CAMEL)
      H[0][j] = SCORE_MATCH + bonus[j] * FIRST_CHAR_MULTIPLIER;
  }
  for (let i = 1; i < m; i++) {
    const prev = H[i - 1];
    // The best gapped predecessor over every k < j - 1, for word starts.
    let farBest = NONE;
    let farFrom = -1;
    for (let j = i; j < n; j++) {
      if (farBest !== NONE) farBest -= GAP_EXTEND;
      if (j >= 2 && prev[j - 2] !== NONE && prev[j - 2] - GAP_OPEN > farBest) {
        farBest = prev[j - 2] - GAP_OPEN;
        farFrom = j - 2;
      }
      if (f.folded[j] !== q[i]) continue;
      let gapBest = NONE;
      let gapFrom = -1;
      if (bonus[j] >= BONUS_CAMEL) {
        gapBest = farBest;
        gapFrom = farFrom;
      } else {
        // Mid-word: only a short gap.
        for (let k = j - 2; k >= 0 && j - k - 1 <= MAX_MIDWORD_GAP; k--) {
          if (prev[k] === NONE) continue;
          const candidate = prev[k] - GAP_OPEN - (j - k - 2) * GAP_EXTEND;
          if (candidate > gapBest) {
            gapBest = candidate;
            gapFrom = k;
          }
        }
      }
      const consecutive = prev[j - 1] === NONE ? NONE : prev[j - 1] + BONUS_CONSECUTIVE;
      const base = SCORE_MATCH + bonus[j];
      if (consecutive !== NONE && consecutive >= gapBest) {
        H[i][j] = consecutive + base;
        back[i][j] = j - 1;
      } else if (gapBest !== NONE) {
        H[i][j] = gapBest + base;
        back[i][j] = gapFrom;
      }
    }
  }
  let best = NONE;
  let end = -1;
  for (let j = 0; j < n; j++) {
    if (H[m - 1][j] > best) {
      best = H[m - 1][j];
      end = j;
    }
  }
  // A contiguous occurrence anywhere is always a match, scored as one.
  const contiguous = substringMatch(f, q);
  if (end === -1 || best < m * MIN_SCORE_PER_CHAR) return contiguous;
  if (contiguous && contiguous.score > best) return contiguous;
  const positions = new Array<number>(m);
  for (let i = m - 1, j = end; i >= 0; i--) {
    positions[i] = j;
    j = back[i][j];
  }
  return { score: best, ranges: toRanges(f, positions) };
}

/**
 * Fuzzy-match `query` against `text`, in the manner of VS Code's quick-open
 * filter: the query's characters must appear in order, case-insensitively;
 * the first must land on a word start (the text's start, after a
 * separator, or a camelCase hump); a gap may exceed `MAX_MIDWORD_GAP` only
 * to reach another word start; and the arrangement must score at least
 * `MIN_SCORE_PER_CHAR` per query character. A contiguous occurrence
 * anywhere always matches, so a plain substring search still works
 * (`pha` finds `alpha`). The first-character rule is what keeps a short
 * query from matching nearly every hash or long identifier.
 *
 * Returns the score and the matched characters as ranges into `text`, or
 * `null`.
 */
export function fuzzyMatch(text: string, query: string): FuzzyMatch | null {
  const q = foldCase(query.trim());
  if (!q) return null;
  const f = foldWithMap(text);
  if (!isSubsequence(q, f.folded)) return null;
  return scoreFolded(text, f, q);
}

/**
 * The nodes whose key or scalar value fuzzy-matches `query`, best first,
 * document order breaking ties. Array indices and container values are
 * not searched: an index is a position, not content, and a container's
 * content is its children's. An empty or whitespace-only query matches
 * nothing.
 */
export function findMatches(nodes: JsonNode[], query: string): JsonMatch[] {
  const q = foldCase(query.trim());
  if (!q) return [];
  const matches: JsonMatch[] = [];
  const match = (text: string, folded: string | null): FuzzyMatch | null =>
    folded !== null && isSubsequence(q, folded)
      ? scoreFolded(text, foldWithMap(text), q)
      : null;
  for (let i = 1; i < nodes.length; i++) {
    const node = nodes[i];
    const key = typeof node.key === "string" ? match(node.key, node.keyLower) : null;
    const value =
      node.valueLower === null ? null : match(scalarText(node.value), node.valueLower);
    if (key || value) {
      matches.push({
        index: i,
        score: Math.max(key?.score ?? -Infinity, value?.score ?? -Infinity),
        keyRanges: key?.ranges ?? null,
        valueRanges: value?.ranges ?? null,
      });
    }
  }
  return matches.sort((a, b) => b.score - a.score || a.index - b.index);
}

export interface Segment {
  text: string;
  match: boolean;
}

/** `text` split into runs inside and outside the given sorted, disjoint ranges. */
export function segmentsOf(text: string, ranges: readonly Range[]): Segment[] {
  const segments: Segment[] = [];
  let from = 0;
  for (const [start, end] of ranges) {
    if (start > from) segments.push({ text: text.slice(from, start), match: false });
    segments.push({ text: text.slice(start, end), match: true });
    from = end;
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
