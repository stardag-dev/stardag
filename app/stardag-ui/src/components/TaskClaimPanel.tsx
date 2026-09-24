import { useCallback, useState } from "react";
import { cancelMember, retryMember } from "../api/registry";
import type { Execution, Task } from "../types/task";
import {
  availableClaimActions,
  CLAIM_ACTION_LABELS,
  claimState,
  type ClaimAction,
} from "../utils/claims";
import { formatAbsoluteTime, formatRelativeTime } from "../utils/time";
import { ClaimActionDialog } from "./ClaimActionDialog";
import { CopyButton, ModalExecutionCallRef } from "./ModalExecution";
import { StatusBadge } from "./StatusBadge";

interface TaskClaimPanelProps {
  task: Task;
  environmentId: string;
  // The viewed build and its active plan, when the task is opened from one;
  // the remedies go through that plan.
  buildId?: string;
  planId?: string | null;
  // The build's execution named by `task.execution_id`, when it has one.
  currentExecution?: Execution | null;
  onChanged: () => void;
}

/**
 * The task's global status and its claim: live or lapsed, until when, by
 * which execution — and, from a build, the two remedies.
 *
 * The claim names a plan on the server (`claim_plan_id`), but the task
 * read does not return it; the plan is known here only when the current
 * execution is one of the viewed build's.
 */
export function TaskClaimPanel({
  task,
  environmentId,
  buildId,
  planId,
  currentExecution,
  onChanged,
}: TaskClaimPanelProps) {
  const [action, setAction] = useState<ClaimAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const claim = claimState(task);
  const actions = buildId && planId ? availableClaimActions(task.status, claim) : [];

  const confirm = useCallback(async () => {
    if (!action || !planId) return;
    setBusy(true);
    setError(null);
    try {
      const result =
        action === "release"
          ? await cancelMember(planId, task.task_id, environmentId)
          : await retryMember(planId, task.task_id, environmentId);
      setNotice(
        result.applied
          ? action === "release"
            ? "Released the claim."
            : "Reset to pending."
          : `Nothing changed: the task is ${result.status}.`,
      );
      setAction(null);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The action failed");
    } finally {
      setBusy(false);
    }
  }, [action, planId, task.task_id, environmentId, onChanged]);

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={task.status} />
        {task.status_at && (
          <span
            className="text-xs text-gray-500 dark:text-gray-400"
            title={formatAbsoluteTime(task.status_at)}
          >
            since {formatRelativeTime(task.status_at)}
          </span>
        )}
      </div>
      {notice && (
        <p role="status" className="text-xs text-green-700 dark:text-green-400">
          {notice}
        </p>
      )}

      {claim !== "none" && (
        <div className="rounded-md border border-gray-200 px-2.5 py-2 text-xs text-gray-700 dark:border-gray-700 dark:text-gray-300">
          <p>
            {claim === "live" ? (
              <>
                Claim live until{" "}
                <span title={formatAbsoluteTime(task.claim_expires_at)}>
                  {formatAbsoluteTime(task.claim_expires_at)}
                </span>
                .
              </>
            ) : (
              <>
                Claim <strong>lapsed</strong>
                {task.claim_expires_at
                  ? ` ${formatRelativeTime(task.claim_expires_at)}`
                  : ""}
                : the next claiming start takes it over.
              </>
            )}
          </p>
          {task.execution_id && (
            <div className="mt-1 flex flex-wrap items-center gap-1">
              <span className="text-gray-500 dark:text-gray-400">Execution</span>
              <code className="font-mono">{task.execution_id}</code>
              <CopyButton text={task.execution_id} />
            </div>
          )}
          {currentExecution && (
            <div className="mt-1 space-y-1">
              <p>
                Under{" "}
                {currentExecution.in_current_plan
                  ? "this build's active plan"
                  : "an older plan of this build (an orphan)"}
                .
              </p>
              <ModalExecutionCallRef
                metadata={currentExecution.executor_metadata}
                executorRef={currentExecution.executor_ref}
              />
            </div>
          )}
        </div>
      )}

      {actions.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {actions.map((a) => (
            <button
              key={a}
              type="button"
              disabled={busy}
              onClick={() => {
                setError(null);
                setNotice(null);
                setAction(a);
              }}
              className="rounded-md border border-gray-300 px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
            >
              {CLAIM_ACTION_LABELS[a]}…
            </button>
          ))}
        </div>
      )}

      {buildId && (
        <ClaimActionDialog
          action={action}
          taskName={task.task_name}
          taskId={task.task_id}
          buildId={buildId}
          status={task.status}
          busy={busy}
          error={error}
          onConfirm={confirm}
          onCancel={() => {
            setAction(null);
            setError(null);
          }}
        />
      )}
    </div>
  );
}
