import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  type ImperativePanelHandle,
  Panel,
  PanelGroup,
  PanelResizeHandle,
} from "react-resizable-panels";
import { fetchBuild, fetchBuildGraph, fetchTasksInBuild } from "../api/tasks";
import { useBreadcrumb, type BreadcrumbItem } from "../context/BreadcrumbContext";
import { useEnvironment } from "../context/EnvironmentContext";
import type {
  Build,
  Task,
  TaskGraphExtendedResponse,
  TaskGraphResponse,
  TaskStatus,
  TaskWithContext,
} from "../types/task";
import { isExtendedResponse } from "../types/task";
import { BuildSchedulingPanel } from "./BuildSchedulingPanel";
import { rootsSatisfiedFrom } from "../utils/claims";
import { BuildFailureReason } from "./BuildFailureReason";
import { BuildStatusBadge } from "./BuildStatusBadge";
import { BuildControlsDialog } from "./BuildControlsDialog";
import { BuildInfoDialog } from "./BuildInfoDialog";
import { ToolbarButton } from "./ui/ToolbarButton";
import { DagControls, type DagControlsState } from "./DagControls";
import { DagGraph } from "./DagGraph";
import {
  createPositionCache,
  type LayoutDirection,
  type PositionCache,
} from "./dagLayout";
import { TaskDetail } from "./TaskDetail";
import { TaskFilters } from "./TaskFilters";
import { TaskTable } from "./TaskTable";

interface BuildViewProps {
  buildId: string;
  onBack: () => void;
  onNavigateToBuild?: (buildId: string) => void;
}

export function BuildView({ buildId, onBack, onNavigateToBuild }: BuildViewProps) {
  const { activeEnvironment } = useEnvironment();
  const { setItems: setBreadcrumb } = useBreadcrumb();
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);

  // Data state
  const [build, setBuild] = useState<Build | null>(null);
  const [allTasks, setAllTasks] = useState<Task[]>([]);
  const [graph, setGraph] = useState<
    TaskGraphResponse | TaskGraphExtendedResponse | null
  >(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Upstream traversal controls
  const [dagControls, setDagControls] = useState<DagControlsState>({
    upstreamDepth: 0,
    downstreamDepth: 0,
    maxPerType: 5,
  });

  // DAG collapse state - expanded by default
  const [showDag, setShowDag] = useState(true);
  const [dagFullscreen, setDagFullscreen] = useState(false);
  const [dagDirection, setDagDirection] = useState<LayoutDirection>("LR");
  const dagPanelRef = useRef<ImperativePanelHandle>(null);
  const dagPositionCacheRef = useRef<PositionCache>(createPositionCache());

  // Refresh state
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(false);
  // Bumped on every build refresh (manual, auto or post-mutation). The
  // scheduling panel refetches off this rather than running its own timer,
  // so the 5s auto-refresh drives one request stream, not two.
  const [refreshToken, setRefreshToken] = useState(0);
  const autoRefreshRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Pending single click, held for the double-click window.
  //
  // Cleared on a change of build or environment, not only on unmount:
  // this component stays mounted through both, and a timer left running
  // fires with the *previous* identity's closure. That is not merely a
  // wasted request — it bumps the shared load epoch, which discards the
  // load that is legitimately in flight, and then applies its own older
  // answer as the current one.
  const clickTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (clickTimerRef.current !== null) {
        clearTimeout(clickTimerRef.current);
        clickTimerRef.current = null;
      }
    },
    [buildId, activeEnvironment?.id],
  );

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

  // What is on screen, and what was asked for. The pair is the identity
  // of a load: the same build id read under a different environment is a
  // different thing to show, and comparing only the build id misses an
  // environment switch entirely — `BuildView` stays mounted through one.
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const requestedKey = `${activeEnvironment?.id ?? ""}:${buildId}`;
  // Stale-response guard, the same shape the panels in this view already
  // use. Without it a slow read for the previous build can land after
  // navigation, overwrite `build`, and drop `loading` — at which point
  // no guard downstream can tell that what is rendered is the wrong
  // build.
  const loadEpochRef = useRef(0);

  // Load build data
  const loadBuild = useCallback(async () => {
    if (!activeEnvironment?.id || !buildId) {
      setLoading(false);
      return;
    }

    const epoch = ++loadEpochRef.current;
    const fresh = () => loadEpochRef.current === epoch;
    const key = `${activeEnvironment.id}:${buildId}`;
    setLoading(true);
    setError(null);
    try {
      const [buildData, tasksData, graphData] = await Promise.all([
        fetchBuild(buildId, activeEnvironment.id),
        fetchTasksInBuild(buildId, { environment_id: activeEnvironment.id }),
        fetchBuildGraph(buildId, activeEnvironment.id, {
          upstream_depth: dagControls.upstreamDepth,
          downstream_depth: dagControls.downstreamDepth,
          max_per_type_per_level: dagControls.maxPerType,
        }),
      ]);
      if (!fresh()) return;
      setBuild(buildData);
      setAllTasks(tasksData);
      setGraph(graphData);
      setLoadedKey(key);
    } catch (err) {
      if (!fresh()) return;
      setError(err instanceof Error ? err.message : "Failed to load build");
    } finally {
      // Only the newest request may declare the view settled. A
      // superseded one clearing this would expose whatever is on screen
      // as though it were the answer.
      if (fresh()) setLoading(false);
    }
  }, [
    activeEnvironment?.id,
    buildId,
    dagControls.upstreamDepth,
    dagControls.downstreamDepth,
    dagControls.maxPerType,
  ]);

  useEffect(() => {
    loadBuild();
  }, [loadBuild]);

  // Reset build-scoped UI state when the user navigates between builds.
  // Otherwise, filters/pagination from Build A can silently apply to
  // Build B, and a previously selected task can leave the detail panel
  // and breadcrumb pointing at stale data from the prior build.
  useEffect(() => {
    setNameFilter("");
    setStatusFilter("");
    setPage(1);
    setSelectedTask(null);
  }, [buildId]);

  // Refresh handler
  // Single-flight, on a ref rather than on `refreshing`.
  //
  // `refreshing` is a state snapshot, and every caller cleared it on its
  // own completion — so with the 5-second interval firing regardless of
  // what was already in flight, an older completion could clear the flag
  // while a newer request was still running, and the next caller would
  // start another on top. The ref is the fact; `refreshing` is only the
  // spin on the icon.
  //
  // It guards *refreshes* rather than `loadBuild` itself, because a load
  // triggered by a change of build, environment or DAG controls is a
  // different request and must supersede rather than be skipped.
  const refreshInFlightRef = useRef(false);
  const handleRefresh = useCallback(async () => {
    if (refreshInFlightRef.current) return;
    refreshInFlightRef.current = true;
    setRefreshing(true);
    setRefreshToken((token) => token + 1);
    try {
      await loadBuild();
    } finally {
      refreshInFlightRef.current = false;
      setRefreshing(false);
    }
  }, [loadBuild]);

  // Auto-refreshing a build that has stopped is pointless, and the
  // interval below has always declined to do it — but the toolbar used
  // to light up and claim otherwise, because nothing connected the two.
  const canAutoRefresh = build?.status === "running";

  // Turn it off when the build stops running, so the control cannot go
  // on asserting something the interval is not doing.
  useEffect(() => {
    if (!canAutoRefresh && autoRefresh) setAutoRefresh(false);
  }, [canAutoRefresh, autoRefresh]);

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

  // ESC to exit DAG fullscreen
  useEffect(() => {
    if (!dagFullscreen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDagFullscreen(false);
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [dagFullscreen]);

  // Single click refreshes; double-click toggles auto-refresh.
  //
  // The single click is *deferred* by the double-click window rather
  // than acted on at once. Acting immediately meant the two gestures
  // overlapped: with auto-refresh on, the first click of a double-click
  // turned it off and the second turned it straight back on, so the
  // gesture could switch it on but never off.
  //
  // The button also stays enabled throughout, because disabling it
  // during the in-flight first refresh is the other thing that made the
  // double-click unreachable.
  const handleRefreshClick = useCallback(() => {
    if (clickTimerRef.current !== null) {
      // Second click inside the window: this is the double.
      clearTimeout(clickTimerRef.current);
      clickTimerRef.current = null;
      if (canAutoRefresh) setAutoRefresh((previous) => !previous);
      else handleRefresh();
      return;
    }
    clickTimerRef.current = setTimeout(() => {
      clickTimerRef.current = null;
      if (autoRefresh) setAutoRefresh(false);
      else handleRefresh();
    }, 300);
  }, [autoRefresh, canAutoRefresh, handleRefresh]);

  const handleBuildOverridden = useCallback(
    (updated: Build) => {
      if (updated.id !== buildId) return;
      setBuild(updated);
    },
    [buildId],
  );

  // Update breadcrumb navigation
  useEffect(() => {
    const items: BreadcrumbItem[] = [
      { label: "Builds", onClick: onBack },
      {
        label: build?.name ?? buildId.slice(0, 8),
        title: buildId,
        // The status badge and nothing else. The executor, reactive and
        // scope chips say what this build *is* rather than where you
        // are, so they belong in the info section of the toolbar below —
        // in the trail they crowded out the one thing a breadcrumb is
        // for, which is knowing which build you have open.
        detail: build ? (
          <BuildStatusBadge status={build.status} isResumed={build.is_resumed} />
        ) : undefined,
      },
    ];
    if (selectedTask) {
      // A task id is a full UUID and the trail is not where it is read —
      // the detail pane shows it in full, with a copy button. Here it
      // only has to distinguish one task from another.
      items.push({
        label: selectedTask.task_id.slice(0, 8),
        title: selectedTask.task_id,
      });
    }
    setBreadcrumb(items);
    return () => setBreadcrumb([]);
  }, [build, buildId, selectedTask, onBack, setBreadcrumb]);

  // Placeholder ("phantom") rows no longer exist on a current server — an
  // edge may only name a registered task — but an older Registry API still
  // returns them, so the flag is still honoured for that case only.
  const realTasks = useMemo(() => allTasks.filter((t) => !t.is_phantom), [allTasks]);

  // Whether every root has since completed — see `rootsSatisfiedFrom`. Computed
  // from the task list this view already fetched rather than from the frontier,
  // which only the scheduling panel holds; both call the same rule so the two
  // cannot disagree about the same build.
  const rootsSuperseded = useMemo(() => {
    const statusById = new Map(
      allTasks.map((t) => [t.task_id, t.latest_status ?? t.status]),
    );
    return rootsSatisfiedFrom(build?.root_task_ids ?? [], (id) => statusById.get(id));
  }, [allTasks, build?.root_task_ids]);

  // Client-side filtering
  const filteredTasks = realTasks.filter((task) => {
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

  // Extended graph metadata
  const extendedGraph = graph && isExtendedResponse(graph) ? graph : null;

  // Tasks with context for DAG - memoized to avoid recalculating on every render
  const tasksWithContext: TaskWithContext[] = useMemo(() => {
    if (!graph) return [];

    const matchingTaskIds = new Set(filteredTasks.map((t) => t.task_id));
    const noFilter = !nameFilter && !statusFilter;

    return graph.nodes.map((node) => {
      const fullTask = allTasks.find((t) => t.task_id === node.task_id);
      const isPrimary =
        "is_primary" in node ? (node as { is_primary: boolean }).is_primary : true;

      return {
        id: node.id,
        task_id: node.task_id,
        environment_id: build?.environment_id ?? "",
        task_namespace: node.task_namespace,
        task_name: node.task_name,
        task_data: fullTask?.task_data ?? {},
        version: fullTask?.version ?? null,
        output_uri: fullTask?.output_uri ?? null,
        created_at: fullTask?.created_at ?? build?.created_at ?? "",
        status: node.status,
        started_at: fullTask?.started_at ?? null,
        completed_at: fullTask?.completed_at ?? null,
        error_message: fullTask?.error_message ?? null,
        artifact_count: node.artifact_count,
        isFilterMatch: isPrimary && (noFilter || matchingTaskIds.has(node.task_id)),
        // Cross-build status fields
        waiting_for_lock: fullTask?.waiting_for_lock,
        status_build_id: fullTask?.status_build_id,
        // Executor identity (DAG node hover + detail panel)
        latest_executor: fullTask?.latest_executor,
        latest_executor_ref: fullTask?.latest_executor_ref,
        latest_executor_metadata: fullTask?.latest_executor_metadata,
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

  // The loader takes over the screen only when what is loaded is not
  // what was asked for.
  //
  // It used to be a plain `if (loading)`, which meant every refresh
  // replaced the whole view — toolbar included — including each
  // 5-second auto-refresh tick. Besides the flashing, that is half of
  // why the advertised double-click could not work: the button the
  // second click needed had unmounted. A refresh keeps the view, and
  // the refresh icon's own spin is the right size of signal.
  //
  // But `!build` is the wrong test for that, because this component
  // stays mounted across a change of build *or environment* and holds
  // the previous data while the new load runs — so the old DAG, rows
  // and controls would render under the new header. Comparing what is
  // loaded against what was asked for distinguishes the two cases: a
  // refresh matches and keeps the view, a change of either does not and
  // gets the loader.
  if (loading && loadedKey !== requestedKey) {
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
      {/* Main content */}
      <div className="flex-1 overflow-hidden">
        <PanelGroup direction="horizontal">
          {/* Left column: Filters + DAG + List */}
          <Panel defaultSize={selectedTask ? 70 : 100} minSize={40}>
            <div className="flex h-full flex-col">
              {/* The build view tool and info bar.

                  Two clusters. On the left, everything about *this list
                  of tasks*: narrowing it, how many there are, and
                  refreshing it. On the right, everything about *the
                  build*: what it is, what the scheduler makes of it, and
                  what you can do to it.

                  Every one of the right-hand controls is an icon with a
                  tooltip that appears at once — see `ui/ToolbarButton`.
                  Between them they replaced four coloured pills and two
                  full-width bands, so nothing now sits between this row
                  and the DAG except a failed build's reason. */}
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-gray-200 bg-white px-3 py-2 dark:border-gray-700 dark:bg-gray-800">
                {/* The task list */}
                <div className="flex min-w-0 flex-1 items-center gap-2">
                  <TaskFilters
                    nameFilter={nameFilter}
                    onNameFilterChange={handleSetNameFilter}
                    statusFilter={statusFilter}
                    onStatusFilterChange={handleSetStatusFilter}
                  />
                  <span className="text-xs whitespace-nowrap text-gray-500 dark:text-gray-400">
                    {realTasks.length} task{realTasks.length === 1 ? "" : "s"}
                  </span>
                  <ToolbarButton
                    label={autoRefresh ? "Stop auto-refreshing" : "Refresh"}
                    hint={
                      autoRefresh
                        ? "Refreshing every 5 seconds"
                        : canAutoRefresh
                          ? "Double-click to refresh every 5 seconds"
                          : undefined
                    }
                    onClick={handleRefreshClick}
                    // Deliberately NOT disabled while refreshing. It used
                    // to be, which quietly made the advertised
                    // double-click impossible: the first click starts a
                    // fetch, `refreshing` goes true, the button disables,
                    // and the second click never lands. Re-entry is
                    // guarded in the handler instead.
                    active={autoRefresh}
                  >
                    <svg
                      aria-hidden="true"
                      className={`h-4 w-4 ${
                        refreshing || autoRefresh ? "animate-spin" : ""
                      }`}
                      fill="none"
                      stroke="currentColor"
                      strokeWidth={2}
                      viewBox="0 0 24 24"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
                      />
                    </svg>
                  </ToolbarButton>
                </div>

                {/* The build */}
                <div className="flex items-center gap-1.5">
                  <BuildInfoDialog build={build} />
                  {activeEnvironment?.id && (
                    <BuildSchedulingPanel
                      buildId={buildId}
                      environmentId={activeEnvironment.id}
                      buildStatus={build.status}
                      refreshToken={refreshToken}
                      onNavigateToBuild={onNavigateToBuild}
                      onChanged={handleRefresh}
                    />
                  )}
                  {activeEnvironment?.id && (
                    <BuildControlsDialog
                      // Keyed by environment *and* build. The dialog
                      // relies on remounting to clear its scan, filters,
                      // ticks and open state rather than on a reset
                      // effect — see its own note on why — and this view
                      // stays mounted across an environment switch as
                      // well as a build one, so a key naming only the
                      // build leaves the previous environment's
                      // executions on screen and actionable.
                      key={`${activeEnvironment.id}:${buildId}`}
                      buildId={buildId}
                      environmentId={activeEnvironment.id}
                      buildStatus={build.status}
                      refreshToken={refreshToken}
                      // Guarded rather than `setBuild` directly: an
                      // override is async, this view stays mounted
                      // across a change of build, and a slow one
                      // resolving afterwards would write the previous
                      // build's record into the current build's view.
                      onBuildChanged={handleBuildOverridden}
                    />
                  )}
                </div>
              </div>

              {/* Why it failed — above the scheduling panel, which goes quiet
                  on a failed build. See BuildFailureReason. */}
              <BuildFailureReason
                status={build.status}
                message={build.latest_error_message}
                failedAt={build.completed_at}
                superseded={rootsSuperseded}
              />

              {/* DAG header - always visible */}
              <div className="flex items-center justify-between border-b border-gray-200 px-4 py-2 dark:border-gray-700">
                <button
                  onClick={handleToggleDag}
                  // A disclosure control whose only state cue was a rotated
                  // chevron: invisible to assistive tech, and to any test that
                  // isn't reading CSS classes.
                  aria-expanded={showDag}
                  aria-controls="build-dag-panel"
                  className="flex items-center gap-2 text-sm text-gray-700 hover:text-gray-900 dark:text-gray-300 dark:hover:text-gray-100"
                >
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
                </button>
                {showDag && (
                  <div className="flex items-center gap-2">
                    <DagControls
                      value={dagControls}
                      onChange={setDagControls}
                      primaryCount={realTasks.length}
                      upstreamCount={extendedGraph?.total_upstream_count ?? 0}
                      downstreamCount={extendedGraph?.total_downstream_count ?? 0}
                      groupCount={extendedGraph?.groups.length ?? 0}
                      truncated={extendedGraph?.truncated ?? false}
                    />
                    <button
                      onClick={() => setDagFullscreen(true)}
                      className="rounded p-1 text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                      title="Fullscreen DAG"
                    >
                      <svg
                        className="h-4 w-4"
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                        strokeWidth={2}
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5v-4m0 4h-4m4 0l-5-5"
                        />
                      </svg>
                    </button>
                  </div>
                )}
              </div>

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
                  {showDag && !dagFullscreen && (
                    <div id="build-dag-panel" className="h-full">
                      <DagGraph
                        tasks={tasksWithContext}
                        graph={graph}
                        selectedTaskId={selectedTask?.task_id ?? null}
                        onTaskClick={handleDagTaskClick}
                        buildId={buildId}
                        onStatusBuildClick={onNavigateToBuild}
                        direction={dagDirection}
                        onDirectionChange={setDagDirection}
                        positionCache={dagPositionCacheRef}
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

      {/* DAG fullscreen overlay */}
      {dagFullscreen && (
        <div className="fixed inset-0 z-50 flex flex-col bg-white dark:bg-gray-900">
          <div className="flex items-center justify-between border-b border-gray-200 px-4 py-2 dark:border-gray-700">
            <div className="flex items-center gap-3">
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                DAG View
              </span>
              <DagControls
                value={dagControls}
                onChange={setDagControls}
                primaryCount={realTasks.length}
                upstreamCount={extendedGraph?.total_upstream_count ?? 0}
                downstreamCount={extendedGraph?.total_downstream_count ?? 0}
                groupCount={extendedGraph?.groups.length ?? 0}
                truncated={extendedGraph?.truncated ?? false}
              />
            </div>
            <button
              onClick={() => setDagFullscreen(false)}
              className="rounded p-1.5 text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
              title="Exit fullscreen (Esc)"
            >
              <svg
                className="h-5 w-5"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M6 18L18 6M6 6l12 12"
                />
              </svg>
            </button>
          </div>
          <div className="flex-1">
            <DagGraph
              tasks={tasksWithContext}
              graph={graph}
              selectedTaskId={selectedTask?.task_id ?? null}
              onTaskClick={handleDagTaskClick}
              buildId={buildId}
              onStatusBuildClick={onNavigateToBuild}
              direction={dagDirection}
              onDirectionChange={setDagDirection}
              positionCache={dagPositionCacheRef}
            />
          </div>
        </div>
      )}
    </div>
  );
}
