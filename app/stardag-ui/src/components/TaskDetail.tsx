import { useEffect, useState } from "react";
import { cancelTask, fetchTaskAssets } from "../api/tasks";
import type { Task, TaskAsset } from "../types/task";
import { AssetList, ExpandButton } from "./AssetViewer";
import { FullscreenModal } from "./FullscreenModal";
import { StatusBadge } from "./StatusBadge";

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
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  const canCancel = buildId && (task.status === "pending" || task.status === "running");

  const handleCancel = async () => {
    if (!buildId || !canCancel) return;

    const confirmed = window.confirm(
      `Are you sure you want to cancel task "${task.task_name}"?`,
    );
    if (!confirmed) return;

    setCancelling(true);
    setCancelError(null);
    try {
      await cancelTask(buildId, task.task_id, task.workspace_id);
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
        const response = await fetchTaskAssets(task.task_id, task.workspace_id);
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
  }, [task.task_id, task.workspace_id]);

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

        {/* Error message */}
        {task.error_message && (
          <div>
            <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
              Error
            </label>
            <pre className="mt-1 overflow-auto rounded-md bg-red-50 dark:bg-red-900/20 p-3 text-sm text-red-700 dark:text-red-400">
              {task.error_message}
            </pre>
          </div>
        )}

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
    </div>
  );
}
