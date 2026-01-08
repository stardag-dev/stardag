import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  type ImperativePanelHandle,
  Panel,
  PanelGroup,
  PanelResizeHandle,
} from "react-resizable-panels";
import { fetchBuild, fetchBuildGraph, fetchTasksInBuild } from "../api/tasks";
import { useWorkspace } from "../context/WorkspaceContext";
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
}

export function BuildView({ buildId, onBack }: BuildViewProps) {
  const { activeWorkspace } = useWorkspace();
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
    if (!activeWorkspace?.id || !buildId) {
      setLoading(false);
      return;
    }

    setLoading(true);
    setError(null);
    try {
      const [buildData, tasksData, graphData] = await Promise.all([
        fetchBuild(buildId, activeWorkspace.id),
        fetchTasksInBuild(buildId, { workspace_id: activeWorkspace.id }),
        fetchBuildGraph(buildId, activeWorkspace.id),
      ]);
      setBuild(buildData);
      setAllTasks(tasksData);
      setGraph(graphData);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load build");
    } finally {
      setLoading(false);
    }
  }, [activeWorkspace?.id, buildId]);

  useEffect(() => {
    loadBuild();
  }, [loadBuild]);

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
        workspace_id: build?.workspace_id ?? "",
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
              className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                build.status === "completed"
                  ? "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-400"
                  : build.status === "failed"
                    ? "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-400"
                    : build.status === "running"
                      ? "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-400"
                      : "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-400"
              }`}
            >
              {build.status}
            </span>
          </div>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {allTasks.length} tasks &middot; Created{" "}
            {new Date(build.created_at).toLocaleString()}
          </p>
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
                    onClose={() => setSelectedTask(null)}
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
