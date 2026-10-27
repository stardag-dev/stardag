import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  allContainerIds,
  ancestorIds,
  copyValueText,
  defaultExpanded,
  findMatches,
  hiddenCount,
  indexJson,
  isContainer,
  isMoreRow,
  moreOf,
  nthChild,
  PAGE_SIZE,
  windowOf,
  revealInPages,
  scalarText,
  segmentsOf,
  showMore,
  sizeLabel,
  union,
  visibleRows,
  type JsonMatch,
  type JsonNode,
  type Range,
  type MoreDirection,
  type Windows,
} from "../utils/jsonTree";

/** How long typing must pause before the tree is searched. */
export const SEARCH_DEBOUNCE_MS = 150;

/**
 * How many matches a search reveals at once. A one-letter query over a
 * large value matches most of it, and expanding to all of those would
 * draw the whole tree; the rest are revealed as next/previous reaches
 * them.
 */
export const REVEAL_MATCHES = 100;

interface JsonTreeProps {
  value: unknown;
  /** The tree's accessible name, and what the search box says it searches. */
  label: string;
  /** Classes for the scrolling tree area (height limits, say). */
  className?: string;
  /** Levels expanded initially, the top level being 1. */
  defaultDepth?: number;
  /** Shown when the value has no children. */
  emptyText?: string;
}

interface SearchState {
  query: string;
  matches: JsonMatch[];
  current: number;
}

const NO_SEARCH: SearchState = { query: "", matches: [], current: 0 };

/**
 * A collapsible, searchable view of a JSON value.
 *
 * The value is indexed once into a flat preorder list; the visible rows
 * are recomputed from the expanded set, and only rows whose props change
 * re-render. The search is debounced and highlights only the rows it
 * matches, so typing in it does not re-render the tree per keystroke. It
 * is fuzzy (see `fuzzyMatch`), and steps through matches best first.
 *
 * It is an ARIA tree in its flat form: every row is a `treeitem` carrying
 * its level, position and (for containers) expanded state, with one row
 * in the tab order at a time. Arrow keys move and fold, Home/End jump,
 * Enter or Space toggles, `c` copies the focused value and `p` its path.
 * The copy buttons on each row are the same actions for a pointer, and
 * are kept out of the tab order for that reason.
 *
 * A wide container draws a page of children at a time, with "show
 * more" rows either side, so the number of rows stays bounded however
 * many siblings a search matches; navigating to a match moves its
 * parent's page to it.
 *
 * State is per value: give the component a `key` when the value it shows
 * can change identity, so expansion and search start over.
 */
export function JsonTree({
  value,
  label,
  className = "",
  defaultDepth = 2,
  emptyText = "Empty.",
}: JsonTreeProps) {
  const stable = useStableJson(value);
  const nodes = useMemo(() => indexJson(stable), [stable]);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() =>
    defaultExpanded(nodes, defaultDepth),
  );
  const [input, setInput] = useState("");
  const [search, setSearch] = useState<SearchState>(NO_SEARCH);
  const [windows, setWindows] = useState<Windows>(() => new Map());
  // The focused row: a node index, or a "show more" row (see visibleRows).
  const [focused, setFocused] = useState(1);
  const [status, setStatus] = useState("");
  const treeRef = useRef<HTMLDivElement>(null);
  const treeId = useId();
  const statusTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // A row to focus once it has rendered (after "show more").
  const pendingFocus = useRef<number | null>(null);

  const rows = useMemo(
    () => visibleRows(nodes, expanded, windows),
    [nodes, expanded, windows],
  );
  const matchByIndex = useMemo(
    () => new Map(search.matches.map((m) => [m.index, m])),
    [search.matches],
  );
  const currentMatch = search.matches[search.current];

  // Search once typing pauses: reveal the matches and go to the first.
  useEffect(() => {
    const timer = setTimeout(() => {
      if (!input.trim()) {
        setSearch((prev) => (prev.query === "" ? prev : NO_SEARCH));
        return;
      }
      const matches = findMatches(nodes, input);
      setSearch({ query: input.trim(), matches, current: 0 });
      if (matches.length > 0) {
        setExpanded((prev) =>
          union(
            prev,
            ancestorIds(
              nodes,
              matches.slice(0, REVEAL_MATCHES).map((m) => m.index),
            ),
          ),
        );
        setWindows((prev) => revealInPages(nodes, prev, matches[0].index));
        setFocused(matches[0].index);
      }
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [input, nodes]);

  // Bring the current match into view on every search and every
  // next/previous, including a step that lands on the same match (a
  // single result the user has scrolled or folded away from).
  useEffect(() => {
    const match = search.matches[search.current];
    if (match === undefined) return;
    const el = treeRef.current?.querySelector<HTMLElement>(
      `[data-row="${match.index}"]`,
    );
    el?.scrollIntoView?.({ block: "nearest" });
  }, [search]);

  // Focus a row that "show more" has just drawn.
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
    async (index: number, what: "value" | "path") => {
      const node = nodes[index];
      const text = what === "path" ? node.id : copyValueText(node);
      try {
        await navigator.clipboard.writeText(text);
        announce(
          what === "path" ? `Copied path ${node.id}` : `Copied value of ${node.id}`,
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

  const goToMatch = (step: 1 | -1) => {
    const count = search.matches.length;
    if (count === 0) return;
    const current = (search.current + step + count) % count;
    const index = search.matches[current].index;
    // A new object even when `current` is unchanged, so the scroll runs.
    setSearch((prev) => ({ ...prev, current }));
    // The user may have folded it away since the search ran, and it may
    // lie past its parent's page.
    setExpanded((prev) => union(prev, ancestorIds(nodes, [index])));
    setWindows((prev) => revealInPages(nodes, prev, index));
    setFocused(index);
  };

  const more = useCallback(
    (row: number, focus: boolean) => {
      const { parent, direction } = moreOf(row);
      const [start, end] = windowOf(nodes, windows, parent);
      // The newly drawn child next to the row: where focus goes.
      const first = nthChild(nodes, parent, direction === "before" ? start - 1 : end);
      setWindows((prev) => showMore(nodes, prev, parent, direction));
      if (first !== -1) {
        setFocused(first);
        if (focus) pendingFocus.current = first;
      }
    },
    [nodes, windows],
  );

  // The roving tab stop: the focused row if visible, else the first row.
  const tabStop = rows.includes(focused) ? focused : rows[0] ?? null;

  const focusRow = useCallback((row: number) => {
    setFocused(row);
    treeRef.current?.querySelector<HTMLElement>(`[data-row="${row}"]`)?.focus();
  }, []);

  const onTreeKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    if (target.getAttribute("role") !== "treeitem") return;
    // Leave the browser's own shortcuts (Ctrl+C on a selection, say) alone.
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const index = Number(target.dataset.row);
    const row = rows.indexOf(index);
    if (row === -1) return;
    let handled = true;
    if (isMoreRow(index)) {
      const { parent } = moreOf(index);
      switch (event.key) {
        case "ArrowDown":
          if (row + 1 < rows.length) focusRow(rows[row + 1]);
          break;
        case "ArrowUp":
          if (row > 0) focusRow(rows[row - 1]);
          break;
        case "Home":
          focusRow(rows[0]);
          break;
        case "End":
          focusRow(rows[rows.length - 1]);
          break;
        case "ArrowLeft":
          if (parent > 0) focusRow(parent);
          break;
        case "Enter":
        case " ":
          more(index, true);
          break;
        default:
          handled = false;
      }
      if (handled) event.preventDefault();
      return;
    }
    const node = nodes[index];
    const open = isContainer(node) && expanded.has(node.id);
    switch (event.key) {
      case "ArrowDown":
        if (row + 1 < rows.length) focusRow(rows[row + 1]);
        break;
      case "ArrowUp":
        if (row > 0) focusRow(rows[row - 1]);
        break;
      case "Home":
        focusRow(rows[0]);
        break;
      case "End":
        focusRow(rows[rows.length - 1]);
        break;
      case "ArrowRight":
        // Into the first drawn child: the next row, which may be a
        // "show earlier" row when the page has moved past the first.
        if (isContainer(node) && !open) toggle(index);
        else if (open && node.size > 0 && row + 1 < rows.length)
          focusRow(rows[row + 1]);
        break;
      case "ArrowLeft":
        if (open) toggle(index);
        else if (node.parent > 0) focusRow(node.parent);
        break;
      case "Enter":
      case " ":
        if (isContainer(node)) toggle(index);
        break;
      case "c":
        void copy(index, "value");
        break;
      case "p":
        void copy(index, "path");
        break;
      default:
        handled = false;
    }
    if (handled) event.preventDefault();
  };

  const onSearchKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      goToMatch(event.shiftKey ? -1 : 1);
    } else if (event.key === "Escape" && input) {
      event.preventDefault();
      event.stopPropagation(); // not the enclosing modal's Escape
      setInput("");
    }
  };

  const searching = search.query !== "";
  const matchCount = search.matches.length;

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-gray-50 dark:bg-gray-900">
      <div className="flex flex-wrap items-center gap-1.5 border-b border-gray-200 px-2 py-1.5 dark:border-gray-700">
        <input
          type="search"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onSearchKeyDown}
          placeholder="Search keys and values"
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
      {rows.length === 0 ? (
        <p className="p-3 text-sm text-gray-500 dark:text-gray-400">{emptyText}</p>
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
              <JsonRow
                key={nodes[index].id}
                node={nodes[index]}
                index={index}
                open={expanded.has(nodes[index].id)}
                tabbable={index === tabStop}
                keyRanges={match?.keyRanges ?? null}
                valueRanges={match?.valueRanges ?? null}
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

interface JsonRowProps {
  node: JsonNode;
  index: number;
  open: boolean;
  tabbable: boolean;
  /** The characters the search matched in the key and the value, if any. */
  keyRanges: Range[] | null;
  valueRanges: Range[] | null;
  /** This row is the current search match. */
  current: boolean;
  onToggle: (index: number) => void;
  onCopy: (index: number, what: "value" | "path") => void;
  onFocusRow: (index: number) => void;
}

const JsonRow = memo(function JsonRow({
  node,
  index,
  open,
  tabbable,
  keyRanges,
  valueRanges,
  current,
  onToggle,
  onCopy,
  onFocusRow,
}: JsonRowProps) {
  const container = isContainer(node);
  // The type is spelled out for scalars too: `false` and `"false"` draw
  // differently, and must not be announced the same.
  const summary = container
    ? `${node.kind === "array" ? "array" : "object"}, ${sizeLabel(node)}`
    : node.kind === "null"
      ? "null"
      : `${scalarText(node.value)}, ${node.kind}`;
  return (
    <div
      role="treeitem"
      aria-level={node.depth}
      aria-posinset={node.posInSet}
      aria-setsize={node.setSize}
      aria-expanded={container ? open : undefined}
      aria-label={`${node.key}: ${
        node.taskLabel ? `${node.taskLabel} ` : ""
      }${summary}`}
      tabIndex={tabbable ? 0 : -1}
      data-row={index}
      onFocus={(e) => {
        if (e.target === e.currentTarget) onFocusRow(index);
      }}
      onClick={() => {
        onFocusRow(index);
        if (container) onToggle(index);
      }}
      style={{ paddingLeft: `${(node.depth - 1) * 1.25 + 0.5}rem` }}
      className={`group flex items-start gap-1 pr-2 leading-6 [contain-intrinsic-size:auto_1.5rem] [content-visibility:auto] focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-inset ${
        container ? "cursor-pointer" : "cursor-default"
      } ${
        current
          ? "bg-amber-50 dark:bg-amber-900/20"
          : "hover:bg-gray-100 dark:hover:bg-gray-800"
      }`}
    >
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
              : "text-sky-800 dark:text-sky-300"
          }
        >
          <Highlighted text={String(node.key)} ranges={keyRanges} current={current} />
        </span>
        <span className="text-gray-400">:</span>
      </span>
      <span className="min-w-0 flex-1 break-all whitespace-pre-wrap">
        {container ? (
          <>
            {node.taskLabel && (
              <span
                className="mr-1.5 rounded bg-indigo-100 px-1 font-sans text-[11px] text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-300"
                title="Nested task"
              >
                {node.taskLabel}
              </span>
            )}
            {!open && (
              <span className="text-gray-400 dark:text-gray-500">
                {node.kind === "array" ? "[…]" : "{…}"}{" "}
                <span className="font-sans text-xs">{sizeLabel(node)}</span>
              </span>
            )}
            {open && node.size === 0 && (
              <span className="text-gray-400 dark:text-gray-500">
                {node.kind === "array" ? "[]" : "{}"}
              </span>
            )}
          </>
        ) : (
          <ScalarValue node={node} ranges={valueRanges} current={current} />
        )}
      </span>
      <span className="flex shrink-0 gap-1 opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100">
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

/** The "show more" row closing a wide container's page. */
const MoreRow = memo(function MoreRow({
  row,
  level,
  direction,
  hidden,
  tabbable,
  onMore,
  onFocusRow,
}: {
  row: number;
  level: number;
  direction: MoreDirection;
  hidden: number;
  tabbable: boolean;
  onMore: (row: number, focus: boolean) => void;
  onFocusRow: (row: number) => void;
}) {
  const label = `Show ${Math.min(PAGE_SIZE, hidden)} ${
    direction === "before" ? "earlier" : "more"
  } (${hidden.toLocaleString()} not shown)`;
  return (
    <div
      role="treeitem"
      aria-level={level}
      aria-label={label}
      tabIndex={tabbable ? 0 : -1}
      data-row={row}
      onFocus={(e) => {
        if (e.target === e.currentTarget) onFocusRow(row);
      }}
      onClick={() => onMore(row, false)}
      style={{ paddingLeft: `${(level - 1) * 1.25 + 0.5}rem` }}
      className="flex cursor-pointer items-center gap-1 pr-2 font-sans text-xs leading-6 text-blue-700 hover:bg-gray-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-inset dark:text-blue-300 dark:hover:bg-gray-800"
    >
      <span aria-hidden="true" className="inline-block w-3 shrink-0" />
      {label}
    </div>
  );
});

/**
 * `value`, kept by identity for as long as its JSON content is unchanged.
 * A refresh that re-fetches the same body produces a new object; without
 * this it would re-index the tree and re-run the search, sending the user
 * back to the first match mid-navigation. Serialises only when identity
 * changes.
 */
function useStableJson(value: unknown): unknown {
  const [seen, setSeen] = useState(value);
  const [stable, setStable] = useState(value);
  if (value !== seen) {
    setSeen(value);
    if (JSON.stringify(value) !== JSON.stringify(stable)) setStable(value);
  }
  // React re-renders with the updated state before committing.
  return stable;
}

const SCALAR_CLASS: Record<string, string> = {
  string: "text-green-700 dark:text-green-400",
  number: "text-blue-700 dark:text-blue-300",
  boolean: "text-purple-700 dark:text-purple-300",
  null: "text-gray-500 italic dark:text-gray-400",
};

function ScalarValue({
  node,
  ranges,
  current,
}: {
  node: JsonNode;
  ranges: Range[] | null;
  current: boolean;
}) {
  const text = scalarText(node.value);
  const quote = node.kind === "string" ? '"' : "";
  return (
    <span className={SCALAR_CLASS[node.kind]}>
      {quote}
      <Highlighted text={text} ranges={ranges} current={current} />
      {quote}
    </span>
  );
}

/** `text` with the matched characters marked, each run of them one `<mark>`. */
function Highlighted({
  text,
  ranges,
  current,
}: {
  text: string;
  ranges: Range[] | null;
  current: boolean;
}) {
  if (!ranges) return <>{text}</>;
  return (
    <>
      {segmentsOf(text, ranges).map((segment, i) =>
        segment.match ? (
          <mark
            key={i}
            className={`rounded-sm text-inherit ${
              current
                ? "bg-amber-300 dark:bg-amber-600/70"
                : "bg-yellow-200 dark:bg-yellow-700/50"
            }`}
          >
            {segment.text}
          </mark>
        ) : (
          <span key={i}>{segment.text}</span>
        ),
      )}
    </>
  );
}

function RowCopyButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      tabIndex={-1}
      aria-label={label}
      title={`${label} (${label === "Copy value" ? "c" : "p"})`}
      onClick={(e) => {
        // Copying must not also toggle the row.
        e.stopPropagation();
        onClick();
      }}
      className="rounded bg-gray-200 px-1 font-sans text-[10px] text-gray-600 hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
    >
      {children}
    </button>
  );
}

function TreeButton({
  label,
  onClick,
  disabled = false,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className="rounded px-1.5 py-0.5 text-xs whitespace-nowrap text-gray-600 hover:bg-gray-200 disabled:opacity-40 dark:text-gray-300 dark:hover:bg-gray-700"
    >
      {children}
    </button>
  );
}
