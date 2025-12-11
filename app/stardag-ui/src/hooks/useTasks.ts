import { useEffect, useState, useCallback } from "react";
import {
  fetchBuilds,
  fetchTasksInBuild,
  fetchBuildGraph,
  type TaskFilters,
} from "../api/tasks";
import type { Build, Task, TaskStatus, TaskGraphResponse } from "../types/task";

export interface TaskWithContext extends Task {
  isFilterMatch: boolean;
}

interface UseTasksReturn {
  tasks: Task[];
  tasksWithContext: TaskWithContext[];
  graph: TaskGraphResponse | null;
  currentBuild: Build | null;
  builds: Build[];
  total: number;
  page: number;
  setPage: (page: number) => void;
  loading: boolean;
  error: string | null;
  familyFilter: string;
  setFamilyFilter: (filter: string) => void;
  statusFilter: TaskStatus | "";
  setStatusFilter: (status: TaskStatus | "") => void;
  pageSize: number;
  totalPages: number;
  refresh: () => void;
  selectBuild: (buildId: string) => void;
}

export function useTasks(pageSize = 20): UseTasksReturn {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [tasksWithContext, setTasksWithContext] = useState<TaskWithContext[]>([]);
  const [graph, setGraph] = useState<TaskGraphResponse | null>(null);
  const [builds, setBuilds] = useState<Build[]>([]);
  const [currentBuild, setCurrentBuild] = useState<Build | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [familyFilter, setFamilyFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<TaskStatus | "">("");
  const [refreshKey, setRefreshKey] = useState(0);

  // Load builds first
  const loadBuilds = useCallback(async () => {
    try {
      const response = await fetchBuilds({ page: 1, page_size: 50 });
      setBuilds(response.builds);
      // Select most recent build if no current build
      if (response.builds.length > 0 && !currentBuild) {
        setCurrentBuild(response.builds[0]);
      }
      return response.builds;
    } catch (err) {
      console.error("Failed to load builds:", err);
      return [];
    }
  }, [currentBuild]);

  // Load tasks for current build
  const loadTasks = useCallback(async () => {
    if (!currentBuild) {
      setLoading(false);
      return;
    }

    setLoading(true);
    setError(null);
    try {
      const filters: TaskFilters = {};
      if (familyFilter) filters.task_family = familyFilter;
      if (statusFilter) filters.status = statusFilter;

      // Fetch tasks and graph in parallel
      const [tasksData, graphData] = await Promise.all([
        fetchTasksInBuild(currentBuild.id, filters),
        fetchBuildGraph(currentBuild.id),
      ]);

      setTasks(tasksData);
      setGraph(graphData);
      setTotal(tasksData.length);

      // Mark which tasks match the filter
      const matchingTaskIds = new Set(tasksData.map((t) => t.task_id));

      // Create tasks with context from graph nodes
      const withContext: TaskWithContext[] = graphData.nodes.map((node) => {
        // Find full task data if available
        const fullTask = tasksData.find((t) => t.task_id === node.task_id);
        return {
          id: node.id,
          task_id: node.task_id,
          workspace_id: currentBuild.workspace_id,
          task_namespace: node.task_namespace,
          task_family: node.task_family,
          task_data: fullTask?.task_data ?? {},
          version: fullTask?.version ?? null,
          created_at: fullTask?.created_at ?? currentBuild.created_at,
          status: node.status,
          started_at: fullTask?.started_at ?? null,
          completed_at: fullTask?.completed_at ?? null,
          error_message: fullTask?.error_message ?? null,
          isFilterMatch:
            matchingTaskIds.has(node.task_id) || matchingTaskIds.size === 0,
        };
      });

      setTasksWithContext(withContext);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load tasks");
    } finally {
      setLoading(false);
    }
  }, [currentBuild, familyFilter, statusFilter]);

  // Initial load of builds
  useEffect(() => {
    loadBuilds();
  }, [refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // Load tasks when build or filters change
  useEffect(() => {
    loadTasks();
  }, [loadTasks, refreshKey]);

  const handleSetFamilyFilter = useCallback((filter: string) => {
    setFamilyFilter(filter);
    setPage(1);
  }, []);

  const handleSetStatusFilter = useCallback((status: TaskStatus | "") => {
    setStatusFilter(status);
    setPage(1);
  }, []);

  const refresh = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  const selectBuild = useCallback(
    (buildId: string) => {
      const build = builds.find((b) => b.id === buildId);
      if (build) {
        setCurrentBuild(build);
      }
    },
    [builds],
  );

  // Paginate tasks client-side since we fetch all from build
  const paginatedTasks = tasks.slice((page - 1) * pageSize, page * pageSize);
  const totalPages = Math.ceil(total / pageSize);

  return {
    tasks: paginatedTasks,
    tasksWithContext,
    graph,
    currentBuild,
    builds,
    total,
    page,
    setPage,
    loading,
    error,
    familyFilter,
    setFamilyFilter: handleSetFamilyFilter,
    statusFilter,
    setStatusFilter: handleSetStatusFilter,
    pageSize,
    totalPages,
    refresh,
    selectBuild,
  };
}
