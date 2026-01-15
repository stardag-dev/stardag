import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  type ImperativePanelHandle,
  Panel,
  PanelGroup,
  PanelResizeHandle,
} from "react-resizable-panels";
import {
  cancelBuild,
  completeBuild,
  failBuild,
  fetchBuild,
  fetchBuildGraph,
  fetchTasksInBuild,
} from "../api/tasks";
import { useAuth } from "../context/AuthContext";
import { useEnvironment } from "../context/EnvironmentContext";
import type { Build, Task, TaskGraphResponse, TaskStatus } from "../types/task";
import { DagGraph } from "./DagGraph";
import { TaskDetail } from "./TaskDetail";
import { TaskFilters } from "./TaskFilters";
import { TaskTable } from "./TaskTable";

interface TaskWithContext extends Task {
  isFilterMatch: boolean;
}

interface BuildViewProps {
  buildId: string;
  onBack: () => void;
  onNavigateToBuild?: (buildId: string) => void;
}

export function BuildView({ buildId, onBack, onNavigateToBuild }: BuildViewProps) {
  const { activeEnvironment } = useEnvironment();
  const { user } = useAuth();
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);

  // Data state
  const [build, setBuild] = useState<Build | null>(null);
  const [allTasks, setAllTasks] = useState<Task[]>([]);
  const [graph, setGraph] = useState<TaskGraphResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // DAG collapse state - expanded by default
  const [showDag, setShowDag] = useState(true);
  const dagPanelRef = useRef<ImperativePanelHandle>(null);

  // Override state dropdown
  const [showOverrideMenu, setShowOverrideMenu] = useState(false);
  const [overriding, setOverriding] = useState(false);
  const [overrideError, setOverrideError] = useState<string | null>(null);
  const overrideMenuRef = useRef<HTMLDivElement>(null);

  // Refresh state
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const autoRefreshRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastClickRef = useRef<number>(0);

  // Handle DAG toggle with panel resize
  const handleToggleDag = useCallback(() => {
    const panel = dagPanelRef.current;
    if (panel) {
      if (showDag) {
        panel.collapse();
      } else {
        panel.expand();
      }
    }
    setShowDag(!showDag);
  }, [showDag]);

  // Filter state
  const [nameFilter, setNameFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<TaskStatus | "">("");
  const [page, setPage] = useState(1);
  const pageSize = 20;

  // Load build data
  const loadBuild = useCallback(async () => {
    if (!activeEnvironment?.id || !buildId) {
      setLoading(false);
      return;
    }

    setLoading(true);
    setError(null);
    try {
      const [buildData, tasksData, graphData] = await Promise.all([
        fetchBuild(buildId, activeEnvironment.id),
        fetchTasksInBuild(buildId, { environment_id: activeEnvironment.id }),
        fetchBuildGraph(buildId, activeEnvironment.id),
      ]);
      setBuild(buildData);
      setAllTasks(tasksData);
      setGraph(graphData);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load build");
    } finally {
      setLoading(false);
    }
  }, [activeEnvironment?.id, buildId]);

  useEffect(() => {
    loadBuild();
  }, [loadBuild]);

  // Refresh handler
  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    await loadBuild();
    setRefreshing(false);
  }, [loadBuild]);

  // Auto-refresh effect
  useEffect(() => {
    if (autoRefresh && build?.status === "running") {
      autoRefreshRef.current = setInterval(handleRefresh, 5000);
    } else if (autoRefreshRef.current) {
      clearInterval(autoRefreshRef.current);
      autoRefreshRef.current = null;
    }

    return () => {
      if (autoRefreshRef.current) {
        clearInterval(autoRefreshRef.current);
        autoRefreshRef.current = null;
      }
    };
  }, [autoRefresh, build?.status, handleRefresh]);

  // Override state handlers
  const handleOverride = useCallback(
    async (action: "cancel" | "complete" | "fail") => {
      if (!activeEnvironment?.id || !buildId) return;

      const actionLabels = {
        cancel: "cancel",
        complete: "mark as completed",
        fail: "mark as failed",
      };

      const confirmed = window.confirm(
        `Are you sure you want to ${actionLabels[action]} this build?`,
      );
      if (!confirmed) return;

      setShowOverrideMenu(false);
      setOverriding(true);
      setOverrideError(null);

      const userId = user?.profile?.sub;

      try {
        let updatedBuild: Build;
        if (action === "cancel") {
          updatedBuild = await cancelBuild(buildId, activeEnvironment.id, userId);
        } else if (action === "complete") {
          updatedBuild = await completeBuild(buildId, activeEnvironment.id, userId);
        } else {
          updatedBuild = await failBuild(buildId, activeEnvironment.id, userId);
        }
        setBuild(updatedBuild);
      } catch (err) {
        setOverrideError(
          err instanceof Error
            ? err.message
            : `Failed to ${actionLabels[action]} build`,
        );
      } finally {
        setOverriding(false);
      }
    },
    [activeEnvironment?.id, buildId, user?.profile?.sub],
  );

  // Close override menu when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (
        overrideMenuRef.current &&
        !overrideMenuRef.current.contains(event.target as Node)
      ) {
        setShowOverrideMenu(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  // Double-click refresh to toggle auto-refresh
  const handleRefreshClick = useCallback(() => {
    const now = Date.now();
    const timeSinceLastClick = now - lastClickRef.current;
    lastClickRef.current = now;

    if (timeSinceLastClick < 300) {
      // Double-click: toggle auto-refresh
      setAutoRefresh((prev) => !prev);
    } else {
      // Single click: manual refresh (only if not in auto-refresh mode)
      if (!autoRefresh) {
        handleRefresh();
      } else {
        // Click while auto-refreshing: stop auto-refresh
        setAutoRefresh(false);
      }
    }
  }, [autoRefresh, handleRefresh]);

  // Can override if build is in an active or stuck state
  const canOverride =
    build?.status === "running" ||
    build?.status === "pending" ||
    build?.status === "exit_early";

  // Client-side filtering
  const filteredTasks = allTasks.filter((task) => {
    if (
      nameFilter &&
      !task.task_name.toLowerCase().includes(nameFilter.toLowerCase())
    ) {
      return false;
    }
    if (statusFilter && task.status !== statusFilter) {
      return false;
    }
    return true;
  });

  // Tasks with context for DAG - memoized to avoid recalculating on every render
  const tasksWithContext: TaskWithContext[] = useMemo(() => {
    if (!graph) return [];

    const matchingTaskIds = new Set(filteredTasks.map((t) => t.task_id));
    const noFilter = !nameFilter && !statusFilter;

    return graph.nodes.map((node) => {
      const fullTask = allTasks.find((t) => t.task_id === node.task_id);

      return {
        id: node.id,
        task_id: node.task_id,
        environment_id: build?.environment_id ?? "",
        task_namespace: node.task_namespace,
        task_name: node.task_name,
        task_data: fullTask?.task_data ?? {},
        version: fullTask?.version ?? null,
        created_at: fullTask?.created_at ?? build?.created_at ?? "",
        status: node.status,
        started_at: fullTask?.started_at ?? null,
        completed_at: fullTask?.completed_at ?? null,
        error_message: fullTask?.error_message ?? null,
        asset_count: node.asset_count,
        isFilterMatch: noFilter || matchingTaskIds.has(node.task_id),
        // Cross-build status fields
        waiting_for_lock: fullTask?.waiting_for_lock,
        status_build_id: fullTask?.status_build_id,
      };
    });
  }, [graph, allTasks, filteredTasks, nameFilter, statusFilter, build]);

  // Pagination
  const total = filteredTasks.length;
  const totalPages = Math.ceil(total / pageSize);
  const paginatedTasks = filteredTasks.slice((page - 1) * pageSize, page * pageSize);

  const handleDagTaskClick = useCallback(
    (taskId: string) => {
      const task = tasksWithContext.find((t) => t.task_id === taskId);
      if (task) setSelectedTask(task);
    },
    [tasksWithContext],
  );

  const handleSetNameFilter = useCallback((filter: string) => {
    setNameFilter(filter);
    setPage(1);
  }, []);

  const handleSetStatusFilter = useCallback((status: TaskStatus | "") => {
    setStatusFilter(status);
    setPage(1);
  }, []);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center text-red-500">
        <p>{error}</p>
        <button
          onClick={onBack}
          className="mt-4 rounded-md bg-gray-100 px-4 py-2 text-sm text-gray-700 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
        >
          Go back
        </button>
      </div>
    );
  }

  if (!build) {
    return (
      <div className="flex h-full flex-col items-center justify-center text-gray-500 dark:text-gray-400">
        <p>Build not found</p>
        <button
          onClick={onBack}
          className="mt-4 rounded-md bg-gray-100 px-4 py-2 text-sm text-gray-700 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
        >
          Go back
        </button>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Build header */}
      <div className="flex items-center gap-4 border-b border-gray-200 bg-white px-4 py-3 dark:border-gray-700 dark:bg-gray-800">
        <button
          onClick={onBack}
          className="rounded-md p-1.5 text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          title="Back to builds"
        >
          <svg
            className="h-5 w-5"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M15 19l-7-7 7-7"
            />
          </svg>
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              {build.name}
            </h1>
            <span
              className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${
                build.status === "completed"
                  ? "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-400"
                  : build.status === "failed"
                    ? "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-400"
                    : build.status === "running"
                      ? "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-400"
                      : build.status === "cancelled"
                        ? "bg-gray-100 text-gray-700 dark:bg-gray-900/50 dark:text-gray-400"
                        : build.status === "exit_early"
                          ? "bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-400"
                          : "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-400"
              }`}
              title={
                build.status_triggered_by_user
                  ? `Manually set by ${
                      build.status_triggered_by_user.display_name ||
                      build.status_triggered_by_user.email
                    }`
                  : undefined
              }
            >
              {build.status_triggered_by_user && (
                <svg
                  className="h-3 w-3"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
                  />
                </svg>
              )}
              {build.status === "exit_early" ? "exited early" : build.status}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <p className="text-sm text-gray-500 dark:text-gray-400">
              {allTasks.length} tasks &middot; Created{" "}
              {new Date(build.created_at).toLocaleString()}
            </p>
            {overrideError && (
              <span className="text-xs text-red-600 dark:text-red-400">
                {overrideError}
              </span>
            )}
          </div>
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-2">
          {/* Refresh button with double-click for auto-refresh */}
          <button
            onClick={handleRefreshClick}
            disabled={refreshing && !autoRefresh}
            className={`rounded-md p-1.5 transition-colors ${
              autoRefresh
                ? "bg-blue-100 text-blue-700 ring-2 ring-blue-400 dark:bg-blue-900/30 dark:text-blue-400"
                : "text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
            } disabled:opacity-50`}
            title={
              autoRefresh
                ? "Auto-refreshing (click to stop)"
                : "Click to refresh, double-click for auto-refresh"
            }
          >
            <svg
              className={`h-5 w-5 ${refreshing || autoRefresh ? "animate-spin" : ""}`}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
              />
            </svg>
          </button>

          {/* Override state dropdown */}
          {canOverride && (
            <div className="relative" ref={overrideMenuRef}>
              <button
                onClick={() => setShowOverrideMenu(!showOverrideMenu)}
                disabled={overriding}
                className="inline-flex items-center gap-1 rounded-md bg-gray-100 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-200 disabled:opacity-50 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
              >
                {overriding ? "Updating..." : "Override State"}
                <svg
                  className={`h-4 w-4 transition-transform ${
                    showOverrideMenu ? "rotate-180" : ""
                  }`}
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M19 9l-7 7-7-7"
                  />
                </svg>
              </button>

              {showOverrideMenu && (
                <div className="absolute right-0 z-10 mt-1 w-40 origin-top-right rounded-md bg-white shadow-lg ring-1 ring-black ring-opacity-5 dark:bg-gray-800 dark:ring-gray-700">
                  <div className="py-1">
                    <button
                      onClick={() => handleOverride("complete")}
                      className="flex w-full items-center gap-2 px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700"
                    >
                      <span className="h-2 w-2 rounded-full bg-green-500" />
                      Mark Completed
                    </button>
                    <button
                      onClick={() => handleOverride("fail")}
                      className="flex w-full items-center gap-2 px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700"
                    >
                      <span className="h-2 w-2 rounded-full bg-red-500" />
                      Mark Failed
                    </button>
                    <button
                      onClick={() => handleOverride("cancel")}
                      className="flex w-full items-center gap-2 px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700"
                    >
                      <span className="h-2 w-2 rounded-full bg-gray-500" />
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Main content */}
      <div className="flex-1 overflow-hidden">
        <PanelGroup direction="horizontal">
          {/* Left column: Filters + DAG + List */}
          <Panel defaultSize={selectedTask ? 70 : 100} minSize={40}>
            <div className="flex h-full flex-col">
              {/* Filters */}
              <TaskFilters
                nameFilter={nameFilter}
                onNameFilterChange={handleSetNameFilter}
                statusFilter={statusFilter}
                onStatusFilterChange={handleSetStatusFilter}
              />

              {/* DAG header - always visible */}
              <button
                onClick={handleToggleDag}
                className="flex w-full items-center justify-between border-b border-gray-200 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                <div className="flex items-center gap-2">
                  <svg
                    className={`h-4 w-4 transition-transform ${
                      showDag ? "rotate-90" : ""
                    }`}
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
                  <span className="font-medium">DAG View</span>
                </div>
                <span className="text-xs text-gray-500 dark:text-gray-400">
                  {showDag ? "Click to collapse" : "Click to expand"}
                </span>
              </button>

              {/* DAG + List with resizable split */}
              <PanelGroup direction="vertical" className="flex-1">
                {/* Collapsible DAG Section */}
                <Panel
                  ref={dagPanelRef}
                  defaultSize={50}
                  minSize={0}
                  collapsible
                  onCollapse={() => setShowDag(false)}
                  onExpand={() => setShowDag(true)}
                >
                  {showDag && (
                    <div className="h-full">
                      <DagGraph
                        tasks={tasksWithContext}
                        graph={graph}
                        selectedTaskId={selectedTask?.task_id ?? null}
                        onTaskClick={handleDagTaskClick}
                        buildId={buildId}
                        onStatusBuildClick={onNavigateToBuild}
                      />
                    </div>
                  )}
                </Panel>

                <PanelResizeHandle className="h-1 cursor-row-resize bg-gray-200 transition-colors hover:bg-blue-400 dark:bg-gray-700 dark:hover:bg-blue-500" />

                {/* Task List */}
                <Panel defaultSize={50} minSize={20}>
                  <TaskTable
                    tasks={paginatedTasks}
                    loading={false}
                    error={null}
                    selectedTaskId={selectedTask?.task_id ?? null}
                    onSelectTask={setSelectedTask}
                    page={page}
                    pageSize={pageSize}
                    total={total}
                    totalPages={totalPages}
                    onPageChange={setPage}
                    buildId={buildId}
                    onStatusBuildClick={onNavigateToBuild}
                  />
                </Panel>
              </PanelGroup>
            </div>
          </Panel>

          {/* Right column: Task Detail (only when task selected) */}
          {selectedTask && (
            <>
              <PanelResizeHandle className="w-1 cursor-col-resize bg-gray-200 transition-colors hover:bg-blue-400 dark:bg-gray-700 dark:hover:bg-blue-500" />
              <Panel defaultSize={30} minSize={20} maxSize={50}>
                <div className="h-full border-l border-gray-200 dark:border-gray-700">
                  <TaskDetail
                    task={selectedTask}
                    buildId={buildId}
                    onClose={() => setSelectedTask(null)}
                    onTaskCancelled={handleRefresh}
                    onStatusBuildClick={onNavigateToBuild}
                  />
                </div>
              </Panel>
            </>
          )}
        </PanelGroup>
      </div>
    </div>
  );
}
