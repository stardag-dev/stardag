import { useCallback, useState } from "react";
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
      "Records this build as failed. Nothing else changes: it does not stop " +
      "anything and it does not release any claim.",
  },
  {
    action: "cancel",
    label: "Cancel build",
    dot: "bg-gray-500",
    effect:
      "Records this build as cancelled and releases its execution claims. " +
      "Anything already running keeps running, and once the claims are gone " +
      "another build may take those tasks over.",
  },
];

interface BuildOverrideSectionProps {
  buildId: string;
  environmentId: string;
  buildStatus: BuildStatus;
  /** Whether anything is still running, which changes the advice given. */
  hasLiveExecutions: boolean;
  onChanged: (build: Build) => void;
}

/**
 * Change what the registry records about a build — and nothing else.
 *
 * The distinction this section has to land is the one the UI never made:
 * **an override edits the record, the stop command ends the work.** They
 * were previously two unrelated controls, a dropdown in the toolbar and
 * a panel below it, so the obvious-looking move on a build you wanted
 * stopped was to pick "Cancel" — which releases the claims, leaves every
 * container running, and invites a neighbouring build to pick the tasks
 * up. Putting them in one dialog, with the record on top and the work
 * below, is what makes the choice visible.
 */
export function BuildOverrideSection({
  buildId,
  environmentId,
  buildStatus,
  hasLiveExecutions,
  onChanged,
}: BuildOverrideSectionProps) {
  const { user } = useAuth();
  const [pending, setPending] = useState<OverrideAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
      setPending(null);
      onChanged(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "The override failed");
    } finally {
      setBusy(false);
    }
  }, [pending, buildId, environmentId, user?.profile?.sub, onChanged]);

  if (!canOverrideStatus(buildStatus)) return null;

  const chosen = ACTIONS.find((a) => a.action === pending) ?? null;

  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
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
              the override is almost certainly not what was wanted. */}
          {chosen.action === "cancel" && hasLiveExecutions && (
            <p className="text-xs font-medium text-amber-800 dark:text-amber-300">
              This build still has executions running. If you want those stopped too, do
              not cancel here — use the command below instead. It stops the calls first
              and then cancels the build for you, so this override is not needed as
              well.
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
