import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { cancelTask, fetchTasks } from "../api/tasks";
import { useEnvironment } from "../context/EnvironmentContext";
import type { Task, TaskStatus } from "../types/task";
import { CLAIM_HOLDING_STATUSES } from "../utils/claims";
import { formatAbsoluteTime, formatDuration } from "../utils/time";
import { useRowSelection } from "../hooks/useRowSelection";
import { StatusBadge } from "./StatusBadge";
import { BulkActionBar } from "./ui/BulkActionBar";
import { Checkbox } from "./ui/Checkbox";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { ResultBanner } from "./ui/ResultBanner";

const PAGE_SIZE = 50;

// Staleness cuts. The API takes an absolute ISO instant rather than a
// duration (a duration would move the cutoff on every page request), so
// these are converted at fetch time and the resulting instant is what the
// request — and this page of results — is pinned to.
const AGE_OPTIONS: { label: string; seconds: number }[] = [
  { label: "Any age", seconds: 0 },
  { label: "Older than 15m", seconds: 15 * 60 },
  { label: "Older than 1h", seconds: 3600 },
  { label: "Older than 6h", seconds: 6 * 3600 },
  { label: "Older than 24h", seconds: 24 * 3600 },
  { label: "Older than 7d", seconds: 7 * 24 * 3600 },
];

/** Per-task result of a bulk release; partial failure is the normal case. */
interface ReleaseOutcome {
  task: Task;
  error: string | null;
}

function EyeIcon() {
  return (
    <svg
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"
      />
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"
      />
    </svg>
  );
}

interface ClaimTriageProps {
  environmentId: string;
  selectedTaskId: string | null;
  onSelectTask: (task: Task) => void;
  onNavigateToBuild?: (buildId: string) => void;
}

/**
 * "Which tasks are holding an execution claim, and who owns them?"
 *
 * A separate mode rather than another filter chip on the generic task
 * search, for a reason that is not cosmetic: `/tasks/search` reports a
 * task's status but neither *when* it entered it nor *which build* put it
 * there, and it evaluates a status filter in Python over the whole
 * unpaginated match set. `/tasks?status=…&status_older_than=…` carries
 * `latest_status_at` and `latest_status_build_id`, filters in SQL, and
 * orders oldest-claim-first — which is the triage order. The generic
 * search is untouched.
 */
export function ClaimTriage({
  environmentId,
  selectedTaskId,
  onSelectTask,
  onNavigateToBuild,
}: ClaimTriageProps) {
  const { activeWorkspaceRole } = useEnvironment();
  const isAdmin = activeWorkspaceRole === "owner" || activeWorkspaceRole === "admin";

  const [statuses, setStatuses] = useState<TaskStatus[]>(CLAIM_HOLDING_STATUSES);
  const [ageSeconds, setAgeSeconds] = useState(0);
  const [page, setPage] = useState(1);

  const [tasks, setTasks] = useState<Task[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [releasing, setReleasing] = useState(false);
  const [releaseError, setReleaseError] = useState<string | null>(null);
  const [outcomes, setOutcomes] = useState<ReleaseOutcome[] | null>(null);

  // Stale-response guard: a slow response from a previous environment or
  // filter must not overwrite the current one's results.
  const epochRef = useRef(0);

  const statusKey = statuses.join(",");

  useEffect(() => {
    if (!environmentId) return;
    const epoch = ++epochRef.current;
    const fresh = () => epochRef.current === epoch;
    setLoading(true);
    setError(null);
    fetchTasks({
      environment_id: environmentId,
      page,
      page_size: PAGE_SIZE,
      status: statuses.length > 0 ? statuses : undefined,
      status_older_than:
        ageSeconds > 0
          ? new Date(Date.now() - ageSeconds * 1000).toISOString()
          : undefined,
    })
      .then((response) => {
        if (!fresh()) return;
        setTasks(response.tasks);
        setTotal(response.total);
      })
      .catch((err: unknown) => {
        if (!fresh()) return;
        setError(err instanceof Error ? err.message : "Failed to load tasks");
        setTasks([]);
        setTotal(0);
      })
      .finally(() => {
        if (fresh()) setLoading(false);
      });
    // `statuses` is covered by the derived `statusKey`; listing the array
    // itself would refetch on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [environmentId, page, statusKey, ageSeconds, reloadNonce]);

  // Reset to page 1 when the filters change under the user.
  useEffect(() => {
    setPage(1);
  }, [environmentId, statusKey, ageSeconds]);

  // Only a task whose owning build is known can be acted on — the cancel
  // endpoint is addressed to a build. Rows without one stay visible (they
  // are still claim holders worth seeing) but are not selectable.
  const actionableTasks = useMemo(
    () => tasks.filter((task) => Boolean(task.latest_status_build_id)),
    [tasks],
  );
  const selectableIds = useMemo(
    () => actionableTasks.map((task) => task.task_id),
    [actionableTasks],
  );
  const {
    selectedIds,
    selectedCount,
    isSelected,
    toggle,
    toggleAllVisible,
    clear,
    allVisibleSelected,
    someVisibleSelected,
  } = useRowSelection(
    selectableIds,
    `${environmentId}|${page}|${statusKey}|${ageSeconds}`,
  );

  const selectedTasks = useMemo(
    () => actionableTasks.filter((task) => selectedIds.includes(task.task_id)),
    [actionableTasks, selectedIds],
  );

  const handleRelease = useCallback(async () => {
    setReleasing(true);
    setReleaseError(null);
    // Each task is cancelled under *its own* owning build. A single
    // failure is expected rather than exceptional — a task may well have
    // completed between listing and acting — so every result is kept and
    // reported per task instead of collapsing into one error.
    const results = await Promise.allSettled(
      selectedTasks.map((task) =>
        cancelTask(task.latest_status_build_id!, task.task_id, environmentId),
      ),
    );
    const collected: ReleaseOutcome[] = selectedTasks.map((task, i) => {
      const result = results[i];
      if (result.status === "fulfilled") return { task, error: null };
      const reason: unknown = result.reason;
      return {
        task,
        error: reason instanceof Error ? reason.message : "Failed to release claim",
      };
    });
    setOutcomes(collected);
    setReleasing(false);
    setConfirmOpen(false);
    clear();
    setReloadNonce((n) => n + 1);
  }, [selectedTasks, environmentId, clear]);

  const totalPages = Math.ceil(total / PAGE_SIZE);
  const filtersActive = statuses.length !== 2 || ageSeconds > 0;
  const failedCount = outcomes?.filter((o) => o.error).length ?? 0;
  const releasedCount = (outcomes?.length ?? 0) - failedCount;

  const toggleStatus = (status: TaskStatus, checked: boolean) => {
    setStatuses((prev) =>
      checked ? [...prev, status] : prev.filter((s) => s !== status),
    );
  };

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar */}
      <div className="border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-800">
        <p className="text-xs text-gray-500 dark:text-gray-400">
          A task left running or suspended holds its execution claim environment-wide:
          every build that needs it waits until something releases it. Longest-held
          first.
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2">
          <Checkbox
            checked={statuses.includes("running")}
            onChange={(checked) => toggleStatus("running", checked)}
            label="Running"
            labelHidden={false}
          />
          <Checkbox
            checked={statuses.includes("suspended")}
            onChange={(checked) => toggleStatus("suspended", checked)}
            label="Suspended"
            labelHidden={false}
          />
          <label className="flex items-center gap-1.5 text-sm text-gray-700 dark:text-gray-300">
            <span className="sr-only">Held for at least</span>
            <select
              value={ageSeconds}
              onChange={(e) => setAgeSeconds(Number(e.target.value))}
              aria-label="Held for at least"
              className="rounded-md border border-gray-300 bg-white px-2 py-1 text-sm text-gray-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200"
            >
              {AGE_OPTIONS.map((option) => (
                <option key={option.seconds} value={option.seconds}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={() => setReloadNonce((n) => n + 1)}
            className="rounded-md border border-gray-300 px-2 py-1 text-sm text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            Refresh
          </button>
          <span className="ml-auto text-xs text-gray-500 dark:text-gray-400">
            {loading ? "Loading…" : `${total} claim${total === 1 ? "" : "s"}`}
          </span>
          {!isAdmin && (
            <span className="text-xs text-gray-500 dark:text-gray-400">
              Releasing claims requires the workspace admin role.
            </span>
          )}
        </div>
      </div>

      {outcomes && (
        <div className="border-b border-gray-200 px-4 py-2 dark:border-gray-700">
          <ResultBanner
            tone={failedCount === 0 ? "success" : "warning"}
            onDismiss={() => setOutcomes(null)}
          >
            <p>
              Released {releasedCount} of {outcomes.length} claim
              {outcomes.length === 1 ? "" : "s"}.
            </p>
            {failedCount > 0 && (
              <ul className="mt-1 list-disc space-y-0.5 pl-4">
                {outcomes
                  .filter((outcome) => outcome.error)
                  .map((outcome) => (
                    <li key={outcome.task.task_id}>
                      <span className="font-medium">{outcome.task.task_name}</span>:{" "}
                      {outcome.error}
                    </li>
                  ))}
              </ul>
            )}
          </ResultBanner>
        </div>
      )}

      {/* Results */}
      <div className="flex-1 overflow-auto">
        {loading && tasks.length === 0 ? (
          <div className="flex h-full items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
          </div>
        ) : error ? (
          <div className="flex h-full items-center justify-center px-4 text-red-500">
            <p role="alert">{error}</p>
          </div>
        ) : tasks.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center px-4 text-center text-gray-500 dark:text-gray-400">
            <p className="text-lg font-medium">No claims held</p>
            <p className="mt-1 text-sm">
              {filtersActive
                ? "No task matches these statuses and this age. Widen the filters to see every claim."
                : "No task in this environment is running or suspended, so nothing is holding a claim."}
            </p>
          </div>
        ) : (
          <table className="w-full table-fixed">
            <thead className="sticky top-0 bg-gray-50 dark:bg-gray-800">
              <tr>
                {isAdmin && (
                  <th className="w-10 border-b border-gray-200 px-3 py-1.5 dark:border-gray-700">
                    <Checkbox
                      checked={allVisibleSelected}
                      indeterminate={someVisibleSelected}
                      onChange={toggleAllVisible}
                      label="Select all claims on this page"
                      disabled={selectableIds.length === 0}
                    />
                  </th>
                )}
                {["Task", "Namespace", "Status", "Held for", "Claim holder"].map(
                  (label) => (
                    <th
                      key={label}
                      className="border-b border-gray-200 px-3 py-1.5 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500 dark:border-gray-700 dark:text-gray-400"
                    >
                      {label}
                    </th>
                  ),
                )}
                <th className="w-12 border-b border-gray-200 px-3 py-1.5 dark:border-gray-700">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 bg-white dark:divide-gray-700 dark:bg-gray-800">
              {tasks.map((task) => {
                const holderBuildId = task.latest_status_build_id ?? null;
                const status = task.latest_status ?? task.status;
                const held = task.latest_status_at
                  ? formatDuration(task.latest_status_at, null)
                  : "—";
                return (
                  <tr
                    key={task.task_id}
                    className={`hover:bg-gray-50 dark:hover:bg-gray-700/50 ${
                      selectedTaskId === task.task_id
                        ? "bg-blue-50 dark:bg-blue-900/20"
                        : ""
                    }`}
                  >
                    {isAdmin && (
                      <td className="px-3 py-1.5">
                        <Checkbox
                          checked={isSelected(task.task_id)}
                          onChange={(checked) => toggle(task.task_id, checked)}
                          disabled={!holderBuildId}
                          label={
                            holderBuildId
                              ? `Select ${task.task_name}`
                              : `${task.task_name} cannot be selected: its owning build was not recorded`
                          }
                        />
                      </td>
                    )}
                    <td className="overflow-hidden text-ellipsis whitespace-nowrap px-3 py-1.5 text-xs text-gray-900 dark:text-gray-100">
                      <span title={task.task_id}>{task.task_name}</span>
                    </td>
                    <td className="overflow-hidden text-ellipsis whitespace-nowrap px-3 py-1.5 text-xs text-gray-500 dark:text-gray-400">
                      {task.task_namespace || "—"}
                    </td>
                    <td className="whitespace-nowrap px-3 py-1.5">
                      <StatusBadge status={status} />
                    </td>
                    <td
                      className="whitespace-nowrap px-3 py-1.5 text-xs text-gray-700 dark:text-gray-300"
                      title={formatAbsoluteTime(task.latest_status_at)}
                    >
                      {held}
                    </td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-xs">
                      {holderBuildId ? (
                        onNavigateToBuild ? (
                          <button
                            type="button"
                            onClick={() => onNavigateToBuild(holderBuildId)}
                            title={`Go to build ${holderBuildId}`}
                            className="rounded font-mono text-blue-600 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-400"
                          >
                            {holderBuildId.slice(0, 8)}
                          </button>
                        ) : (
                          <span className="font-mono text-gray-700 dark:text-gray-300">
                            {holderBuildId.slice(0, 8)}
                          </span>
                        )
                      ) : (
                        <span
                          className="text-gray-400 dark:text-gray-500"
                          title="Recorded before the owning build was denormalised onto the task"
                        >
                          not recorded
                        </span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-3 py-1.5">
                      <button
                        type="button"
                        onClick={() => onSelectTask(task)}
                        aria-label={`View details for ${task.task_name}`}
                        className="rounded p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:hover:bg-gray-700 dark:hover:text-gray-300"
                      >
                        <EyeIcon />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <BulkActionBar
        count={selectedCount}
        noun="claim"
        onClear={clear}
        note="Selection covers this page only."
      >
        <button
          type="button"
          onClick={() => {
            setReleaseError(null);
            setConfirmOpen(true);
          }}
          className="rounded-md bg-red-600 px-2.5 py-1 text-sm font-medium text-white hover:bg-red-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500"
        >
          Release claims
        </button>
      </BulkActionBar>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between border-t border-gray-200 bg-white px-4 py-1.5 dark:border-gray-700 dark:bg-gray-800">
          <span className="text-xs text-gray-500 dark:text-gray-400">
            {(page - 1) * PAGE_SIZE + 1}-{Math.min(page * PAGE_SIZE, total)} of {total}
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage(Math.max(1, page - 1))}
              disabled={page === 1}
              className="rounded border border-gray-300 px-2 py-0.5 text-xs text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              Prev
            </button>
            <span className="text-xs text-gray-500 dark:text-gray-400">
              {page}/{totalPages}
            </span>
            <button
              onClick={() => setPage(Math.min(totalPages, page + 1))}
              disabled={page === totalPages}
              className="rounded border border-gray-300 px-2 py-0.5 text-xs text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              Next
            </button>
          </div>
        </div>
      )}

      <ConfirmDialog
        isOpen={confirmOpen}
        title={`Release ${selectedCount} execution claim${
          selectedCount === 1 ? "" : "s"
        }`}
        destructive
        confirmLabel="Release claims"
        busyLabel="Releasing…"
        cancelLabel="Close"
        busy={releasing}
        error={releaseError}
        onConfirm={handleRelease}
        onCancel={() => setConfirmOpen(false)}
        maxWidthClass="max-w-lg"
      >
        <p className="text-sm text-gray-600 dark:text-gray-400">
          Cancels each selected task{" "}
          <em>under the build that put it into its current status</em> — one request per
          task, addressed to a different build each time. That frees the execution
          claims and any concurrency-limit slots they hold.
        </p>
        <ul className="max-h-40 space-y-1 overflow-auto text-xs text-gray-600 dark:text-gray-400">
          {selectedTasks.map((task) => (
            <li key={task.task_id} className="flex items-center gap-2">
              <span className="truncate font-medium text-gray-800 dark:text-gray-200">
                {task.task_name}
              </span>
              <span>{task.latest_status ?? task.status}</span>
              <code className="rounded bg-gray-100 px-1 font-mono dark:bg-gray-700">
                {task.latest_status_build_id?.slice(0, 8)}
              </code>
            </li>
          ))}
        </ul>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Some may fail — a task can complete between this list being drawn and the
          request landing. Every outcome is reported per task, and the server cannot
          stop a worker that is still running: if it completes anyway, completed wins.
        </p>
      </ConfirmDialog>
    </div>
  );
}
