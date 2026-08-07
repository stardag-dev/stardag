import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchBuilds } from "../api/tasks";
import { useBreadcrumb } from "../context/BreadcrumbContext";
import { useEnvironment } from "../context/EnvironmentContext";
import { useRowSelection } from "../hooks/useRowSelection";
import type { Build, BuildStatus, BulkCancelBuildsResponse } from "../types/task";
import {
  formatAbsoluteTime,
  formatDuration,
  formatIdleThreshold,
  formatRelativeTime,
  secondsSince,
} from "../utils/time";
import { BuildStatusBadge } from "./BuildStatusBadge";
import { BulkCancelDialog } from "./BulkCancelDialog";
import { BulkActionBar } from "./ui/BulkActionBar";
import { Checkbox } from "./ui/Checkbox";
import { ResultBanner } from "./ui/ResultBanner";

interface BuildsListProps {
  onSelectBuild: (buildId: string) => void;
}

const PAGE_SIZE = 20;

// Every filter here is server-side. The "idle for" one matters most:
// `GET /builds?idle_for_seconds=` and `POST /builds/bulk-cancel` share one
// SQL predicate on the API side, so the set this table shows and the set
// "Clean up idle builds" acts on are identical by construction rather than
// by two implementations happening to agree. `total` is an exact COUNT
// over that predicate and pagination is unbounded — nothing is dropped, so
// there is nothing to caveat.
const IDLE_OPTIONS: { seconds: number; label: string }[] = [
  { seconds: 3600, label: "1 hour" },
  { seconds: 6 * 3600, label: "6 hours" },
  { seconds: 24 * 3600, label: "24 hours" },
  { seconds: 7 * 86400, label: "7 days" },
  { seconds: 30 * 86400, label: "30 days" },
];

const STATUS_OPTIONS: BuildStatus[] = [
  "running",
  "pending",
  "completed",
  "failed",
  "cancelled",
  "exit_early",
];

const STATUS_LABELS: Record<BuildStatus, string> = {
  running: "Running",
  pending: "Pending",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  exit_early: "Exited early",
};

// A running build with no activity for longer than this is flagged in the
// table even with no idle filter set — the at-a-glance version of "which
// builds are stale?".
const STALE_HINT_SECONDS = 24 * 3600;

const LAST_ACTIVITY_EXPLAINER =
  "Newest of the build's whole event stream (task events included), its " +
  "last lifecycle transition, and any pending scheduler wake-up. This is " +
  "the signal stale-build cleanup measures idleness against — not the " +
  "build's last lifecycle change, which stays put while tasks are running.";

// With an idle filter the API returns stalest-first, but it sorts on the
// index-backed `last_active_at` proxy rather than on the composed
// `last_activity_at` this column renders. The two nearly always agree;
// where they don't, a row can sit slightly out of order. Said plainly here
// rather than "fixed" by re-sorting the page, which would only impose a
// local ordering on one page of a server-side result set.
const STALEST_FIRST_CAVEAT =
  "Stalest first. The server orders on the build's last lifecycle change, " +
  "an index-backed proxy for last activity, so this column runs close to — " +
  "but not strictly — oldest-first.";

// Statuses other than "running" cannot be combined with an idle filter:
// only RUNNING has a SQL predicate, so the API answers 422 rather than
// serving an approximate `total` that looks exact. The controls below make
// that combination unreachable instead of letting a user click into it.
const IDLE_COMPATIBLE_STATUSES: (BuildStatus | "")[] = ["", "running"];

const STATUS_WITH_IDLE_HINT =
  "An idle filter can only be combined with All statuses or Running. The " +
  "other statuses are derived by scanning a bounded window of builds, so " +
  "pairing them with idleness would silently drop the oldest matches — " +
  "exactly the builds a staleness query is looking for.";

export function BuildsList({ onSelectBuild }: BuildsListProps) {
  const { activeEnvironment, activeWorkspaceRole } = useEnvironment();
  const { setItems: setBreadcrumb } = useBreadcrumb();
  // Bulk cancel is destructive and the API gates it to workspace admins on
  // the JWT path, so mirror that gate here rather than offering a control
  // that can only 403 — same rule as the concurrency-limit admin surface.
  const isAdmin = activeWorkspaceRole === "owner" || activeWorkspaceRole === "admin";

  const [pageBuilds, setPageBuilds] = useState<Build[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);

  // Filters — all visible in the toolbar, all clearable.
  const [statusFilter, setStatusFilter] = useState<BuildStatus | "">("");
  const [idleForSeconds, setIdleForSeconds] = useState(0);
  const [reactiveAppInput, setReactiveAppInput] = useState("");
  const [reactiveApp, setReactiveApp] = useState("");

  // Bulk cleanup
  const [dialogMode, setDialogMode] = useState<"selection" | "idle" | null>(null);
  const [result, setResult] = useState<BulkCancelBuildsResponse | null>(null);

  const idleFilterActive = idleForSeconds > 0;
  // The API rejects `idle_for_seconds` alongside any status but "running"
  // (422). Both controls are constrained so that pair cannot be built:
  // incompatible statuses are disabled while an idle filter is set, and
  // the idle select is disabled while such a status is selected. Nothing
  // is silently rewritten behind the user — the block is visible and
  // explained on both sides.
  const idleBlockedByStatus = !IDLE_COMPATIBLE_STATUSES.includes(statusFilter);

  useEffect(() => {
    setBreadcrumb([{ label: "Builds" }]);
    return () => setBreadcrumb([]);
  }, [setBreadcrumb]);

  // Debounce the reactive-app box: it is a server-side filter, so a
  // refetch per keystroke would be wasteful.
  useEffect(() => {
    const next = reactiveAppInput.trim();
    const handle = setTimeout(() => {
      setReactiveApp(next);
      setPage(1);
    }, 300);
    return () => clearTimeout(handle);
  }, [reactiveAppInput]);

  // Track the env id of the most recent fetch we initiated. Used below
  // to detect "env just changed" inside loadBuilds — see comment there.
  const lastFetchedEnvIdRef = useRef<string | null>(null);
  // Stale-response guard: a slow response for a previous environment (or
  // a superseded filter) must not overwrite the current state.
  const fetchEpochRef = useRef(0);

  const loadBuilds = useCallback(async () => {
    if (!activeEnvironment?.id) {
      // Don't flip loading=false here — the parent gate handles the
      // no-environment case, and clearing loading would briefly leak
      // the empty-state UI on env transitions.
      setPageBuilds([]);
      setTotal(0);
      return;
    }

    // If the env just changed and we're not already on page 1, reset
    // the page and bail. The page-state update re-creates this callback
    // on the next render, which will run with page=1 against the new
    // env. Doing this here (instead of in a separate ``useEffect`` that
    // runs *alongside* the fetch effect) prevents a wasted fetch with
    // the previous page that would briefly flash an empty result.
    if (
      lastFetchedEnvIdRef.current !== null &&
      lastFetchedEnvIdRef.current !== activeEnvironment.id &&
      page !== 1
    ) {
      setPage(1);
      return;
    }
    lastFetchedEnvIdRef.current = activeEnvironment.id;

    const epoch = ++fetchEpochRef.current;
    const fresh = () => fetchEpochRef.current === epoch;
    setLoading(true);
    setError(null);
    try {
      const response = await fetchBuilds({
        page,
        page_size: PAGE_SIZE,
        environment_id: activeEnvironment.id,
        status: statusFilter || undefined,
        reactive_app_name: reactiveApp || undefined,
        // Filtered, counted, paginated and ordered by the server, using
        // the same predicate the cleanup sweep runs.
        idle_for_seconds: idleForSeconds || undefined,
      });
      if (!fresh()) return;
      setPageBuilds(response.builds);
      setTotal(response.total);
    } catch (err) {
      if (!fresh()) return;
      setPageBuilds([]);
      setTotal(0);
      setError(err instanceof Error ? err.message : "Failed to load builds");
    } finally {
      if (fresh()) setLoading(false);
    }
  }, [activeEnvironment?.id, page, idleForSeconds, statusFilter, reactiveApp]);

  useEffect(() => {
    loadBuilds();
  }, [loadBuilds]);

  const totalPages = Math.ceil(total / PAGE_SIZE);

  const visibleIds = useMemo(() => pageBuilds.map((build) => build.id), [pageBuilds]);
  // Selection is scoped to the visible page and cleared whenever the
  // visible set changes. See useRowSelection for why.
  const selectionResetKey = `${
    activeEnvironment?.id ?? ""
  }|${statusFilter}|${idleForSeconds}|${reactiveApp}|${page}`;
  const {
    selectedIds,
    selectedCount,
    isSelected,
    toggle,
    toggleAllVisible,
    clear: clearSelection,
    allVisibleSelected,
    someVisibleSelected,
  } = useRowSelection(visibleIds, selectionResetKey);

  const buildNames = useMemo(
    () => Object.fromEntries(pageBuilds.map((build) => [build.id, build.name])),
    [pageBuilds],
  );

  const handleApplied = useCallback(
    (applied: BulkCancelBuildsResponse) => {
      setDialogMode(null);
      setResult(applied);
      clearSelection();
      loadBuilds();
    },
    [clearSelection, loadBuilds],
  );

  const handleStatusChange = useCallback((value: BuildStatus | "") => {
    setStatusFilter(value);
    setPage(1);
  }, []);

  const handleIdleChange = useCallback((seconds: number) => {
    setIdleForSeconds(seconds);
    setPage(1);
  }, []);

  const handleFilterReactiveApp = useCallback((appName: string) => {
    setReactiveAppInput(appName);
  }, []);

  const clearFilters = useCallback(() => {
    setStatusFilter("");
    setIdleForSeconds(0);
    setReactiveAppInput("");
    setReactiveApp("");
    setPage(1);
  }, []);

  const filtersActive = statusFilter !== "" || idleFilterActive || reactiveApp !== "";

  // Names offered by the reactive-app box, taken from what is loaded.
  const knownReactiveApps = useMemo(
    () =>
      Array.from(
        new Set(
          pageBuilds
            .map((build) => build.reactive_app_name)
            .filter((name): name is string => !!name),
        ),
      ).sort(),
    [pageBuilds],
  );

  if (!activeEnvironment) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500 dark:text-gray-400">
        <p>Select an environment to view builds</p>
      </div>
    );
  }

  const skippedCount = Object.keys(result?.skipped ?? {}).length;

  return (
    <div className="flex h-full flex-col">
      {/* Filters + cleanup entry point */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-800">
        <label className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
          Status
          <select
            aria-label="Filter by build status"
            value={statusFilter}
            onChange={(e) => handleStatusChange(e.target.value as BuildStatus | "")}
            className="rounded-md border border-gray-300 px-2 py-1 text-xs text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100"
          >
            <option value="">All statuses</option>
            {STATUS_OPTIONS.map((status) => {
              // Disabled rather than hidden while an idle filter is set:
              // the option stays visible so the constraint is legible, and
              // the pair the API rejects can never be submitted.
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
                  {blocked ? " — n/a with an idle filter" : ""}
                </option>
              );
            })}
          </select>
        </label>

        <label
          className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400"
          title={idleBlockedByStatus ? STATUS_WITH_IDLE_HINT : undefined}
        >
          Idle for
          <select
            aria-label="Filter by time since last activity"
            value={idleForSeconds}
            disabled={idleBlockedByStatus}
            onChange={(e) => handleIdleChange(Number(e.target.value))}
            className="rounded-md border border-gray-300 px-2 py-1 text-xs text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100"
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
          list="builds-reactive-apps"
          aria-label="Filter by reactive app"
          placeholder="Reactive app…"
          value={reactiveAppInput}
          onChange={(e) => setReactiveAppInput(e.target.value)}
          className="w-36 rounded-md border border-gray-300 px-2 py-1 text-xs text-gray-900 placeholder-gray-500 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 dark:placeholder-gray-400"
        />
        <datalist id="builds-reactive-apps">
          {knownReactiveApps.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>

        {filtersActive && (
          <button
            onClick={clearFilters}
            className="rounded-md px-2 py-1 text-xs font-medium text-blue-600 hover:bg-blue-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-400 dark:hover:bg-blue-900/30"
          >
            Clear filters
          </button>
        )}

        <div className="ml-auto flex items-center gap-3">
          <span
            className="text-xs text-gray-500 dark:text-gray-400"
            title={idleFilterActive ? STALEST_FIRST_CAVEAT : undefined}
          >
            {loading
              ? "Loading…"
              : `${total} build${total === 1 ? "" : "s"}` +
                (idleFilterActive
                  ? ` idle ≥ ${formatIdleThreshold(idleForSeconds)}, stalest first`
                  : "")}
          </span>
          {isAdmin ? (
            <button
              onClick={() => setDialogMode("idle")}
              disabled={!idleFilterActive}
              title={
                idleFilterActive
                  ? `Cancel every running build in this environment idle for at least ${formatIdleThreshold(
                      idleForSeconds,
                    )}`
                  : "Pick an “Idle for” filter first — the sweep uses that threshold, server-side, across the whole environment"
              }
              className="rounded-md border border-gray-300 px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              Clean up idle builds…
            </button>
          ) : (
            <span className="text-xs text-gray-400 dark:text-gray-500">
              Bulk cleanup requires the workspace admin role
            </span>
          )}
        </div>
      </div>

      {/* Outcome of the last cleanup, and load errors */}
      {(result || error) && (
        <div className="space-y-2 border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-800">
          {result && (
            <ResultBanner
              tone={result.build_count > 0 ? "success" : "info"}
              onDismiss={() => setResult(null)}
            >
              {result.build_count === 0
                ? "No running build matched — nothing was cancelled."
                : `Cancelled ${result.build_count} build${
                    result.build_count === 1 ? "" : "s"
                  } and released ${result.task_count} task claim${
                    result.task_count === 1 ? "" : "s"
                  }.`}
              {skippedCount > 0 &&
                ` ${skippedCount} build${
                  skippedCount === 1 ? " was" : "s were"
                } skipped.`}
              {result.truncated && " More builds still match — run the cleanup again."}
            </ResultBanner>
          )}
          {error && (
            <ResultBanner tone="error">
              {error}{" "}
              <button
                onClick={loadBuilds}
                className="font-medium underline hover:no-underline"
              >
                Retry
              </button>
            </ResultBanner>
          )}
        </div>
      )}

      {/* Table */}
      <div className="flex-1 overflow-auto">
        {loading ? (
          <div className="flex h-full items-center justify-center py-12">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
          </div>
        ) : pageBuilds.length === 0 ? (
          <EmptyState filtersActive={filtersActive} onClearFilters={clearFilters} />
        ) : (
          <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
            <thead className="sticky top-0 z-10 bg-gray-50 dark:bg-gray-800">
              <tr>
                {isAdmin && (
                  <th scope="col" className="w-10 px-4 py-2">
                    <Checkbox
                      checked={allVisibleSelected}
                      indeterminate={someVisibleSelected}
                      onChange={toggleAllVisible}
                      label="Select all builds on this page"
                    />
                  </th>
                )}
                <HeaderCell>Status</HeaderCell>
                <HeaderCell>Build</HeaderCell>
                <HeaderCell>Description</HeaderCell>
                <HeaderCell>Duration</HeaderCell>
                <HeaderCell
                  title={
                    idleFilterActive
                      ? `${LAST_ACTIVITY_EXPLAINER} ${STALEST_FIRST_CAVEAT}`
                      : LAST_ACTIVITY_EXPLAINER
                  }
                >
                  Last activity
                </HeaderCell>
                <HeaderCell>Created</HeaderCell>
                <th scope="col" className="w-8" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 bg-white dark:divide-gray-700 dark:bg-gray-900">
              {pageBuilds.map((build) => (
                <BuildRow
                  key={build.id}
                  build={build}
                  selectable={isAdmin}
                  selected={isSelected(build.id)}
                  onToggleSelect={toggle}
                  onOpen={onSelectBuild}
                  onFilterReactiveApp={handleFilterReactiveApp}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      <BulkActionBar
        count={selectedCount}
        noun="build"
        onClear={clearSelection}
        note="Applies to this page only — the selection clears when you change page or filters."
      >
        <button
          onClick={() => setDialogMode("selection")}
          className="rounded-md bg-red-600 px-3 py-1 text-sm font-medium text-white hover:bg-red-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-blue-950"
        >
          Cancel builds…
        </button>
      </BulkActionBar>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between border-t border-gray-200 bg-white px-6 py-3 dark:border-gray-700 dark:bg-gray-800">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page === 1}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            Previous
          </button>
          <span className="text-sm text-gray-500 dark:text-gray-400">
            Page {page} of {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page === totalPages}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            Next
          </button>
        </div>
      )}

      {dialogMode && activeEnvironment && (
        <BulkCancelDialog
          environmentId={activeEnvironment.id}
          mode={dialogMode}
          buildIds={dialogMode === "selection" ? selectedIds : undefined}
          buildNames={dialogMode === "selection" ? buildNames : undefined}
          idleForSeconds={dialogMode === "idle" ? idleForSeconds : undefined}
          reactiveAppName={dialogMode === "idle" ? reactiveApp || null : null}
          onClose={() => setDialogMode(null)}
          onApplied={handleApplied}
        />
      )}
    </div>
  );
}

function HeaderCell({
  children,
  title,
}: {
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <th
      scope="col"
      title={title}
      className="px-4 py-2 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400"
    >
      {children}
      {title && <span aria-hidden="true"> ⓘ</span>}
    </th>
  );
}

function EmptyState({
  filtersActive,
  onClearFilters,
}: {
  filtersActive: boolean;
  onClearFilters: () => void;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center py-12 text-gray-500 dark:text-gray-400">
      <svg
        className="mb-4 h-16 w-16"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={1.5}
          d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"
        />
      </svg>
      {filtersActive ? (
        <>
          <p className="text-lg font-medium">No builds match these filters</p>
          <button
            onClick={onClearFilters}
            className="mt-3 rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            Clear filters
          </button>
        </>
      ) : (
        <>
          <p className="text-lg font-medium">No builds yet</p>
          <p className="mt-1 text-sm">
            Run a build with the Stardag SDK to see it here
          </p>
        </>
      )}
    </div>
  );
}

interface BuildRowProps {
  build: Build;
  selectable: boolean;
  selected: boolean;
  onToggleSelect: (buildId: string, checked: boolean) => void;
  onOpen: (buildId: string) => void;
  onFilterReactiveApp: (appName: string) => void;
}

function BuildRow({
  build,
  selectable,
  selected,
  onToggleSelect,
  onOpen,
  onFilterReactiveApp,
}: BuildRowProps) {
  const idleSeconds = secondsSince(build.last_activity_at);
  const stale =
    build.status === "running" &&
    idleSeconds !== null &&
    idleSeconds >= STALE_HINT_SECONDS;
  const activityTitle = build.last_activity_at
    ? `Last activity ${formatAbsoluteTime(build.last_activity_at)}${
        stale ? " — running, but nothing has happened for over a day" : ""
      }`
    : "No activity recorded for this build";

  return (
    <tr
      // Whole-row click is a mouse convenience. Keyboard users reach the
      // build through the real button in the Build cell, which is the
      // focusable, accessibly-named way in — the row itself is not
      // focusable and carries no interactive role.
      onClick={() => onOpen(build.id)}
      className={`cursor-pointer transition-colors ${
        selected
          ? "bg-blue-50 dark:bg-blue-950/40"
          : "hover:bg-gray-50 dark:hover:bg-gray-700/50"
      }`}
    >
      {selectable && (
        <td
          className="w-10 px-4 py-3"
          // Selecting a row must never navigate away from the list, so the
          // whole cell swallows the click rather than only the input.
          onClick={(e) => e.stopPropagation()}
        >
          <Checkbox
            checked={selected}
            onChange={(checked) => onToggleSelect(build.id, checked)}
            label={`Select build ${build.name}`}
          />
        </td>
      )}
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
          {build.commit_hash && (
            <span
              title={build.commit_hash}
              className="rounded bg-gray-100 px-1.5 py-0.5 font-mono text-xs text-gray-600 dark:bg-gray-700 dark:text-gray-400"
            >
              {build.commit_hash.slice(0, 7)}
            </span>
          )}
          {build.reactive_app_name && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onFilterReactiveApp(build.reactive_app_name as string);
              }}
              title={`Reactive build — filter by app “${build.reactive_app_name}”`}
              className="rounded bg-purple-100 px-1.5 py-0.5 text-xs text-purple-700 hover:bg-purple-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-purple-500 dark:bg-purple-900/40 dark:text-purple-300 dark:hover:bg-purple-900/70"
            >
              {build.reactive_app_name}
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
      <td className="whitespace-nowrap px-4 py-3 text-sm">
        <span
          title={activityTitle}
          className={
            stale
              ? "font-medium text-amber-600 dark:text-amber-400"
              : "text-gray-500 dark:text-gray-400"
          }
        >
          {formatRelativeTime(build.last_activity_at)}
        </span>
      </td>
      <td
        className="whitespace-nowrap px-4 py-3 text-sm text-gray-500 dark:text-gray-400"
        title={formatAbsoluteTime(build.created_at)}
      >
        {formatRelativeTime(build.created_at)}
      </td>
      <td className="px-2 py-3">
        <svg
          aria-hidden="true"
          className="h-5 w-5 text-gray-400"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M9 5l7 7-7 7"
          />
        </svg>
      </td>
    </tr>
  );
}
