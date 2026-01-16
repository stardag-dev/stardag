import { useCallback, useEffect, useState } from "react";
import { cancelTask, fetchTaskAssets, fetchTaskEvents } from "../api/tasks";
import type { Task, TaskAsset, TaskEvent, EventType } from "../types/task";
import { AssetList, ExpandButton } from "./AssetViewer";
import { FullscreenModal } from "./FullscreenModal";
import { StatusBadge } from "./StatusBadge";

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
  const [assets, setAssets] = useState<TaskAsset[]>([]);
  const [assetsLoading, setAssetsLoading] = useState(false);
  const [showParamsModal, setShowParamsModal] = useState(false);
  const [showEventsModal, setShowEventsModal] = useState(false);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  const canCancel = buildId && (task.status === "pending" || task.status === "running");

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

  const handleCancel = async () => {
    if (!buildId || !canCancel) return;

    const confirmed = window.confirm(
      `Are you sure you want to cancel task "${task.task_name}"?`,
    );
    if (!confirmed) return;

    setCancelling(true);
    setCancelError(null);
    try {
      await cancelTask(buildId, task.task_id, task.environment_id);
      onTaskCancelled?.();
    } catch (err) {
      setCancelError(err instanceof Error ? err.message : "Failed to cancel task");
    } finally {
      setCancelling(false);
    }
  };

  useEffect(() => {
    let cancelled = false;

    async function loadAssets() {
      setAssetsLoading(true);
      try {
        const response = await fetchTaskAssets(task.task_id, task.environment_id);
        if (!cancelled) {
          setAssets(response.assets);
        }
      } catch (error) {
        console.error("Failed to load task assets:", error);
        if (!cancelled) {
          setAssets([]);
        }
      } finally {
        if (!cancelled) {
          setAssetsLoading(false);
        }
      }
    }

    loadAssets();

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
          <p className="font-mono text-sm text-gray-500 dark:text-gray-400 truncate">
            {task.task_id}
          </p>
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
          {cancelError && (
            <p className="mt-1 text-xs text-red-600 dark:text-red-400">{cancelError}</p>
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

        {/* Output URI - only show when present */}
        {task.output_uri && (
          <div>
            <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
              Output URI
            </label>
            <p className="mt-1 text-sm font-mono text-gray-900 dark:text-gray-100 break-all">
              {task.output_uri}
            </p>
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

        {/* Assets */}
        {assetsLoading ? (
          <div className="text-sm text-gray-500 dark:text-gray-400">
            Loading assets...
          </div>
        ) : (
          <AssetList assets={assets} />
        )}
      </div>

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
