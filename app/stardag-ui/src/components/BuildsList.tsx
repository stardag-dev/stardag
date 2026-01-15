import { useCallback, useEffect, useState } from "react";
import { fetchBuilds } from "../api/tasks";
import { useEnvironment } from "../context/EnvironmentContext";
import type { Build, BuildStatus } from "../types/task";

interface BuildsListProps {
  onSelectBuild: (buildId: string) => void;
}

interface BuildWithStats extends Build {
  taskCount?: number;
}

function formatRelativeTime(dateString: string): string {
  const date = new Date(dateString);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSecs = Math.floor(diffMs / 1000);
  const diffMins = Math.floor(diffSecs / 60);
  const diffHours = Math.floor(diffMins / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffSecs < 60) return "just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

function getBuildStatusStyle(status: BuildStatus): string {
  switch (status) {
    case "completed":
      return "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300";
    case "failed":
      return "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300";
    case "running":
      return "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300";
    case "cancelled":
      return "bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-300";
    default:
      return "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300";
  }
}

function formatDuration(startedAt: string | null, completedAt: string | null): string {
  if (!startedAt) return "-";
  const start = new Date(startedAt);
  const end = completedAt ? new Date(completedAt) : new Date();
  const diffMs = end.getTime() - start.getTime();
  const diffSecs = Math.floor(diffMs / 1000);
  const diffMins = Math.floor(diffSecs / 60);
  const diffHours = Math.floor(diffMins / 60);

  if (diffSecs < 60) return `${diffSecs}s`;
  if (diffMins < 60) return `${diffMins}m ${diffSecs % 60}s`;
  return `${diffHours}h ${diffMins % 60}m`;
}

export function BuildsList({ onSelectBuild }: BuildsListProps) {
  const { activeEnvironment } = useEnvironment();
  const [builds, setBuilds] = useState<BuildWithStats[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const pageSize = 20;

  const loadBuilds = useCallback(async () => {
    if (!activeEnvironment?.id) {
      setBuilds([]);
      setLoading(false);
      return;
    }

    setLoading(true);
    setError(null);
    try {
      const response = await fetchBuilds({
        page,
        page_size: pageSize,
        environment_id: activeEnvironment.id,
      });
      setBuilds(response.builds);
      setTotal(response.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load builds");
    } finally {
      setLoading(false);
    }
  }, [activeEnvironment?.id, page]);

  useEffect(() => {
    loadBuilds();
  }, [loadBuilds]);

  const totalPages = Math.ceil(total / pageSize);

  if (!activeEnvironment) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500 dark:text-gray-400">
        <p>Select an environment to view builds</p>
      </div>
    );
  }

  if (loading && builds.length === 0) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full items-center justify-center text-red-500">
        <p>{error}</p>
      </div>
    );
  }

  if (builds.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center text-gray-500 dark:text-gray-400">
        <svg
          className="mb-4 h-16 w-16"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={1.5}
            d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"
          />
        </svg>
        <p className="text-lg font-medium">No builds yet</p>
        <p className="mt-1 text-sm">Run a build with the Stardag SDK to see it here</p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="border-b border-gray-200 bg-white px-6 py-4 dark:border-gray-700 dark:bg-gray-800">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
          Recent Builds
        </h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          {total} build{total !== 1 ? "s" : ""} in this environment
        </p>
      </div>

      {/* Build list */}
      <div className="flex-1 overflow-auto">
        <div className="divide-y divide-gray-200 dark:divide-gray-700">
          {builds.map((build) => (
            <button
              key={build.id}
              onClick={() => onSelectBuild(build.id)}
              className="flex w-full items-center gap-4 px-6 py-4 text-left transition-colors hover:bg-gray-50 dark:hover:bg-gray-700/50"
            >
              {/* Status indicator */}
              <div className="flex-shrink-0">
                <span
                  className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${getBuildStatusStyle(
                    build.status,
                  )}`}
                >
                  {build.status}
                </span>
              </div>

              {/* Build info */}
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-gray-900 dark:text-gray-100">
                    {build.name}
                  </span>
                  {build.commit_hash && (
                    <span className="rounded bg-gray-100 px-1.5 py-0.5 font-mono text-xs text-gray-600 dark:bg-gray-700 dark:text-gray-400">
                      {build.commit_hash.slice(0, 7)}
                    </span>
                  )}
                </div>
                {build.description && (
                  <p className="mt-0.5 truncate text-sm text-gray-500 dark:text-gray-400">
                    {build.description}
                  </p>
                )}
              </div>

              {/* Stats */}
              <div className="flex items-center gap-6 text-sm text-gray-500 dark:text-gray-400">
                {/* Duration */}
                <div className="flex items-center gap-1.5" title="Duration">
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
                      d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"
                    />
                  </svg>
                  <span>{formatDuration(build.started_at, build.completed_at)}</span>
                </div>

                {/* Time ago */}
                <div
                  className="w-20 text-right"
                  title={new Date(build.created_at).toLocaleString()}
                >
                  {formatRelativeTime(build.created_at)}
                </div>

                {/* Arrow */}
                <svg
                  className="h-5 w-5 text-gray-400"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M9 5l7 7-7 7"
                  />
                </svg>
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between border-t border-gray-200 bg-white px-6 py-3 dark:border-gray-700 dark:bg-gray-800">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page === 1}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            Previous
          </button>
          <span className="text-sm text-gray-500 dark:text-gray-400">
            Page {page} of {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page === totalPages}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}
