import { useCallback, useEffect, useId, useRef, useState } from "react";
import { cancelBuild, completeBuild, failBuild } from "../api/tasks";
import { useAuth } from "../context/AuthContext";
import type { Build, BuildStatus } from "../types/task";
import { canOverrideStatus } from "../utils/builds";
import { ResultBanner } from "./ui/ResultBanner";

/** The overrides worth offering, and what each one is for. */
type OverrideAction = "complete" | "fail" | "cancel";

const ACTIONS: {
  action: OverrideAction;
  label: string;
  dot: string;
  /** What it does, in the confirmation. */
  effect: string;
}[] = [
  {
    action: "complete",
    label: "Mark completed",
    dot: "bg-green-500",
    effect:
      "Records this build as completed. Use it to reconcile a record that is " +
      "wrong — most often a build whose roots were finished by another build.",
  },
  {
    action: "fail",
    label: "Mark failed",
    dot: "bg-red-500",
    effect:
      "Records this build as failed. It does not reach the execution " +
      "backend, so anything already running carries on.",
  },
  {
    action: "cancel",
    label: "Cancel build",
    dot: "bg-gray-500",
    effect:
      "Records this build as cancelled. It does not reach the execution " +
      "backend, so anything already running carries on.",
  },
];

interface BuildOverrideSectionProps {
  buildId: string;
  environmentId: string;
  buildStatus: BuildStatus;
  /**
   * Whether the build still has executions running.
   *
   * Three values, not two. `"unknown"` is the scan still running or
   * failed, and it has to be distinguishable: treating it as `"none"`
   * silently withholds the warning that is the whole reason these two
   * controls share a dialog, and it withholds it in the state where the
   * operator has *least* information.
   */
  liveExecutions: "unknown" | "none" | "some";
  onChanged: (build: Build) => void;
}

/**
 * Change what the registry records about a build — and nothing else.
 *
 * The distinction this section has to land is the one the UI never made:
 * **an override edits the record, the stop command ends the work.** They
 * were previously two unrelated controls, a dropdown in the toolbar and
 * a panel below it, so the obvious-looking move on a build you wanted
 * stopped was to pick "Cancel" — which writes one BUILD_CANCELLED event
 * and nothing else, leaving every container running. Putting them in one
 * dialog, with the record on top and the work below, is what makes the
 * choice visible.
 *
 * The copy deliberately says nothing about what an override does to the
 * *claims*, in either direction. That behaviour has now changed twice
 * under this file: before STA-81 a cancel released none of them, and
 * since STA-81 both cancel and fail release them all. Each time, copy
 * that named the claims went stale the moment the server moved, and
 * once it went stale in the worst way — asserting the opposite of the
 * truth about a destructive action.
 *
 * What does not move is the fact the decision actually turns on: an
 * override edits the record and does not stop what is running. That is
 * true on both sides of every change so far, so it is what is said
 * here, and the claim semantics are left to the CLI docs that own
 * them.
 */
export function BuildOverrideSection({
  buildId,
  environmentId,
  buildStatus,
  liveExecutions,
  onChanged,
}: BuildOverrideSectionProps) {
  const { user } = useAuth();
  // The one async mutation in this view. Every fetch around it takes an
  // epoch; this takes the simpler equivalent, because the component is
  // remounted on any change of identity — so "still mounted" is exactly
  // "still the same build and environment".
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const [pending, setPending] = useState<OverrideAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const headingId = useId();

  const confirm = useCallback(async () => {
    if (!pending) return;
    setBusy(true);
    setError(null);
    const userId = user?.profile?.sub;
    try {
      const updated =
        pending === "cancel"
          ? await cancelBuild(buildId, environmentId, userId)
          : pending === "complete"
            ? await completeBuild(buildId, environmentId, userId)
            : await failBuild(buildId, environmentId, userId);
      if (!alive.current) return;
      setPending(null);
      onChanged(updated);
    } catch (err) {
      if (!alive.current) return;
      setError(err instanceof Error ? err.message : "The override failed");
    } finally {
      if (alive.current) setBusy(false);
    }
  }, [pending, buildId, environmentId, user?.profile?.sub, onChanged]);

  if (!canOverrideStatus(buildStatus)) return null;

  const chosen = ACTIONS.find((a) => a.action === pending) ?? null;

  return (
    // Named, so it is a landmark: this dialog has two halves that do
    // different things to different subjects, and a screen-reader user
    // navigating it should be able to tell which one they are in.
    <section aria-labelledby={headingId} className="space-y-2">
      <h3
        id={headingId}
        className="text-sm font-semibold text-gray-900 dark:text-gray-100"
      >
        Override the recorded status
      </h3>
      <p className="text-xs text-gray-600 dark:text-gray-400">
        This changes what the registry says about the build.{" "}
        <strong>It does not reach the execution backend</strong>, so nothing that is
        running stops.
      </p>

      {chosen === null ? (
        <div className="flex flex-wrap gap-2">
          {ACTIONS.map(({ action, label, dot }) => (
            <button
              key={action}
              type="button"
              onClick={() => {
                setError(null);
                setPending(action);
              }}
              className="inline-flex items-center gap-2 rounded-md border border-gray-300 px-2.5 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
            >
              <span className={`h-2 w-2 rounded-full ${dot}`} />
              {label}
            </button>
          ))}
        </div>
      ) : (
        <div className="space-y-2 rounded-md border border-gray-300 bg-gray-50 px-3 py-2 dark:border-gray-600 dark:bg-gray-900/40">
          <p className="text-xs text-gray-700 dark:text-gray-300">{chosen.effect}</p>

          {/* The whole point of the dialog: when there is work running,
              the override is almost certainly not what was wanted —
              and when we cannot yet tell, saying nothing would be the
              same as saying there is none. */}
          {chosen.action === "cancel" && liveExecutions === "unknown" && (
            <p className="text-xs font-medium text-amber-800 dark:text-amber-300">
              Whether this build still has executions running is not known yet — the
              list below has not finished loading, or could not be read. Cancelling here
              would not stop them either way. If anything may still be running, stop it
              with the command below rather than cancelling here.
            </p>
          )}
          {chosen.action === "cancel" && liveExecutions === "some" && (
            <p className="text-xs font-medium text-amber-800 dark:text-amber-300">
              This build still has executions running, and cancelling here will not stop
              them. The command below is the one that does: it ends the selected
              containers first and cancels the build afterwards. Reach for it instead —
              you do not need both.
            </p>
          )}

          {error && <ResultBanner tone="error">{error}</ResultBanner>}

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={confirm}
              disabled={busy}
              className="rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-50"
            >
              {busy ? `${chosen.label}…` : chosen.label}
            </button>
            <button
              type="button"
              onClick={() => {
                setPending(null);
                setError(null);
              }}
              disabled={busy}
              className="rounded-md border border-gray-300 px-2.5 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
            >
              Back
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
