import type { TaskStatus } from "../types/task";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import type { ClaimAction } from "../utils/claims";

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
          : "Release this task's execution claim"
      }
      destructive
      confirmLabel={action === "retry" ? "Reset to pending" : "Release claim"}
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
            Cancels {target} {addressed} — the build whose event put it into{" "}
            <em>{status}</em>, and therefore the build that owns its claim.
          </p>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            A task&rsquo;s status is environment-wide, so this one task denies its
            execution claim to <em>every</em> build that needs it until something
            releases it. Releasing frees the claim and any concurrency-limit slot it
            holds.
          </p>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            The server cannot stop anything: a worker whose task is cancelled here keeps
            going until it notices, and if it completes anyway, completed wins.
          </p>
        </>
      )}
    </ConfirmDialog>
  );
}
