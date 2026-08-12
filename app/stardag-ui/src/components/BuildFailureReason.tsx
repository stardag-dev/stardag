import { useState } from "react";
import type { BuildStatus } from "../types/task";
import { formatAbsoluteTime } from "../utils/time";

interface BuildFailureReasonProps {
  status: BuildStatus;
  /** `Build.latest_error_message` — the reason recorded on BUILD_FAILED. */
  message?: string | null;
  /** When the build reached its terminal state (`Build.completed_at`). */
  failedAt?: string | null;
  /**
   * True when every root this build asked for has since completed (see
   * `rootsSatisfied`). The reason is then a historical record rather than a
   * live problem, so it collapses instead of shouting.
   */
  superseded?: boolean;
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
 * `latest_error_message` omits the field rather than sending an empty one. The
 * presence check trims — a heading over nothing is worse than silence.
 *
 * **Two registers.** Live, it is a red banner: something needs attention. Once
 * the roots have completed anyway (`superseded`), the same text is a *record* —
 * why this build stopped, then — so it collapses behind a disclosure and is
 * dated. It is not dropped: the reason is the only account of why the build
 * stopped, and "did we ever understand what happened here?" is a question worth
 * being able to answer months later. But it must not read as a current claim,
 * least of all because the message embeds the status counts *as they were*,
 * which is precisely what makes a stale one misleading.
 */
export function BuildFailureReason({
  status,
  message,
  failedAt,
  superseded = false,
}: BuildFailureReasonProps) {
  const [open, setOpen] = useState(false);
  if (status !== "failed" || !message?.trim()) return null;

  const when = failedAt ? formatAbsoluteTime(failedAt) : null;

  // Pre-wrapped and never truncated: these reasons name the blocking task, the
  // build that owns it and the remedy to run, and a truncated remedy is not a
  // remedy. `break-words` so a bare task id cannot push the page sideways.
  const body = (
    <p className="mt-1 whitespace-pre-wrap break-words text-xs text-gray-700 dark:text-gray-300">
      {message}
    </p>
  );

  if (superseded) {
    return (
      <div className="border-b border-gray-200 bg-gray-50 px-4 py-1.5 dark:border-gray-700 dark:bg-gray-800/50">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex items-center gap-1.5 rounded text-xs text-gray-600 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-gray-400 dark:hover:text-gray-100"
        >
          <svg
            className={`h-3 w-3 transition-transform ${open ? "rotate-90" : ""}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M9 5l7 7-7 7"
            />
          </svg>
          <span>Why it was recorded as failed{when ? ` on ${when}` : ""}</span>
        </button>
        {open && body}
      </div>
    );
  }

  return (
    <div
      role="alert"
      className="border-b border-red-200 bg-red-50 px-4 py-2 dark:border-red-900/60 dark:bg-red-950/30"
    >
      <p className="text-xs font-semibold text-red-900 dark:text-red-200">
        Why this build failed
      </p>
      <p className="mt-1 whitespace-pre-wrap break-words text-xs text-red-900/90 dark:text-red-100/90">
        {message}
      </p>
    </div>
  );
}
