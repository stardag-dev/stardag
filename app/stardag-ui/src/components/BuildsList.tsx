import { useCallback, useEffect, useRef, useState } from "react";
import { fetchBuilds } from "../api/registry";
import { useBreadcrumb } from "../context/BreadcrumbContext";
import { useEnvironment } from "../context/EnvironmentContext";
import type { Build, BuildStatus } from "../types/task";
import { shortBuildId } from "../utils/ids";
import {
  formatAbsoluteTime,
  formatDuration,
  formatIdleThreshold,
  formatRelativeTime,
} from "../utils/time";
import { BuildStatusBadge } from "./BuildStatusBadge";
import { CopyChip } from "./ui/CopyChip";
import { ResultBanner } from "./ui/ResultBanner";

interface BuildsListProps {
  onSelectBuild: (buildId: string) => void;
}

const STATUS_LABELS: Record<BuildStatus, string> = {
  running: "Running",
  pending: "Pending",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  exit_early: "Exited early",
};

const PAGE_SIZE = 20;

// v1's thresholds. `GET /builds?idle_for_seconds=` filters server-side, so
// the total and the pages are over the idle set itself.
const IDLE_OPTIONS: { seconds: number; label: string }[] = [
  { seconds: 3600, label: "1 hour" },
  { seconds: 6 * 3600, label: "6 hours" },
  { seconds: 24 * 3600, label: "24 hours" },
  { seconds: 7 * 86400, label: "7 days" },
  { seconds: 30 * 86400, label: "30 days" },
];

// An idle filter means "still running", so the server implies RUNNING and
// refuses any other status alongside it. The controls make that pair
// unreachable rather than letting a user click into a 400.
const IDLE_COMPATIBLE_STATUSES: (BuildStatus | "")[] = ["", "running"];

const STATUS_WITH_IDLE_HINT =
  "“Idle for” finds builds that are still running, so it only combines " +
  "with Running (or All). A build that finished isn’t idle.";

const LAST_ACTIVE_EXPLAINER =
  "The build's last lifecycle change: created, resumed or finished. Task " +
  "activity does not move it, so a long-running busy build also reads as idle.";

const CONTROL =
  "rounded-md border border-gray-300 px-2 py-1 text-xs text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100";

const PAGER_BUTTON =
  "rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700";

/**
 * The environment's builds, most recently active first, filtered and paged
 * server-side. Paging is keyset (`cursor`), so a page is reached by
 * Previous/Next rather than by number; `total` gives the page count.
 */
export function BuildsList({ onSelectBuild }: BuildsListProps) {
  const { activeEnvironment } = useEnvironment();
  const { setItems: setBreadcrumb } = useBreadcrumb();
  const [builds, setBuilds] = useState<Build[]>([]);
  const [total, setTotal] = useState(0);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  // Keyset paging: the cursor each page was read with, page 1's being
  // null. "Next" pushes the last page's `next_cursor`, "Previous" pops.
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<BuildStatus | "">("");
  const [reactiveAppInput, setReactiveAppInput] = useState("");
  const [reactiveApp, setReactiveApp] = useState("");
  const [idleForSeconds, setIdleForSeconds] = useState(0);
  const epochRef = useRef(0);

  useEffect(() => {
    setBreadcrumb([{ label: "Builds" }]);
    return () => setBreadcrumb([]);
  }, [setBreadcrumb]);

  // Debounced: the reactive-app filter is server-side.
  useEffect(() => {
    const next = reactiveAppInput.trim();
    if (next === reactiveApp) return;
    const handle = setTimeout(() => {
      setReactiveApp(next);
      setCursors([null]);
    }, 300);
    return () => clearTimeout(handle);
  }, [reactiveAppInput, reactiveApp]);

  // A cursor belongs to one environment's listing: back to page 1 on a
  // switch (adjusted during render, so no fetch runs with a foreign cursor).
  const environmentId = activeEnvironment?.id ?? null;
  const [pagedEnvironmentId, setPagedEnvironmentId] = useState(environmentId);
  if (environmentId !== pagedEnvironmentId) {
    setPagedEnvironmentId(environmentId);
    setCursors([null]);
  }

  const page = cursors.length;
  const cursor = cursors[cursors.length - 1];

  const load = useCallback(async () => {
    const epoch = ++epochRef.current;
    if (!activeEnvironment?.id) {
      setBuilds([]);
      setTotal(0);
      setNextCursor(null);
      return;
    }
    const fresh = () => epochRef.current === epoch;
    setLoading(true);
    setError(null);
    try {
      const response = await fetchBuilds(activeEnvironment.id, {
        status: statusFilter || undefined,
        reactiveAppName: reactiveApp || undefined,
        idleForSeconds: idleForSeconds || undefined,
        limit: PAGE_SIZE,
        cursor: cursor ?? undefined,
      });
      if (!fresh()) return;
      setBuilds(response.builds);
      setTotal(response.total);
      setNextCursor(response.next_cursor);
    } catch (err) {
      if (!fresh()) return;
      setBuilds([]);
      setTotal(0);
      setNextCursor(null);
      setError(err instanceof Error ? err.message : "Failed to load builds");
    } finally {
      if (fresh()) setLoading(false);
    }
  }, [activeEnvironment?.id, statusFilter, reactiveApp, idleForSeconds, cursor]);

  useEffect(() => {
    load();
  }, [load]);

  if (!activeEnvironment) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500 dark:text-gray-400">
        <p>Select an environment to view builds</p>
      </div>
    );
  }

  const idleFilterActive = idleForSeconds > 0;
  const idleBlockedByStatus = !IDLE_COMPATIBLE_STATUSES.includes(statusFilter);
  const filtersActive = statusFilter !== "" || reactiveApp !== "" || idleFilterActive;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const clearFilters = () => {
    setStatusFilter("");
    setReactiveAppInput("");
    setReactiveApp("");
    setIdleForSeconds(0);
    setCursors([null]);
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-800">
        <label className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
          Status
          <select
            aria-label="Filter by build status"
            value={statusFilter}
            onChange={(e) => {
              setStatusFilter(e.target.value as BuildStatus | "");
              setCursors([null]);
            }}
            className={CONTROL}
          >
            <option value="">All statuses</option>
            {(Object.keys(STATUS_LABELS) as BuildStatus[]).map((status) => {
              // Disabled rather than hidden, so the constraint stays legible.
              const blocked =
                idleFilterActive && !IDLE_COMPATIBLE_STATUSES.includes(status);
              return (
                <option
                  key={status}
                  value={status}
                  disabled={blocked}
                  title={blocked ? STATUS_WITH_IDLE_HINT : undefined}
                >
                  {STATUS_LABELS[status]}
                  {blocked ? " — not idle-filterable" : ""}
                </option>
              );
            })}
          </select>
        </label>
        <label
          className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400"
          title={idleBlockedByStatus ? STATUS_WITH_IDLE_HINT : LAST_ACTIVE_EXPLAINER}
        >
          Idle for
          <select
            aria-label="Filter by time since last activity"
            value={idleForSeconds}
            disabled={idleBlockedByStatus}
            onChange={(e) => {
              setIdleForSeconds(Number(e.target.value));
              setCursors([null]);
            }}
            className={`${CONTROL} disabled:cursor-not-allowed disabled:opacity-50`}
          >
            <option value={0}>Any</option>
            {IDLE_OPTIONS.map((option) => (
              <option key={option.seconds} value={option.seconds}>
                ≥ {option.label}
              </option>
            ))}
          </select>
        </label>
        <input
          type="text"
          aria-label="Filter by reactive app"
          placeholder="Reactive app…"
          value={reactiveAppInput}
          onChange={(e) => setReactiveAppInput(e.target.value)}
          className={`w-36 ${CONTROL}`}
        />
        {filtersActive && (
          <button
            onClick={clearFilters}
            className="rounded-md px-2 py-1 text-xs font-medium text-blue-600 hover:bg-blue-50 dark:text-blue-400 dark:hover:bg-blue-900/30"
          >
            Clear filters
          </button>
        )}
        <span className="ml-auto text-xs text-gray-500 dark:text-gray-400">
          {loading
            ? "Loading…"
            : `${total} build${total === 1 ? "" : "s"}` +
              (idleFilterActive
                ? ` running, idle ≥ ${formatIdleThreshold(idleForSeconds)}`
                : "")}
        </span>
      </div>

      {error && (
        <div className="border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-800">
          <ResultBanner tone="error">
            {error}{" "}
            <button onClick={load} className="font-medium underline hover:no-underline">
              Retry
            </button>
          </ResultBanner>
        </div>
      )}

      <div className="flex-1 overflow-auto">
        {loading ? (
          <div className="flex h-full items-center justify-center py-12">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
          </div>
        ) : builds.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center py-12 text-gray-500 dark:text-gray-400">
            <p className="text-lg font-medium">
              {filtersActive ? "No builds match these filters" : "No builds yet"}
            </p>
            {!filtersActive && (
              <p className="mt-1 text-sm">
                Run a build with the Stardag SDK to see it here
              </p>
            )}
          </div>
        ) : (
          <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
            <thead className="sticky top-0 z-10 bg-gray-50 dark:bg-gray-800">
              <tr>
                {[
                  "Status",
                  "Build",
                  "Description",
                  "Duration",
                  "Last active",
                  "Created",
                ].map((h) => (
                  <th
                    key={h}
                    scope="col"
                    title={h === "Last active" ? LAST_ACTIVE_EXPLAINER : undefined}
                    className="px-4 py-2 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400"
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 bg-white dark:divide-gray-700 dark:bg-gray-900">
              {builds.map((build) => (
                <BuildRow
                  key={build.id}
                  build={build}
                  onOpen={onSelectBuild}
                  onFilterReactiveApp={setReactiveAppInput}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-between border-t border-gray-200 bg-white px-6 py-3 dark:border-gray-700 dark:bg-gray-800">
          <button
            onClick={() => setCursors((c) => (c.length > 1 ? c.slice(0, -1) : c))}
            disabled={page === 1 || loading}
            className={PAGER_BUTTON}
          >
            Previous
          </button>
          <span className="text-sm text-gray-500 dark:text-gray-400">
            Page {page} of {totalPages}
          </span>
          <button
            onClick={() => nextCursor && setCursors((c) => [...c, nextCursor])}
            disabled={!nextCursor || loading}
            className={PAGER_BUTTON}
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}

function BuildRow({
  build,
  onOpen,
  onFilterReactiveApp,
}: {
  build: Build;
  onOpen: (buildId: string) => void;
  onFilterReactiveApp: (appName: string) => void;
}) {
  return (
    <tr
      onClick={() => onOpen(build.id)}
      className="cursor-pointer transition-colors hover:bg-gray-50 dark:hover:bg-gray-700/50"
    >
      <td className="px-4 py-3">
        <BuildStatusBadge status={build.status} isResumed={build.is_resumed} />
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onOpen(build.id);
            }}
            className="rounded text-left font-medium text-gray-900 hover:text-blue-700 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-gray-100 dark:hover:text-blue-400"
          >
            {build.name}
          </button>
          <CopyChip label={shortBuildId(build.id)} value={build.id} title="Build id" />
          {build.reactive_app_name && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onFilterReactiveApp(build.reactive_app_name as string);
              }}
              title={`Reactive build, ticked by the app “${build.reactive_app_name}” — click to show only this app's builds`}
              className="rounded bg-purple-100 px-1.5 py-0.5 text-[11px] text-purple-700 hover:bg-purple-200 dark:bg-purple-900/40 dark:text-purple-300 dark:hover:bg-purple-900/70"
            >
              reactive app: {build.reactive_app_name}
            </button>
          )}
        </div>
      </td>
      <td className="max-w-xs px-4 py-3">
        <p className="truncate text-sm text-gray-500 dark:text-gray-400">
          {build.description || "—"}
        </p>
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
        {formatDuration(build.started_at, build.completed_at)}
      </td>
      <td
        className="whitespace-nowrap px-4 py-3 text-sm text-gray-500 dark:text-gray-400"
        title={formatAbsoluteTime(build.last_active_at)}
      >
        {formatRelativeTime(build.last_active_at)}
      </td>
      <td
        className="whitespace-nowrap px-4 py-3 text-sm text-gray-500 dark:text-gray-400"
        title={formatAbsoluteTime(build.created_at)}
      >
        {formatRelativeTime(build.created_at)}
      </td>
    </tr>
  );
}
