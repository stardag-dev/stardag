import type { Execution } from "../types/task";
import { shortTaskId } from "../utils/ids";
import { modalFunctionCallUrl } from "../utils/modalLinks";
import { notStoppableReason, workerOf } from "../utils/stoppable";
import { formatAbsoluteTime, formatDuration } from "../utils/time";
import { Checkbox } from "./ui/Checkbox";

/** How many rows the table draws; the rest are counted, never dropped. */
export const MAX_ROWS_DRAWN = 50;

interface ExecutionTableProps {
  executions: Execution[];
  // With ticks: the stop list. Keyed by task id, as `--task-id` is.
  ticked?: Set<string>;
  onToggle?: (taskId: string, on: boolean) => void;
  onOpenTask?: (taskId: string) => void;
}

function OrphanBadge({ execution }: { execution: Execution }) {
  if (execution.in_current_plan) {
    return <span className="text-gray-500 dark:text-gray-400">current</span>;
  }
  return (
    <span
      title="Orphaned: its plan is not the build's active plan. It started under code or settings the build has since rolled over from."
      className="rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"
    >
      orphan
    </span>
  );
}

/** What happened to the claim, and whether the execution reported an end. */
function ClaimCell({ execution }: { execution: Execution }) {
  if (execution.ended_at) {
    return (
      <span title={formatAbsoluteTime(execution.ended_at)}>
        ended {execution.outcome ?? ""}
      </span>
    );
  }
  if (!execution.claim_outcome) return <span>holds the claim</span>;
  return (
    <span
      title={`Claim closed ${formatAbsoluteTime(execution.claim_released_at)}; no end reported by the execution itself`}
      className={
        execution.claim_outcome === "taken_over" || execution.claim_outcome === "lapsed"
          ? "text-amber-800 dark:text-amber-300"
          : undefined
      }
    >
      claim {execution.claim_outcome.replace("_", " ")}
    </span>
  );
}

/**
 * Executions, one row each: which task, under which plan (orphans marked),
 * on which worker and call, since when, and what became of the claim.
 */
export function ExecutionTable({
  executions,
  ticked,
  onToggle,
  onOpenTask,
}: ExecutionTableProps) {
  if (executions.length === 0) {
    return (
      <p className="text-xs text-gray-600 dark:text-gray-400">No execution to list.</p>
    );
  }
  const drawn = executions.slice(0, MAX_ROWS_DRAWN);
  const undrawn = executions.length - drawn.length;
  const selectable = ticked !== undefined && onToggle !== undefined;
  return (
    <div className="max-h-80 overflow-y-auto">
      <table className="w-full text-left text-xs">
        <thead className="text-gray-600 dark:text-gray-400">
          <tr>
            {selectable && (
              <th className="w-6 py-1 pr-2 font-medium">
                <span className="sr-only">Include</span>
              </th>
            )}
            <th className="py-1 pr-2 font-medium">Task</th>
            <th className="py-1 pr-2 font-medium">Plan</th>
            <th className="py-1 pr-2 font-medium">Worker</th>
            <th className="py-1 pr-2 font-medium">Call</th>
            <th className="py-1 pr-2 font-medium">Running for</th>
            <th className="py-1 font-medium">Claim</th>
          </tr>
        </thead>
        <tbody>
          {drawn.map((execution) => {
            const callUrl = modalFunctionCallUrl(
              execution.executor_metadata,
              execution.executor_ref,
            );
            return (
              <tr
                key={execution.id}
                className={`border-t border-gray-200 dark:border-gray-700 ${
                  execution.in_current_plan ? "" : "bg-amber-50/60 dark:bg-amber-950/20"
                }`}
              >
                {selectable && (
                  <td className="py-1 pr-2">
                    <Checkbox
                      checked={ticked.has(execution.task_id)}
                      onChange={(on) => onToggle(execution.task_id, on)}
                      label={`Include task ${shortTaskId(execution.task_id)}`}
                    />
                  </td>
                )}
                <td className="py-1 pr-2">
                  {onOpenTask ? (
                    <button
                      type="button"
                      onClick={() => onOpenTask(execution.task_id)}
                      title={execution.task_id}
                      className="font-mono text-blue-700 hover:underline dark:text-blue-300"
                    >
                      {shortTaskId(execution.task_id)}
                    </button>
                  ) : (
                    <code title={execution.task_id} className="font-mono">
                      {shortTaskId(execution.task_id)}
                    </code>
                  )}
                </td>
                <td className="py-1 pr-2">
                  <OrphanBadge execution={execution} />
                </td>
                <td className="py-1 pr-2 text-gray-700 dark:text-gray-300">
                  {workerOf(execution) ?? "—"}
                </td>
                <td className="py-1 pr-2">
                  {!execution.executor_ref ? (
                    <span
                      className="text-[11px] text-amber-800 dark:text-amber-300"
                      title={notStoppableReason(execution) ?? undefined}
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
                      {execution.executor_ref}
                    </a>
                  ) : (
                    <code className="font-mono text-[11px]">{execution.executor_ref}</code>
                  )}
                </td>
                <td
                  className="py-1 pr-2 text-gray-700 dark:text-gray-300"
                  title={formatAbsoluteTime(execution.started_at)}
                >
                  {formatDuration(execution.started_at, execution.ended_at)}
                </td>
                <td className="py-1 text-gray-700 dark:text-gray-300">
                  <ClaimCell execution={execution} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {undrawn > 0 && (
        <p className="px-1 py-1.5 text-xs text-gray-600 dark:text-gray-400">
          {undrawn} more execution{undrawn === 1 ? "" : "s"} not listed.
        </p>
      )}
    </div>
  );
}
