import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchTasks } from "../api/tasks";
import type { Build, BuildStatus } from "../types/task";
import { modalFunctionCallUrl } from "../utils/modalLinks";
import {
  CLAIM_PAGE_SIZE,
  collectExecutions,
  executorsIn,
  matchesFilters,
  stopCommand,
  workersIn,
  MAX_CLAIM_PAGES,
  NO_EXECUTOR,
  NO_REF_YET,
  STOPPABLE_STATUSES,
  type StopFilters,
  type StoppableExecution,
} from "../utils/stoppable";
import { formatAbsoluteTime, formatDuration } from "../utils/time";
import { StatusBadge } from "./StatusBadge";
import { Modal } from "./Modal";
import { BuildOverrideSection } from "./BuildOverrideSection";
import { canOverrideStatus } from "../utils/builds";
import { Checkbox } from "./ui/Checkbox";
import { ResultBanner } from "./ui/ResultBanner";
import { ToolbarButton } from "./ui/ToolbarButton";

// The staleness options the filter offers, in seconds. Round numbers an
// operator would actually type after `--older-than`.
const OLDER_THAN_CHOICES: { label: string; seconds: number }[] = [
  { label: "any age", seconds: 0 },
  { label: "over 5m", seconds: 5 * 60 },
  { label: "over 30m", seconds: 30 * 60 },
  { label: "over 2h", seconds: 2 * 3600 },
  { label: "over 12h", seconds: 12 * 3600 },
];

/**
 * How many execution rows the table draws.
 *
 * The panel's job is to hand over a command, not to be a second task
 * table — the command acts on the whole set however much of it is
 * listed. Fifty is well past the point where anyone is reading rows one
 * by one, and the cap is what stops a wide fan-out owning the screen
 * (STA-83). Whatever is not drawn is stated, never silently dropped.
 */
export const MAX_ROWS_DRAWN = 50;

/**
 * Where `stardag builds stop` gives up — `_stop.py`'s 200 pages of 100.
 *
 * Kept here only so the copy can be accurate about it. It is a much
 * larger number than this dialog's own scan, and past it the CLI raises
 * rather than acting on a partial list, because stopping on a partial
 * list releases the claims with containers still running.
 */
const CLI_MAX_CLAIM_HOLDERS = 20_000;

interface BuildControlsDialogProps {
  buildId: string;
  environmentId: string;
  /**
   * The build's status. Only decides whether the trigger is offered: a
   * completed build has nothing left running, and anything else may,
   * including a failed or cancelled one — that is the case this panel
   * exists for, since cancelling a build does not stop its containers.
   */
  buildStatus: BuildStatus;
  /**
   * Whether this build holds any execution claim, from its own task
   * list. Not inferred from the stop scan below: that asks for the
   * stoppable statuses only, so a SUSPENDED task — a claim with nothing
   * to stop — is invisible to it, and it can truncate besides.
   */
  holdsClaims: boolean;
  /**
   * Bumped by the parent on every refresh, so this dialog refetches in
   * step with the build view rather than running a timer of its own.
   */
  refreshToken?: number;
  /** Called with the updated build after a status override lands. */
  onBuildChanged: (build: Build) => void;
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
export function BuildControlsDialog({
  buildId,
  environmentId,
  buildStatus,
  holdsClaims,
  refreshToken = 0,
  onBuildChanged,
}: BuildControlsDialogProps) {
  const [held, setHeld] = useState<StoppableExecution[] | null>(null);
  const [total, setTotal] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  // Kept here rather than in the override section, because a successful
  // override can take the build out of the overridable statuses — which
  // unmounts that section, and would take its own confirmation with it.
  const [statusNotice, setStatusNotice] = useState<string | null>(null);

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
  // Whether a scan is in flight; see the effect below.
  const scanningRef = useRef(false);
  // The "Copied" flash's timer, so it can be cancelled. Two reasons it
  // needs to be: unmounting mid-flash would set state on a dead
  // component, and a second copy before the first flash expires would
  // otherwise leave the earlier timer to clear the label early.
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (copiedTimer.current !== null) clearTimeout(copiedTimer.current);
    },
    [],
  );

  // Only while the dialog is open. The scan is up to 20 sequential
  // requests, and it used to run on every 5s auto-refresh whether or not
  // anyone had opened the panel — 20 requests every 5 seconds to draw
  // nothing (STA-83). Opening it is the signal that the answer is wanted.
  useEffect(() => {
    if (!open || !buildId || !environmentId) return;
    // One scan at a time. The effect re-runs on every `refreshToken`
    // bump, which auto-refresh produces every 5 seconds, and a scan is
    // up to 20 sequential requests with no cancellation — so past a few
    // hundred claim holders a second scan starts before the first ends
    // and they pile up for as long as the dialog stays open. The epoch
    // keeps the *data* right; this keeps the *requests* bounded.
    if (scanningRef.current) return;
    scanningRef.current = true;
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
      })
      .finally(() => {
        scanningRef.current = false;
      });
  }, [open, buildId, environmentId, refreshToken]);

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
  //
  // The third case is the one that has to be a case rather than a
  // fallback: ticks survive a filter change, so the dropdowns can be moved
  // until none of the ticked rows are shown. "No ids" is then not a
  // narrower request, it is the *broadest* one — a command with no filters
  // stops everything the build holds. There is no command string meaning
  // "stop nothing", so none is offered.
  const filters: StopFilters | null = !ticked.size
    ? narrowed
    : chosen.length
      ? { taskIds: chosen.map((execution) => execution.taskId) }
      : null;
  // Whether anything is narrowing the list, which decides how to
  // explain an empty selection.
  const anyFilterSet = Boolean(worker || executor || namespace || olderThanSeconds);
  const workers = useMemo(() => workersIn(executions), [executions]);
  const executors = useMemo(() => executorsIn(executions), [executions]);
  const command = filters === null ? null : stopCommand(buildId, filters);

  const toggle = useCallback((taskId: string, on: boolean) => {
    setTicked((previous) => {
      const next = new Set(previous);
      if (on) next.add(taskId);
      else next.delete(taskId);
      return next;
    });
  }, []);

  const handleCopy = useCallback(async () => {
    if (command === null) return;
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      if (copiedTimer.current !== null) clearTimeout(copiedTimer.current);
      copiedTimer.current = setTimeout(() => {
        copiedTimer.current = null;
        setCopied(false);
      }, 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
    }
  }, [command]);

  // Offered for every build that is not finished. A cancelled or failed
  // build is exactly when this is wanted, because cancelling a build does
  // not stop its containers.
  //
  // `&& !open` matters: the status can reach `completed` while the
  // dialog is being read — the operator marks it completed from this
  // very dialog, or a 5-second auto-refresh brings the news — and
  // returning null then unmounts the dialog out from under them,
  // mid-action and with no confirmation. As a panel that was invisible;
  // as a dialog it is not. Once it is open it stays open until closed.
  if (buildStatus === "completed" && !open) return null;

  const excluded = executions.length - chosen.length;
  // Split by reason, not counted together: one is permanent and one is
  // over in seconds, and an operator deciding whether to wait or to go to
  // the Modal dashboard needs to know which they are looking at.
  const pendingReasons: (string | null)[] = [NO_REF_YET, NO_EXECUTOR];
  const unreachable = chosen.filter(
    (execution) =>
      !execution.stoppable && !pendingReasons.includes(execution.notStoppableReason),
  ).length;

  return (
    <>
      <ToolbarButton
        label="Build controls"
        hint="Override the build status and, optionally, stop running tasks"
        align="right"
        onClick={() => setOpen(true)}
      >
        <svg
          aria-hidden="true"
          className="h-4 w-4"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
          />
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 9h6v6H9z" />
        </svg>
      </ToolbarButton>

      <Modal
        isOpen={open}
        onClose={() => setOpen(false)}
        title="Build controls"
        maxWidthClass="max-w-4xl"
      >
        <h3 className="mb-2 text-sm font-semibold text-gray-900 dark:text-gray-100">
          Stop what is running
        </h3>
        <StopDialogBody
          error={error}
          held={held}
          total={total}
          truncated={truncated}
          buildId={buildId}
        >
          <p className="text-xs text-gray-600 dark:text-gray-400">
            Ends the listed Modal calls from your credentials, then cancels the build
            and releases every claim it holds. Rows without a call id exit at their next
            checkpoint.
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
            {ticked.size === 0
              ? "Nothing ticked — the command below targets every execution listed. Tick rows to narrow it to those."
              : `The command below names the ${chosen.length} you ticked.`}
          </p>

          {/* The spawn-reporting explanation is gone: the section's own
              first line already says rows without a call id exit at their
              next checkpoint. What survives is the case that is permanent
              rather than a moment — another backend entirely. */}
          {unreachable > 0 && (
            <p className="text-xs text-gray-600 dark:text-gray-400">
              {unreachable} run on an executor stardag cannot stop; ending those is that
              backend&rsquo;s own business.
            </p>
          )}

          {command === null ? (
            <p role="status" className="text-xs text-amber-800 dark:text-amber-300">
              {anyFilterSet
                ? "None of the rows you ticked match these filters, so there is nothing to stop. Clear the ticks or widen the filters."
                : // No filter is set, so the ticked rows did not fall out of a
                  // narrowing — they fell out of the list. They finished, or
                  // something else took them over, between ticking and the
                  // last rescan.
                  "The executions you ticked are no longer running, so there is nothing to stop. Clear the ticks to target whatever is still listed."}{" "}
              No command is offered, because one with no targets would stop everything.
            </p>
          ) : (
            <div className="space-y-3 py-1">
              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded bg-gray-900 px-3 py-2 font-mono text-[11px] text-gray-100">
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
                Add <code>--dry-run</code> to see its own list before anything happens.
                {excluded > 0 && (
                  <>
                    {" "}
                    The {excluded} execution{excluded === 1 ? "" : "s"} it does not name
                    will keep running once the build is cancelled.
                  </>
                )}
              </p>
            </div>
          )}

          {truncated && (
            <p className="text-xs text-amber-800 dark:text-amber-300">
              This environment has {total} tasks holding a claim, more than the{" "}
              {MAX_CLAIM_PAGES * CLAIM_PAGE_SIZE} this dialog scans —{" "}
              <strong>the list above may be incomplete.</strong> The CLI scans ten times
              further, and past {CLI_MAX_CLAIM_HOLDERS.toLocaleString("en-US")} it
              refuses outright rather than acting on a partial list.
            </p>
          )}
        </StopDialogBody>

        {/* The work first, then the record. Stopping is what an
            operator opening this dialog almost always came for, and
            the rule between the two halves is doing the work the old
            separate controls did not: these act on different things.
            The notice sits outside the override block because a
            successful override can take the build out of the
            overridable statuses, which unmounts that block. */}
        {(canOverrideStatus(buildStatus) || statusNotice) && (
          <hr className="my-4 border-gray-200 dark:border-gray-700" />
        )}

        {canOverrideStatus(buildStatus) && (
          <BuildOverrideSection
            buildId={buildId}
            environmentId={environmentId}
            buildStatus={buildStatus}
            holdsClaims={holdsClaims}
            // The stop scan answers this one: it lists exactly the
            // executions that can be ended, which a suspended claim is
            // not one of.
            //
            // Empty is only "none" from an *exhaustive* scan that
            // answered *just now*. Three things break that, and each
            // leaves a "none" the operator should not be shown:
            //
            //  - `held === null`: no scan has answered yet.
            //  - a truncated empty result: it found nothing only because
            //    it stopped looking, and this build's executions can sit
            //    entirely on pages it never read.
            //  - `error`: a refresh failed, and the catch leaves the
            //    previous `held` in place — so an empty answer from
            //    minutes ago would keep reading as a fresh one.
            //
            // All three are "unknown", which for a warning behaves like
            // "maybe".
            runningExecutions={
              held === null || error !== null || (truncated && held.length === 0)
                ? "unknown"
                : held.length > 0
                  ? "some"
                  : "none"
            }
            onChanged={(updated) => {
              setStatusNotice(`This build is now recorded as ${updated.status}.`);
              onBuildChanged(updated);
            }}
          />
        )}

        {statusNotice && (
          <ResultBanner
            tone="success"
            className="mt-3"
            onDismiss={() => setStatusNotice(null)}
          >
            {statusNotice} Nothing running was stopped.
          </ResultBanner>
        )}
      </Modal>
    </>
  );
}

interface StopDialogBodyProps {
  error: string | null;
  held: StoppableExecution[] | null;
  total: number;
  truncated: boolean;
  buildId: string;
  children: React.ReactNode;
}

/**
 * The three states that are not "here is the list", and the list itself.
 *
 * As a panel this component simply rendered nothing when a build held no
 * executions, which was right for something that appeared unbidden above
 * the DAG. Inside a dialog somebody has deliberately opened, silence is
 * the wrong answer: "nothing is running" has to be said, because the
 * alternative reading is that the dialog is broken.
 */
function StopDialogBody({
  error,
  held,
  total,
  truncated,
  buildId,
  children,
}: StopDialogBodyProps) {
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
  if (held === null) {
    return (
      <p role="status" className="text-xs text-gray-600 dark:text-gray-400">
        Reading this build&rsquo;s running tasks…
      </p>
    );
  }
  if (held.length === 0) {
    // "Found none" and "stopped looking" are different answers, and only
    // one of them means nothing is running.
    if (truncated) {
      return (
        <div
          role="status"
          className="rounded-md border border-amber-200 bg-amber-50/60 px-3 py-2 text-xs text-amber-900 dark:border-amber-900/60 dark:bg-amber-900/10 dark:text-amber-200"
        >
          This environment has {total} tasks holding an execution claim — more than this
          dialog will scan, so whether this build has live executions could not be
          determined here. <code>stardag builds stop {buildId} --dry-run</code> scans
          ten times further before it gives up.
        </div>
      );
    }
    return (
      <p role="status" className="text-xs text-gray-600 dark:text-gray-400">
        This build has nothing running that can be stopped from here.
      </p>
    );
  }
  return <div className="space-y-3">{children}</div>;
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
  // Two independent guards, and both are needed. The cap bounds the DOM,
  // which is what a several-hundred-wide fan-out would otherwise blow up;
  // the max height bounds the pixels, so even fifty rows cannot take the
  // dialog over. Neither changes what the command acts on.
  const drawn = executions.slice(0, MAX_ROWS_DRAWN);
  const undrawn = executions.length - drawn.length;
  return (
    <div className="max-h-80 overflow-y-auto">
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
          {drawn.map((execution) => {
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
                  {/* The ref decides first, not the URL: modalFunctionCallUrl
                    still resolves an app-level link without a call id, and
                    linking that would render an empty anchor where the
                    reason belongs. */}
                  {!execution.executorRef ? (
                    <span
                      className="text-[11px] text-amber-800 dark:text-amber-300"
                      title={execution.notStoppableReason ?? undefined}
                    >
                      not recorded yet
                    </span>
                  ) : callUrl ? (
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
      {undrawn > 0 && (
        <p className="px-1 py-1.5 text-xs text-gray-600 dark:text-gray-400">
          {undrawn} more execution{undrawn === 1 ? "" : "s"} not listed.{" "}
          {ticked.size > 0
            ? // Ticking switches the command to exact task ids, so the undrawn
              // rows really are excluded. Claiming otherwise would err towards
              // "everything is covered", which is the dangerous direction.
              "Ticked rows are named individually, so these are not included — clear the ticks to target the whole list."
            : "The command below still targets every one of them — narrow with the filters above to see a particular set."}
        </p>
      )}
    </div>
  );
}
