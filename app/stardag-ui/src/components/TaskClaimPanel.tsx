import { useCallback, useState } from "react";
import { cancelMember, retryMember } from "../api/registry";
import type { Execution, Task } from "../types/task";
import {
  availableClaimActions,
  CLAIM_ACTION_LABELS,
  claimState,
  type ClaimAction,
  type ClaimState,
} from "../utils/claims";
import { shortBuildId } from "../utils/ids";
import { executorOf } from "../utils/stoppable";
import { formatAbsoluteTime, formatDuration, formatRelativeTime } from "../utils/time";
import { ClaimActionDialog } from "./ClaimActionDialog";
import { Modal } from "./Modal";
import {
  CopyButton,
  ModalExecutionCallRef,
  ModalExecutionDetails,
} from "./ModalExecution";
import { StatusBadge } from "./StatusBadge";

interface TaskClaimPanelProps {
  task: Task;
  environmentId: string;
  // The viewed build and its active plan, when the task is opened from one.
  // A reset goes through that plan; a release never does (see below).
  buildId?: string;
  planId?: string | null;
  // The execution named by `task.execution_id`, from the task's executions.
  currentExecution?: Execution | null;
  onChanged: () => void;
  // Jump to another build (the claim holder); omitted where no navigation
  // is wired, and the holder is then named without a link.
  onOpenBuild?: (buildId: string) => void;
}

/**
 * The task's global status and its claim. The pane keeps one line — "Claim
 * live until …" or "Claim lapsed at …" — and a Manage button; the "Claim"
 * modal holds the rest: **which build holds it**, its plan, the execution
 * and its executor ids, the expiry, and the remedies.
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

  const [manageOpen, setManageOpen] = useState(false);
  // The Modal pieces only for a Modal execution, as in the executions list.
  const isModal = currentExecution ? executorOf(currentExecution) === "modal" : false;
  const claimActions = actions.length > 0 && claim !== "none";

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
      setAction(null);
      if (result.applied) {
        // The claim modal closes and the line updates from the re-read;
        // a reset outside a claim says what it did in the pane.
        if (manageOpen) setManageOpen(false);
        else setNotice(`Reset to pending${under}.`);
      } else {
        setNotice(`Nothing changed: the task is ${result.status}.`);
      }
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The action failed");
    } finally {
      setBusy(false);
    }
  }, [
    action,
    targetPlanId,
    targetBuildId,
    task.task_id,
    environmentId,
    onChanged,
    manageOpen,
  ]);

  const actionButtons = (
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
          className={SMALL_BUTTON}
        >
          {CLAIM_ACTION_LABELS[a]}…
        </button>
      ))}
    </div>
  );

  const noticeLine = notice && (
    <p role="status" className="text-xs text-green-700 dark:text-green-400">
      {notice}
    </p>
  );

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

      {claim !== "none" && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-gray-700 dark:text-gray-300">
          <span>{claimLine(claim, task.claim_expires_at)}</span>
          <button
            type="button"
            onClick={() => {
              setNotice(null);
              setManageOpen(true);
            }}
            className={SMALL_BUTTON}
          >
            Manage
          </button>
        </div>
      )}

      {!manageOpen && noticeLine}
      {claim === "none" && actions.length > 0 && actionButtons}

      <Modal
        isOpen={manageOpen}
        onClose={() => setManageOpen(false)}
        title="Claim"
        maxWidthClass="max-w-xl"
      >
        <div className="space-y-3 text-sm text-gray-700 dark:text-gray-300">
          <p>
            {claimLine(claim, task.claim_expires_at)}
            {claim === "lapsed" ? ": the next claiming start takes it over." : "."}
          </p>
          <dl className="grid grid-cols-[max-content_1fr] items-baseline gap-x-4 gap-y-1">
            <dt className={DT}>Held by</dt>
            <dd>
              {holderBuildId ? (
                <>
                  build{" "}
                  {onOpenBuild ? (
                    <button
                      type="button"
                      onClick={() => {
                        setManageOpen(false);
                        onOpenBuild(holderBuildId);
                      }}
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
                  {viewingAnotherBuild ? " (not the build you are viewing)" : ""}
                  {heldFor && heldFor !== "—" ? (
                    <span title={formatAbsoluteTime(task.status_at)}>
                      , running {heldFor}
                    </span>
                  ) : null}
                </>
              ) : (
                // The holder's plan was deleted (the pointer is SET NULL):
                // the claim is there, the build is not. Naming the build in
                // view would invent the one fact that is missing.
                <>The build holding its claim is not recorded.</>
              )}
            </dd>
            {holderPlanId && (
              <>
                <dt className={DT}>Plan</dt>
                <dd className="flex items-center gap-1">
                  <code className="font-mono text-xs">{holderPlanId}</code>
                  <CopyButton text={holderPlanId} />
                </dd>
              </>
            )}
            {task.execution_id && (
              <>
                <dt className={DT}>Execution</dt>
                <dd className="flex items-center gap-1">
                  <code className="font-mono text-xs">{task.execution_id}</code>
                  <CopyButton text={task.execution_id} />
                </dd>
              </>
            )}
            {currentExecution && (
              <>
                <dt className={DT}>Executor</dt>
                <dd>
                  {executorOf(currentExecution) ?? "not recorded"}, under{" "}
                  {currentExecution.in_current_plan
                    ? "its build's active plan"
                    : "an older plan of its build (an orphan)"}
                </dd>
                <dt className={DT}>Started</dt>
                <dd>{formatAbsoluteTime(currentExecution.started_at)}</dd>
                {currentExecution.executor_ref && (
                  <>
                    <dt className={DT}>{isModal ? "Call ref" : "Executor ref"}</dt>
                    <dd className="text-xs">
                      {isModal ? (
                        <ModalExecutionCallRef
                          metadata={currentExecution.executor_metadata}
                          executorRef={currentExecution.executor_ref}
                        />
                      ) : (
                        <span className="flex items-center gap-1">
                          <code className="font-mono">
                            {currentExecution.executor_ref}
                          </code>
                          <CopyButton text={currentExecution.executor_ref} />
                        </span>
                      )}
                    </dd>
                  </>
                )}
              </>
            )}
            {task.claim_expires_at && (
              <>
                <dt className={DT}>Expires</dt>
                <dd>{formatAbsoluteTime(task.claim_expires_at)}</dd>
              </>
            )}
          </dl>
          {currentExecution && isModal && (
            <ModalExecutionDetails
              metadata={currentExecution.executor_metadata}
              executorRef={currentExecution.executor_ref}
            />
          )}
          {holderBuildId && (
            <p className="text-xs text-gray-500 dark:text-gray-400">
              To stop what is running, use Build controls &rarr; Stop on build{" "}
              {shortBuildId(holderBuildId)}.
            </p>
          )}
          {manageOpen && noticeLine}
          {claimActions && actionButtons}
        </div>
      </Modal>

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

const SMALL_BUTTON =
  "rounded-md border border-gray-300 px-2 py-0.5 text-xs font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700";
const DT = "text-gray-500 dark:text-gray-400";

/** The one line the pane keeps: `Claim live until …` / `Claim lapsed at …`. */
function claimLine(claim: ClaimState, expiresAt: string | null): string {
  if (claim === "live") return `Claim live until ${formatAbsoluteTime(expiresAt)}`;
  return expiresAt
    ? `Claim lapsed at ${formatAbsoluteTime(expiresAt)}`
    : "Claim lapsed";
}
