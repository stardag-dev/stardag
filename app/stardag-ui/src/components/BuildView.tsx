import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  type ImperativePanelHandle,
  Panel,
  PanelGroup,
  PanelResizeHandle,
} from "react-resizable-panels";
import { useBreadcrumb, type BreadcrumbItem } from "../context/BreadcrumbContext";
import { useEnvironment } from "../context/EnvironmentContext";
import { useBuildPlan } from "../hooks/useBuildPlan";
import { useDeployments } from "../hooks/useDeployments";
import type { Build, TaskStatus } from "../types/task";
import { rootsCompleted } from "../utils/builds";
import { shortTaskId } from "../utils/ids";
import { BuildControlsDialog } from "./BuildControlsDialog";
import { BuildFailureReason } from "./BuildFailureReason";
import { BuildInfoDialog } from "./BuildInfoDialog";
import { BuildSchedulingPanel } from "./BuildSchedulingPanel";
import { BuildStatusBadge } from "./BuildStatusBadge";
import { DagGraph } from "./DagGraph";
import {
  createPositionCache,
  type LayoutDirection,
  type PositionCache,
} from "./dagLayout";
import { MemberTable } from "./MemberTable";
import { TaskDetail } from "./TaskDetail";
import { TaskFilters } from "./TaskFilters";
import { ToolbarButton } from "./ui/ToolbarButton";

interface BuildViewProps {
  buildId: string;
  onBack: () => void;
  onOpenTask?: (taskId: string) => void;
}

const PAGE_SIZE = 20;
// The window in which a second click on refresh counts as a double-click.
const DOUBLE_CLICK_MS = 300;

/**
 * One build, over its **active plan**: the plan's scope and state, its
 * members as a table and as a graph over instance edges, and the selected
 * task's detail. The toolbar opens the build's info, scheduling state and
 * controls (stop list and overrides).
 */
export function BuildView(props: BuildViewProps) {
  const { activeEnvironment } = useEnvironment();
  // Keyed on environment and build: a change of either remounts the view,
  // so no selection, filter or layout survives into the other identity.
  return (
    <BuildViewForIdentity
      key={`${activeEnvironment?.id ?? ""}:${props.buildId}`}
      environmentId={activeEnvironment?.id}
      {...props}
    />
  );
}

function BuildViewForIdentity({
  buildId,
  onBack,
  onOpenTask,
  environmentId,
}: BuildViewProps & { environmentId: string | undefined }) {
  const { setItems: setBreadcrumb } = useBreadcrumb();
  const plan = useBuildPlan(buildId, environmentId);
  const { byId: deploymentsById } = useDeployments(environmentId);

  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [nameFilter, setNameFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<TaskStatus | "">("");
  const [page, setPage] = useState(1);
  const [refreshToken, setRefreshToken] = useState(0);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [showDag, setShowDag] = useState(true);
  const [dagDirection, setDagDirection] = useState<LayoutDirection>("LR");
  const dagPanelRef = useRef<ImperativePanelHandle>(null);
  const positionCacheRef = useRef<PositionCache>(createPositionCache());

  const requestedKey = `${environmentId ?? ""}:${buildId}`;
  const { build, frontier, view, reload, setBuild } = plan;

  const refresh = useCallback(async () => {
    setRefreshToken((t) => t + 1);
    await reload();
  }, [reload]);

  // Auto-refreshing a build that has stopped is pointless: the interval
  // declines to run, and the control is switched off (adjusted during
  // render) so it cannot go on claiming otherwise.
  const canAutoRefresh = build?.status === "running";
  if (!canAutoRefresh && autoRefresh) setAutoRefresh(false);
  useEffect(() => {
    if (!autoRefresh || !canAutoRefresh) return;
    const handle = setInterval(refresh, 5000);
    return () => clearInterval(handle);
  }, [autoRefresh, canAutoRefresh, refresh]);

  // Single click refreshes; double-click toggles auto-refresh (v1's
  // affordance). The single click is deferred by the double-click window:
  // acting at once made the gestures overlap, so with auto-refresh on the
  // first click of a double turned it off and the second straight back on.
  // The button stays enabled while a refresh is in flight, or the second
  // click could never land. The pending timer is cleared on unmount; the
  // view remounts on a change of build or environment (see `BuildView`).
  const clickTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (clickTimerRef.current !== null) clearTimeout(clickTimerRef.current);
    },
    [],
  );
  const handleRefreshClick = useCallback(() => {
    if (clickTimerRef.current !== null) {
      clearTimeout(clickTimerRef.current);
      clickTimerRef.current = null;
      if (canAutoRefresh) setAutoRefresh((previous) => !previous);
      else void refresh();
      return;
    }
    clickTimerRef.current = setTimeout(() => {
      clickTimerRef.current = null;
      if (autoRefresh) setAutoRefresh(false);
      else void refresh();
    }, DOUBLE_CLICK_MS);
  }, [autoRefresh, canAutoRefresh, refresh]);

  const handleBuildChanged = useCallback(
    (updated: Build) => {
      if (updated.id !== buildId) return;
      setBuild(updated);
      void reload();
    },
    [buildId, setBuild, reload],
  );

  useEffect(() => {
    const items: BreadcrumbItem[] = [
      { label: "Builds", onClick: onBack },
      {
        label: build?.name ?? buildId.slice(0, 8),
        title: buildId,
        detail: build ? (
          <BuildStatusBadge status={build.status} isResumed={build.is_resumed} />
        ) : undefined,
      },
    ];
    if (selectedTaskId) {
      items.push({ label: shortTaskId(selectedTaskId), title: selectedTaskId });
    }
    setBreadcrumb(items);
    return () => setBreadcrumb([]);
  }, [build, buildId, selectedTaskId, onBack, setBreadcrumb]);

  const members = useMemo(() => view?.members ?? [], [view]);
  const filtered = useMemo(
    () =>
      members.filter(
        (m) =>
          (!nameFilter ||
            m.task_name.toLowerCase().includes(nameFilter.toLowerCase())) &&
          (!statusFilter || m.status === statusFilter),
      ),
    [members, nameFilter, statusFilter],
  );
  const mutedTaskIds = useMemo(() => {
    if (!nameFilter && !statusFilter) return undefined;
    const kept = new Set(filtered.map((m) => m.task_id));
    return new Set(members.filter((m) => !kept.has(m.task_id)).map((m) => m.task_id));
  }, [members, filtered, nameFilter, statusFilter]);

  const selectedMember = members.find((m) => m.task_id === selectedTaskId) ?? null;
  const deployment = frontier?.deployment_id
    ? deploymentsById.get(frontier.deployment_id) ?? null
    : null;

  if (plan.loading && plan.loadedKey !== requestedKey) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
      </div>
    );
  }
  if (plan.error || !build || !environmentId) {
    return (
      <div className="flex h-full flex-col items-center justify-center text-red-500">
        <p>{plan.error ?? "Build not found"}</p>
        <button
          onClick={onBack}
          className="mt-4 rounded-md bg-gray-100 px-4 py-2 text-sm text-gray-700 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
        >
          Go back
        </button>
      </div>
    );
  }

  const dag = (
    <DagGraph
      view={view ?? { members: [], edges: [], complete: true }}
      selectedTaskId={selectedTaskId}
      onTaskClick={setSelectedTaskId}
      mutedTaskIds={mutedTaskIds}
      direction={dagDirection}
      onDirectionChange={setDagDirection}
      positionCache={positionCacheRef}
    />
  );

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-hidden">
        <PanelGroup direction="horizontal">
          <Panel defaultSize={selectedTaskId ? 65 : 100} minSize={40}>
            <div className="flex h-full flex-col">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-gray-200 bg-white px-3 py-2 dark:border-gray-700 dark:bg-gray-800">
                <div className="flex min-w-0 flex-1 items-center gap-2">
                  <TaskFilters
                    nameFilter={nameFilter}
                    onNameFilterChange={(v) => {
                      setNameFilter(v);
                      setPage(1);
                    }}
                    statusFilter={statusFilter}
                    onStatusFilterChange={(v) => {
                      setStatusFilter(v);
                      setPage(1);
                    }}
                  />
                  <span className="text-xs whitespace-nowrap text-gray-500 dark:text-gray-400">
                    {members.length} member{members.length === 1 ? "" : "s"}
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
                    active={autoRefresh}
                  >
                    <svg
                      aria-hidden="true"
                      className={`h-4 w-4 ${
                        plan.loading || autoRefresh ? "animate-spin" : ""
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
                <div className="flex items-center gap-1.5">
                  <BuildInfoDialog
                    build={build}
                    environmentId={environmentId}
                    frontier={frontier}
                    deployment={deployment}
                  />
                  <BuildSchedulingPanel
                    buildId={buildId}
                    environmentId={environmentId}
                    buildStatus={build.status}
                    frontier={frontier}
                    frontierError={plan.frontierError}
                    refreshToken={refreshToken}
                    membershipComplete={view?.complete ?? true}
                    onOpenTask={setSelectedTaskId}
                  />
                  <BuildControlsDialog
                    key={requestedKey}
                    buildId={buildId}
                    environmentId={environmentId}
                    buildStatus={build.status}
                    refreshToken={refreshToken}
                    onBuildChanged={handleBuildChanged}
                    onOpenTask={setSelectedTaskId}
                  />
                </div>
              </div>

              {/* Why it failed, kept on screen: the scheduling dialog goes
                  quiet on a failed build. See BuildFailureReason. */}
              <BuildFailureReason
                status={build.status}
                message={build.error_message}
                failedAt={build.completed_at}
                superseded={rootsCompleted(members)}
              />

              <div className="flex items-center justify-between border-b border-gray-200 px-4 py-2 dark:border-gray-700">
                <button
                  onClick={() => {
                    const panel = dagPanelRef.current;
                    if (showDag) panel?.collapse();
                    else panel?.expand();
                    setShowDag(!showDag);
                  }}
                  aria-expanded={showDag}
                  aria-controls="build-dag-panel"
                  className="flex items-center gap-2 text-sm text-gray-700 hover:text-gray-900 dark:text-gray-300 dark:hover:text-gray-100"
                >
                  <svg
                    aria-hidden="true"
                    data-testid="dag-toggle-chevron"
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
                  <span className="font-medium">Plan graph</span>
                </button>
              </div>

              <PanelGroup direction="vertical" className="flex-1">
                <Panel
                  ref={dagPanelRef}
                  defaultSize={50}
                  minSize={0}
                  collapsible
                  onCollapse={() => setShowDag(false)}
                  onExpand={() => setShowDag(true)}
                >
                  {showDag && (
                    <div id="build-dag-panel" className="h-full">
                      {dag}
                    </div>
                  )}
                </Panel>
                <PanelResizeHandle className="h-1 cursor-row-resize bg-gray-200 hover:bg-blue-400 dark:bg-gray-700 dark:hover:bg-blue-500" />
                <Panel defaultSize={50} minSize={20}>
                  <MemberTable
                    members={filtered}
                    selectedTaskId={selectedTaskId}
                    onSelectTask={setSelectedTaskId}
                    page={page}
                    pageSize={PAGE_SIZE}
                    onPageChange={setPage}
                  />
                </Panel>
              </PanelGroup>
            </div>
          </Panel>

          {selectedTaskId && (
            <>
              <PanelResizeHandle className="w-1 cursor-col-resize bg-gray-200 hover:bg-blue-400 dark:bg-gray-700 dark:hover:bg-blue-500" />
              <Panel defaultSize={35} minSize={20} maxSize={55}>
                <div className="flex h-full flex-col border-l border-gray-200 dark:border-gray-700">
                  <div className="min-h-0 flex-1">
                    <TaskDetail
                      taskId={selectedTaskId}
                      environmentId={environmentId}
                      context={{
                        buildId,
                        planId: frontier?.plan_id ?? null,
                        planInstanceId: selectedMember?.instance_id ?? null,
                        member: selectedMember,
                      }}
                      onClose={() => setSelectedTaskId(null)}
                      onOpenTaskPage={
                        onOpenTask ? () => onOpenTask(selectedTaskId) : undefined
                      }
                      onChanged={refresh}
                      refreshToken={refreshToken}
                    />
                  </div>
                </div>
              </Panel>
            </>
          )}
        </PanelGroup>
      </div>
    </div>
  );
}
