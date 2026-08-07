import { useCallback, useEffect, useState } from "react";
import {
  cancelTask,
  fetchTaskArtifacts,
  fetchTaskEvents,
  retryTask,
} from "../api/tasks";
import { useEnvironment } from "../context/EnvironmentContext";
import type {
  Task,
  TaskArtifact,
  TaskEvent,
  EventType,
  ExecutorMetadata,
} from "../types/task";
import {
  availableClaimActions,
  CLAIM_ACTION_LABELS,
  type ClaimAction,
} from "../utils/claims";
import {
  isModalMetadata,
  modalAppUrl,
  modalEnvironmentUrl,
  modalFunctionCallUrl,
  modalFunctionUrl,
} from "../utils/modalLinks";
import { formatAbsoluteTime, formatDuration } from "../utils/time";
import { ArtifactList, ExpandButton } from "./ArtifactViewer";
import { ClaimActionDialog } from "./ClaimActionDialog";
import { ExecutorBadge } from "./ExecutorBadge";
import { FullscreenModal } from "./FullscreenModal";
import { StatusBadge } from "./StatusBadge";

// Copy button component
function CopyButton({ text, className = "" }: { text: string; className?: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
    }
  };

  return (
    <button
      onClick={handleCopy}
      className={`p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 ${className}`}
      title={copied ? "Copied!" : "Copy to clipboard"}
    >
      {copied ? (
        <svg
          className="h-4 w-4 text-green-500"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M5 13l4 4L19 7"
          />
        </svg>
      ) : (
        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"
          />
        </svg>
      )}
    </button>
  );
}

// The Modal function-call reference (fc-…) for an execution: the raw id as a
// deep link to the function call in the Modal dashboard when a genuine
// call-level URL is resolvable, otherwise plain text, always followed by a
// click-to-copy button.
//
// The call-level link is gated on modalFunctionUrl being non-null.
// modalFunctionCallUrl falls back to the app page when function_id is missing
// (other callers, e.g. the "View on Modal" links, rely on that shared
// fallback), but here a clickable call ref must never navigate to a coarser
// level — so we only link when the function itself is addressable; otherwise
// the ref renders as plain text (the copy button stays either way).
//
// Gated to Modal executions (reuses isModalMetadata): renders for modal
// metadata and the legacy kind-less case (treated as modal). Renders nothing
// for an explicitly non-modal kind, or when there is no call ref to show.
export function ModalExecutionCallRef({
  metadata,
  executorRef,
}: {
  metadata?: ExecutorMetadata | null;
  executorRef?: string | null;
}) {
  const isModal = !metadata || isModalMetadata(metadata);
  if (!isModal) return null;

  const fcId =
    typeof executorRef === "string" && executorRef.length > 0 ? executorRef : null;
  if (!fcId) return null;

  const callUrl = modalFunctionUrl(metadata)
    ? modalFunctionCallUrl(metadata, fcId)
    : null;

  return (
    <div className="flex items-center gap-1 font-mono">
      {callUrl ? (
        <a
          href={callUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="break-all text-blue-600 hover:underline dark:text-blue-400"
        >
          {fcId}
        </a>
      ) : (
        <span className="break-all">{fcId}</span>
      )}
      <CopyButton text={fcId} className="flex-shrink-0" />
    </div>
  );
}

// Collapsible "more details" block: a 2-column table of the captured Modal
// identifiers verbatim, each value click-to-copy. Rows are grouped top-down —
// human-readable names first (Workspace, Environment, App, Function), then a
// hairline divider, then the raw ids (App ID, Function ID, Call ref). Modal
// gives no URL-format guarantee (see utils/modalLinks.ts), so surfacing the
// raw ids lets a user reconstruct or paste a reference by hand even if the
// dashboard URL format drifts. Only present fields render; the divider shows
// only when both groups are non-empty; renders nothing when none are.
//
// Gated to Modal executions: this block's labels ("App", "Function ID", …)
// are Modal-specific, so it renders only for modal metadata, for the legacy
// kind-less case (treated as modal for back-compat), and for the
// executorRef-only case (no metadata at all). It never renders Modal-labeled
// fields for an explicitly non-modal kind (e.g. "k8s").
export function ModalExecutionDetails({
  metadata,
  executorRef,
}: {
  metadata?: ExecutorMetadata | null;
  executorRef?: string | null;
}) {
  const [open, setOpen] = useState(false);

  const isModal = !metadata || isModalMetadata(metadata);

  const collect = (entries: [string, unknown, string | null][]) => {
    const rows: { label: string; value: string; url: string | null }[] = [];
    for (const [label, value, url] of entries) {
      if (typeof value === "string" && value.length > 0) {
        rows.push({ label, value, url });
      }
    }
    return rows;
  };

  // Best-effort Modal dashboard links per level (null when not resolvable —
  // see utils/modalLinks.ts). Workspace has no meaningful standalone URL, so
  // it stays plain text. The call ref is gated on the function being
  // addressable (modalFunctionUrl non-null) so it never links to a coarser
  // level; see ModalExecutionCallRef.
  const appUrl = modalAppUrl(metadata);
  const funcUrl = modalFunctionUrl(metadata);
  const callUrl = funcUrl ? modalFunctionCallUrl(metadata, executorRef) : null;

  // Human-readable names first, then the raw object ids.
  const names = collect([
    ["Workspace", metadata?.workspace, null],
    ["Environment", metadata?.environment, modalEnvironmentUrl(metadata)],
    ["App", metadata?.app_name, appUrl],
    ["Function", metadata?.function_name, funcUrl],
  ]);
  const ids = collect([
    ["App ID", metadata?.app_id, appUrl],
    ["Function ID", metadata?.function_id, funcUrl],
    ["Call ref", executorRef, callUrl],
  ]);

  if (!isModal || (names.length === 0 && ids.length === 0)) return null;

  const renderRow = (field: { label: string; value: string; url: string | null }) => (
    <tr key={field.label}>
      <th
        scope="row"
        className="whitespace-nowrap py-0.5 pr-3 align-top font-normal text-gray-500 dark:text-gray-400"
      >
        {field.label}
      </th>
      <td className="py-0.5 align-top">
        <span className="flex items-start gap-1">
          {field.url ? (
            <a
              href={field.url}
              target="_blank"
              rel="noopener noreferrer"
              className="break-all font-mono text-blue-600 hover:underline dark:text-blue-400"
            >
              {field.value}
            </a>
          ) : (
            <span className="break-all font-mono">{field.value}</span>
          )}
          <CopyButton text={field.value} className="flex-shrink-0" />
        </span>
      </td>
    </tr>
  );

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
      >
        <svg
          className={`h-3 w-3 transition-transform ${open ? "rotate-90" : ""}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M9 5l7 7-7 7"
          />
        </svg>
        {open ? "Hide details" : "More details"}
      </button>
      {open && (
        <table className="mt-1 w-full text-left align-top">
          <tbody>
            {names.map(renderRow)}
            {names.length > 0 && ids.length > 0 && (
              <tr aria-hidden="true">
                {/* Same total height as py-1, but the hairline is nudged down
                    (less top, more bottom padding) to sit visually centered
                    between the groups: the align-top value rows leave
                    line-height descender slack above the divider, so equal
                    padding would render the line too close to the group
                    below. */}
                <td colSpan={2} className="pt-0.5 pb-1.5">
                  <hr className="border-gray-200 dark:border-gray-700" />
                </td>
              </tr>
            )}
            {ids.map(renderRow)}
          </tbody>
        </table>
      )}
    </div>
  );
}

// Helper to format event type for display
function formatEventType(eventType: EventType): string {
  return eventType
    .replace("task_", "")
    .replace("build_", "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// Get style for event type badge
function getEventTypeStyle(eventType: EventType): string {
  if (eventType.includes("completed")) {
    return "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400";
  }
  if (eventType.includes("failed")) {
    return "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400";
  }
  if (eventType.includes("started") || eventType.includes("resumed")) {
    return "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400";
  }
  if (eventType.includes("cancelled") || eventType.includes("skipped")) {
    return "bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-400";
  }
  if (eventType.includes("waiting") || eventType.includes("suspended")) {
    return "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400";
  }
  return "bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400";
}

interface TaskDetailProps {
  task: Task;
  buildId?: string;
  onClose: () => void;
  onTaskCancelled?: () => void;
  onStatusBuildClick?: (buildId: string) => void;
}

export function TaskDetail({
  task,
  buildId,
  onClose,
  onTaskCancelled,
  onStatusBuildClick,
}: TaskDetailProps) {
  const [artifacts, setArtifacts] = useState<TaskArtifact[]>([]);
  const [artifactsLoading, setArtifactsLoading] = useState(false);
  const [showParamsModal, setShowParamsModal] = useState(false);
  const [showEventsModal, setShowEventsModal] = useState(false);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [claimAction, setClaimAction] = useState<ClaimAction | null>(null);
  const [claimNotice, setClaimNotice] = useState<string | null>(null);

  const { activeWorkspaceRole } = useEnvironment();
  const isAdmin = activeWorkspaceRole === "owner" || activeWorkspaceRole === "admin";

  // ---- Claim holder ----
  //
  // A task's status is environment-global, so a task left RUNNING (or
  // SUSPENDED) by some build denies its execution claim to every build
  // that needs it, indefinitely. `StatusBadge` already hints at a
  // cross-build status with an icon; that is not enough to act on, so
  // when the holder is a *different* build from the one being viewed we
  // say it in words, with the elapsed time and the remedies.
  const holderBuildId = task.latest_status_build_id ?? task.status_build_id ?? null;
  const globalStatus = task.latest_status ?? task.status;
  const holdsClaim = globalStatus === "running" || globalStatus === "suspended";
  // In the task explorer there is no build in view, so every holder is
  // "elsewhere" — which is exactly the claim-holder question being asked
  // there. The copy below adapts rather than the condition.
  const crossBuild = Boolean(holderBuildId && buildId && holderBuildId !== buildId);
  const showClaimHolder = holdsClaim && Boolean(holderBuildId);
  const claimSince =
    task.latest_status_at ?? (globalStatus === "running" ? task.started_at : null);
  const heldFor = claimSince ? formatDuration(claimSince, null) : null;

  // The plain Cancel button addresses the *viewed* build. When the claim
  // is held by a different one, that is the wrong build to address, so the
  // claim callout below owns the action instead of offering two.
  const canCancel =
    buildId && !crossBuild && (task.status === "pending" || task.status === "running");

  const loadEvents = useCallback(async () => {
    setEventsLoading(true);
    try {
      const fetchedEvents = await fetchTaskEvents(task.task_id, task.environment_id);
      setEvents(fetchedEvents);
    } catch (error) {
      console.error("Failed to load task events:", error);
      setEvents([]);
    } finally {
      setEventsLoading(false);
    }
  }, [task.task_id, task.environment_id]);

  const handleShowEvents = useCallback(() => {
    setShowEventsModal(true);
    loadEvents();
  }, [loadEvents]);

  // The one place a task mutation is issued from this panel. `targetBuildId`
  // is whichever build the action belongs to: the viewed build for the plain
  // Cancel, the *claim holder* for a cross-build release/reset.
  const runTaskAction = useCallback(
    async (action: ClaimAction, targetBuildId: string) => {
      setCancelling(true);
      setCancelError(null);
      try {
        if (action === "release") {
          await cancelTask(targetBuildId, task.task_id, task.environment_id);
        } else {
          await retryTask(targetBuildId, task.task_id, task.environment_id);
        }
        return true;
      } catch (err) {
        setCancelError(
          err instanceof Error
            ? err.message
            : action === "release"
              ? "Failed to cancel task"
              : "Failed to retry task",
        );
        return false;
      } finally {
        setCancelling(false);
      }
    },
    [task.task_id, task.environment_id],
  );

  const handleCancel = async () => {
    if (!buildId || !canCancel) return;

    const confirmed = window.confirm(
      `Are you sure you want to cancel task "${task.task_name}"?`,
    );
    if (!confirmed) return;

    if (await runTaskAction("release", buildId)) {
      onTaskCancelled?.();
    }
  };

  const handleClaimAction = useCallback(async () => {
    if (!claimAction || !holderBuildId) return;
    if (await runTaskAction(claimAction, holderBuildId)) {
      setClaimNotice(
        claimAction === "release"
          ? `Released the claim under build ${holderBuildId.slice(0, 8)}.`
          : `Reset to pending under build ${holderBuildId.slice(0, 8)}.`,
      );
      setClaimAction(null);
      onTaskCancelled?.();
    }
  }, [claimAction, holderBuildId, runTaskAction, onTaskCancelled]);

  useEffect(() => {
    let cancelled = false;

    async function loadArtifacts() {
      setArtifactsLoading(true);
      try {
        const response = await fetchTaskArtifacts(task.task_id, task.environment_id);
        if (!cancelled) {
          setArtifacts(response.artifacts);
        }
      } catch (error) {
        console.error("Failed to load task artifacts:", error);
        if (!cancelled) {
          setArtifacts([]);
        }
      } finally {
        if (!cancelled) {
          setArtifactsLoading(false);
        }
      }
    }

    loadArtifacts();

    return () => {
      cancelled = true;
    };
  }, [task.task_id, task.environment_id]);

  return (
    <div className="h-full overflow-auto bg-white dark:bg-gray-800 p-4">
      {/* Header */}
      <div className="mb-4 flex items-start justify-between">
        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 truncate">
            {task.task_name}
          </h2>
          <div className="flex items-center gap-1">
            <p
              className="font-mono text-sm text-gray-500 dark:text-gray-400 truncate"
              title={task.task_id}
            >
              {task.task_id}
            </p>
            <CopyButton text={task.task_id} />
          </div>
          {task.task_namespace && (
            <p className="text-sm text-gray-500 dark:text-gray-400">
              Namespace: {task.task_namespace}
            </p>
          )}
        </div>
        <button
          onClick={onClose}
          className="ml-2 text-gray-400 hover:text-gray-500 dark:hover:text-gray-300"
        >
          <span className="sr-only">Close</span>
          <svg
            className="h-6 w-6"
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth="1.5"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M6 18L18 6M6 6l12 12"
            />
          </svg>
        </button>
      </div>

      <div className="space-y-4">
        {/* Status */}
        <div>
          <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
            Status
          </label>
          <div className="mt-1 flex items-center gap-2">
            <StatusBadge
              status={task.status}
              waitingForLock={task.waiting_for_lock}
              statusBuildId={task.status_build_id}
              currentBuildId={buildId}
              onStatusBuildClick={onStatusBuildClick}
            />
            {canCancel && (
              <button
                onClick={handleCancel}
                disabled={cancelling}
                className="rounded-md bg-red-100 px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-200 disabled:opacity-50 dark:bg-red-900/30 dark:text-red-400 dark:hover:bg-red-900/50"
              >
                {cancelling ? "Cancelling..." : "Cancel"}
              </button>
            )}
          </div>
          {cancelError && !claimAction && (
            <p className="mt-1 text-xs text-red-600 dark:text-red-400">{cancelError}</p>
          )}
          {claimNotice && (
            <p
              role="status"
              className="mt-1 text-xs text-green-700 dark:text-green-400"
            >
              {claimNotice}
            </p>
          )}

          {/* Claim holder: who is sitting on this task, and for how long. */}
          {showClaimHolder && holderBuildId && (
            <div className="mt-2 rounded-md border border-amber-200 bg-amber-50 p-2.5 dark:border-amber-900/60 dark:bg-amber-950/30">
              <p className="text-xs font-semibold text-amber-900 dark:text-amber-200">
                {crossBuild
                  ? "Execution claim held by another build"
                  : "Holding an execution claim"}
              </p>
              <p className="mt-1 text-xs text-amber-900/90 dark:text-amber-100/90">
                This task has been <em>{globalStatus}</em>
                {heldFor && heldFor !== "—" ? (
                  <span title={formatAbsoluteTime(claimSince)}> for {heldFor}</span>
                ) : null}{" "}
                under build{" "}
                {onStatusBuildClick ? (
                  <button
                    type="button"
                    onClick={() => onStatusBuildClick(holderBuildId)}
                    title={`Go to build ${holderBuildId}`}
                    className="rounded bg-amber-100 px-1 py-0.5 font-mono text-blue-700 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:bg-amber-900/40 dark:text-blue-300"
                  >
                    {holderBuildId.slice(0, 8)}
                  </button>
                ) : (
                  <code className="rounded bg-amber-100 px-1 py-0.5 font-mono dark:bg-amber-900/40">
                    {holderBuildId.slice(0, 8)}
                  </code>
                )}
                {crossBuild ? ", not the build you are viewing." : "."} A task&rsquo;s
                status is environment-wide, so until this claim is released every build
                that needs this task waits on it.
              </p>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                {isAdmin ? (
                  availableClaimActions(globalStatus).map((action) => (
                    <button
                      key={action}
                      type="button"
                      disabled={cancelling}
                      onClick={() => {
                        setCancelError(null);
                        setClaimNotice(null);
                        setClaimAction(action);
                      }}
                      className="rounded-md border border-red-300 px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500 disabled:opacity-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-900/30"
                    >
                      {CLAIM_ACTION_LABELS[action]}
                    </button>
                  ))
                ) : (
                  <span className="text-xs text-amber-900/80 dark:text-amber-200/80">
                    Releasing a claim requires the workspace admin role.
                  </span>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Timestamps */}
        <div className="space-y-2">
          <div>
            <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
              Created
            </label>
            <p className="mt-1 text-sm text-gray-900 dark:text-gray-100">
              {new Date(task.created_at).toLocaleString()}
            </p>
          </div>
          {task.started_at && (
            <div>
              <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
                Started
              </label>
              <p className="mt-1 text-sm text-gray-900 dark:text-gray-100">
                {new Date(task.started_at).toLocaleString()}
              </p>
            </div>
          )}
          {task.completed_at && (
            <div>
              <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
                Completed
              </label>
              <p className="mt-1 text-sm text-gray-900 dark:text-gray-100">
                {new Date(task.completed_at).toLocaleString()}
              </p>
            </div>
          )}
        </div>

        {/* Commit hash - from the event that determined current status */}
        {task.commit_hash && (
          <div>
            <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
              Commit
            </label>
            <div className="mt-1 flex items-center gap-1">
              <code className="rounded bg-gray-100 px-1.5 py-0.5 text-sm font-mono text-gray-800 dark:bg-gray-700 dark:text-gray-200">
                {task.commit_hash}
              </code>
              <CopyButton text={task.commit_hash} className="flex-shrink-0" />
            </div>
          </div>
        )}

        {/* Execution - only when executor identity/metadata was recorded */}
        {(task.latest_executor ||
          task.latest_executor_ref ||
          task.latest_executor_metadata) && (
          <div>
            <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
              Execution
            </label>
            <div className="mt-1 space-y-2 text-sm text-gray-900 dark:text-gray-100">
              {task.latest_executor && (
                <div className="flex items-center gap-2">
                  <ExecutorBadge executor={task.latest_executor} />
                </div>
              )}
              <ModalExecutionCallRef
                metadata={task.latest_executor_metadata}
                executorRef={task.latest_executor_ref}
              />
              <ModalExecutionDetails
                metadata={task.latest_executor_metadata}
                executorRef={task.latest_executor_ref}
              />
            </div>
          </div>
        )}

        {/* Output URI - only show when present */}
        {task.output_uri && (
          <div>
            <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
              Output URI
            </label>
            <div className="mt-1 flex items-center gap-1">
              <p
                className="text-sm font-mono text-gray-900 dark:text-gray-100 truncate"
                title={task.output_uri}
              >
                {task.output_uri}
              </p>
              <CopyButton text={task.output_uri} className="flex-shrink-0" />
            </div>
          </div>
        )}

        {/* Error message - only show when status is failed */}
        {task.status === "failed" && task.error_message && (
          <div>
            <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
              Error
            </label>
            <pre className="mt-1 overflow-auto rounded-md bg-red-50 dark:bg-red-900/20 p-3 text-sm text-red-700 dark:text-red-400">
              {task.error_message}
            </pre>
          </div>
        )}

        {/* Event log link */}
        <button
          onClick={handleShowEvents}
          className="flex items-center gap-1.5 text-sm text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
        >
          <svg
            className="h-4 w-4"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"
            />
          </svg>
          See full event log
        </button>

        {/* Task data */}
        <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
          <div className="flex items-center justify-between border-b border-gray-200 bg-gray-50 px-3 py-2 dark:border-gray-700 dark:bg-gray-800">
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
              Task Parameters
            </span>
            <div className="flex items-center gap-2">
              <span className="rounded bg-gray-200 px-2 py-0.5 text-xs text-gray-500 dark:bg-gray-700 dark:text-gray-400">
                json
              </span>
              <ExpandButton
                onClick={() => setShowParamsModal(true)}
                title="View fullscreen"
              />
            </div>
          </div>
          <div className="p-3">
            <pre className="max-h-64 overflow-auto rounded-md bg-gray-50 p-3 text-sm text-gray-800 dark:bg-gray-900 dark:text-gray-200">
              {JSON.stringify(task.task_data, null, 2)}
            </pre>
          </div>
        </div>

        {/* Artifacts */}
        {artifactsLoading ? (
          <div className="text-sm text-gray-500 dark:text-gray-400">
            Loading artifacts...
          </div>
        ) : (
          <ArtifactList artifacts={artifacts} />
        )}
      </div>

      {holderBuildId && (
        <ClaimActionDialog
          action={claimAction}
          taskName={task.task_name}
          taskId={task.task_id}
          ownerBuildId={holderBuildId}
          currentBuildId={buildId}
          status={globalStatus}
          busy={cancelling}
          error={cancelError}
          onConfirm={handleClaimAction}
          onCancel={() => {
            setClaimAction(null);
            setCancelError(null);
          }}
        />
      )}

      {/* Task Parameters Fullscreen Modal */}
      <FullscreenModal
        isOpen={showParamsModal}
        onClose={() => setShowParamsModal(false)}
        title="Task Parameters"
      >
        <pre className="overflow-auto rounded-md bg-gray-50 p-3 text-sm text-gray-800 dark:bg-gray-900 dark:text-gray-200">
          {JSON.stringify(task.task_data, null, 2)}
        </pre>
      </FullscreenModal>

      {/* Event Log Modal */}
      <FullscreenModal
        isOpen={showEventsModal}
        onClose={() => setShowEventsModal(false)}
        title="Task Event Log"
      >
        <div className="space-y-4">
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Complete event history for task{" "}
            <span className="font-mono font-medium text-gray-700 dark:text-gray-300">
              {task.task_name}
            </span>{" "}
            across all builds.
          </p>

          {eventsLoading ? (
            <div className="flex items-center justify-center py-8">
              <div className="h-8 w-8 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
            </div>
          ) : events.length === 0 ? (
            <div className="py-8 text-center text-gray-500 dark:text-gray-400">
              No events found for this task.
            </div>
          ) : (
            <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
              <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                <thead className="bg-gray-50 dark:bg-gray-800">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                      Timestamp
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                      Event
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                      Build
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                      Commit
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                      Details
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 bg-white dark:divide-gray-700 dark:bg-gray-900">
                  {events.map((event) => (
                    <tr
                      key={event.id}
                      className="hover:bg-gray-50 dark:hover:bg-gray-800"
                    >
                      <td className="whitespace-nowrap px-4 py-3 text-sm text-gray-900 dark:text-gray-100">
                        {(() => {
                          const d = new Date(event.created_at);
                          const date = d.toLocaleDateString();
                          const time = d.toLocaleTimeString(undefined, {
                            hour12: false,
                          });
                          const centiseconds = Math.floor(d.getMilliseconds() / 10)
                            .toString()
                            .padStart(2, "0");
                          return `${date} ${time}.${centiseconds}`;
                        })()}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <span
                          className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${getEventTypeStyle(
                            event.event_type,
                          )}`}
                        >
                          {formatEventType(event.event_type)}
                        </span>
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        {onStatusBuildClick ? (
                          <button
                            onClick={() => {
                              setShowEventsModal(false);
                              onStatusBuildClick(event.build_id);
                            }}
                            className="font-mono text-sm text-blue-600 hover:text-blue-700 hover:underline dark:text-blue-400 dark:hover:text-blue-300"
                          >
                            {event.build_id.slice(0, 8)}...
                          </button>
                        ) : (
                          <span className="font-mono text-sm text-gray-500 dark:text-gray-400">
                            {event.build_id.slice(0, 8)}...
                          </span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        {event.event_metadata?.commit_hash ? (
                          <code className="rounded bg-gray-100 px-1.5 py-0.5 text-xs font-mono text-gray-700 dark:bg-gray-700 dark:text-gray-300">
                            {event.event_metadata.commit_hash as string}
                          </code>
                        ) : (
                          <span className="text-sm text-gray-400 dark:text-gray-500">
                            -
                          </span>
                        )}
                      </td>
                      <td className="max-w-xs truncate px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                        {event.error_message ? (
                          <span
                            className="text-red-600 dark:text-red-400"
                            title={event.error_message}
                          >
                            {event.error_message.length > 50
                              ? `${event.error_message.slice(0, 50)}...`
                              : event.error_message}
                          </span>
                        ) : event.event_metadata ? (
                          <span title={JSON.stringify(event.event_metadata)}>
                            {Object.keys(event.event_metadata).length > 0
                              ? `${Object.keys(event.event_metadata).length} field(s)`
                              : "-"}
                          </span>
                        ) : (
                          "-"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </FullscreenModal>
    </div>
  );
}
