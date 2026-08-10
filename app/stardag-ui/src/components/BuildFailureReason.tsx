import type { BuildStatus } from "../types/task";

interface BuildFailureReasonProps {
  status: BuildStatus;
  /** `Build.latest_error_message` — the reason recorded on BUILD_FAILED. */
  message?: string | null;
}

/**
 * Why a failed build failed.
 *
 * Its own component, and not folded into the build header, because of when it
 * has to appear. The scheduling panel explains a build that is *stalled*, and
 * goes quiet the moment the build fails: failing runs `skip_blocked`, the
 * blocked tasks go terminal, and terminal tasks drop out of the frontier's
 * blocked-by query — so the list the panel renders empties exactly when the
 * user most wants it. The scheduler wrote the same explanation onto the
 * BUILD_FAILED event on its way out. This is that, kept on screen.
 *
 * Renders nothing unless the build is failed *and* a reason was recorded. Both
 * halves matter: a build resumed after failing is running again and must not
 * show the previous round's reason, and a server predating
 * `latest_error_message` omits the field rather than sending an empty one.
 *
 * The presence check trims. The server excludes blank reasons, so this is not
 * load-bearing against the current API — but the failure mode if it ever slips
 * (a heading reading "Why this build failed" above nothing) is worse than the
 * cost of one `trim()`, and this component is the last place that can tell.
 */
export function BuildFailureReason({ status, message }: BuildFailureReasonProps) {
  if (status !== "failed" || !message?.trim()) return null;

  return (
    <div
      role="alert"
      className="border-b border-red-200 bg-red-50 px-4 py-2 dark:border-red-900/60 dark:bg-red-950/30"
    >
      <p className="text-xs font-semibold text-red-900 dark:text-red-200">
        Why this build failed
      </p>
      {/* Pre-wrapped and never truncated: these reasons name the blocking
          task, the build that owns it and the remedy to run, and a truncated
          remedy is not a remedy. `break-words` so a bare task id cannot push
          the page sideways. */}
      <p className="mt-1 whitespace-pre-wrap break-words text-xs text-red-900/90 dark:text-red-100/90">
        {message}
      </p>
    </div>
  );
}
