import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchTasks } from "../api/tasks";
import type { Task } from "../types/task";
import { modalFunctionCallUrl } from "../utils/modalLinks";
import {
  executionsForBuild,
  executorsIn,
  matchesFilters,
  stopCommand,
  workersIn,
  STOPPABLE_STATUSES,
  type StopFilters,
  type StoppableExecution,
} from "../utils/stoppable";
import { formatAbsoluteTime, formatDuration } from "../utils/time";
import { StatusBadge } from "./StatusBadge";

// One page of the environment's claim holders, at the server's maximum.
// The population is "tasks holding a claim", not "tasks", so one page
// covers all but the largest environments; if the server says there are
// more, the panel says so rather than quietly showing a prefix.
const PAGE_SIZE = 100;

// The staleness options the filter offers, in seconds. Round numbers an
// operator would actually type after `--older-than`.
const OLDER_THAN_CHOICES: { label: string; seconds: number }[] = [
  { label: "any age", seconds: 0 },
  { label: "over 5m", seconds: 5 * 60 },
  { label: "over 30m", seconds: 30 * 60 },
  { label: "over 2h", seconds: 2 * 3600 },
  { label: "over 12h", seconds: 12 * 3600 },
];

interface BuildStopPanelProps {
  buildId: string;
  environmentId: string;
  /**
   * Bumped by the parent on every refresh, so this panel refetches in
   * step with the build view rather than running a timer of its own.
   */
  refreshToken?: number;
}

/**
 * "What is this build still running, and how do I stop it?"
 *
 * The UI half of `stardag builds stop`. It shows the executions the build
 * currently holds — read off the task row, while the claims still make
 * that list exact — and hands over the command that acts on exactly what
 * is on screen.
 *
 * **It does not stop anything, by design.** Stopping a container means
 * reaching the execution backend, the server cannot do that and must not
 * learn to, so the credentials that can are the operator's. The panel's
 * job is to make sure the command they run matches the list they read,
 * and to link each call to its Modal dashboard page for the hard-kill
 * case.
 *
 * The list is fetched from `GET /tasks` (status-filtered), which is the
 * same endpoint and the same filter the CLI uses — so the two agree by
 * construction rather than by two implementations staying in step.
 *
 * The parent keys this on `buildId`, so navigating between builds remounts
 * it and every piece of state here — the list, the filters, the disclosure
 * — starts clean. Deliberate rather than incidental: a reset effect would
 * have to remember each new piece of state someone adds, and forgetting
 * one shows a previous build's executions under this build's header.
 */
export function BuildStopPanel({
  buildId,
  environmentId,
  refreshToken = 0,
}: BuildStopPanelProps) {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const [worker, setWorker] = useState("");
  const [executor, setExecutor] = useState("");
  const [namespace, setNamespace] = useState("");
  const [olderThanSeconds, setOlderThanSeconds] = useState(0);

  // A slow response from a previous build or environment must not
  // overwrite the current one's list.
  const epochRef = useRef(0);

  useEffect(() => {
    if (!buildId || !environmentId) return;
    const epoch = ++epochRef.current;
    fetchTasks({
      status: STOPPABLE_STATUSES,
      page_size: PAGE_SIZE,
      environment_id: environmentId,
    })
      .then((page) => {
        if (epochRef.current !== epoch) return;
        setTasks(page.tasks);
        setTotal(page.total);
        setError(null);
      })
      .catch((err: unknown) => {
        if (epochRef.current !== epoch) return;
        setError(err instanceof Error ? err.message : "Failed to read running tasks");
      });
  }, [buildId, environmentId, refreshToken]);

  const held = useMemo(
    () => executionsForBuild(tasks ?? [], buildId),
    [tasks, buildId],
  );
  const filters: StopFilters = useMemo(
    () => ({
      worker: worker || undefined,
      executor: executor || undefined,
      namespace: namespace || undefined,
      olderThanSeconds: olderThanSeconds || undefined,
    }),
    [worker, executor, namespace, olderThanSeconds],
  );
  const selected = useMemo(
    () => held.filter((execution) => matchesFilters(execution, filters)),
    [held, filters],
  );
  const workers = useMemo(() => workersIn(held), [held]);
  const executors = useMemo(() => executorsIn(held), [held]);
  const command = stopCommand(buildId, filters);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
    }
  }, [command]);

  // Nothing running under this build is the normal, healthy state — so the
  // panel is absent rather than empty. An error is worth a line, since a
  // reader who cannot see the list must not read that as "none".
  if (error) {
    return (
      <div
        role="alert"
        className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-900/20 dark:text-red-400"
      >
        Could not read this build&rsquo;s running tasks: {error}
      </div>
    );
  }
  if (tasks === null || held.length === 0) return null;

  const excluded = held.length - selected.length;
  const unstoppable = selected.filter((execution) => !execution.stoppable).length;

  return (
    <div className="rounded-md border border-amber-200 bg-amber-50/60 dark:border-amber-900/60 dark:bg-amber-900/10">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
      >
        <svg
          className={`h-3 w-3 flex-shrink-0 transition-transform ${
            open ? "rotate-90" : ""
          }`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M9 5l7 7-7 7"
          />
        </svg>
        <span className="font-medium text-amber-900 dark:text-amber-200">
          Stop running tasks
        </span>
        <span className="text-amber-800/80 dark:text-amber-300/80">
          {held.length} execution{held.length === 1 ? "" : "s"} held by this build
        </span>
      </button>

      {open && (
        <div className="space-y-3 px-3 pb-3">
          <p className="text-xs text-amber-900/80 dark:text-amber-200/80">
            These are read off the task rows while this build still holds their claims,
            which is the only moment the list is exact — cancelling the build releases
            the claims, and another build may then take a task over. Stopping the
            containers is the operator&rsquo;s to do:{" "}
            <strong>stardag never reaches the execution backend from here.</strong> Run
            the command below, or open a call in Modal and kill it there.
          </p>

          <div className="flex flex-wrap items-center gap-2 text-xs">
            {workers.length > 1 && (
              <label className="flex items-center gap-1">
                <span className="text-gray-600 dark:text-gray-400">Worker</span>
                <select
                  value={worker}
                  onChange={(e) => setWorker(e.target.value)}
                  className="rounded border border-gray-300 bg-white px-1.5 py-0.5 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
                >
                  <option value="">all</option>
                  {workers.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {/* Both dropdowns appear only when there is something to
                choose between — one worker or one executor is not a filter,
                it is a fact the table already shows. */}
            {executors.length > 1 && (
              <label className="flex items-center gap-1">
                <span className="text-gray-600 dark:text-gray-400">Executor</span>
                <select
                  value={executor}
                  onChange={(e) => setExecutor(e.target.value)}
                  className="rounded border border-gray-300 bg-white px-1.5 py-0.5 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
                >
                  <option value="">all</option>
                  {executors.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className="flex items-center gap-1">
              <span className="text-gray-600 dark:text-gray-400">Namespace</span>
              <input
                value={namespace}
                onChange={(e) => setNamespace(e.target.value)}
                placeholder="prefix"
                className="w-28 rounded border border-gray-300 bg-white px-1.5 py-0.5 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
              />
            </label>
            <label className="flex items-center gap-1">
              <span className="text-gray-600 dark:text-gray-400">Running</span>
              <select
                value={olderThanSeconds}
                onChange={(e) => setOlderThanSeconds(Number(e.target.value))}
                className="rounded border border-gray-300 bg-white px-1.5 py-0.5 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
              >
                {OLDER_THAN_CHOICES.map((choice) => (
                  <option key={choice.seconds} value={choice.seconds}>
                    {choice.label}
                  </option>
                ))}
              </select>
            </label>
            {excluded > 0 && (
              <span className="text-gray-600 dark:text-gray-400">
                {excluded} excluded by these filters
              </span>
            )}
          </div>

          <ExecutionTable executions={selected} />

          {unstoppable > 0 && (
            <p className="text-xs text-gray-600 dark:text-gray-400">
              {unstoppable} of these run on an executor stardag cannot stop. They are
              listed so nothing is invisible; ending them is that backend&rsquo;s own
              business.
            </p>
          )}

          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded bg-gray-900 px-2 py-1 font-mono text-[11px] text-gray-100">
                {command}
              </code>
              <button
                type="button"
                onClick={handleCopy}
                className="flex-shrink-0 rounded border border-gray-300 px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
              >
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
            <p className="text-xs text-gray-600 dark:text-gray-400">
              It stops these calls first and cancels the build afterwards, in that
              order. Add <code>--dry-run</code> to see its own list before anything
              happens.
              {excluded > 0 && (
                <>
                  {" "}
                  The {excluded} execution{excluded === 1 ? "" : "s"} your filters
                  exclude will keep running once the build is cancelled.
                </>
              )}
            </p>
          </div>

          {total > (tasks?.length ?? 0) && (
            <p className="text-xs text-amber-800 dark:text-amber-300">
              This environment has {total} tasks holding a claim, more than one page —
              the list above may be incomplete. The CLI pages through them all.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function ExecutionTable({ executions }: { executions: StoppableExecution[] }) {
  if (executions.length === 0) {
    return (
      <p className="text-xs text-gray-600 dark:text-gray-400">
        No execution matches these filters.
      </p>
    );
  }
  return (
    <table className="w-full text-left text-xs">
      <thead className="text-gray-600 dark:text-gray-400">
        <tr>
          <th className="py-1 pr-2 font-medium">Task</th>
          <th className="py-1 pr-2 font-medium">Status</th>
          <th className="py-1 pr-2 font-medium">Worker</th>
          <th className="py-1 pr-2 font-medium">Call</th>
          <th className="py-1 font-medium">Running for</th>
        </tr>
      </thead>
      <tbody>
        {executions.map((execution) => {
          const callUrl = modalFunctionCallUrl(
            execution.metadata,
            execution.executorRef,
          );
          return (
            <tr
              key={execution.taskId}
              className="border-t border-amber-200/60 dark:border-amber-900/40"
            >
              <td className="py-1 pr-2">
                <span
                  title={execution.taskId}
                  className="font-medium text-gray-900 dark:text-gray-100"
                >
                  {execution.qualifiedName}
                </span>
              </td>
              <td className="py-1 pr-2">
                <StatusBadge status={execution.status} />
                {execution.restartDue && (
                  <span
                    className="ml-1 text-amber-800 dark:text-amber-300"
                    title="The platform said it was restarting this execution and the restart has not landed yet."
                  >
                    restart due
                  </span>
                )}
              </td>
              <td className="py-1 pr-2 text-gray-700 dark:text-gray-300">
                {execution.worker ?? "—"}
              </td>
              <td className="py-1 pr-2">
                {callUrl ? (
                  <a
                    href={callUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    title="Open this call in the Modal dashboard"
                    className="font-mono text-[11px] text-blue-700 hover:underline dark:text-blue-300"
                  >
                    {execution.executorRef}
                  </a>
                ) : (
                  <code className="font-mono text-[11px] text-gray-700 dark:text-gray-300">
                    {execution.executorRef}
                  </code>
                )}
              </td>
              <td
                className="py-1 text-gray-700 dark:text-gray-300"
                title={formatAbsoluteTime(execution.statusAt)}
              >
                {formatDuration(execution.statusAt, null)}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
