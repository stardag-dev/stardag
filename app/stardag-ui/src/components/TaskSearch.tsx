import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { fetchTasks, type TaskFilters } from "../api/registry";
import { useTaskSearchUrl, type TaskSearchUrl } from "../hooks/useTaskSearchUrl";
import type { TaskStatus, TaskSummary } from "../types/task";
import { checkTag } from "../utils/annotations";
import { shortTaskId } from "../utils/ids";
import { qualifiedName } from "../utils/instances";
import { formatAbsoluteTime, formatRelativeTime } from "../utils/time";
import {
  classifyQuery,
  EMPTY_SEARCH,
  isEmptySearch,
  isFullTaskId,
  MIN_TEXT_QUERY_LENGTH,
  normaliseIdPrefix,
  TASK_STATUSES,
  toTaskFilters,
  withTag,
  withoutTag,
  type TaskSearchState,
} from "../utils/taskSearch";
import {
  harvestInto,
  loadStore,
  rememberSearch,
  suggest,
  type Suggestion,
} from "../utils/taskSuggestions";
import { StatusBadge } from "./StatusBadge";
import { CopyChip } from "./ui/CopyChip";
import { ResultBanner } from "./ui/ResultBanner";
import { TagChip } from "./ui/TagChip";

/** A request identity (`JSON.stringify` of filters) without its `q`. */
function withoutQ(query: string): string {
  const { q: _q, ...rest } = JSON.parse(query) as TaskFilters;
  void _q;
  return JSON.stringify(rest);
}

/** Rows per page; "Load more" appends the next. */
export const TASK_PAGE_SIZE = 50;
/** How long typing settles before the search runs. */
export const SEARCH_DEBOUNCE_MS = 250;
/** One unfiltered read per environment and session seeds autocomplete. */
const SEED_LIMIT = 200;
const seededEnvironments = new Set<string>();

const CONTROL =
  "rounded-md border border-gray-300 px-2 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100";

const FILTER_CHIP =
  "inline-flex items-center gap-1 rounded-full border border-gray-300 bg-white px-2 py-0.5 text-xs text-gray-700 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200";

const LINK_BUTTON =
  "rounded px-1 text-xs font-medium text-blue-600 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-400";

// A tag typed into the search box: `#tag` or `tag:tag`.
const TAG_DRAFT = /^(?:#|tag:\s*)(.*)$/i;

interface TaskSearchProps {
  environmentId: string;
  onOpenTask: (taskId: string) => void;
}

interface TaskSearchPanelProps extends TaskSearchProps {
  /** The search and its URL binding (`useTaskSearchUrl`). */
  url: TaskSearchUrl;
  /** Drawn under the filter chips: a page's note on its filters. */
  notice?: ReactNode;
  /** A row to mark as selected (the one a page has picked). */
  selectedTaskId?: string | null;
  /** What opening a row does, for its accessible name: "Open", "Compare with". */
  openVerb?: string;
  /**
   * Record searches as "Recent" suggestions. On for `/tasks`; off where
   * the search is a picker pre-filled by the page.
   */
  rememberSearches?: boolean;
}

type Option =
  | { kind: "add-tag"; tag: string; label: string }
  | { kind: "open-id"; taskId: string; label: string }
  | Suggestion;

function optionText(option: Option): { prefix: string; text: string } {
  switch (option.kind) {
    case "add-tag":
      return { prefix: "Filter by tag", text: option.tag };
    case "open-id":
      return { prefix: "Open task", text: option.taskId };
    case "name":
      return { prefix: "Name", text: option.label };
    case "namespace":
      return { prefix: "Namespace", text: option.label };
    case "tag":
      return { prefix: "Tag", text: option.label };
    case "recent":
      return { prefix: "Recent", text: option.label };
  }
}

/**
 * `/tasks`: search the environment's completions — free text, a task-id
 * prefix, exact name and namespace, tags (all of them) and status — over
 * `GET /tasks`, most recent status change first. The search lives in the
 * URL. Autocomplete is derived client-side (names, namespaces and tags
 * already seen, and recent searches): the registry has no suggestion route.
 */
export function TaskSearch({ environmentId, onOpenTask }: TaskSearchProps) {
  const url = useTaskSearchUrl();
  return (
    <TaskSearchPanel url={url} environmentId={environmentId} onOpenTask={onOpenTask} />
  );
}

/**
 * The search bar, filter chips and results table of `/tasks`, over a
 * search the caller binds to the URL: `/tasks` itself, and the right-hand
 * picker of `/task-diff`, which pre-filters it to one task type.
 */
export function TaskSearchPanel({
  url,
  environmentId,
  onOpenTask,
  notice,
  selectedTaskId = null,
  openVerb = "Open",
  rememberSearches = true,
}: TaskSearchPanelProps) {
  const { search, setSearch, navigationKey } = url;
  const [text, setText] = useState(search.text);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retryNonce, setRetryNonce] = useState(0);
  // The search the shown rows answer, so a stale page is never appended to.
  const [shownQuery, setShownQuery] = useState<string | null>(null);
  const [store, setStore] = useState(() => loadStore(environmentId));
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // The first-page read has started and not settled.
  const readInFlightRef = useRef(false);
  const listboxId = useId();

  // Back/Forward replaced the search under the page: follow it in the box.
  const [syncedKey, setSyncedKey] = useState(navigationKey);
  if (syncedKey !== navigationKey) {
    setSyncedKey(navigationKey);
    setText(search.text);
  }

  // What the box searches for: a `#tag` being typed is not text yet.
  const tagDraft = TAG_DRAFT.exec(text.trim());
  const effectiveText = tagDraft ? "" : text;

  // Typing settles into the URL without a history entry per keystroke.
  useEffect(() => {
    // The URL holds the text trimmed: compare like with like, or trailing
    // whitespace would keep this firing.
    if (effectiveText.trim() === search.text) return;
    const handle = setTimeout(
      () => setSearch({ ...search, text: effectiveText }, { replace: true }),
      SEARCH_DEBOUNCE_MS,
    );
    return () => clearTimeout(handle);
  }, [effectiveText, search, setSearch]);

  // The search with what is typed now, for edits made before it settles.
  const current: TaskSearchState = useMemo(
    () => ({ ...search, text: effectiveText }),
    [search, effectiveText],
  );

  const filters = useMemo(() => toTaskFilters(search), [search]);
  // The request's identity: what is sent, not what is typed. Text under
  // three characters sends nothing, so typing it issues no request.
  const query = JSON.stringify(filters);
  // Text shortened under three characters keeps the rows it found: the
  // only change would be dropping `q`, and short text is suggestions-only.
  // Any other change (a filter, clearing the box) reads as usual.
  const held =
    classifyQuery(search.text).kind === "short" &&
    shownQuery !== null &&
    withoutQ(shownQuery) === query;

  useEffect(() => {
    if (held) {
      // A read for text since shortened is no longer wanted.
      if (readInFlightRef.current) {
        abortRef.current?.abort();
        readInFlightRef.current = false;
        setLoading(false);
      }
      return;
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    readInFlightRef.current = true;
    setLoading(true);
    setLoadingMore(false);
    setError(null);
    fetchTasks(environmentId, { ...filters, limit: TASK_PAGE_SIZE }, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) return;
        setTasks(page.tasks);
        setNextCursor(page.next_cursor);
        setShownQuery(query);
        harvestInto(environmentId, page.tasks);
        setStore(loadStore(environmentId));
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setTasks([]);
        setNextCursor(null);
        setShownQuery(null);
        setError(err instanceof Error ? err.message : "Failed to search tasks");
      })
      .finally(() => {
        if (controller.signal.aborted) return;
        readInFlightRef.current = false;
        setLoading(false);
      });
    // Not aborted on re-run: a held search keeps the shown rows' read (and
    // its "Load more") alive. The next real read aborts it.
    // `query` is the request's identity; `filters` is what it serialises.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [environmentId, query, retryNonce, held]);

  useEffect(() => () => abortRef.current?.abort(), []);

  // Seed autocomplete with one wider unfiltered read per environment.
  useEffect(() => {
    if (seededEnvironments.has(environmentId)) return;
    seededEnvironments.add(environmentId);
    fetchTasks(environmentId, { limit: SEED_LIMIT })
      .then((page) => {
        harvestInto(environmentId, page.tasks);
        setStore(loadStore(environmentId));
      })
      .catch(() => seededEnvironments.delete(environmentId));
  }, [environmentId]);

  const loadMore = useCallback(() => {
    if (!nextCursor || loadingMore) return;
    if (shownQuery === null) return;
    const controller = abortRef.current;
    setLoadingMore(true);
    // The next page of the rows shown, which a held search may no longer
    // describe; any newer read aborts this controller.
    fetchTasks(environmentId, {
      ...(JSON.parse(shownQuery) as TaskFilters),
      limit: TASK_PAGE_SIZE,
      cursor: nextCursor,
    })
      .then((page) => {
        if (controller?.signal.aborted) return;
        setError(null);
        setTasks((rows) => {
          const seen = new Set(rows.map((r) => r.task_id));
          return [...rows, ...page.tasks.filter((t) => !seen.has(t.task_id))];
        });
        setNextCursor(page.next_cursor);
        harvestInto(environmentId, page.tasks);
        setStore(loadStore(environmentId));
      })
      .catch((err: unknown) => {
        if (controller?.signal.aborted) return;
        setError(err instanceof Error ? err.message : "Failed to load more tasks");
      })
      .finally(() => {
        // A superseded search's page must not clear the new one's flag.
        if (!controller?.signal.aborted) setLoadingMore(false);
      });
  }, [environmentId, nextCursor, loadingMore, shownQuery]);

  // ---- Edits ----

  const commit = useCallback(
    (next: TaskSearchState) => {
      setSearch(next);
      setText(next.text);
    },
    [setSearch],
  );

  const addTagFilter = useCallback(
    (tag: string) => commit(withTag(current, tag)),
    [commit, current],
  );

  const openTask = useCallback(
    (taskId: string) => {
      if (rememberSearches) rememberSearch(environmentId, search);
      onOpenTask(taskId);
    },
    [environmentId, search, onOpenTask, rememberSearches],
  );

  // ---- Autocomplete ----

  const options: Option[] = useMemo(() => {
    const out: Option[] = [];
    if (tagDraft) {
      const draft = tagDraft[1];
      const check = checkTag(draft);
      if (check.ok && !search.tags.includes(check.tag)) {
        out.push({ kind: "add-tag", tag: check.tag, label: check.tag });
      }
      for (const s of suggest(store, draft, search, ["tag"])) {
        if (s.kind === "tag" && !(check.ok && s.tag === check.tag)) out.push(s);
      }
      return out;
    }
    if (isFullTaskId(text)) {
      const id = normaliseIdPrefix(text.replace(/^id:\s*/i, "")) ?? text.trim();
      out.push({ kind: "open-id", taskId: id, label: id });
    }
    out.push(...suggest(store, text, search));
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, store, search]);

  const listOpen = open && options.length > 0;

  const choose = (option: Option) => {
    setOpen(false);
    setActive(-1);
    switch (option.kind) {
      case "add-tag":
      case "tag":
        commit(withTag({ ...search, text: "" }, option.tag));
        break;
      case "open-id":
        openTask(option.taskId);
        break;
      case "name":
        commit({ ...search, text: "", namespace: option.namespace, name: option.name });
        break;
      case "namespace":
        commit({ ...search, text: "", namespace: option.namespace });
        break;
      case "recent":
        commit(option.search);
        break;
    }
    inputRef.current?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setActive((i) => (options.length ? (i + 1) % options.length : -1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true);
      setActive((i) => (options.length ? (i <= 0 ? options.length - 1 : i - 1) : -1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (listOpen && active >= 0 && active < options.length) {
        choose(options[active]);
      } else if (tagDraft) {
        const check = checkTag(tagDraft[1]);
        if (check.ok) choose({ kind: "add-tag", tag: check.tag, label: check.tag });
      } else if (isFullTaskId(text)) {
        openTask(normaliseIdPrefix(text.replace(/^id:\s*/i, "")) ?? text.trim());
      } else {
        // Run now rather than after the debounce, and remember it (text
        // too short to search is not worth remembering).
        const next = current;
        setSearch(next, { replace: true });
        if (rememberSearches && classifyQuery(next.text).kind !== "short") {
          rememberSearch(environmentId, next);
        }
        setStore(loadStore(environmentId));
        setOpen(false);
      }
    } else if (e.key === "Escape") {
      if (listOpen) {
        e.preventDefault();
        setOpen(false);
        setActive(-1);
      } else if (text) {
        e.preventDefault();
        setText("");
      }
    } else if (e.key === "Backspace" && text === "" && search.tags.length > 0) {
      // Backspace in an empty box takes the last tag filter off, as in
      // any chip input.
      commit(withoutTag(search, search.tags[search.tags.length - 1]));
    }
  };

  const queryKind = classifyQuery(text);
  const filtersActive = !isEmptySearch(current);
  const showingStale = loading && shownQuery !== null;

  return (
    <div className="flex h-full flex-col">
      <div className="space-y-2 border-b border-gray-200 bg-white px-4 py-3 dark:border-gray-700 dark:bg-gray-800">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[16rem] max-w-2xl flex-1">
            <svg
              aria-hidden="true"
              className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M21 21l-4.35-4.35M10.5 18a7.5 7.5 0 100-15 7.5 7.5 0 000 15z"
              />
            </svg>
            <input
              ref={inputRef}
              type="search"
              role="combobox"
              aria-label="Search tasks"
              aria-autocomplete="list"
              aria-expanded={listOpen}
              aria-controls={listboxId}
              aria-activedescendant={
                listOpen && active >= 0 ? `${listboxId}-${active}` : undefined
              }
              placeholder="Search by name, namespace, description or task id…  #tag to filter by tag"
              value={text}
              onChange={(e) => {
                setText(e.target.value);
                setOpen(true);
                setActive(-1);
              }}
              onFocus={() => setOpen(true)}
              onBlur={() => setOpen(false)}
              onKeyDown={onKeyDown}
              className={`w-full pl-8 ${CONTROL}`}
            />
            {listOpen && (
              <ul
                id={listboxId}
                role="listbox"
                aria-label="Suggestions"
                className="absolute left-0 right-0 top-full z-20 mt-1 max-h-80 overflow-auto rounded-md border border-gray-200 bg-white py-1 text-sm shadow-lg dark:border-gray-700 dark:bg-gray-800"
              >
                {options.map((option, i) => {
                  const { prefix, text: optionLabel } = optionText(option);
                  return (
                    <li
                      key={`${option.kind}:${optionLabel}`}
                      id={`${listboxId}-${i}`}
                      role="option"
                      aria-selected={i === active}
                      // Before the input's blur, so the choice lands.
                      onMouseDown={(e) => {
                        e.preventDefault();
                        choose(option);
                      }}
                      onMouseEnter={() => setActive(i)}
                      className={`flex cursor-pointer items-baseline gap-2 px-3 py-1.5 ${
                        i === active
                          ? "bg-blue-50 dark:bg-blue-900/40"
                          : "hover:bg-gray-50 dark:hover:bg-gray-700/50"
                      }`}
                    >
                      <span className="w-24 flex-shrink-0 text-xs text-gray-500 dark:text-gray-400">
                        {prefix}
                      </span>
                      <span
                        className={`truncate text-gray-900 dark:text-gray-100 ${
                          option.kind === "open-id" ? "font-mono text-xs" : ""
                        }`}
                      >
                        {optionLabel}
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
          <label className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
            Status
            <select
              aria-label="Filter by task status"
              value={search.status ?? ""}
              onChange={(e) =>
                commit({
                  ...current,
                  status: (e.target.value || null) as TaskStatus | null,
                })
              }
              className={CONTROL}
            >
              <option value="">All</option>
              {TASK_STATUSES.map((status) => (
                <option key={status} value={status}>
                  {status}
                </option>
              ))}
            </select>
          </label>
          <span
            role="status"
            aria-live="polite"
            className="ml-auto text-xs text-gray-500 dark:text-gray-400"
          >
            {loading
              ? "Searching…"
              : error
                ? ""
                : `${tasks.length}${nextCursor ? "+" : ""} task${
                    tasks.length === 1 && !nextCursor ? "" : "s"
                  }`}
          </span>
        </div>

        {queryKind.kind === "short" && (
          <p role="status" className="text-xs text-gray-500 dark:text-gray-400">
            Type at least {MIN_TEXT_QUERY_LENGTH} characters to search names, namespaces
            and descriptions.
          </p>
        )}

        {(filtersActive || queryKind.kind === "id") && (
          <div className="flex flex-wrap items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
            {queryKind.kind === "id" && (
              <span
                className={FILTER_CHIP}
                title="Typed text that looks like a task id searches by id prefix. Prefix with “id:” to force it."
              >
                task id starts with{" "}
                <span className="font-mono">{queryKind.idPrefix}</span>
              </span>
            )}
            {search.namespace !== null && (
              <span className={FILTER_CHIP}>
                namespace:{" "}
                <span className="font-medium">{search.namespace || "(root)"}</span>
                <RemoveFilter
                  label="Remove namespace filter"
                  onClick={() => commit({ ...current, namespace: null })}
                />
              </span>
            )}
            {search.name && (
              <span className={FILTER_CHIP}>
                name: <span className="font-medium">{search.name}</span>
                <RemoveFilter
                  label="Remove name filter"
                  onClick={() => commit({ ...current, name: null })}
                />
              </span>
            )}
            {search.tags.map((tag) => (
              <TagChip
                key={tag}
                tag={tag}
                onRemove={() => commit(withoutTag(current, tag))}
                removeLabel={`Remove tag filter ${tag}`}
              />
            ))}
            {search.status && (
              <span className={FILTER_CHIP}>
                status: <span className="font-medium">{search.status}</span>
                <RemoveFilter
                  label="Remove status filter"
                  onClick={() => commit({ ...current, status: null })}
                />
              </span>
            )}
            {filtersActive && (
              <button
                type="button"
                onClick={() => commit(EMPTY_SEARCH)}
                className={LINK_BUTTON}
              >
                Clear search
              </button>
            )}
          </div>
        )}

        {notice}
      </div>

      {error && (
        <div className="border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-800">
          <ResultBanner tone="error">
            {error}{" "}
            <button
              type="button"
              onClick={() => setRetryNonce((n) => n + 1)}
              className="font-medium underline hover:no-underline"
            >
              Retry
            </button>
          </ResultBanner>
        </div>
      )}

      <div className="flex-1 overflow-auto">
        {loading && shownQuery === null ? (
          <div className="flex h-full items-center justify-center py-12">
            <div
              aria-hidden="true"
              className="h-8 w-8 animate-spin rounded-full border-2 border-blue-500 border-t-transparent"
            />
          </div>
        ) : error && tasks.length === 0 ? null : tasks.length === 0 ? (
          <EmptyState
            filtered={!isEmptySearch(search)}
            idPrefix={classifyQuery(search.text).kind === "id"}
            onClear={() => commit(EMPTY_SEARCH)}
          />
        ) : (
          <div
            className={showingStale ? "opacity-60 transition-opacity" : undefined}
            aria-busy={loading}
          >
            <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
              <thead className="sticky top-0 z-10 bg-gray-50 dark:bg-gray-800">
                <tr>
                  {["Task", "Task id", "Status", "Tags", "Description", "Updated"].map(
                    (h) => (
                      <th
                        key={h}
                        scope="col"
                        aria-sort={h === "Updated" ? "descending" : undefined}
                        title={
                          h === "Updated"
                            ? "The task's last status change, most recent first."
                            : undefined
                        }
                        className="px-4 py-2 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400"
                      >
                        {h}
                        {h === "Updated" && (
                          <span aria-hidden="true" className="ml-1">
                            ↓
                          </span>
                        )}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 bg-white dark:divide-gray-700 dark:bg-gray-900">
                {tasks.map((task) => (
                  <TaskRow
                    key={task.task_id}
                    task={task}
                    activeTags={search.tags}
                    onOpen={openTask}
                    onFilterTag={addTagFilter}
                    selected={task.task_id === selectedTaskId}
                    openVerb={openVerb}
                  />
                ))}
              </tbody>
            </table>
            {nextCursor && (
              <div className="flex justify-center border-t border-gray-200 bg-white py-3 dark:border-gray-700 dark:bg-gray-800">
                <button
                  type="button"
                  onClick={loadMore}
                  disabled={loadingMore || loading}
                  className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                >
                  {loadingMore ? "Loading…" : "Load more"}
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function RemoveFilter({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="-mr-1 flex h-4 w-4 items-center justify-center rounded-full text-gray-400 hover:bg-gray-200 hover:text-gray-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:hover:bg-gray-600 dark:hover:text-gray-100"
    >
      <svg aria-hidden="true" className="h-2.5 w-2.5" viewBox="0 0 12 12" fill="none">
        <path
          d="M3 3l6 6M9 3l-6 6"
          stroke="currentColor"
          strokeWidth={1.6}
          strokeLinecap="round"
        />
      </svg>
    </button>
  );
}

function EmptyState({
  filtered,
  idPrefix,
  onClear,
}: {
  filtered: boolean;
  idPrefix: boolean;
  onClear: () => void;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center py-12 text-center text-gray-500 dark:text-gray-400">
      <p className="text-lg font-medium">
        {filtered ? "No tasks match this search" : "No tasks yet"}
      </p>
      <p className="mt-1 max-w-md text-sm">
        {filtered
          ? idPrefix
            ? "No task id in this environment starts with that. Ids are matched from their first character."
            : "Text matches names, namespaces and descriptions; name, namespace and tag filters match exactly, and every tag must be present."
          : "Tasks appear here once a build registers them with the Stardag SDK."}
      </p>
      {filtered && (
        <button
          type="button"
          onClick={onClear}
          className={`mt-3 ${LINK_BUTTON} text-sm`}
        >
          Clear search
        </button>
      )}
    </div>
  );
}

function TaskRow({
  task,
  activeTags,
  onOpen,
  onFilterTag,
  selected = false,
  openVerb = "Open",
}: {
  task: TaskSummary;
  activeTags: string[];
  onOpen: (taskId: string) => void;
  onFilterTag: (tag: string) => void;
  selected?: boolean;
  openVerb?: string;
}) {
  const tags = task.tags ?? [];
  return (
    <tr
      onClick={() => onOpen(task.task_id)}
      aria-selected={selected || undefined}
      className={`cursor-pointer align-top transition-colors ${
        selected
          ? "bg-blue-50 dark:bg-blue-950/40"
          : "hover:bg-gray-50 dark:hover:bg-gray-700/50"
      }`}
    >
      <td className="px-4 py-2.5">
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onOpen(task.task_id);
          }}
          aria-label={
            openVerb === "Open"
              ? undefined
              : `${openVerb} ${qualifiedName(
                  task.task_namespace,
                  task.task_name,
                )} ${shortTaskId(task.task_id)}`
          }
          className="rounded text-left text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
        >
          {task.task_namespace && (
            <span className="text-gray-400 dark:text-gray-500">
              {task.task_namespace}.
            </span>
          )}
          <span className="font-medium text-gray-900 hover:text-blue-700 hover:underline dark:text-gray-100 dark:hover:text-blue-400">
            {task.task_name}
          </span>
        </button>
      </td>
      <td className="whitespace-nowrap px-4 py-2.5">
        <CopyChip
          label={shortTaskId(task.task_id)}
          value={task.task_id}
          title="Task id"
        />
      </td>
      <td className="whitespace-nowrap px-4 py-2.5">
        <StatusBadge status={task.status} />
      </td>
      <td className="px-4 py-2.5">
        {tags.length === 0 ? (
          <span className="text-sm text-gray-400 dark:text-gray-500">—</span>
        ) : (
          <div className="flex max-w-xs flex-wrap gap-1">
            {tags.map((tag) =>
              activeTags.includes(tag) ? (
                <TagChip key={tag} tag={tag} />
              ) : (
                <TagChip
                  key={tag}
                  tag={tag}
                  onClick={() => onFilterTag(tag)}
                  clickLabel={`Filter by tag ${tag}`}
                />
              ),
            )}
          </div>
        )}
      </td>
      <td className="max-w-xs px-4 py-2.5">
        <p
          className="truncate text-sm text-gray-500 dark:text-gray-400"
          title={task.description ?? undefined}
        >
          {task.description || "—"}
        </p>
      </td>
      <td
        className="whitespace-nowrap px-4 py-2.5 text-sm text-gray-500 dark:text-gray-400"
        title={
          [
            task.status_at && `Status changed ${formatAbsoluteTime(task.status_at)}`,
            task.completed_at && `Completed ${formatAbsoluteTime(task.completed_at)}`,
          ]
            .filter(Boolean)
            .join("\n") || undefined
        }
      >
        {formatRelativeTime(task.status_at)}
      </td>
    </tr>
  );
}
