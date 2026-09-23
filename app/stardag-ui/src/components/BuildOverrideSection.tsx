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
    effect: "Records completion.",
  },
  {
    action: "fail",
    label: "Mark failed",
    dot: "bg-red-500",
    effect: "Releases the claims and skips the tasks blocked behind the failure.",
  },
  {
    action: "cancel",
    label: "Cancel build",
    dot: "bg-gray-500",
    effect: "Releases the build's claims; other builds may take its tasks over.",
  },
];

interface BuildOverrideSectionProps {
  buildId: string;
  environmentId: string;
  buildStatus: BuildStatus;
  /**
   * Whether the build holds any execution claim.
   *
   * Binary because the caller answers it from the build's own task list,
   * which is complete and build-scoped. It was briefly three-valued when
   * it was inferred from the stop scan, which could be stale, truncated,
   * or blind to a suspended claim — an instrument that could not answer
   * the question being asked of it.
   */
  holdsClaims: boolean;
  /**
   * Whether any of those claims has a running execution behind it.
   *
   * A separate question from `holdsClaims`, and the two genuinely
   * differ: a SUSPENDED task holds a claim with nothing running. Using
   * one for both is how a suspended-only build gets told it has "tasks
   * running" and is sent to a stop command with nothing to stop.
   *
   * Three-valued where `holdsClaims` is binary, because the two are
   * answered by different sources: claims come from the build's own
   * complete task list, while this comes from the stop scan, which has
   * not answered yet on first paint and may fail. For a *warning*,
   * "not known" has to behave like "maybe" — withholding it while
   * unsure is the one direction that costs something.
   */
  runningExecutions: "unknown" | "none" | "some";
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
 * dialog — the work first, the record after it — is what makes the
 * choice visible.
 *
 * **The copy names the claims exactly where an action changes them**,
 * and nowhere else. That rule replaced an earlier one — say nothing
 * about claims at all — which was right only while the behaviour was in
 * flight: before STA-81 a cancel released none of them, and since
 * STA-81 both cancel and fail release them all, so for a day no
 * sentence was true on both sides and silence was the only honest
 * option. The behaviour has settled, so accuracy is now the better
 * constraint, and a test pins each sentence to it.
 *
 * What still does not move, and leads every description here: an
 * override edits the record and does not stop what is running.
 */
export function BuildOverrideSection({
  buildId,
  environmentId,
  buildStatus,
  holdsClaims,
  runningExecutions,
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
    // Re-checked here, not only when the list was drawn. The scan
    // refreshes underneath an open confirmation, so a task can start
    // between choosing "Mark completed" and confirming it — and that is
    // the one override that would strand its claim.
    if (pending === "complete" && holdsClaims) {
      setError(
        "This build holds execution claims again, and Mark completed is the " +
          "one outcome that releases none. Close and choose Cancel build or " +
          "Mark failed.",
      );
      return;
    }
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
  }, [pending, holdsClaims, buildId, environmentId, user?.profile?.sub, onChanged]);

  if (!canOverrideStatus(buildStatus)) return null;

  const chosen = ACTIONS.find((a) => a.action === pending) ?? null;

  // "Mark completed" is the one terminal override that releases nothing
  // (STA-103), so offering it while the build holds any claim invites
  // stranding every one of them until expiry.
  //
  // Keyed on claims, not on running work: a SUSPENDED task holds a claim
  // with nothing running behind it, and it is the claim that completion
  // fails to release. `runningExecutions` answers the other question and
  // gates the cancel warning, which is about work that a stop command
  // could end.
  const completedIsSafe = !holdsClaims;
  const offered = ACTIONS.filter((a) => a.action !== "complete" || completedIsSafe);

  return (
    // Named, so it is a landmark: this dialog has two halves that do
    // different things to different subjects, and a screen-reader user
    // navigating it should be able to tell which one they are in.
    <section aria-labelledby={headingId} className="space-y-2">
      <h3
        id={headingId}
        className="text-sm font-semibold text-gray-900 dark:text-gray-100"
      >
        Record an outcome instead
      </h3>
      <p className="text-xs text-gray-600 dark:text-gray-400">
        For a build that will not finish on its own. Changes the record only; nothing
        running is stopped.
      </p>

      {chosen === null ? (
        <div className="flex flex-wrap gap-2">
          {offered.map(({ action, label, dot }) => (
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
          {!completedIsSafe && (
            <p className="w-full text-xs text-gray-500 dark:text-gray-400">
              Mark completed is not offered while this build holds execution claims: it
              is the one outcome that releases no claims.
            </p>
          )}
        </div>
      ) : (
        <div className="space-y-2 rounded-md border border-gray-300 bg-gray-50 px-3 py-2 dark:border-gray-600 dark:bg-gray-900/40">
          <p className="text-xs text-gray-700 dark:text-gray-300">{chosen.effect}</p>

          {/* The whole point of the dialog: when there is work running,
              the override is almost certainly not what was wanted —
              and when we cannot yet tell, saying nothing would be the
              same as saying there is none. */}
          {/* Names the command rather than pointing at it. Every state
              that produces "unknown" is a state in which the stop
              section above rendered a notice instead of its children,
              so there is no command on screen to point at. */}
          {chosen.action === "cancel" && runningExecutions === "unknown" && (
            <p className="text-xs font-medium text-amber-800 dark:text-amber-300">
              Whether this build has executions running is not known yet. If any are,
              cancelling here will not stop them.{" "}
              <code>stardag builds stop {buildId}</code> ends them first and cancels the
              build afterwards.
            </p>
          )}
          {/* Names the command, like the "unknown" copy above, rather
              than pointing at the stop section. That section draws the
              command for most of this state but not all of it: ticking
              rows and then filtering them out leaves it explaining that
              no command is offered, and a warning that points at a place
              has to be right about every state that reaches it. */}
          {chosen.action === "cancel" && runningExecutions === "some" && (
            <p className="text-xs font-medium text-amber-800 dark:text-amber-300">
              This build still has executions running, and cancelling here will not stop
              them. <code>stardag builds stop {buildId}</code> is the command that does:
              it ends the containers first and cancels the build afterwards. Reach for
              it instead — you do not need both.
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
