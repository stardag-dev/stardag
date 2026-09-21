import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchTasks } from "../api/tasks";
import { modalFunctionCallUrl } from "../utils/modalLinks";
import {
  CLAIM_PAGE_SIZE,
  collectExecutions,
  executorsIn,
  matchesFilters,
  stopCommand,
  workersIn,
  MAX_CLAIM_PAGES,
  STOPPABLE_STATUSES,
  type StopFilters,
  type StoppableExecution,
} from "../utils/stoppable";
import { formatAbsoluteTime, formatDuration } from "../utils/time";
import { StatusBadge } from "./StatusBadge";
import { Checkbox } from "./ui/Checkbox";

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
  const [held, setHeld] = useState<StoppableExecution[] | null>(null);
  const [total, setTotal] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const [worker, setWorker] = useState("");
  const [executor, setExecutor] = useState("");
  const [namespace, setNamespace] = useState("");
  const [olderThanSeconds, setOlderThanSeconds] = useState(0);
  // Rows the operator ticked. Empty means "everything the filters show",
  // which is the state the panel opens in.
  const [ticked, setTicked] = useState<Set<string>>(new Set());

  // A slow response from a previous build or environment must not
  // overwrite the current one's list.
  const epochRef = useRef(0);

  useEffect(() => {
    if (!buildId || !environmentId) return;
    const epoch = ++epochRef.current;
    collectExecutions(
      (page) =>
        fetchTasks({
          status: STOPPABLE_STATUSES,
          page,
          page_size: CLAIM_PAGE_SIZE,
          environment_id: environmentId,
        }),
      buildId,
    )
      .then((result) => {
        if (epochRef.current !== epoch) return;
        setHeld(result.executions);
        setTotal(result.total);
        setTruncated(result.truncated);
        setError(null);
      })
      .catch((err: unknown) => {
        if (epochRef.current !== epoch) return;
        setError(err instanceof Error ? err.message : "Failed to read running tasks");
      });
  }, [buildId, environmentId, refreshToken]);

  const executions = useMemo(() => held ?? [], [held]);
  const narrowed: StopFilters = useMemo(
    () => ({
      worker: worker || undefined,
      executor: executor || undefined,
      namespace: namespace || undefined,
      olderThanSeconds: olderThanSeconds || undefined,
    }),
    [worker, executor, namespace, olderThanSeconds],
  );
  // What the table shows: the dropdowns narrow, and ticking picks within
  // that. Ticking deliberately does not hide the rows it leaves out — a
  // selection whose alternatives are off-screen is not a selection.
  const shown = useMemo(
    () => executions.filter((execution) => matchesFilters(execution, narrowed)),
    [executions, narrowed],
  );
  const chosen = useMemo(
    () => (ticked.size ? shown.filter((e) => ticked.has(e.taskId)) : shown),
    [shown, ticked],
  );
  // The command targets exactly what is chosen, which is why ticking
  // replaces the narrowing flags with ids rather than adding to them.
  const filters: StopFilters = ticked.size
    ? { taskIds: chosen.map((execution) => execution.taskId) }
    : narrowed;
  const workers = useMemo(() => workersIn(executions), [executions]);
  const executors = useMemo(() => executorsIn(executions), [executions]);
  const command = stopCommand(buildId, filters);

  const toggle = useCallback((taskId: string, on: boolean) => {
    setTicked((previous) => {
      const next = new Set(previous);
      if (on) next.add(taskId);
      else next.delete(taskId);
      return next;
    });
  }, []);

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
  if (held === null) return null;
  if (executions.length === 0) {
    // Nothing found. That is the normal, healthy state and the panel stays
    // out of the way — *unless* the scan gave up early, in which case
    // "found none" and "stopped looking" are the same screen, and the
    // difference is a build whose live executions nobody was shown.
    if (!truncated) return null;
    return (
      <div
        role="status"
        className="rounded-md border border-amber-200 bg-amber-50/60 px-3 py-2 text-xs text-amber-900 dark:border-amber-900/60 dark:bg-amber-900/10 dark:text-amber-200"
      >
        This environment has {total} tasks holding an execution claim — more than this
        page will scan, so whether this build has live executions could not be
        determined here. <code>stardag builds stop {buildId} --dry-run</code> pages
        through all of them.
      </div>
    );
  }

  const excluded = executions.length - chosen.length;
  const unstoppable = chosen.filter((execution) => !execution.stoppable).length;

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
          {executions.length} execution{executions.length === 1 ? "" : "s"} held by this
          build
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
                {excluded} not selected
              </span>
            )}
            {ticked.size > 0 && (
              <button
                type="button"
                onClick={() => setTicked(new Set())}
                className="rounded border border-gray-300 px-1.5 py-0.5 text-gray-700 hover:bg-gray-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
              >
                Clear {ticked.size} tick{ticked.size === 1 ? "" : "s"}
              </button>
            )}
          </div>

          <ExecutionTable executions={shown} ticked={ticked} onToggle={toggle} />

          <p className="text-xs text-gray-600 dark:text-gray-400">
            {ticked.size > 0
              ? `The command below names the ${ticked.size} you ticked.`
              : "Nothing ticked — the command below targets every execution listed. Tick rows to narrow it to those."}
          </p>

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
                  The {excluded} execution{excluded === 1 ? "" : "s"} it does not name
                  will keep running once the build is cancelled.
                </>
              )}
            </p>
          </div>

          {truncated && (
            <p className="text-xs text-amber-800 dark:text-amber-300">
              This environment has {total} tasks holding a claim, more than the{" "}
              {MAX_CLAIM_PAGES * CLAIM_PAGE_SIZE} this page scans —{" "}
              <strong>the list above may be incomplete.</strong> The CLI pages through
              all of them.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

interface ExecutionTableProps {
  executions: StoppableExecution[];
  ticked: Set<string>;
  onToggle: (taskId: string, on: boolean) => void;
}

/**
 * Boxes reflect `ticked` literally, so none are checked until someone
 * ticks one — and no tick at all means the command targets every row
 * shown.
 *
 * The alternative, rendering them all checked to match what the command
 * does, inverts the first click: on a table of checked boxes, clicking one
 * reads as "not that one" while it would have to mean "only that one".
 * Better to say what "nothing ticked" means in words, once, than to have
 * the first click do the opposite of what it looks like.
 */
function ExecutionTable({ executions, ticked, onToggle }: ExecutionTableProps) {
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
          <th className="w-6 py-1 pr-2 font-medium">
            <span className="sr-only">Include</span>
          </th>
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
                <Checkbox
                  checked={ticked.has(execution.taskId)}
                  onChange={(on) => onToggle(execution.taskId, on)}
                  label={`Include ${execution.qualifiedName}`}
                />
              </td>
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
