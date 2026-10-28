/**
 * The pure half of the task diff view: a structural diff of two JSON
 * values (two task-instance bodies), as a flat preorder node list.
 *
 * The list has the shape of the JSON tree's index (`TreeNode`), so the
 * view reuses that tree's paging, visibility and expansion helpers
 * unchanged: a node's subtree ends at `end`, and skipping a collapsed one
 * is a jump.
 *
 * Semantics, per node:
 *
 * - **unchanged**: present on both sides with equal content. A nested task
 *   whose two bodies are identical is one unchanged node, however deep.
 * - **added** / **removed**: present on one side only. A container keeps
 *   its children (all added, or all removed), so its content can be read.
 * - **changed**: a scalar whose value differs, or a type change (`3` to
 *   `"3"`, an object to a number, an object to an array). Containers on
 *   both sides of one kind are never "changed" themselves: they recurse,
 *   and are `changed` exactly when something beneath them differs. A type
 *   change involving a container keeps that side's children (removed or
 *   added) for the same reason an added container does.
 *
 * Counts are of the differing nodes at their highest level: an added
 * object is one addition, not one per key inside it, and a container on
 * both sides sums its children. That is what "how many properties differ
 * beneath" means, and what a collapsed node's `+a −r ~c` badge shows.
 *
 * Arrays are aligned, not only compared by index: see `alignArrays`.
 */

import { identityOf, parametersOf, qualifiedName } from "./instances";
import {
  foldCase,
  kindOf,
  pathSegment,
  queryMatcher,
  scalarText,
  type JsonKind,
  type Range,
  type TreeNode,
} from "./jsonTree";

export type DiffStatus = "added" | "removed" | "changed" | "unchanged";

export interface DiffCounts {
  added: number;
  removed: number;
  changed: number;
}

export interface DiffNode extends TreeNode {
  /**
   * The node's identity, unique in the list: the right side's path where
   * the node is on the right, else the left side's path marked with `-`
   * (a removed array item and an added one may share an index).
   */
  id: string;
  /** The key drawn: the right side's key where present, else the left's; `null` for the root. */
  key: string | number | null;
  /** Each side's own key and JSON path; `null` on a side the node is absent from. */
  leftKey: string | number | null;
  rightKey: string | number | null;
  leftPath: string | null;
  rightPath: string | null;
  status: DiffStatus;
  hasLeft: boolean;
  hasRight: boolean;
  /** Each side's value; `undefined` where absent. */
  left: unknown;
  right: unknown;
  leftKind: JsonKind | null;
  rightKind: JsonKind | null;
  /** The differing nodes in this subtree, at their highest level (this one included). */
  counts: DiffCounts;
  /** `namespace.Name` where that side's value is a nested task body. */
  leftTaskLabel: string | null;
  rightTaskLabel: string | null;
  /** Folded key (object keys only) and each side's drawn scalar text, for search. */
  keyLower: string | null;
  leftLower: string | null;
  rightLower: string | null;
}

const ZERO: DiffCounts = { added: 0, removed: 0, changed: 0 };

export function totalCount(counts: DiffCounts): number {
  return counts.added + counts.removed + counts.changed;
}

function isContainerKind(kind: JsonKind | null): kind is "object" | "array" {
  return kind === "object" || kind === "array";
}

function entriesOf(value: unknown, kind: JsonKind): [string | number, unknown][] {
  if (kind === "array") return (value as unknown[]).map((v, i) => [i, v]);
  if (kind === "object") return Object.entries(value as Record<string, unknown>);
  return [];
}

function taskLabel(value: unknown, kind: JsonKind | null): string | null {
  if (kind !== "object") return null;
  const { namespace, name } = identityOf(value as Record<string, unknown>);
  return name ? qualifiedName(namespace, name) : null;
}

// ---- Equality ----

const canonicalCache = new WeakMap<object, string>();

/**
 * A string equal for equal JSON values: object keys sorted, so key order
 * does not count as a difference. Cached per object, so aligning nested
 * arrays does not re-serialise their elements at every level.
 */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return value === undefined ? "null" : JSON.stringify(value);
  }
  const cached = canonicalCache.get(value);
  if (cached !== undefined) return cached;
  let out: string;
  if (Array.isArray(value)) {
    out = `[${value.map(canonicalJson).join(",")}]`;
  } else {
    const keys = Object.keys(value).sort();
    out = `{${keys
      .map(
        (k) =>
          `${JSON.stringify(k)}:${canonicalJson(
            (value as Record<string, unknown>)[k],
          )}`,
      )
      .join(",")}}`;
  }
  canonicalCache.set(value, out);
  return out;
}

// ---- Array alignment ----

/** A pairing of a left index and a right index; `null` on the side it is absent from. */
export type Alignment = [number | null, number | null];

/**
 * Above this many `left × right` cells the longest-common-subsequence table
 * is not built and the arrays are compared by index. A million cells is
 * 4 MB and a few milliseconds.
 */
export const MAX_ALIGN_CELLS = 1_000_000;

/**
 * Align two arrays for diffing.
 *
 * Comparing by index alone reads one insertion at the front as every
 * element changing. So the elements equal on both sides are matched first
 * (longest common subsequence over `canonicalJson`, after trimming the
 * common prefix and suffix, which is all a typical edit needs), and the
 * unmatched runs between two matches are then paired by position: in
 * place edits (a nested task with one hyperparameter changed, an element
 * replaced) still pair up and diff into their content, and only a run's
 * excess is added or removed.
 *
 * This is what makes arrays of tasks or objects read naturally without
 * any identity to align them by: a nested task body has no id of its own,
 * and its content is its identity, which is exactly what equality
 * compares. When nothing is inserted or removed the result is the
 * index-by-index pairing. Beyond `MAX_ALIGN_CELLS` it falls back to that
 * pairing outright.
 */
export function alignArrays(left: unknown[], right: unknown[]): Alignment[] {
  const n = left.length;
  const m = right.length;
  const byIndex = (): Alignment[] => {
    const out: Alignment[] = [];
    for (let i = 0; i < Math.max(n, m); i++) {
      out.push([i < n ? i : null, i < m ? i : null]);
    }
    return out;
  };
  const lk = left.map(canonicalJson);
  const rk = right.map(canonicalJson);
  let prefix = 0;
  while (prefix < n && prefix < m && lk[prefix] === rk[prefix]) prefix++;
  let suffix = 0;
  while (
    suffix < n - prefix &&
    suffix < m - prefix &&
    lk[n - 1 - suffix] === rk[m - 1 - suffix]
  ) {
    suffix++;
  }
  const a = n - prefix - suffix;
  const b = m - prefix - suffix;
  if (a * b > MAX_ALIGN_CELLS) return byIndex();

  // LCS lengths over the middle, from the end: `T[i][j]` is the LCS of
  // left[prefix + i..] and right[prefix + j..].
  const width = b + 1;
  const T = new Int32Array((a + 1) * width);
  for (let i = a - 1; i >= 0; i--) {
    for (let j = b - 1; j >= 0; j--) {
      T[i * width + j] =
        lk[prefix + i] === rk[prefix + j]
          ? T[(i + 1) * width + j + 1] + 1
          : Math.max(T[(i + 1) * width + j], T[i * width + j + 1]);
    }
  }

  const out: Alignment[] = [];
  for (let k = 0; k < prefix; k++) out.push([k, k]);
  let gapLeft: number[] = [];
  let gapRight: number[] = [];
  const flush = () => {
    const paired = Math.min(gapLeft.length, gapRight.length);
    for (let k = 0; k < paired; k++) out.push([gapLeft[k], gapRight[k]]);
    for (let k = paired; k < gapLeft.length; k++) out.push([gapLeft[k], null]);
    for (let k = paired; k < gapRight.length; k++) out.push([null, gapRight[k]]);
    gapLeft = [];
    gapRight = [];
  };
  let i = 0;
  let j = 0;
  while (i < a && j < b) {
    if (lk[prefix + i] === rk[prefix + j]) {
      flush();
      out.push([prefix + i, prefix + j]);
      i++;
      j++;
    } else if (T[(i + 1) * width + j] >= T[i * width + j + 1]) {
      gapLeft.push(prefix + i++);
    } else {
      gapRight.push(prefix + j++);
    }
  }
  while (i < a) gapLeft.push(prefix + i++);
  while (j < b) gapRight.push(prefix + j++);
  flush();
  for (let k = 0; k < suffix; k++) out.push([n - suffix + k, m - suffix + k]);
  return out;
}

// ---- The diff ----

const ABSENT = Symbol("absent");

interface Child {
  id: string;
  leftKey: string | number | null;
  rightKey: string | number | null;
  left: unknown;
  right: unknown;
}

/**
 * The children of a node to diff: object keys in the left side's order
 * then the right side's new ones; array items as `alignArrays` pairs them.
 */
function childrenOf(
  id: string,
  left: unknown,
  leftKind: JsonKind | null,
  right: unknown,
  rightKind: JsonKind | null,
): Child[] {
  const out: Child[] = [];
  const sameKind = leftKind === rightKind;
  if (sameKind && leftKind === "object") {
    const l = left as Record<string, unknown>;
    const r = right as Record<string, unknown>;
    for (const key of Object.keys(l)) {
      const onRight = Object.prototype.hasOwnProperty.call(r, key);
      out.push({
        id: id + pathSegment(key),
        leftKey: key,
        rightKey: onRight ? key : null,
        left: l[key],
        right: onRight ? r[key] : ABSENT,
      });
    }
    for (const key of Object.keys(r)) {
      if (Object.prototype.hasOwnProperty.call(l, key)) continue;
      out.push({
        id: id + pathSegment(key),
        leftKey: null,
        rightKey: key,
        left: ABSENT,
        right: r[key],
      });
    }
    return out;
  }
  if (sameKind && leftKind === "array") {
    const l = left as unknown[];
    const r = right as unknown[];
    for (const [li, ri] of alignArrays(l, r)) {
      out.push({
        id: ri !== null ? id + pathSegment(ri) : `${id}-${pathSegment(li as number)}`,
        leftKey: li,
        rightKey: ri,
        left: li !== null ? l[li] : ABSENT,
        right: ri !== null ? r[ri] : ABSENT,
      });
    }
    return out;
  }
  // One side only (added, removed), or a type change: each container
  // side's children, all removed or all added.
  if (left !== ABSENT && isContainerKind(leftKind)) {
    for (const [key, value] of entriesOf(left, leftKind)) {
      out.push({
        id: `${id}-${pathSegment(key)}`,
        leftKey: key,
        rightKey: null,
        left: value,
        right: ABSENT,
      });
    }
  }
  if (right !== ABSENT && isContainerKind(rightKind)) {
    for (const [key, value] of entriesOf(right, rightKind)) {
      out.push({
        id: id + pathSegment(key),
        leftKey: null,
        rightKey: key,
        left: ABSENT,
        right: value,
      });
    }
  }
  return out;
}

function scalarLower(value: unknown, kind: JsonKind | null): string | null {
  return kind === null || isContainerKind(kind) ? null : foldCase(scalarText(value));
}

/**
 * Diff two JSON values into a preorder node list; the root is node 0 and
 * stands for the values themselves.
 */
export function diffJson(left: unknown, right: unknown): DiffNode[] {
  const nodes: DiffNode[] = [];

  function visit(
    child: Child,
    leftPath: string | null,
    rightPath: string | null,
    depth: number,
    parent: number,
    posInSet: number,
    setSize: number,
  ): DiffCounts {
    const hasLeft = child.left !== ABSENT;
    const hasRight = child.right !== ABSENT;
    const leftKind = hasLeft ? kindOf(child.left) : null;
    const rightKind = hasRight ? kindOf(child.right) : null;
    const sameContainer =
      hasLeft && hasRight && leftKind === rightKind && isContainerKind(leftKind);
    let status: DiffStatus;
    if (!hasLeft) status = "added";
    else if (!hasRight) status = "removed";
    else if (sameContainer) status = "unchanged"; // until a child differs
    else if (leftKind !== rightKind) status = "changed";
    else status = child.left === child.right ? "unchanged" : "changed";

    // Navigation kind: the container side's, so a node with children is
    // drawn expandable; the right side's otherwise.
    const kind: JsonKind = isContainerKind(rightKind)
      ? rightKind
      : isContainerKind(leftKind)
        ? leftKind
        : rightKind ?? leftKind ?? "null";

    const drawnKey = child.rightKey ?? child.leftKey;
    const index = nodes.length;
    const node: DiffNode = {
      id: child.id,
      key: drawnKey,
      leftKey: child.leftKey,
      rightKey: child.rightKey,
      leftPath,
      rightPath,
      depth,
      kind,
      size: 0,
      parent,
      end: index + 1,
      posInSet,
      setSize,
      status,
      hasLeft,
      hasRight,
      left: hasLeft ? child.left : undefined,
      right: hasRight ? child.right : undefined,
      leftKind,
      rightKind,
      counts: ZERO,
      leftTaskLabel: taskLabel(child.left, leftKind),
      rightTaskLabel: taskLabel(child.right, rightKind),
      keyLower: typeof drawnKey === "string" ? foldCase(drawnKey) : null,
      // An unchanged scalar is drawn once, from the right.
      leftLower: status === "unchanged" ? null : scalarLower(child.left, leftKind),
      rightLower: scalarLower(child.right, rightKind),
    };
    nodes.push(node);

    // An equal container needs no walk of its pairs, but its children are
    // still listed (unchanged), so it can be expanded and read.
    const children = childrenOf(
      child.id,
      hasLeft ? child.left : ABSENT,
      leftKind,
      hasRight ? child.right : ABSENT,
      rightKind,
    );
    node.size = children.length;
    const sum: DiffCounts = { added: 0, removed: 0, changed: 0 };
    children.forEach((c, i) => {
      const counts = visit(
        c,
        leftPath !== null && c.leftKey !== null
          ? leftPath + pathSegment(c.leftKey)
          : null,
        rightPath !== null && c.rightKey !== null
          ? rightPath + pathSegment(c.rightKey)
          : null,
        depth + 1,
        index,
        i + 1,
        children.length,
      );
      sum.added += counts.added;
      sum.removed += counts.removed;
      sum.changed += counts.changed;
    });
    node.end = nodes.length;

    if (sameContainer) {
      node.counts = sum;
      if (totalCount(sum) > 0) node.status = "changed";
    } else if (status !== "unchanged") {
      node.counts = { ...ZERO, [status]: 1 };
    }
    return node.counts;
  }

  visit({ id: "$", leftKey: null, rightKey: null, left, right }, "$", "$", 0, -1, 1, 1);
  return nodes;
}

/**
 * Diff two task-instance bodies: their parameters, without the top-level
 * `__` discriminator keys (as the parameter view shows them). Nested task
 * bodies keep theirs; they label the nested task (`leftTaskLabel`).
 */
export function diffBodies(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): DiffNode[] {
  return diffJson(parametersOf(left), parametersOf(right));
}

/** Whether a node is a container on both sides that recursed (not a type change). */
export function isPairedContainer(node: DiffNode): boolean {
  return (
    node.hasLeft &&
    node.hasRight &&
    node.leftKind === node.rightKind &&
    isContainerKind(node.leftKind)
  );
}

/**
 * The same diff with every unchanged node dropped: the "only differences"
 * view. Re-indexed (sizes, positions, subtree ends), so paging and ARIA
 * positions describe what is drawn; ids are kept, so expansion carries
 * over between the two views.
 */
export function onlyDifferences(nodes: readonly DiffNode[]): DiffNode[] {
  const out: DiffNode[] = [];
  if (nodes.length === 0) return out;
  function visit(
    index: number,
    parent: number,
    depth: number,
    posInSet: number,
    setSize: number,
  ): void {
    const source = nodes[index];
    const kept: number[] = [];
    for (let c = index + 1; c < source.end; c = nodes[c].end) {
      if (nodes[c].status !== "unchanged") kept.push(c);
    }
    const at = out.length;
    const node: DiffNode = {
      ...source,
      parent,
      depth,
      posInSet,
      setSize,
      size: kept.length,
      end: at + 1,
    };
    out.push(node);
    kept.forEach((c, i) => visit(c, at, depth + 1, i + 1, kept.length));
    node.end = out.length;
  }
  visit(0, -1, 0, 1, 1);
  return out;
}

/**
 * The initially expanded containers: the root, and every container that
 * recursed into a difference, level by level, until opening the next
 * level would show more than `budget` rows. Unchanged subtrees, and added
 * or removed ones, start collapsed with their summary showing.
 */
export function defaultDiffExpanded(
  nodes: readonly DiffNode[],
  budget = 500,
  pageSize = 200,
): Set<string> {
  const expanded = new Set<string>();
  if (nodes.length === 0) return expanded;
  expanded.add(nodes[0].id);
  let shown = nodes[0].size;
  let level = [0];
  while (level.length > 0) {
    const next: number[] = [];
    for (const index of level) {
      for (let c = index + 1; c < nodes[index].end; c = nodes[c].end) {
        if (nodes[c].status === "changed" && isPairedContainer(nodes[c])) next.push(c);
      }
    }
    const added = next.reduce((sum, c) => sum + Math.min(nodes[c].size, pageSize), 0);
    if (next.length === 0 || shown + added > budget) break;
    for (const c of next) expanded.add(nodes[c].id);
    shown += added;
    level = next;
  }
  return expanded;
}

export interface DiffMatch {
  index: number;
  score: number;
  keyRanges: Range[] | null;
  leftRanges: Range[] | null;
  rightRanges: Range[] | null;
}

/**
 * The nodes whose key, or scalar value on either side, fuzzy-matches
 * `query` (the JSON tree's matcher), best first, document order breaking
 * ties. As in the tree, array indices and container values are not
 * searched.
 */
export function findDiffMatches(
  nodes: readonly DiffNode[],
  query: string,
): DiffMatch[] {
  const match = queryMatcher(query);
  if (!match) return [];
  const out: DiffMatch[] = [];
  for (let i = 1; i < nodes.length; i++) {
    const node = nodes[i];
    const key = typeof node.key === "string" ? match(node.key, node.keyLower) : null;
    const left =
      node.leftLower === null ? null : match(scalarText(node.left), node.leftLower);
    const right =
      node.rightLower === null ? null : match(scalarText(node.right), node.rightLower);
    if (key || left || right) {
      out.push({
        index: i,
        score: Math.max(
          key?.score ?? -Infinity,
          left?.score ?? -Infinity,
          right?.score ?? -Infinity,
        ),
        keyRanges: key?.ranges ?? null,
        leftRanges: left?.ranges ?? null,
        rightRanges: right?.ranges ?? null,
      });
    }
  }
  return out.sort((a, b) => b.score - a.score || a.index - b.index);
}

/** `+2 −1 ~3`, leaving out the zeros; `""` when nothing differs. */
export function countsLabel(counts: DiffCounts): string {
  const parts: string[] = [];
  if (counts.added) parts.push(`+${counts.added}`);
  if (counts.removed) parts.push(`−${counts.removed}`);
  if (counts.changed) parts.push(`~${counts.changed}`);
  return parts.join(" ");
}

/** The same counts in words, for screen readers and tooltips. */
export function countsSentence(counts: DiffCounts): string {
  const parts: string[] = [];
  if (counts.added) parts.push(`${counts.added} added`);
  if (counts.removed) parts.push(`${counts.removed} removed`);
  if (counts.changed) parts.push(`${counts.changed} changed`);
  return parts.length ? parts.join(", ") : "no differences";
}

/** What "copy path" copies: the right side's path where the node is there, else the left's. */
export function diffPath(node: DiffNode): string {
  return node.rightPath ?? node.leftPath ?? node.id;
}

/** What "copy value" copies on one side: a string as-is, anything else as JSON. */
export function diffValueText(node: DiffNode, side: "left" | "right"): string {
  const value = side === "left" ? node.left : node.right;
  const kind = side === "left" ? node.leftKind : node.rightKind;
  if (kind === "string") return value as string;
  if (isContainerKind(kind)) return JSON.stringify(value, null, 2);
  return scalarText(value);
}
