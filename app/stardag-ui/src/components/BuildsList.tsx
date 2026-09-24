import { useCallback, useEffect, useRef, useState } from "react";
import { fetchBuilds } from "../api/registry";
import { useBreadcrumb } from "../context/BreadcrumbContext";
import { useEnvironment } from "../context/EnvironmentContext";
import type { Build, BuildStatus } from "../types/task";
import { shortBuildId } from "../utils/ids";
import { formatAbsoluteTime, formatDuration, formatRelativeTime } from "../utils/time";
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

// `GET /builds` takes a limit (at most 500) and has no pagination or total,
// so the list shows the newest N and says when it may be cut short.
const LIMIT_OPTIONS = [50, 100, 500];

const CONTROL =
  "rounded-md border border-gray-300 px-2 py-1 text-xs text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100";

/** The environment's builds, newest first, filtered server-side. */
export function BuildsList({ onSelectBuild }: BuildsListProps) {
  const { activeEnvironment } = useEnvironment();
  const { setItems: setBreadcrumb } = useBreadcrumb();
  const [builds, setBuilds] = useState<Build[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<BuildStatus | "">("");
  const [reactiveAppInput, setReactiveAppInput] = useState("");
  const [reactiveApp, setReactiveApp] = useState("");
  const [limit, setLimit] = useState(LIMIT_OPTIONS[0]);
  const epochRef = useRef(0);

  useEffect(() => {
    setBreadcrumb([{ label: "Builds" }]);
    return () => setBreadcrumb([]);
  }, [setBreadcrumb]);

  // Debounced: the reactive-app filter is server-side.
  useEffect(() => {
    const handle = setTimeout(() => setReactiveApp(reactiveAppInput.trim()), 300);
    return () => clearTimeout(handle);
  }, [reactiveAppInput]);

  const load = useCallback(async () => {
    const epoch = ++epochRef.current;
    if (!activeEnvironment?.id) {
      setBuilds([]);
      return;
    }
    const fresh = () => epochRef.current === epoch;
    setLoading(true);
    setError(null);
    try {
      const rows = await fetchBuilds(activeEnvironment.id, {
        status: statusFilter || undefined,
        reactiveAppName: reactiveApp || undefined,
        limit,
      });
      if (fresh()) setBuilds(rows);
    } catch (err) {
      if (!fresh()) return;
      setBuilds([]);
      setError(err instanceof Error ? err.message : "Failed to load builds");
    } finally {
      if (fresh()) setLoading(false);
    }
  }, [activeEnvironment?.id, statusFilter, reactiveApp, limit]);

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

  const filtersActive = statusFilter !== "" || reactiveApp !== "";
  const clearFilters = () => {
    setStatusFilter("");
    setReactiveAppInput("");
    setReactiveApp("");
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-800">
        <label className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
          Status
          <select
            aria-label="Filter by build status"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as BuildStatus | "")}
            className={CONTROL}
          >
            <option value="">All statuses</option>
            {(Object.keys(STATUS_LABELS) as BuildStatus[]).map((status) => (
              <option key={status} value={status}>
                {STATUS_LABELS[status]}
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
        <label className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
          Show
          <select
            aria-label="How many builds to show"
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className={CONTROL}
          >
            {LIMIT_OPTIONS.map((n) => (
              <option key={n} value={n}>
                newest {n}
              </option>
            ))}
          </select>
        </label>
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
            : `${builds.length} build${builds.length === 1 ? "" : "s"}${
                builds.length === limit ? " (newest shown; there may be more)" : ""
              }`}
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
              <p className="mt-1 text-sm">Run a build with the Stardag SDK to see it here</p>
            )}
          </div>
        ) : (
          <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
            <thead className="sticky top-0 z-10 bg-gray-50 dark:bg-gray-800">
              <tr>
                {["Status", "Build", "Description", "Duration", "Created"].map((h) => (
                  <th
                    key={h}
                    scope="col"
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
        title={formatAbsoluteTime(build.created_at)}
      >
        {formatRelativeTime(build.created_at)}
      </td>
    </tr>
  );
}
