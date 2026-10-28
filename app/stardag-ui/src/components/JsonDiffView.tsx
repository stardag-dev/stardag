import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useStableJson } from "../hooks/useStableJson";
import { useTreeSearch } from "../hooks/useTreeSearch";
import {
  countsSentence,
  defaultDiffExpanded,
  diffBodies,
  diffPath,
  diffValueText,
  findDiffMatches,
  isPairedContainer,
  onlyDifferences,
  totalCount,
  type DiffCounts,
  type DiffNode,
} from "../utils/jsonDiff";
import {
  allContainerIds,
  hiddenCount,
  isContainer,
  isMoreRow,
  moreOf,
  nthChild,
  SCALAR_CLASS,
  scalarText,
  showMore,
  visibleRows,
  windowOf,
  type JsonKind,
  type Range,
  type Windows,
} from "../utils/jsonTree";
import { treeKeyDown } from "../utils/treeKeys";
import { Highlighted, MoreRow, RowCopyButton, TreeButton } from "./JsonTree";
import { Checkbox } from "./ui/Checkbox";

interface JsonDiffViewProps {
  /** The two task-instance bodies; their top-level `__` keys are left out. */
  left: Record<string, unknown>;
  right: Record<string, unknown>;
  /** The tree's accessible name. */
  label?: string;
  /** Classes for the scrolling tree area (height limits, say). */
  className?: string;
}

// GitHub's diff colours, in both themes: red for the left (removed) side,
// green for the right (added), amber for "changed beneath".
const ROW_CLASS: Record<DiffNode["status"], string> = {
  added: "bg-green-50 dark:bg-green-950/40",
  removed: "bg-red-50 dark:bg-red-950/40",
  changed: "",
  unchanged: "",
};
const SIGN: Record<DiffNode["status"], { text: string; className: string }> = {
  added: { text: "+", className: "text-green-700 dark:text-green-400" },
  removed: { text: "−", className: "text-red-700 dark:text-red-400" },
  changed: { text: "~", className: "text-amber-600 dark:text-amber-400" },
  unchanged: { text: "", className: "" },
};
const OLD_CLASS =
  "rounded-sm bg-red-100 px-0.5 text-red-800 line-through decoration-red-400/70 dark:bg-red-900/50 dark:text-red-200";
const NEW_CLASS =
  "rounded-sm bg-green-100 px-0.5 text-green-800 no-underline dark:bg-green-900/50 dark:text-green-200";

// The tree's scalar colours, but strings neutral: green and red mean
// added and removed here.
const VALUE_CLASS: Record<string, string> = {
  ...SCALAR_CLASS,
  string: "text-gray-900 dark:text-gray-100",
};

/**
 * A structural diff of two task-instance bodies, drawn like the JSON tree
 * (`JsonTree`) and sharing its machinery: the flat preorder index, paged
 * wide containers, the debounced fuzzy search with next/previous, the
 * ARIA tree keyboard model and copy actions.
 *
 * Containers that differ beneath start expanded, the rest collapsed with
 * a `+a −r ~c` summary of what differs beneath them; "only differences"
 * drops the unchanged nodes. Search covers keys and both sides' values.
 *
 * State is per pair of bodies: give it a `key` when either changes.
 */
export function JsonDiffView({
  left,
  right,
  label = "Parameter differences",
  className = "",
}: JsonDiffViewProps) {
  const stableLeft = useStableJson(left);
  const stableRight = useStableJson(right);
  const full = useMemo(
    () => diffBodies(stableLeft, stableRight),
    [stableLeft, stableRight],
  );
  const [onlyDiffs, setOnlyDiffs] = useState(false);
  const pruned = useMemo(
    () => (onlyDiffs ? onlyDifferences(full) : null),
    [full, onlyDiffs],
  );
  const nodes = pruned ?? full;
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() =>
    defaultDiffExpanded(full),
  );
  const [windows, setWindows] = useState<Windows>(() => new Map());
  const [focused, setFocused] = useState(1);
  const [status, setStatus] = useState("");
  const treeRef = useRef<HTMLDivElement>(null);
  const treeId = useId();
  const statusTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingFocus = useRef<number | null>(null);

  const rows = useMemo(
    () => visibleRows(nodes, expanded, windows),
    [nodes, expanded, windows],
  );
  const {
    input,
    setInput,
    search,
    matchByIndex,
    currentMatch,
    goToMatch,
    onSearchKeyDown,
  } = useTreeSearch({
    nodes,
    find: findDiffMatches,
    setExpanded,
    setWindows,
    setFocused,
    treeRef,
  });

  useEffect(() => {
    if (pendingFocus.current === null) return;
    treeRef.current
      ?.querySelector<HTMLElement>(`[data-row="${pendingFocus.current}"]`)
      ?.focus();
    pendingFocus.current = null;
  }, [rows]);

  useEffect(
    () => () => {
      if (statusTimer.current !== null) clearTimeout(statusTimer.current);
    },
    [],
  );

  const announce = useCallback((message: string) => {
    setStatus(message);
    if (statusTimer.current !== null) clearTimeout(statusTimer.current);
    statusTimer.current = setTimeout(() => {
      statusTimer.current = null;
      setStatus("");
    }, 2000);
  }, []);

  const copy = useCallback(
    async (index: number, what: "value" | "old" | "path") => {
      const node = nodes[index];
      const text =
        what === "path"
          ? diffPath(node)
          : diffValueText(node, what === "old" || !node.hasRight ? "left" : "right");
      try {
        await navigator.clipboard.writeText(text);
        announce(
          what === "path"
            ? `Copied path ${diffPath(node)}`
            : `Copied ${what === "old" ? "old value" : "value"} of ${diffPath(node)}`,
        );
      } catch (err) {
        console.error("Failed to copy:", err);
        announce("Copy failed");
      }
    },
    [nodes, announce],
  );

  const toggle = useCallback(
    (index: number) => {
      const id = nodes[index].id;
      setExpanded((prev) => {
        const next = new Set(prev);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
    },
    [nodes],
  );

  const more = useCallback(
    (row: number, focus: boolean) => {
      const { parent, direction } = moreOf(row);
      const [start, end] = windowOf(nodes, windows, parent);
      const first = nthChild(nodes, parent, direction === "before" ? start - 1 : end);
      setWindows((prev) => showMore(nodes, prev, parent, direction));
      if (first !== -1) {
        setFocused(first);
        if (focus) pendingFocus.current = first;
      }
    },
    [nodes, windows],
  );

  const focusRow = useCallback((row: number) => {
    setFocused(row);
    treeRef.current?.querySelector<HTMLElement>(`[data-row="${row}"]`)?.focus();
  }, []);

  const tabStop = rows.includes(focused) ? focused : rows[0] ?? null;

  // Paging and focus are positions in one list; the other list's do not
  // carry over. Expansion is by id, and does.
  const setOnly = (value: boolean) => {
    setOnlyDiffs(value);
    setWindows(new Map());
    setFocused(1);
  };

  const onTreeKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) =>
    treeKeyDown(event, { rows, nodes, expanded, focusRow, toggle, more, copy });

  const searching = search.query !== "";
  const matchCount = search.matches.length;
  const totals = full[0]?.counts ?? { added: 0, removed: 0, changed: 0 };
  const identical = totalCount(totals) === 0;

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-gray-50 dark:bg-gray-900">
      <div className="flex flex-wrap items-center gap-1.5 border-b border-gray-200 px-2 py-1.5 dark:border-gray-700">
        <input
          type="search"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onSearchKeyDown}
          placeholder="Search keys and values on both sides"
          aria-label={`Search ${label.toLowerCase()}`}
          aria-controls={treeId}
          className="min-w-0 flex-1 rounded border border-gray-300 bg-white px-2 py-0.5 text-xs text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
        />
        {searching && (
          <>
            <span
              className="text-xs whitespace-nowrap text-gray-500 tabular-nums dark:text-gray-400"
              aria-live="polite"
            >
              {matchCount === 0
                ? "No matches"
                : `${search.current + 1} of ${matchCount}`}
            </span>
            <TreeButton
              label="Previous match"
              onClick={() => goToMatch(-1)}
              disabled={matchCount === 0}
            >
              ↑
            </TreeButton>
            <TreeButton
              label="Next match"
              onClick={() => goToMatch(1)}
              disabled={matchCount === 0}
            >
              ↓
            </TreeButton>
          </>
        )}
        <Checkbox
          checked={onlyDiffs}
          onChange={setOnly}
          label="Only differences"
          labelHidden={false}
          className="px-1 text-xs [&>span]:text-xs"
        />
        <TreeButton
          label="Expand all"
          onClick={() => setExpanded(allContainerIds(nodes))}
        >
          Expand all
        </TreeButton>
        <TreeButton
          label="Collapse all"
          onClick={() => setExpanded(new Set(nodes.length > 0 ? [nodes[0].id] : []))}
        >
          Collapse all
        </TreeButton>
        <span className="sr-only" role="status">
          {status}
        </span>
      </div>
      <div className="flex items-center gap-2 border-b border-gray-200 px-3 py-1 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400">
        {identical ? (
          <span>Identical parameters.</span>
        ) : (
          <>
            <CountsBadge counts={totals} />
            <span>{countsSentence(totals)}</span>
          </>
        )}
      </div>
      {rows.length === 0 ? (
        <p className="p-3 text-sm text-gray-500 dark:text-gray-400">
          {onlyDiffs ? "No differences." : "No parameters."}
        </p>
      ) : (
        <div
          ref={treeRef}
          id={treeId}
          role="tree"
          aria-label={label}
          onKeyDown={onTreeKeyDown}
          className={`overflow-auto py-1 font-mono text-sm text-gray-800 dark:text-gray-200 ${className}`}
        >
          {rows.map((index) => {
            if (isMoreRow(index)) {
              const { parent, direction } = moreOf(index);
              return (
                <MoreRow
                  key={`${nodes[parent].id} ${direction}`}
                  row={index}
                  level={nodes[parent].depth + 1}
                  direction={direction}
                  hidden={hiddenCount(nodes, windows, parent, direction)}
                  tabbable={index === tabStop}
                  onMore={more}
                  onFocusRow={setFocused}
                />
              );
            }
            const match = matchByIndex.get(index);
            return (
              <DiffRow
                key={nodes[index].id}
                node={nodes[index]}
                index={index}
                open={expanded.has(nodes[index].id)}
                tabbable={index === tabStop}
                keyRanges={match?.keyRanges ?? null}
                leftRanges={match?.leftRanges ?? null}
                rightRanges={match?.rightRanges ?? null}
                current={currentMatch?.index === index}
                onToggle={toggle}
                onCopy={copy}
                onFocusRow={setFocused}
              />
            );
          })}
        </div>
      )}
      {status && (
        <div
          aria-hidden="true"
          className="border-t border-gray-200 px-2 py-0.5 text-xs text-green-700 dark:border-gray-700 dark:text-green-400"
        >
          {status}
        </div>
      )}
    </div>
  );
}

/** `+2 −1 ~3`, each count in its colour. */
export function CountsBadge({ counts }: { counts: DiffCounts }) {
  if (totalCount(counts) === 0) return null;
  return (
    <span
      className="inline-flex gap-1 rounded bg-gray-100 px-1 font-mono text-[11px] tabular-nums dark:bg-gray-800"
      title={countsSentence(counts)}
      aria-label={countsSentence(counts)}
      data-testid="diff-counts"
    >
      {counts.added > 0 && (
        <span className="text-green-700 dark:text-green-400">+{counts.added}</span>
      )}
      {counts.removed > 0 && (
        <span className="text-red-700 dark:text-red-400">−{counts.removed}</span>
      )}
      {counts.changed > 0 && (
        <span className="text-amber-600 dark:text-amber-400">~{counts.changed}</span>
      )}
    </span>
  );
}

interface DiffRowProps {
  node: DiffNode;
  index: number;
  open: boolean;
  tabbable: boolean;
  keyRanges: Range[] | null;
  leftRanges: Range[] | null;
  rightRanges: Range[] | null;
  current: boolean;
  onToggle: (index: number) => void;
  onCopy: (index: number, what: "value" | "old" | "path") => void;
  onFocusRow: (index: number) => void;
}

function sizeText(kind: JsonKind | null, value: unknown): string {
  if (kind === "array") {
    const n = (value as unknown[]).length;
    return `${n} item${n === 1 ? "" : "s"}`;
  }
  const n = Object.keys(value as object).length;
  return `${n} key${n === 1 ? "" : "s"}`;
}

/** One side's value in words, for the row's accessible name. */
function spoken(kind: JsonKind | null, value: unknown): string {
  if (kind === "object" || kind === "array") return `${kind}, ${sizeText(kind, value)}`;
  if (kind === "null") return "null";
  return `${scalarText(value)}, ${kind}`;
}

function rowLabel(node: DiffNode): string {
  const task =
    node.leftTaskLabel &&
    node.rightTaskLabel &&
    node.leftTaskLabel !== node.rightTaskLabel
      ? `${node.leftTaskLabel} to ${node.rightTaskLabel} `
      : node.rightTaskLabel || node.leftTaskLabel
        ? `${node.rightTaskLabel ?? node.leftTaskLabel} `
        : "";
  const key = String(node.key);
  switch (node.status) {
    case "added":
      return `${key}: added ${task}${spoken(node.rightKind, node.right)}`;
    case "removed":
      return `${key}: removed ${task}${spoken(node.leftKind, node.left)}`;
    case "unchanged":
      return `${key}: unchanged ${task}${spoken(node.rightKind, node.right)}`;
    case "changed":
      if (isPairedContainer(node)) {
        return `${key}: ${task}${spoken(node.rightKind, node.right)}, ${countsSentence(
          node.counts,
        )}`;
      }
      return `${key}: ${task}changed from ${spoken(
        node.leftKind,
        node.left,
      )} to ${spoken(node.rightKind, node.right)}`;
  }
}

const DiffRow = memo(function DiffRow({
  node,
  index,
  open,
  tabbable,
  keyRanges,
  leftRanges,
  rightRanges,
  current,
  onToggle,
  onCopy,
  onFocusRow,
}: DiffRowProps) {
  const container = isContainer(node);
  const paired = isPairedContainer(node);
  const sign = SIGN[node.status];
  // A key shown on the right that sat at another index on the left.
  const moved =
    typeof node.leftKey === "number" &&
    typeof node.rightKey === "number" &&
    node.leftKey !== node.rightKey;
  return (
    <div
      role="treeitem"
      aria-level={node.depth}
      aria-posinset={node.posInSet}
      aria-setsize={node.setSize}
      aria-expanded={container ? open : undefined}
      aria-label={rowLabel(node)}
      tabIndex={tabbable ? 0 : -1}
      data-row={index}
      data-status={node.status}
      onFocus={(e) => {
        if (e.target === e.currentTarget) onFocusRow(index);
      }}
      onClick={() => {
        onFocusRow(index);
        if (container) onToggle(index);
      }}
      style={{ paddingLeft: `${(node.depth - 1) * 1.25 + 0.25}rem` }}
      className={`group flex items-start gap-1 pr-2 leading-6 [contain-intrinsic-size:auto_1.5rem] [content-visibility:auto] focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-inset ${
        container ? "cursor-pointer" : "cursor-default"
      } ${ROW_CLASS[node.status]} ${
        current
          ? "ring-2 ring-amber-400 ring-inset dark:ring-amber-500"
          : "hover:brightness-95 dark:hover:brightness-110"
      }`}
    >
      <span
        aria-hidden="true"
        className={`inline-block w-3 shrink-0 text-center select-none ${sign.className}`}
      >
        {sign.text}
      </span>
      <span
        aria-hidden="true"
        className="inline-block w-3 shrink-0 text-center text-gray-400 select-none"
      >
        {container ? (open ? "▾" : "▸") : ""}
      </span>
      <span className="shrink-0">
        <span
          className={
            typeof node.key === "number"
              ? "text-gray-400 dark:text-gray-500"
              : "whitespace-pre text-sky-800 dark:text-sky-300"
          }
          title={moved ? `At index ${node.leftKey} on the left` : undefined}
        >
          <Highlighted text={String(node.key)} ranges={keyRanges} current={current} />
        </span>
        <span className="text-gray-400">:</span>
      </span>
      <span className="min-w-0 flex-1 break-all whitespace-pre-wrap">
        <TaskLabels node={node} />
        {paired ? (
          <ContainerSummary
            kind={node.rightKind}
            value={node.right}
            open={open}
            counts={node.counts}
            identical={node.status === "unchanged" && node.rightTaskLabel !== null}
          />
        ) : node.status === "changed" ? (
          <>
            <SideValue
              kind={node.leftKind}
              value={node.left}
              ranges={leftRanges}
              current={current}
              className={OLD_CLASS}
              as="del"
            />
            <span aria-hidden="true" className="mx-1 text-gray-400">
              →
            </span>
            <SideValue
              kind={node.rightKind}
              value={node.right}
              ranges={rightRanges}
              current={current}
              className={NEW_CLASS}
              as="ins"
            />
          </>
        ) : node.status === "removed" ? (
          <SideValue
            kind={node.leftKind}
            value={node.left}
            ranges={leftRanges}
            current={current}
            open={open}
          />
        ) : (
          <SideValue
            kind={node.rightKind}
            value={node.right}
            ranges={rightRanges}
            current={current}
            open={open}
          />
        )}
      </span>
      <span className="flex shrink-0 gap-1 opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100">
        {node.status === "changed" && !paired && (
          <RowCopyButton label="Copy old value" onClick={() => onCopy(index, "old")}>
            old
          </RowCopyButton>
        )}
        <RowCopyButton label="Copy value" onClick={() => onCopy(index, "value")}>
          value
        </RowCopyButton>
        <RowCopyButton label="Copy path" onClick={() => onCopy(index, "path")}>
          path
        </RowCopyButton>
      </span>
    </div>
  );
});

const TASK_CHIP = "mr-1.5 rounded px-1 font-sans text-[11px]";
const TASK_NEUTRAL =
  "bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-300";

/** The nested task's `namespace.Name`, or both when the task type changed. */
function TaskLabels({ node }: { node: DiffNode }) {
  const { leftTaskLabel: l, rightTaskLabel: r } = node;
  if (!l && !r) return null;
  if (l && r && l === r) {
    return (
      <span className={`${TASK_CHIP} ${TASK_NEUTRAL}`} title="Nested task">
        {r}
      </span>
    );
  }
  return (
    <>
      {l && (
        <span
          className={`${TASK_CHIP} ${r ? OLD_CLASS : TASK_NEUTRAL}`}
          title="Nested task (left)"
        >
          {l}
        </span>
      )}
      {l && r && (
        <span aria-hidden="true" className="mr-1.5 text-gray-400">
          →
        </span>
      )}
      {r && (
        <span
          className={`${TASK_CHIP} ${l ? NEW_CLASS : TASK_NEUTRAL}`}
          title="Nested task (right)"
        >
          {r}
        </span>
      )}
    </>
  );
}

function ContainerSummary({
  kind,
  value,
  open,
  counts,
  identical,
}: {
  kind: JsonKind | null;
  value: unknown;
  open: boolean;
  counts: DiffCounts;
  identical: boolean;
}) {
  const empty = sizeText(kind, value).startsWith("0 ");
  return (
    <>
      {!open && (
        <span className="mr-1.5 text-gray-400 dark:text-gray-500">
          {kind === "array" ? "[…]" : "{…}"}{" "}
          <span className="font-sans text-xs">{sizeText(kind, value)}</span>
        </span>
      )}
      {open && empty && (
        <span className="mr-1.5 text-gray-400 dark:text-gray-500">
          {kind === "array" ? "[]" : "{}"}
        </span>
      )}
      {!open && <CountsBadge counts={counts} />}
      {identical && !open && (
        <span className="font-sans text-xs text-gray-400 dark:text-gray-500">
          identical
        </span>
      )}
    </>
  );
}

/** One side's value: a scalar, or a container's collapsed summary. */
function SideValue({
  kind,
  value,
  ranges,
  current,
  className,
  as,
  open = false,
}: {
  kind: JsonKind | null;
  value: unknown;
  ranges: Range[] | null;
  current: boolean;
  className?: string;
  as?: "del" | "ins";
  open?: boolean;
}) {
  const Tag = as ?? "span";
  if (kind === "object" || kind === "array") {
    if (open && !className) return null;
    return (
      <Tag className={className ?? "text-gray-400 dark:text-gray-500"}>
        {kind === "array" ? "[…]" : "{…}"}{" "}
        <span className="font-sans text-xs">{sizeText(kind, value)}</span>
      </Tag>
    );
  }
  const quote = kind === "string" ? '"' : "";
  return (
    <Tag className={className ?? VALUE_CLASS[kind ?? "null"]}>
      {quote}
      <Highlighted text={scalarText(value)} ranges={ranges} current={current} />
      {quote}
    </Tag>
  );
}
