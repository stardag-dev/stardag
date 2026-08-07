import { useEffect, useMemo, useRef, useState } from "react";
import { bulkCancelBuilds } from "../api/tasks";
import type { BulkCancelBuildsRequest, BulkCancelBuildsResponse } from "../types/task";
import {
  formatAbsoluteTime,
  formatIdleThreshold,
  formatRelativeTime,
} from "../utils/time";
import { Checkbox } from "./ui/Checkbox";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { ResultBanner } from "./ui/ResultBanner";

// The API's per-build skip reasons, spelled out. Anything unrecognised
// falls back to the raw reason so a newly added server reason still shows
// rather than vanishing.
const SKIP_REASONS: Record<string, string> = {
  not_found: "Not found in this environment",
  not_running: "Already finished — only running builds can be cancelled",
  reactive: "Reactive build — tick “Include reactive builds” to cancel it",
  not_idle: "Active more recently than the idle threshold",
  limit_reached: "Batch limit reached — run the cleanup again to include it",
};

interface BulkCancelDialogProps {
  environmentId: string;
  /**
   * "selection" cancels an explicit set of build ids; "idle" is the
   * reaper sweep over everything quiet beyond a threshold. Both are the
   * same endpoint with a different filter.
   */
  mode: "selection" | "idle";
  /** mode="selection": the ids to cancel. */
  buildIds?: string[];
  /** mode="selection": id -> name, so skipped ids read as build names. */
  buildNames?: Record<string, string>;
  /** mode="idle": the idleness threshold, in seconds. */
  idleForSeconds?: number;
  /** mode="idle": restrict the sweep to one reactive app. */
  reactiveAppName?: string | null;
  onClose: () => void;
  onApplied: (result: BulkCancelBuildsResponse) => void;
}

/**
 * Confirm a bulk build cancellation by showing what it will do first.
 *
 * The endpoint has a `dry_run` that reports the exact selection a real
 * call would act on — builds, the task claims a cascade would release,
 * per-build skip reasons, truncation — so this dialog runs the dry run on
 * open (and again whenever an option changes) and only ever applies what
 * the user has just been shown. A destructive bulk action that reports its
 * consequences before committing is the entire point.
 *
 * Mount only while the dialog should be open: the component keeps its
 * options in local state and relies on unmounting to reset them.
 */
export function BulkCancelDialog({
  environmentId,
  mode,
  buildIds,
  buildNames,
  idleForSeconds,
  reactiveAppName,
  onClose,
  onApplied,
}: BulkCancelDialogProps) {
  const [cascade, setCascade] = useState(true);
  const [includeReactive, setIncludeReactive] = useState(false);
  const [reason, setReason] = useState("");

  const [preview, setPreview] = useState<BulkCancelBuildsResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(true);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);

  // Primitive-valued deps so the dry-run effect can't loop on a fresh
  // array/object identity from the parent's render.
  const buildIdsKey = (buildIds ?? []).join(",");
  const selectedIds = useMemo(
    () => (buildIdsKey ? buildIdsKey.split(",") : []),
    [buildIdsKey],
  );
  const sweepIdleSeconds = mode === "idle" ? idleForSeconds ?? null : null;
  const sweepApp = mode === "idle" ? reactiveAppName ?? null : null;

  // `reason` is deliberately absent: it does not change the selection, so
  // typing it must not re-run the dry run.
  const filters = useMemo<BulkCancelBuildsRequest>(
    () => ({
      ...(selectedIds.length > 0 ? { build_ids: selectedIds } : {}),
      ...(sweepIdleSeconds !== null ? { idle_for_seconds: sweepIdleSeconds } : {}),
      ...(sweepApp ? { reactive_app_name: sweepApp } : {}),
      include_reactive: includeReactive,
      cascade,
    }),
    [selectedIds, sweepIdleSeconds, sweepApp, includeReactive, cascade],
  );

  // Stale-response guard: an option toggled while a dry run is in flight
  // must not have the older answer land on top of the newer one.
  const previewEpochRef = useRef(0);

  useEffect(() => {
    const epoch = ++previewEpochRef.current;
    const fresh = () => previewEpochRef.current === epoch;
    setPreviewLoading(true);
    setPreviewError(null);
    bulkCancelBuilds({ ...filters, dry_run: true }, environmentId)
      .then((result) => {
        if (fresh()) setPreview(result);
      })
      .catch((err: unknown) => {
        if (!fresh()) return;
        setPreview(null);
        setPreviewError(
          err instanceof Error ? err.message : "Failed to preview the cleanup",
        );
      })
      .finally(() => {
        if (fresh()) setPreviewLoading(false);
      });
  }, [filters, environmentId]);

  const handleConfirm = async () => {
    setApplying(true);
    setApplyError(null);
    try {
      const result = await bulkCancelBuilds(
        { ...filters, dry_run: false, reason: reason.trim() || null },
        environmentId,
      );
      onApplied(result);
    } catch (err) {
      setApplyError(err instanceof Error ? err.message : "Failed to cancel builds");
    } finally {
      setApplying(false);
    }
  };

  const skippedEntries = Object.entries(preview?.skipped ?? {});
  const buildCount = preview?.build_count ?? 0;
  const confirmLabel =
    buildCount === 1 ? "Cancel 1 build" : `Cancel ${buildCount} builds`;

  return (
    <ConfirmDialog
      isOpen
      title={mode === "idle" ? "Clean up idle builds" : "Cancel selected builds"}
      maxWidthClass="max-w-2xl"
      destructive
      confirmLabel={confirmLabel}
      busyLabel="Cancelling…"
      cancelLabel="Close"
      confirmDisabled={previewLoading || buildCount === 0}
      busy={applying}
      error={applyError}
      onConfirm={handleConfirm}
      onCancel={onClose}
    >
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {mode === "idle" ? (
          <>
            Cancel every <strong>running</strong> build in this environment with no
            activity for at least{" "}
            <strong>{formatIdleThreshold(sweepIdleSeconds ?? 0)}</strong>
            {sweepApp ? (
              <>
                {" "}
                owned by the reactive app <code className="font-mono">{sweepApp}</code>
              </>
            ) : null}
            . Idleness is measured on <em>last activity</em> — the same signal the
            server reaps on — not on the build's last lifecycle transition.
          </>
        ) : (
          <>
            Cancel the {selectedIds.length} selected build
            {selectedIds.length === 1 ? "" : "s"}. Only builds that are still{" "}
            <strong>running</strong> can be cancelled; anything already finished is
            reported below and left alone.
          </>
        )}
      </p>

      <div className="space-y-2 rounded-md border border-gray-200 p-3 dark:border-gray-700">
        <Checkbox
          checked={cascade}
          onChange={setCascade}
          labelHidden={false}
          label="Release the execution claims these builds' tasks hold"
        />
        <p className="ml-6 text-xs text-gray-500 dark:text-gray-400">
          Cancels each build's running and suspended tasks too, freeing their execution
          claims and concurrency-limit slots. Without this, cancelling a build leaves
          behind exactly the leaked claims a cleanup exists to remove.
        </p>
        <Checkbox
          checked={includeReactive || !!sweepApp}
          disabled={!!sweepApp}
          onChange={setIncludeReactive}
          labelHidden={false}
          label="Include reactive builds"
        />
        <p className="ml-6 text-xs text-gray-500 dark:text-gray-400">
          {sweepApp
            ? "Implied: the sweep is already scoped to a single reactive app."
            : "A reactive build is quiet between ticks by design, so quiet does not mean abandoned. Tick this only when the owning app is gone."}
        </p>
        <div className="pt-1">
          <label
            htmlFor="bulk-cancel-reason"
            className="block text-xs font-medium text-gray-500 dark:text-gray-400"
          >
            Reason (optional)
          </label>
          <input
            id="bulk-cancel-reason"
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Recorded on each cancellation event"
            className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-1 text-sm text-gray-900 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
          />
        </div>
      </div>

      {/* Dry-run preview: exactly what the confirm button will do. */}
      <div className="space-y-3">
        {previewLoading && (
          <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
            Checking what this will affect…
          </div>
        )}

        {previewError && <ResultBanner tone="error">{previewError}</ResultBanner>}

        {preview && !previewLoading && (
          <>
            <p
              role="status"
              className="text-sm font-medium text-gray-900 dark:text-gray-100"
            >
              {buildCount === 0
                ? "Nothing to cancel — no running build matches."
                : `Will cancel ${buildCount} build${buildCount === 1 ? "" : "s"}` +
                  (cascade
                    ? ` and release ${preview.task_count} task claim${
                        preview.task_count === 1 ? "" : "s"
                      }.`
                    : " and release no task claims (cascade is off).")}
            </p>

            {preview.truncated && (
              <ResultBanner tone="warning">
                More builds match than one call may cancel. Run the cleanup again
                afterwards to continue.
              </ResultBanner>
            )}

            {preview.builds.length > 0 && (
              <ul className="max-h-48 divide-y divide-gray-200 overflow-y-auto rounded-md border border-gray-200 text-sm dark:divide-gray-700 dark:border-gray-700">
                {preview.builds.map((build) => (
                  <li
                    key={build.build_id}
                    className="flex items-center gap-2 px-3 py-1.5"
                  >
                    <span className="min-w-0 flex-1 truncate text-gray-900 dark:text-gray-100">
                      {build.name}
                      {build.reactive_app_name && (
                        <span className="ml-1.5 rounded bg-purple-100 px-1.5 py-0.5 text-xs text-purple-700 dark:bg-purple-900/40 dark:text-purple-300">
                          {build.reactive_app_name}
                        </span>
                      )}
                    </span>
                    {cascade && build.cascaded_task_ids.length > 0 && (
                      <span className="flex-shrink-0 text-xs text-gray-500 dark:text-gray-400">
                        {build.cascaded_task_ids.length} claim
                        {build.cascaded_task_ids.length === 1 ? "" : "s"}
                      </span>
                    )}
                    <span
                      className="flex-shrink-0 text-xs text-gray-500 dark:text-gray-400"
                      title={formatAbsoluteTime(build.last_activity_at)}
                    >
                      idle {formatRelativeTime(build.last_activity_at)}
                    </span>
                  </li>
                ))}
              </ul>
            )}

            {skippedEntries.length > 0 && (
              <div className="rounded-md border border-amber-200 bg-amber-50 p-3 dark:border-amber-900 dark:bg-amber-900/20">
                <p className="text-sm font-medium text-amber-800 dark:text-amber-300">
                  Skipped ({skippedEntries.length})
                </p>
                <ul className="mt-1 space-y-0.5 text-xs text-amber-800 dark:text-amber-300">
                  {skippedEntries.map(([buildId, skipReason]) => (
                    <li key={buildId}>
                      <span className="font-medium">
                        {buildNames?.[buildId] ?? buildId.slice(0, 8)}
                      </span>
                      {" — "}
                      {SKIP_REASONS[skipReason] ?? skipReason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>

      <p className="text-xs text-gray-500 dark:text-gray-400">
        Cancelling rewrites the registry's view of a build. It does not stop a process
        that is still alive — a worker keeps going until it notices.
      </p>
    </ConfirmDialog>
  );
}
