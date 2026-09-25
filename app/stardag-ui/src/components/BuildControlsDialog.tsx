import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchBuildExecutions } from "../api/registry";
import type { Build, BuildStatus, Execution } from "../types/task";
import { canOverrideStatus } from "../utils/builds";
import {
  executorsIn,
  matchesFilters,
  notStoppableReason,
  NO_EXECUTOR,
  NO_REF_YET,
  stopCommand,
  stopCommandEffect,
  workersIn,
  type StopFilters,
} from "../utils/stoppable";
import { BuildOverrideSection } from "./BuildOverrideSection";
import { ExecutionTable, type ExecutionTaskInfo } from "./ExecutionTable";
import { Modal } from "./Modal";
import { Checkbox } from "./ui/Checkbox";
import { ResultBanner } from "./ui/ResultBanner";
import { ToolbarButton } from "./ui/ToolbarButton";

const OLDER_THAN_CHOICES: { label: string; seconds: number }[] = [
  { label: "any age", seconds: 0 },
  { label: "over 5m", seconds: 5 * 60 },
  { label: "over 30m", seconds: 30 * 60 },
  { label: "over 2h", seconds: 2 * 3600 },
  { label: "over 12h", seconds: 12 * 3600 },
];

const SELECT_CLASS =
  "rounded border border-gray-300 bg-white px-1.5 py-0.5 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100";

interface BuildControlsDialogProps {
  buildId: string;
  environmentId: string;
  buildStatus: BuildStatus;
  // Bumped by the parent on every refresh.
  refreshToken?: number;
  onBuildChanged: (build: Build) => void;
  onOpenTask?: (taskId: string) => void;
  // Task names and statuses from the build's active plan, for the rows.
  taskInfo?: Map<string, ExecutionTaskInfo>;
}

/**
 * "What is this build still running, and how do I stop it?"
 *
 * The UI half of `stardag builds stop`: the build's executions with no end
 * reported, over all its plans (`GET /builds/{id}/executions`), orphans
 * marked, and the exact command for what is on screen. It stops nothing
 * itself — see `utils/stoppable`. Below it, the record overrides.
 *
 * The parent keys this on environment and build, so navigating remounts it
 * and every piece of state starts clean.
 */
export function BuildControlsDialog({
  buildId,
  environmentId,
  buildStatus,
  refreshToken = 0,
  onBuildChanged,
  onOpenTask,
  taskInfo,
}: BuildControlsDialogProps) {
  const [executions, setExecutions] = useState<Execution[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [statusNotice, setStatusNotice] = useState<string | null>(null);

  const [orphansOnly, setOrphansOnly] = useState(false);
  const [worker, setWorker] = useState("");
  const [executor, setExecutor] = useState("");
  const [olderThanSeconds, setOlderThanSeconds] = useState(0);
  const [ticked, setTicked] = useState<Set<string>>(new Set());

  const epochRef = useRef(0);
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (copiedTimer.current !== null) clearTimeout(copiedTimer.current);
    },
    [],
  );

  // The full list, read while the dialog is open; `--not-in-current-plan`
  // narrows it client-side on `in_current_plan`, the same predicate the
  // server applies for the CLI.
  useEffect(() => {
    if (!open) return;
    const epoch = ++epochRef.current;
    fetchBuildExecutions(buildId, environmentId)
      .then((rows) => {
        if (epochRef.current !== epoch) return;
        setExecutions(rows);
        setError(null);
      })
      .catch((err: unknown) => {
        if (epochRef.current !== epoch) return;
        setError(err instanceof Error ? err.message : "Failed to read executions");
      });
  }, [open, buildId, environmentId, refreshToken]);

  const all = useMemo(() => executions ?? [], [executions]);
  const narrowed: StopFilters = useMemo(
    () => ({
      notInCurrentPlan: orphansOnly || undefined,
      worker: worker || undefined,
      executor: executor || undefined,
      olderThanSeconds: olderThanSeconds || undefined,
    }),
    [orphansOnly, worker, executor, olderThanSeconds],
  );
  const shown = useMemo(
    () => all.filter((execution) => matchesFilters(execution, narrowed)),
    [all, narrowed],
  );
  const chosen = useMemo(
    () => (ticked.size ? shown.filter((e) => ticked.has(e.task_id)) : shown),
    [shown, ticked],
  );
  const filters: StopFilters | null = !ticked.size
    ? narrowed
    : chosen.length
      ? {
          // The filters stay on the command: `--task-id` alone would also
          // select a ticked task's executions the filters hid.
          ...narrowed,
          taskIds: [...new Set(chosen.map((e) => e.task_id))],
        }
      : null;
  const command = filters === null ? null : stopCommand(buildId, filters);
  const orphanCount = all.filter((e) => !e.in_current_plan).length;
  const pendingReasons: (string | null)[] = [NO_REF_YET, NO_EXECUTOR];
  const unreachable = chosen.filter((e) => {
    const reason = notStoppableReason(e);
    return reason !== null && !pendingReasons.includes(reason);
  }).length;
  // Selected rows with no call id: the command cannot cancel them.
  const noCallId = chosen.filter((e) => {
    const reason = notStoppableReason(e);
    return reason !== null && pendingReasons.includes(reason);
  }).length;
  // Listed rows the command does not name: they keep running.
  const notSelected = all.length - chosen.length;
  const anyFilterSet = Boolean(
    narrowed.notInCurrentPlan ||
      narrowed.worker ||
      narrowed.executor ||
      narrowed.olderThanSeconds,
  );

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

  const workers = workersIn(all);
  const executors = executorsIn(all);

  return (
    <>
      <ToolbarButton
        label="Build controls"
        hint="Executions to stop, and the build's recorded outcome"
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
        {error ? (
          <ResultBanner tone="error">
            Could not read this build&rsquo;s executions: {error}
          </ResultBanner>
        ) : executions === null ? (
          <p role="status" className="text-xs text-gray-600 dark:text-gray-400">
            Reading this build&rsquo;s executions…
          </p>
        ) : all.length === 0 ? (
          <p role="status" className="text-xs text-gray-600 dark:text-gray-400">
            Every execution of this build has reported an end; there is nothing to stop.
          </p>
        ) : (
          <div className="space-y-3">
            <p className="text-xs text-gray-600 dark:text-gray-400">
              Executions with no end reported, under any of this build&rsquo;s plans.
              {orphanCount > 0 &&
                ` ${orphanCount} ${
                  orphanCount === 1 ? "is an orphan" : "are orphans"
                }: started under a plan the build has since rolled over from.`}
            </p>
            <div className="flex flex-wrap items-center gap-3 text-xs">
              <Checkbox
                checked={orphansOnly}
                onChange={setOrphansOnly}
                label="Orphans only (not in the current plan)"
                labelHidden={false}
              />
              {workers.length > 1 && (
                <label className="flex items-center gap-1">
                  <span className="text-gray-600 dark:text-gray-400">Worker</span>
                  <select
                    value={worker}
                    onChange={(e) => setWorker(e.target.value)}
                    className={SELECT_CLASS}
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
              {executors.length > 1 && (
                <label className="flex items-center gap-1">
                  <span className="text-gray-600 dark:text-gray-400">Executor</span>
                  <select
                    value={executor}
                    onChange={(e) => setExecutor(e.target.value)}
                    className={SELECT_CLASS}
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
                <span className="text-gray-600 dark:text-gray-400">Running</span>
                <select
                  value={olderThanSeconds}
                  onChange={(e) => setOlderThanSeconds(Number(e.target.value))}
                  className={SELECT_CLASS}
                >
                  {OLDER_THAN_CHOICES.map((choice) => (
                    <option key={choice.seconds} value={choice.seconds}>
                      {choice.label}
                    </option>
                  ))}
                </select>
              </label>
              {notSelected > 0 && (
                <span className="text-gray-600 dark:text-gray-400">
                  {notSelected} not selected
                </span>
              )}
              {ticked.size > 0 && (
                <button
                  type="button"
                  onClick={() => setTicked(new Set())}
                  className="rounded border border-gray-300 px-1.5 py-0.5 text-gray-700 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
                >
                  Clear {ticked.size} tick{ticked.size === 1 ? "" : "s"}
                </button>
              )}
            </div>

            <ExecutionTable
              executions={shown}
              ticked={ticked}
              onToggle={toggle}
              onOpenTask={onOpenTask}
              taskInfo={taskInfo}
            />

            <p className="text-xs text-gray-600 dark:text-gray-400">
              {ticked.size === 0
                ? "Nothing ticked — the command below targets every execution listed. Tick rows to narrow it to those."
                : `The command below names the ${chosen.length} you ticked.`}
            </p>

            {noCallId > 0 && (
              <p className="text-xs text-gray-600 dark:text-gray-400">
                {noCallId} selected {noCallId === 1 ? "has" : "have"} no call id, so the
                command cannot stop {noCallId === 1 ? "it" : "them"}: re-run it in a few
                seconds to catch a spawn about to report one, add{" "}
                <code>--mark-lost</code> to end {noCallId === 1 ? "it" : "them"} as lost
                (no later report is applied), or <code>--no-cancel</code> to leave the
                build running.
              </p>
            )}

            {unreachable > 0 && (
              <p className="text-xs text-gray-600 dark:text-gray-400">
                {unreachable} run on an executor stardag cannot stop; ending those is
                that backend&rsquo;s own business.
              </p>
            )}

            {command === null ? (
              <p role="status" className="text-xs text-amber-800 dark:text-amber-300">
                {anyFilterSet
                  ? "None of the rows you ticked match these filters, so there is nothing to stop. Clear the ticks or widen the filters."
                  : // No filter is set, so the ticked rows did not fall out of
                    // a narrowing — they fell out of the list: they reported
                    // an end between ticking and the last read.
                    "The executions you ticked are no longer listed — they reported an end — so there is nothing to stop. Clear the ticks to target whatever is still listed."}{" "}
                No command is offered, because one with no targets would stop
                everything.
              </p>
            ) : (
              <div className="space-y-1 py-1">
                <div className="flex items-center gap-2">
                  <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded bg-gray-900 px-3 py-2 font-mono text-[11px] text-gray-100">
                    {command}
                  </code>
                  <button
                    type="button"
                    onClick={handleCopy}
                    className="flex-shrink-0 rounded border border-gray-300 px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
                  >
                    {copied ? "Copied" : "Copy"}
                  </button>
                </div>
                <p className="text-xs text-gray-600 dark:text-gray-400">
                  {stopCommandEffect(narrowed)} Add <code>--dry-run</code> to see its
                  own list before anything happens.
                  {notSelected > 0 &&
                    ` The ${notSelected} execution${
                      notSelected === 1 ? "" : "s"
                    } it does not name will keep running${
                      narrowed.notInCurrentPlan ? "" : " once the build is cancelled"
                    }.`}
                </p>
              </div>
            )}
          </div>
        )}

        {(canOverrideStatus(buildStatus) || statusNotice) && (
          <hr className="my-4 border-gray-200 dark:border-gray-700" />
        )}
        {canOverrideStatus(buildStatus) && (
          <BuildOverrideSection
            buildId={buildId}
            environmentId={environmentId}
            buildStatus={buildStatus}
            runningExecutions={
              executions === null || error !== null
                ? "unknown"
                : executions.length > 0
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
