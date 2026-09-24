import { useCallback, useState } from "react";
import { cancelMember, retryMember } from "../api/registry";
import type { Execution, Task } from "../types/task";
import {
  availableClaimActions,
  CLAIM_ACTION_LABELS,
  claimState,
  type ClaimAction,
} from "../utils/claims";
import { shortBuildId } from "../utils/ids";
import { formatAbsoluteTime, formatDuration, formatRelativeTime } from "../utils/time";
import { ClaimActionDialog } from "./ClaimActionDialog";
import { CopyButton, ModalExecutionCallRef } from "./ModalExecution";
import { StatusBadge } from "./StatusBadge";

interface TaskClaimPanelProps {
  task: Task;
  environmentId: string;
  // The viewed build and its active plan, when the task is opened from one.
  // A reset goes through that plan; a release never does (see below).
  buildId?: string;
  planId?: string | null;
  // The build's execution named by `task.execution_id`, when it has one.
  currentExecution?: Execution | null;
  onChanged: () => void;
  // Jump to another build (the claim holder); omitted where no navigation
  // is wired, and the holder is then named without a link.
  onOpenBuild?: (buildId: string) => void;
}

/**
 * The task's global status and its claim: live or lapsed, until when, by
 * which execution, **held by which build** — and the remedies.
 *
 * The holder is `claim_plan_id` / `claim_build_id` on the task read. The
 * release is addressed to the holder's plan
 * (`POST /plans/{claim_plan_id}/members/{task_id}/cancel`), not to the
 * viewed build's, so it works from the task page and from another build's
 * view alike. It is not admin-gated: any workspace member may release a
 * claim, as with the CLI; the server is the authority and records the act
 * as an event. A reset goes through the viewed build's plan, or — with no
 * build in view — through the holder's plan for a lapsed claim.
 */
export function TaskClaimPanel({
  task,
  environmentId,
  buildId,
  planId,
  currentExecution,
  onChanged,
  onOpenBuild,
}: TaskClaimPanelProps) {
  const [action, setAction] = useState<ClaimAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const claim = claimState(task);
  const holderPlanId = claim !== "none" ? task.claim_plan_id : null;
  const holderBuildId = claim !== "none" ? task.claim_build_id : null;
  const viewingAnotherBuild = Boolean(
    holderBuildId && buildId && holderBuildId !== buildId,
  );
  const retryPlanId = planId ?? (claim === "lapsed" ? holderPlanId : null);
  const actions = availableClaimActions(task.status, claim).filter((a) =>
    a === "release" ? Boolean(holderPlanId) : Boolean(retryPlanId),
  );
  // The plan an action is addressed to, and that plan's build.
  const targetPlanId = action === "release" ? holderPlanId : retryPlanId;
  const targetBuildId =
    action === "release" ? holderBuildId : planId ? buildId ?? null : holderBuildId;
  const heldFor =
    claim !== "none" && task.status_at ? formatDuration(task.status_at, null) : null;

  const confirm = useCallback(async () => {
    if (!action || !targetPlanId) return;
    setBusy(true);
    setError(null);
    try {
      const result =
        action === "release"
          ? await cancelMember(targetPlanId, task.task_id, environmentId)
          : await retryMember(targetPlanId, task.task_id, environmentId);
      const under = targetBuildId ? ` under build ${shortBuildId(targetBuildId)}` : "";
      setNotice(
        result.applied
          ? action === "release"
            ? `Released the claim${under}.`
            : `Reset to pending${under}.`
          : `Nothing changed: the task is ${result.status}.`,
      );
      setAction(null);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The action failed");
    } finally {
      setBusy(false);
    }
  }, [action, targetPlanId, targetBuildId, task.task_id, environmentId, onChanged]);

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge
          status={task.status}
          holderBuildId={holderBuildId}
          currentBuildId={buildId}
          onOpenBuild={onOpenBuild}
        />
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
          <p className="mt-1">
            {holderBuildId ? (
              <>
                Running
                {heldFor && heldFor !== "—" ? (
                  <span title={formatAbsoluteTime(task.status_at)}> {heldFor}</span>
                ) : null}{" "}
                under build{" "}
                {onOpenBuild ? (
                  <button
                    type="button"
                    onClick={() => onOpenBuild(holderBuildId)}
                    title={`Go to build ${holderBuildId}`}
                    className="rounded font-mono text-blue-700 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-300"
                  >
                    {shortBuildId(holderBuildId)}
                  </button>
                ) : (
                  <code className="font-mono" title={holderBuildId}>
                    {shortBuildId(holderBuildId)}
                  </code>
                )}
                {viewingAnotherBuild ? " (not the build you are viewing)" : ""}, which
                holds its claim.
              </>
            ) : (
              // The holder's plan was deleted (the pointer is SET NULL):
              // the claim is there, the build is not. Naming the build in
              // view would invent the one fact that is missing.
              <>The build holding its claim is not recorded.</>
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
          {holderBuildId && (
            <p className="mt-1 text-gray-500 dark:text-gray-400">
              To stop what is running, use Build controls &rarr; Stop on build{" "}
              {shortBuildId(holderBuildId)}.
            </p>
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

      {targetBuildId && (
        <ClaimActionDialog
          action={action}
          taskName={task.task_name}
          taskId={task.task_id}
          buildId={targetBuildId}
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
