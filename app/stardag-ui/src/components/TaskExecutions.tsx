import { TASK_EXECUTION_LIMIT } from "../api/registry";
import type { Execution } from "../types/task";
import { shortBuildId } from "../utils/ids";
import { executorOf, workerOf } from "../utils/stoppable";
import { formatAbsoluteTime, formatDuration, formatRelativeTime } from "../utils/time";
import { ExecutorBadge } from "./ExecutorBadge";
import { ModalExecutionCallRef, ModalExecutionDetails } from "./ModalExecution";

interface TaskExecutionsProps {
  executions: Execution[];
  // The build on screen, if any: its executions are marked "this build".
  currentBuildId?: string;
  // The task's current execution (the claim's, while running).
  currentExecutionId?: string | null;
  onOpenBuild?: (buildId: string) => void;
}

/** How the execution ended, or what became of its claim while it runs on. */
function executionState(execution: Execution): { text: string; tone: string } {
  if (execution.ended_at) {
    const tone =
      execution.outcome === "completed"
        ? "text-green-700 dark:text-green-400"
        : execution.outcome === "failed" || execution.outcome === "lost"
          ? "text-red-700 dark:text-red-400"
          : "text-gray-600 dark:text-gray-400";
    return { text: `ended ${execution.outcome ?? ""}`.trim(), tone };
  }
  if (!execution.claim_outcome) {
    return {
      text: "running, holds the claim",
      tone: "text-blue-700 dark:text-blue-300",
    };
  }
  return {
    // The claim moved on without a report of this execution's end: it may
    // still be running (an execution ref is not a claim).
    text: `no end reported; claim ${execution.claim_outcome.replace("_", " ")}`,
    tone: "text-amber-800 dark:text-amber-300",
  };
}

/**
 * Every execution of a task across builds, newest first
 * (`GET /tasks/{id}/executions?include_ended=true`): which build and plan,
 * on which executor and call, how long, and how it ended. The Modal ids
 * are one click away, verbatim.
 */
export function TaskExecutions({
  executions,
  currentBuildId,
  currentExecutionId,
  onOpenBuild,
}: TaskExecutionsProps) {
  if (executions.length === 0) {
    return (
      <p className="text-xs text-gray-500 dark:text-gray-400">
        No execution recorded: no build has claimed this task.
      </p>
    );
  }
  return (
    <div className="space-y-2">
      <ul className="space-y-2">
        {executions.map((execution) => {
          const state = executionState(execution);
          const worker = workerOf(execution);
          const executor = executorOf(execution);
          return (
            <li
              key={execution.id}
              aria-label={`Execution ${execution.id}`}
              className="rounded-md border border-gray-200 px-2.5 py-2 text-xs text-gray-700 dark:border-gray-700 dark:text-gray-300"
            >
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                {executor ? (
                  <ExecutorBadge
                    executor={executor}
                    executorRef={execution.executor_ref}
                  />
                ) : (
                  <span className="text-gray-500 dark:text-gray-400">no executor</span>
                )}
                {worker && <span title="Worker">{worker}</span>}
                <span className={state.tone}>{state.text}</span>
                {execution.id === currentExecutionId && (
                  <span className="rounded bg-blue-100 px-1.5 py-0.5 text-[11px] font-medium text-blue-800 dark:bg-blue-900/40 dark:text-blue-300">
                    current
                  </span>
                )}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-gray-500 dark:text-gray-400">
                <span>
                  Build{" "}
                  {execution.build_id === currentBuildId ? (
                    <span title={execution.build_id}>this build</span>
                  ) : onOpenBuild ? (
                    <button
                      type="button"
                      onClick={() => onOpenBuild(execution.build_id)}
                      title={`Go to build ${execution.build_id}`}
                      className="rounded font-mono text-blue-700 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-300"
                    >
                      {shortBuildId(execution.build_id)}
                    </button>
                  ) : (
                    <code className="font-mono" title={execution.build_id}>
                      {shortBuildId(execution.build_id)}
                    </code>
                  )}
                  {execution.in_current_plan ? "" : " (older plan)"}
                </span>
                <span title={formatAbsoluteTime(execution.started_at)}>
                  started {formatRelativeTime(execution.started_at)}
                </span>
                <span>
                  {execution.ended_at ? "ran" : "running for"}{" "}
                  {formatDuration(execution.started_at, execution.ended_at)}
                </span>
              </div>
              {executor === "modal" && (
                <div className="mt-1 space-y-1">
                  <ModalExecutionCallRef
                    metadata={execution.executor_metadata}
                    executorRef={execution.executor_ref}
                  />
                  <ModalExecutionDetails
                    metadata={execution.executor_metadata}
                    executorRef={execution.executor_ref}
                  />
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {executions.length >= TASK_EXECUTION_LIMIT && (
        <p className="text-xs text-amber-800 dark:text-amber-300">
          Showing the newest {TASK_EXECUTION_LIMIT} executions; older ones are not
          listed.
        </p>
      )}
    </div>
  );
}
