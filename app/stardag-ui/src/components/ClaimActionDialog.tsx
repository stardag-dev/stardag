import type { TaskStatus } from "../types/task";
import { CLAIM_ACTION_LABELS, type ClaimAction } from "../utils/claims";
import { shortBuildId } from "../utils/ids";
import { ConfirmDialog } from "./ui/ConfirmDialog";

interface ClaimActionDialogProps {
  action: ClaimAction | null;
  taskName: string;
  taskId: string;
  // The build the action is addressed to: for a release, the one whose
  // plan holds the claim; for a reset, the plan's build.
  buildId: string;
  status: TaskStatus;
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Confirmation for one remedy on one task. A release is addressed to the
 * plan holding the claim, so it is honoured from any view; if the claim
 * moved in the meantime the server refuses (`not_claim_holder`) and the
 * refusal is shown as the error.
 */
export function ClaimActionDialog({
  action,
  taskName,
  taskId,
  buildId,
  status,
  busy,
  error,
  onConfirm,
  onCancel,
}: ClaimActionDialogProps) {
  const target = (
    <>
      <span className="font-medium text-gray-900 dark:text-gray-100">{taskName}</span>{" "}
      <code className="rounded bg-gray-100 px-1 py-0.5 text-xs text-gray-700 dark:bg-gray-700 dark:text-gray-200">
        {taskId}
      </code>
    </>
  );
  return (
    <ConfirmDialog
      isOpen={action !== null}
      title={
        action === "retry"
          ? "Reset this task to pending"
          : "Release this task's claim and let the build retry it"
      }
      destructive
      confirmLabel={CLAIM_ACTION_LABELS[action ?? "release"]}
      busyLabel={action === "retry" ? "Resetting…" : "Releasing…"}
      cancelLabel="Close"
      busy={busy}
      error={error}
      onConfirm={onConfirm}
      onCancel={onCancel}
      maxWidthClass="max-w-lg"
    >
      {action === "retry" ? (
        <p className="text-sm text-gray-600 dark:text-gray-400">
          Moves {target} from <em>{status}</em> back to <em>pending</em>, so any build
          whose plan holds it can run it again. This starts nothing on its own.
        </p>
      ) : (
        <>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            Cancels {target} as build{" "}
            <code className="text-xs">{shortBuildId(buildId)}</code>, the build holding
            its claim, releasing the claim so that build retries it on its next tick.
            Any workspace member may do this; it is recorded on the task&rsquo;s event
            log.
          </p>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            It stops nothing: if the worker is still running, it finds out at its next
            checkpoint, and a second execution may start beside it.
          </p>
        </>
      )}
    </ConfirmDialog>
  );
}
