import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  allContainerIds,
  ancestorIds,
  copyValueText,
  defaultExpanded,
  findMatches,
  highlightSegments,
  indexJson,
  isContainer,
  scalarText,
  sizeLabel,
  union,
  visibleRows,
  type JsonMatch,
  type JsonNode,
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
 * matches, so typing in it does not re-render the tree per keystroke.
 *
 * It is an ARIA tree in its flat form: every row is a `treeitem` carrying
 * its level, position and (for containers) expanded state, with one row
 * in the tab order at a time. Arrow keys move and fold, Home/End jump,
 * Enter or Space toggles, `c` copies the focused value and `p` its path.
 * The copy buttons on each row are the same actions for a pointer, and
 * are kept out of the tab order for that reason.
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
  const nodes = useMemo(() => indexJson(value), [value]);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() =>
    defaultExpanded(nodes, defaultDepth),
  );
  const [input, setInput] = useState("");
  const [search, setSearch] = useState<SearchState>(NO_SEARCH);
  const [focused, setFocused] = useState(1);
  const [status, setStatus] = useState("");
  const treeRef = useRef<HTMLDivElement>(null);
  const treeId = useId();
  const statusTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const rows = useMemo(() => visibleRows(nodes, expanded), [nodes, expanded]);
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
        setFocused(matches[0].index);
      }
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [input, nodes]);

  // Keep the current match in view.
  useEffect(() => {
    if (currentMatch === undefined) return;
    const el = treeRef.current?.querySelector<HTMLElement>(
      `[data-index="${currentMatch.index}"]`,
    );
    el?.scrollIntoView?.({ block: "nearest" });
  }, [currentMatch]);

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
    setSearch((prev) => ({ ...prev, current }));
    // The user may have folded it away since the search ran.
    setExpanded((prev) => union(prev, ancestorIds(nodes, [index])));
    setFocused(index);
  };

  // The roving tab stop: the focused row if visible, else the first row.
  const tabStop = rows.includes(focused) ? focused : rows[0] ?? -1;

  const focusRow = useCallback((index: number) => {
    setFocused(index);
    treeRef.current?.querySelector<HTMLElement>(`[data-index="${index}"]`)?.focus();
  }, []);

  const onTreeKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    if (target.getAttribute("role") !== "treeitem") return;
    // Leave the browser's own shortcuts (Ctrl+C on a selection, say) alone.
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const index = Number(target.dataset.index);
    const node = nodes[index];
    const row = rows.indexOf(index);
    if (node === undefined || row === -1) return;
    const open = isContainer(node) && expanded.has(node.id);
    let handled = true;
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
        if (isContainer(node) && !open) toggle(index);
        else if (open && node.size > 0) focusRow(index + 1);
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
            const match = matchByIndex.get(index);
            return (
              <JsonRow
                key={nodes[index].id}
                node={nodes[index]}
                index={index}
                open={expanded.has(nodes[index].id)}
                tabbable={index === tabStop}
                keyQuery={match?.inKey ? search.query : ""}
                valueQuery={match?.inValue ? search.query : ""}
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
  /** The search query when it matched this row's key or value, else "". */
  keyQuery: string;
  valueQuery: string;
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
  keyQuery,
  valueQuery,
  current,
  onToggle,
  onCopy,
  onFocusRow,
}: JsonRowProps) {
  const container = isContainer(node);
  const summary = container
    ? `${node.kind === "array" ? "array" : "object"}, ${sizeLabel(node)}`
    : scalarText(node.value);
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
      data-index={index}
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
          <Highlighted text={String(node.key)} query={keyQuery} current={current} />
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
          <ScalarValue node={node} query={valueQuery} current={current} />
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

const SCALAR_CLASS: Record<string, string> = {
  string: "text-green-700 dark:text-green-400",
  number: "text-blue-700 dark:text-blue-300",
  boolean: "text-purple-700 dark:text-purple-300",
  null: "text-gray-500 italic dark:text-gray-400",
};

function ScalarValue({
  node,
  query,
  current,
}: {
  node: JsonNode;
  query: string;
  current: boolean;
}) {
  const text = scalarText(node.value);
  const quote = node.kind === "string" ? '"' : "";
  return (
    <span className={SCALAR_CLASS[node.kind]}>
      {quote}
      <Highlighted text={text} query={query} current={current} />
      {quote}
    </span>
  );
}

function Highlighted({
  text,
  query,
  current,
}: {
  text: string;
  query: string;
  current: boolean;
}) {
  if (!query) return <>{text}</>;
  return (
    <>
      {highlightSegments(text, query).map((segment, i) =>
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
