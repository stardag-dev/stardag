import { useEffect, useState, useCallback, useMemo } from "react";
import { fetchBuilds, fetchTasksInBuild, fetchBuildGraph } from "../api/tasks";
import { useWorkspace } from "../context/WorkspaceContext";
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
  nameFilter: string;
  setNameFilter: (filter: string) => void;
  statusFilter: TaskStatus | "";
  setStatusFilter: (status: TaskStatus | "") => void;
  pageSize: number;
  totalPages: number;
  refresh: () => void;
  selectBuild: (buildId: string) => void;
}

export function useTasks(pageSize = 20): UseTasksReturn {
  const { activeWorkspace } = useWorkspace();

  // Raw data from API
  const [allTasks, setAllTasks] = useState<Task[]>([]);
  const [graph, setGraph] = useState<TaskGraphResponse | null>(null);
  const [builds, setBuilds] = useState<Build[]>([]);
  const [currentBuild, setCurrentBuild] = useState<Build | null>(null);

  // UI state
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nameFilter, setNameFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<TaskStatus | "">("");
  const [refreshKey, setRefreshKey] = useState(0);

  // Load builds for current workspace
  const loadBuilds = useCallback(async () => {
    // Don't fetch builds if no workspace is active (user hasn't selected/created org yet)
    if (!activeWorkspace?.id) {
      setBuilds([]);
      setCurrentBuild(null);
      setAllTasks([]);
      setGraph(null);
      setLoading(false);
      return [];
    }

    try {
      const response = await fetchBuilds({
        page: 1,
        page_size: 50,
        workspace_id: activeWorkspace.id,
      });
      setBuilds(response.builds);
      // Select most recent build if no current build or workspace changed
      if (response.builds.length > 0) {
        setCurrentBuild(response.builds[0]);
      } else {
        setCurrentBuild(null);
        // Clear stale data when no builds for this workspace
        setAllTasks([]);
        setGraph(null);
      }
      return response.builds;
    } catch (err) {
      console.error("Failed to load builds:", err);
      return [];
    }
  }, [activeWorkspace?.id]);

  // Load tasks for current build (no filtering - get all)
  const loadTasks = useCallback(async () => {
    if (!currentBuild || !activeWorkspace?.id) {
      setLoading(false);
      return;
    }

    setLoading(true);
    setError(null);
    try {
      // Fetch all tasks and graph in parallel
      const [tasksData, graphData] = await Promise.all([
        fetchTasksInBuild(currentBuild.id, { workspace_id: activeWorkspace.id }),
        fetchBuildGraph(currentBuild.id, activeWorkspace.id),
      ]);

      setAllTasks(tasksData);
      setGraph(graphData);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load tasks");
    } finally {
      setLoading(false);
    }
  }, [currentBuild, activeWorkspace?.id]);

  // Load builds when workspace changes
  useEffect(() => {
    loadBuilds();
  }, [loadBuilds, refreshKey]);

  // Load tasks when build changes
  useEffect(() => {
    loadTasks();
  }, [loadTasks, refreshKey]);

  // Client-side filtering
  const filteredTasks = useMemo(() => {
    let result = allTasks;

    if (nameFilter) {
      const lowerFilter = nameFilter.toLowerCase();
      result = result.filter((t) => t.task_name.toLowerCase().includes(lowerFilter));
    }

    if (statusFilter) {
      result = result.filter((t) => t.status === statusFilter);
    }

    return result;
  }, [allTasks, nameFilter, statusFilter]);

  // Tasks with context for DAG (shows all tasks, marks which match filter)
  const tasksWithContext = useMemo(() => {
    if (!graph || !currentBuild) return [];

    const matchingTaskIds = new Set(filteredTasks.map((t) => t.task_id));
    const noFilter = !nameFilter && !statusFilter;

    return graph.nodes.map((node) => {
      const fullTask = allTasks.find((t) => t.task_id === node.task_id);
      return {
        id: node.id,
        task_id: node.task_id,
        workspace_id: currentBuild.workspace_id,
        task_namespace: node.task_namespace,
        task_name: node.task_name,
        task_data: fullTask?.task_data ?? {},
        version: fullTask?.version ?? null,
        created_at: fullTask?.created_at ?? currentBuild.created_at,
        status: node.status,
        started_at: fullTask?.started_at ?? null,
        completed_at: fullTask?.completed_at ?? null,
        error_message: fullTask?.error_message ?? null,
        isFilterMatch: noFilter || matchingTaskIds.has(node.task_id),
      };
    });
  }, [graph, currentBuild, allTasks, filteredTasks, nameFilter, statusFilter]);

  const handleSetNameFilter = useCallback((filter: string) => {
    setNameFilter(filter);
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

  // Pagination on filtered tasks
  const total = filteredTasks.length;
  const totalPages = Math.ceil(total / pageSize);
  const paginatedTasks = filteredTasks.slice((page - 1) * pageSize, page * pageSize);

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
    nameFilter,
    setNameFilter: handleSetNameFilter,
    statusFilter,
    setStatusFilter: handleSetStatusFilter,
    pageSize,
    totalPages,
    refresh,
    selectBuild,
  };
}
