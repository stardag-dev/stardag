import type { TaskStatus } from "../types/task";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { CLAIM_ACTION_LABELS, type ClaimAction } from "../utils/claims";

interface ClaimActionDialogProps {
  action: ClaimAction | null;
  taskName: string;
  taskId: string;
  /** The build whose event produced the task's current status. */
  ownerBuildId: string;
  /** The build the user is currently looking at, when there is one. */
  currentBuildId?: string;
  status: TaskStatus;
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Confirmation for a single cross-build claim remedy.
 *
 * The dialog names the build the action is addressed to, because that is
 * the part a user cannot infer: a task's status is environment-global, so
 * the build that owns the claim is frequently *not* the build on screen,
 * and acting on it changes state outside what the current page shows.
 */
export function ClaimActionDialog({
  action,
  taskName,
  taskId,
  ownerBuildId,
  currentBuildId,
  status,
  busy,
  error,
  onConfirm,
  onCancel,
}: ClaimActionDialogProps) {
  const shortBuild = ownerBuildId.slice(0, 8);
  const crossBuild = Boolean(currentBuildId && currentBuildId !== ownerBuildId);

  const target = (
    <>
      <span className="font-medium text-gray-900 dark:text-gray-100">{taskName}</span>{" "}
      <code className="rounded bg-gray-100 px-1 py-0.5 text-xs text-gray-700 dark:bg-gray-700 dark:text-gray-200">
        {taskId}
      </code>
    </>
  );

  const addressed = (
    <>
      under build{" "}
      <code className="rounded bg-gray-100 px-1 py-0.5 text-xs text-gray-700 dark:bg-gray-700 dark:text-gray-200">
        {shortBuild}
      </code>
      {crossBuild ? " — a different build from the one you are viewing" : ""}
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
        <>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            Records a retry for {target} {addressed}, moving it from <em>{status}</em>{" "}
            back to <em>pending</em> so any build that needs it can run it again.
          </p>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            This does not start anything on its own — a scheduler tick or a new build
            has to pick the task up.
          </p>
        </>
      ) : (
        <>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            Releases {target}&rsquo;s claim {addressed}, so that build retries it on its
            next tick.
          </p>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            Use this when the worker is gone but the claim was not released. If the
            worker is still running, a second one starts beside it.
          </p>
        </>
      )}
    </ConfirmDialog>
  );
}
