import type { Execution, TaskStatus } from "../types/task";
import { shortTaskId } from "../utils/ids";
import { qualifiedName } from "../utils/instances";
import { modalFunctionCallUrl, modalFunctionUrl } from "../utils/modalLinks";
import { notStoppableReason, workerOf } from "../utils/stoppable";
import { formatAbsoluteTime, formatDuration } from "../utils/time";
import { StatusBadge } from "./StatusBadge";
import { Checkbox } from "./ui/Checkbox";
import { Tooltip } from "./ui/Tooltip";

/** What the build's plan says about a task: its name and global status. */
export interface ExecutionTaskInfo {
  namespace: string;
  name: string;
  status: TaskStatus;
}

/** How many rows the table draws; the rest are counted, never dropped. */
export const MAX_ROWS_DRAWN = 50;

interface ExecutionTableProps {
  executions: Execution[];
  // With ticks: the stop list. Keyed by task id, as `--task-id` is.
  ticked?: Set<string>;
  onToggle?: (taskId: string, on: boolean) => void;
  onOpenTask?: (taskId: string) => void;
  // Task names and statuses by task id, from the build's active plan. A
  // task it does not hold (an orphan's) falls back to its short id.
  taskInfo?: Map<string, ExecutionTaskInfo>;
}

function OrphanBadge({ execution }: { execution: Execution }) {
  if (execution.in_current_plan) {
    return <span className="text-gray-500 dark:text-gray-400">current</span>;
  }
  return (
    <Tooltip content="Orphaned: its plan is not the build's active plan. It started under code or settings the build has since rolled over from.">
      <span className="rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
        orphan
      </span>
    </Tooltip>
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
    <Tooltip
      content={`Claim closed ${formatAbsoluteTime(
        execution.claim_released_at,
      )}; no end reported by the execution itself`}
    >
      <span
        className={
          execution.claim_outcome === "taken_over" ||
          execution.claim_outcome === "lapsed"
            ? "text-amber-800 dark:text-amber-300"
            : undefined
        }
      >
        claim {execution.claim_outcome.replace("_", " ")}
      </span>
    </Tooltip>
  );
}

/**
 * Executions, one row each: which task and its status, under which plan
 * (orphans marked), on which worker and call, since when, and what became
 * of the claim.
 *
 * Boxes reflect `ticked` literally, so none are checked until someone
 * ticks one — and no tick at all means the command targets every row
 * (v1's choice: rendering them all checked would make the first click
 * read as "not that one" while it means "only that one").
 */
export function ExecutionTable({
  executions,
  ticked,
  onToggle,
  onOpenTask,
  taskInfo,
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
            <th className="py-1 pr-2 font-medium">Status</th>
            <th className="py-1 pr-2 font-medium">Plan</th>
            <th className="py-1 pr-2 font-medium">Worker</th>
            <th className="py-1 pr-2 font-medium">Call</th>
            <th className="py-1 pr-2 font-medium">Running for</th>
            <th className="py-1 font-medium">Claim</th>
          </tr>
        </thead>
        <tbody>
          {drawn.map((execution) => {
            const info = taskInfo?.get(execution.task_id);
            const name = info
              ? qualifiedName(info.namespace, info.name)
              : shortTaskId(execution.task_id);
            // modalFunctionCallUrl falls back to the coarser app page when
            // function_id is missing (other callers rely on that shared
            // fallback), but a link rendered here as the call ref itself
            // must never resolve to the app page instead — so gate on the
            // function being addressable, same as ModalExecutionCallRef.
            const callUrl = modalFunctionUrl(execution.executor_metadata)
              ? modalFunctionCallUrl(
                  execution.executor_metadata,
                  execution.executor_ref,
                )
              : null;
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
                      label={`Include ${name}`}
                    />
                  </td>
                )}
                <td className="py-1 pr-2">
                  {onOpenTask ? (
                    <button
                      type="button"
                      onClick={() => onOpenTask(execution.task_id)}
                      title={execution.task_id}
                      className={`text-blue-700 hover:underline dark:text-blue-300 ${
                        info ? "font-medium" : "font-mono"
                      }`}
                    >
                      {name}
                    </button>
                  ) : (
                    <span
                      title={execution.task_id}
                      className={info ? "font-medium" : "font-mono"}
                    >
                      {name}
                    </span>
                  )}
                </td>
                <td className="py-1 pr-2">
                  {info ? <StatusBadge status={info.status} /> : "—"}
                </td>
                <td className="py-1 pr-2">
                  <OrphanBadge execution={execution} />
                </td>
                <td className="py-1 pr-2 text-gray-700 dark:text-gray-300">
                  {workerOf(execution) ?? "—"}
                </td>
                <td className="py-1 pr-2">
                  {!execution.executor_ref ? (
                    <Tooltip content={notStoppableReason(execution) ?? undefined}>
                      <span className="text-[11px] text-amber-800 dark:text-amber-300">
                        not recorded yet
                      </span>
                    </Tooltip>
                  ) : callUrl ? (
                    <Tooltip content="Open this call in the Modal dashboard">
                      <a
                        href={callUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="font-mono text-[11px] text-blue-700 hover:underline dark:text-blue-300"
                      >
                        {execution.executor_ref}
                      </a>
                    </Tooltip>
                  ) : (
                    <code className="font-mono text-[11px]">
                      {execution.executor_ref}
                    </code>
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
          {selectable &&
            (ticked.size > 0
              ? // Ticking switches the command to exact task ids, so the
                // undrawn rows really are excluded; claiming otherwise errs
                // towards "everything is covered", the dangerous direction.
                " Ticked rows are named individually, so these are not included — clear the ticks to target the whole list."
              : " The command below still targets every one of them — narrow with the filters above to see a particular set.")}
        </p>
      )}
    </div>
  );
}
