import { useCallback, useEffect, useId, useRef, useState } from "react";
import { cancelBuild, completeBuild, failBuild } from "../api/registry";
import type { Build, BuildStatus } from "../types/task";
import { canOverrideStatus } from "../utils/builds";
import { Checkbox } from "./ui/Checkbox";
import { ResultBanner } from "./ui/ResultBanner";

type OverrideAction = "complete" | "fail" | "cancel";

const ACTIONS: { action: OverrideAction; label: string; dot: string; effect: string }[] =
  [
    {
      action: "complete",
      label: "Mark completed",
      dot: "bg-green-500",
      effect:
        "Records completion and releases the build's claims. Refused while the " +
        "active plan has outstanding members, unless forced.",
    },
    {
      action: "fail",
      label: "Mark failed",
      dot: "bg-red-500",
      effect: "Records failure and releases the build's claims.",
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
  // Whether the build has executions with no end reported. "unknown"
  // behaves like "some": withholding the warning while unsure is the one
  // direction that costs something.
  runningExecutions: "unknown" | "none" | "some";
  onChanged: (build: Build) => void;
}

/**
 * Change what the registry records about a build — and nothing else.
 *
 * **An override edits the record; `builds stop` ends the work.** All three
 * outcomes release the build's claims (the server's terminal transition
 * does, uniformly), so another build may take a task over while the old
 * container still runs. That is why the dialog puts Stop first.
 *
 * Mark completed is refused (409 `plan_incomplete`) while the active plan
 * has outstanding members; `force` overrides that, but never a plan that
 * is not sealed or an excluded root.
 */
export function BuildOverrideSection({
  buildId,
  environmentId,
  buildStatus,
  runningExecutions,
  onChanged,
}: BuildOverrideSectionProps) {
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const [pending, setPending] = useState<OverrideAction | null>(null);
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const headingId = useId();

  const confirm = useCallback(async () => {
    if (!pending) return;
    setBusy(true);
    setError(null);
    try {
      const updated =
        pending === "cancel"
          ? await cancelBuild(buildId, environmentId)
          : pending === "complete"
            ? await completeBuild(buildId, environmentId, force)
            : await failBuild(buildId, environmentId);
      if (!alive.current) return;
      setPending(null);
      onChanged(updated);
    } catch (err) {
      if (!alive.current) return;
      setError(err instanceof Error ? err.message : "The override failed");
    } finally {
      if (alive.current) setBusy(false);
    }
  }, [pending, force, buildId, environmentId, onChanged]);

  if (!canOverrideStatus(buildStatus)) return null;
  const chosen = ACTIONS.find((a) => a.action === pending) ?? null;

  return (
    <section aria-labelledby={headingId} className="space-y-2">
      <h3 id={headingId} className="text-sm font-semibold text-gray-900 dark:text-gray-100">
        Record an outcome instead
      </h3>
      <p className="text-xs text-gray-600 dark:text-gray-400">
        For a build that will not finish on its own. Changes the record only; nothing
        running is stopped.
      </p>

      {chosen === null ? (
        <div className="flex flex-wrap gap-2">
          {ACTIONS.map(({ action, label, dot }) => (
            <button
              key={action}
              type="button"
              onClick={() => {
                setError(null);
                setForce(false);
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
          {chosen.action === "complete" && (
            <Checkbox
              checked={force}
              onChange={setForce}
              label="Force: complete over outstanding members"
              labelHidden={false}
            />
          )}
          {runningExecutions !== "none" && (
            <p className="text-xs font-medium text-amber-800 dark:text-amber-300">
              {runningExecutions === "some"
                ? "This build still has executions with no end reported, and this will not stop them."
                : "Whether this build has executions running is not known yet. If any are, this will not stop them."}{" "}
              <code>stardag builds stop {buildId}</code> ends them first and cancels the
              build afterwards.
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
