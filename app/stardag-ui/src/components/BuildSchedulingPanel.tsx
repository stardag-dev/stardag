import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelTask,
  fetchBuildFrontier,
  fetchBuildTickSummaries,
  retryTask,
} from "../api/tasks";
import { useEnvironment } from "../context/EnvironmentContext";
import type {
  BuildFrontier,
  BuildStatus,
  BuildTickSummary,
  FrontierExternalBlocker,
  TaskStatus,
} from "../types/task";
import {
  availableClaimActions,
  CLAIM_ACTION_LABELS,
  schedulingPanelForm,
  type ClaimAction,
} from "../utils/claims";
import { formatAbsoluteTime, formatDuration } from "../utils/time";
import { ClaimActionDialog } from "./ClaimActionDialog";
import { StatusBadge } from "./StatusBadge";
import { TickSummaryTrail } from "./TickSummaryTrail";
import { ResultBanner } from "./ui/ResultBanner";

// How many ticks to pull. Enough to see a repeating outcome without
// turning the panel into a log viewer.
const TICK_LIMIT = 20;

// Order the status chips read in, rather than whatever order the server's
// GROUP BY produced. Unknown statuses (a newer SDK) are appended.
const STATUS_ORDER: TaskStatus[] = [
  "running",
  "suspended",
  "pending",
  "failed",
  "cancelled",
  "skipped",
  "unregistered",
  "completed",
];

function shortId(id: string): string {
  return id.slice(0, 8);
}

/** "for 3h 12m", or nothing when the timestamp was never recorded. */
function heldFor(since: string | null | undefined): string | null {
  if (!since) return null;
  const duration = formatDuration(since, null);
  return duration === "—" ? null : duration;
}

function BuildLink({
  buildId,
  onNavigateToBuild,
}: {
  buildId: string;
  onNavigateToBuild?: (buildId: string) => void;
}) {
  if (!onNavigateToBuild) {
    return (
      <code className="rounded bg-gray-100 px-1 py-0.5 font-mono text-xs text-gray-700 dark:bg-gray-700 dark:text-gray-200">
        {shortId(buildId)}
      </code>
    );
  }
  return (
    <button
      type="button"
      onClick={() => onNavigateToBuild(buildId)}
      title={`Go to build ${buildId}`}
      className="rounded bg-gray-100 px-1 py-0.5 font-mono text-xs text-blue-700 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:bg-gray-700 dark:text-blue-300"
    >
      {shortId(buildId)}
    </button>
  );
}

interface BlockerCardProps {
  blocker: FrontierExternalBlocker;
  buildId: string;
  isAdmin: boolean;
  busyAction: boolean;
  onNavigateToBuild?: (buildId: string) => void;
  onAct: (blocker: FrontierExternalBlocker, action: ClaimAction) => void;
}

function BlockerCard({
  blocker,
  buildId,
  isAdmin,
  busyAction,
  onNavigateToBuild,
  onAct,
}: BlockerCardProps) {
  const ownerBuildId = blocker.blocking_status_build_id ?? null;
  const ownedByThisBuild = ownerBuildId === buildId;
  const held = heldFor(blocker.blocking_status_at);
  const actions = availableClaimActions(blocker.blocking_status);
  const qualifiedName = blocker.blocking_task_namespace
    ? `${blocker.blocking_task_namespace}/${blocker.blocking_task_name}`
    : blocker.blocking_task_name;

  // The remedy differs by ownership, so the copy has to as well.
  let explanation: string;
  if (!blocker.blocking_in_build) {
    explanation =
      "This build has never registered the blocking task, so it will never schedule it — it can only wait for whoever owns it.";
  } else if (ownedByThisBuild) {
    explanation =
      "The blocking task is in this build's own task set and this build put it into that status, but it is not actionable, so nothing here will move it on.";
  } else {
    explanation =
      "The blocking task is in this build's own task set — it is in the table below — but another build set its current status, so this build will not advance it.";
  }

  return (
    <li className="rounded-md border border-amber-200 bg-white/70 p-2.5 dark:border-amber-900/60 dark:bg-gray-800/60">
      <p className="text-xs text-gray-600 dark:text-gray-400">
        This build&rsquo;s task{" "}
        <code
          title={blocker.task_id}
          className="rounded bg-gray-100 px-1 py-0.5 font-mono text-gray-800 dark:bg-gray-700 dark:text-gray-200"
        >
          {blocker.task_id}
        </code>{" "}
        is waiting on:
      </p>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {qualifiedName}
        </span>
        <StatusBadge status={blocker.blocking_status} />
        {held ? (
          <span
            className="text-xs text-gray-600 dark:text-gray-400"
            title={formatAbsoluteTime(blocker.blocking_status_at)}
          >
            for {held}
          </span>
        ) : (
          <span className="text-xs text-gray-500 dark:text-gray-400">
            since an unrecorded time
          </span>
        )}
        {ownerBuildId ? (
          <span className="text-xs text-gray-600 dark:text-gray-400">
            held by build{" "}
            <BuildLink buildId={ownerBuildId} onNavigateToBuild={onNavigateToBuild} />
            {ownedByThisBuild ? " (this build)" : ""}
          </span>
        ) : (
          <span className="text-xs text-gray-500 dark:text-gray-400">
            owning build not recorded
          </span>
        )}
      </div>
      <p className="mt-1.5 text-xs text-gray-600 dark:text-gray-400">{explanation}</p>
      <p
        className="mt-1 truncate font-mono text-[11px] text-gray-500 dark:text-gray-500"
        title={blocker.blocking_task_id}
      >
        {blocker.blocking_task_id}
      </p>

      {actions.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {isAdmin && ownerBuildId ? (
            actions.map((action) => (
              <button
                key={action}
                type="button"
                disabled={busyAction}
                onClick={() => onAct(blocker, action)}
                // Several identical-looking buttons can sit in this list,
                // so each names its target rather than just its verb.
                aria-label={`${CLAIM_ACTION_LABELS[action]} on ${blocker.blocking_task_name}`}
                className="rounded-md border border-red-300 px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500 disabled:opacity-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-900/30"
              >
                {CLAIM_ACTION_LABELS[action]}
              </button>
            ))
          ) : !ownerBuildId ? (
            <span className="text-xs text-gray-500 dark:text-gray-400">
              No remedy can be offered: the build that set this status was not recorded,
              so there is nothing to address a cancel or retry to.
            </span>
          ) : (
            <span className="text-xs text-gray-500 dark:text-gray-400">
              Releasing or resetting another build&rsquo;s task requires the workspace
              admin role.
            </span>
          )}
        </div>
      )}
    </li>
  );
}

interface BuildSchedulingPanelProps {
  buildId: string;
  environmentId: string;
  buildStatus: BuildStatus;
  /**
   * Bumped by the parent on every refresh. The panel refetches in step
   * with the build view rather than running its own timer, so the 5s
   * auto-refresh does not end up issuing two independent request streams.
   */
  refreshToken?: number;
  onNavigateToBuild?: (buildId: string) => void;
  /** Called after a remedy changed server state, so the parent refetches. */
  onChanged?: () => void;
}

/**
 * "Why is this build not progressing?" — the scheduler's view of a build.
 *
 * Answers the question the task table cannot: which of this build's tasks
 * are held back, by *which* task, in what status, for how long, and under
 * whose build — plus what the reactive scheduler itself thought it was
 * doing on each of its recent ticks.
 *
 * See `schedulingPanelForm` for when it renders.
 */
export function BuildSchedulingPanel({
  buildId,
  environmentId,
  buildStatus,
  refreshToken = 0,
  onNavigateToBuild,
  onChanged,
}: BuildSchedulingPanelProps) {
  const { activeWorkspaceRole } = useEnvironment();
  const isAdmin = activeWorkspaceRole === "owner" || activeWorkspaceRole === "admin";

  const [frontier, setFrontier] = useState<BuildFrontier | null>(null);
  const [frontierError, setFrontierError] = useState<string | null>(null);

  const [summaries, setSummaries] = useState<BuildTickSummary[]>([]);
  const [ticksLoading, setTicksLoading] = useState(false);
  const [ticksUnavailable, setTicksUnavailable] = useState(false);
  const [ticksError, setTicksError] = useState<string | null>(null);

  // Collapsed-form disclosure. Only meaningful in the "collapsed" form;
  // the stalled form is always open.
  const [stripOpen, setStripOpen] = useState(false);

  // In-flight remedy.
  const [pending, setPending] = useState<{
    blocker: FrontierExternalBlocker;
    action: ClaimAction;
  } | null>(null);
  const [acting, setActing] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Bumped locally after a remedy so the panel refreshes even when the
  // parent does not pass a refreshToken.
  const [localNonce, setLocalNonce] = useState(0);

  // Stale-response guards: a slow response from a previous build or
  // environment must not overwrite the current one's state.
  const frontierEpochRef = useRef(0);
  const ticksEpochRef = useRef(0);

  useEffect(() => {
    if (!buildId || !environmentId) return;
    const epoch = ++frontierEpochRef.current;
    const fresh = () => frontierEpochRef.current === epoch;
    fetchBuildFrontier(buildId, environmentId)
      .then((data) => {
        if (!fresh()) return;
        setFrontier(data);
        setFrontierError(null);
      })
      .catch((err: unknown) => {
        if (!fresh()) return;
        setFrontierError(
          err instanceof Error ? err.message : "Failed to read scheduler state",
        );
      });
  }, [buildId, environmentId, refreshToken, localNonce]);

  // Reset per-build state when navigating between builds, so a previous
  // build's blockers/ticks never show under a new one's header.
  useEffect(() => {
    setFrontier(null);
    setFrontierError(null);
    setSummaries([]);
    setTicksUnavailable(false);
    setTicksError(null);
    setStripOpen(false);
    setNotice(null);
    setActionError(null);
  }, [buildId]);

  const form = frontier ? schedulingPanelForm(frontier, buildStatus) : "hidden";
  // Tick history is fetched only when it will actually be read: always for
  // a stalled build, and on demand behind the collapsed form's disclosure.
  const wantTicks = form === "stalled" || (form === "collapsed" && stripOpen);

  useEffect(() => {
    if (!wantTicks || !buildId || !environmentId) return;
    const epoch = ++ticksEpochRef.current;
    const fresh = () => ticksEpochRef.current === epoch;
    setTicksLoading(true);
    fetchBuildTickSummaries(buildId, environmentId, TICK_LIMIT)
      .then((data) => {
        if (!fresh()) return;
        // A null response means the server has no such endpoint (404).
        setTicksUnavailable(data === null);
        setSummaries(data?.summaries ?? []);
        setTicksError(null);
      })
      .catch((err: unknown) => {
        if (!fresh()) return;
        setTicksError(
          err instanceof Error ? err.message : "Failed to load tick history",
        );
      })
      .finally(() => {
        if (fresh()) setTicksLoading(false);
      });
  }, [wantTicks, buildId, environmentId, refreshToken, localNonce]);

  const handleAct = useCallback(
    (blocker: FrontierExternalBlocker, action: ClaimAction) => {
      setActionError(null);
      setNotice(null);
      setPending({ blocker, action });
    },
    [],
  );

  const handleConfirm = useCallback(async () => {
    if (!pending) return;
    const { blocker, action } = pending;
    const ownerBuildId = blocker.blocking_status_build_id;
    if (!ownerBuildId) return;
    setActing(true);
    setActionError(null);
    try {
      if (action === "release") {
        await cancelTask(ownerBuildId, blocker.blocking_task_id, environmentId);
      } else {
        await retryTask(ownerBuildId, blocker.blocking_task_id, environmentId);
      }
      setPending(null);
      setNotice(
        action === "release"
          ? `Released the claim on ${blocker.blocking_task_name} under build ${shortId(
              ownerBuildId,
            )}.`
          : `Reset ${blocker.blocking_task_name} to pending under build ${shortId(
              ownerBuildId,
            )}.`,
      );
      setLocalNonce((n) => n + 1);
      onChanged?.();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setActing(false);
    }
  }, [pending, environmentId, onChanged]);

  // The first load renders nothing rather than a placeholder: this panel is
  // an answer, and on a healthy build there is no question — flashing a
  // "checking…" box on every build open would be worse than the ~200ms of
  // nothing. A *failed* read is different, and does render (below).
  if (frontierError) {
    return (
      <div className="border-b border-gray-200 px-4 py-2 dark:border-gray-700">
        <ResultBanner tone="warning">
          Could not read this build&rsquo;s scheduler state, so the panel that explains
          a stalled build is unavailable: {frontierError}
        </ResultBanner>
      </div>
    );
  }
  if (!frontier || form === "hidden") return null;

  const blockers = frontier.blocked_by_external;
  const counts = Object.entries(frontier.status_counts).sort((a, b) => {
    const ai = STATUS_ORDER.indexOf(a[0] as TaskStatus);
    const bi = STATUS_ORDER.indexOf(b[0] as TaskStatus);
    return (ai < 0 ? STATUS_ORDER.length : ai) - (bi < 0 ? STATUS_ORDER.length : bi);
  });

  const countChips = (
    <div className="flex flex-wrap items-center gap-1">
      {counts.map(([status, count]) => (
        <span
          key={status}
          className="inline-flex items-baseline gap-1 rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-700 dark:bg-gray-700 dark:text-gray-200"
        >
          <span>{status}</span>
          <span className="font-medium">{count}</span>
        </span>
      ))}
    </div>
  );

  const appChip = frontier.reactive_app_name ? (
    <span
      className="rounded bg-indigo-100 px-1.5 py-0.5 text-xs text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-300"
      title="The reactive app whose scheduler ticks drive this build"
    >
      ⚡ {frontier.reactive_app_name}
    </span>
  ) : null;

  const tickTrail = (
    <TickSummaryTrail
      summaries={summaries}
      loading={ticksLoading && summaries.length === 0}
      unavailable={ticksUnavailable}
      error={ticksError}
    />
  );

  if (form === "collapsed") {
    // A progressing reactive build: one line, never an empty box. It must
    // not imply anything about blockers — the server does not look for
    // them while a build is moving.
    return (
      <div className="border-b border-gray-200 bg-gray-50 px-4 py-1.5 dark:border-gray-700 dark:bg-gray-800/50">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setStripOpen((v) => !v)}
            aria-expanded={stripOpen}
            className="flex items-center gap-1.5 rounded text-xs font-medium text-gray-700 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-gray-300 dark:hover:text-gray-100"
          >
            <svg
              className={`h-3 w-3 transition-transform ${stripOpen ? "rotate-90" : ""}`}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M9 5l7 7-7 7"
              />
            </svg>
            Scheduling
          </button>
          {appChip}
          <span className="text-xs text-gray-600 dark:text-gray-400">
            {frontier.actionable.length} actionable · {frontier.running.length} running
            {frontier.needs_tick ? " · wake-up pending" : ""}
          </span>
        </div>
        {stripOpen && (
          <div className="mt-2 space-y-2 pb-1">
            {countChips}
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Upstreams owned by other builds are only looked for while a build is
              stalled, so nothing here says whether this build has any.
            </p>
            <div>
              <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                Recent scheduler ticks
              </h4>
              {tickTrail}
            </div>
          </div>
        )}
      </div>
    );
  }

  // --- Stalled form ---

  const blockedTaskCount = new Set(blockers.map((b) => b.task_id)).size;
  const externalCount = blockers.filter((b) => !b.blocking_in_build).length;

  let verdict: string;
  if (blockers.length > 0) {
    verdict =
      `Nothing in this build is actionable and nothing is running. ` +
      `${blockedTaskCount} of its task${blockedTaskCount === 1 ? " is" : "s are"} ` +
      `held back by ${blockers.length} upstream${blockers.length === 1 ? "" : "s"} ` +
      `whose status ${blockers.length === 1 ? "was" : "were"} set outside this build` +
      (externalCount > 0
        ? ` — ${externalCount} of which ${
            externalCount === 1 ? "is" : "are"
          } not even part of this build.`
        : ".");
  } else if (frontier.needs_tick) {
    verdict =
      "Nothing in this build is actionable and nothing is running, and no upstream " +
      "outside the build is holding it back. A scheduler wake-up is pending, so the " +
      "next tick may still move it.";
  } else if (frontier.reactive_app_name) {
    verdict =
      "Nothing in this build is actionable and nothing is running, no upstream " +
      "outside the build is holding it back, and no scheduler wake-up is pending. " +
      "Nothing is going to happen without intervention.";
  } else {
    verdict =
      "Nothing in this build is actionable and nothing is running, and no upstream " +
      "outside the build is holding it back. This build is not reactively scheduled, " +
      "so nothing will advance it on its own.";
  }

  return (
    <div className="border-b border-amber-200 bg-amber-50 px-4 py-3 dark:border-amber-900/60 dark:bg-amber-950/30">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-amber-900 dark:text-amber-200">
          Why is this build not progressing?
        </h3>
        {appChip}
        {frontier.needs_tick && (
          <span
            className="rounded bg-blue-100 px-1.5 py-0.5 text-xs text-blue-800 dark:bg-blue-900/40 dark:text-blue-300"
            title="The scheduler has a wake-up queued for this build"
          >
            wake-up pending
          </span>
        )}
      </div>

      <p className="mt-1 text-sm text-amber-900/90 dark:text-amber-100/90">{verdict}</p>

      <div className="mt-2">{countChips}</div>

      {notice && (
        <ResultBanner tone="success" className="mt-2" onDismiss={() => setNotice(null)}>
          {notice}
        </ResultBanner>
      )}
      {actionError && !pending && (
        <ResultBanner
          tone="error"
          className="mt-2"
          onDismiss={() => setActionError(null)}
        >
          {actionError}
        </ResultBanner>
      )}

      {blockers.length > 0 && (
        <div className="mt-3">
          <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-amber-900/80 dark:text-amber-200/80">
            Blocked by upstreams outside this build ({blockers.length})
          </h4>
          <ul className="space-y-2">
            {blockers.map((blocker) => (
              <BlockerCard
                key={`${blocker.task_id}->${blocker.blocking_task_id}`}
                blocker={blocker}
                buildId={buildId}
                isAdmin={isAdmin}
                busyAction={acting}
                onNavigateToBuild={onNavigateToBuild}
                onAct={handleAct}
              />
            ))}
          </ul>
          {frontier.blocked_by_external_truncated && (
            <p className="mt-1.5 text-xs text-amber-900/80 dark:text-amber-200/80">
              More blockers were found than are listed here — the list is capped because
              it is a diagnostic, not a work queue. Clearing the ones shown will reveal
              the rest.
            </p>
          )}
        </div>
      )}

      <div className="mt-3">
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-amber-900/80 dark:text-amber-200/80">
          Recent scheduler ticks
        </h4>
        {tickTrail}
      </div>

      {pending && pending.blocker.blocking_status_build_id && (
        <ClaimActionDialog
          action={pending.action}
          taskName={pending.blocker.blocking_task_name}
          taskId={pending.blocker.blocking_task_id}
          ownerBuildId={pending.blocker.blocking_status_build_id}
          currentBuildId={buildId}
          status={pending.blocker.blocking_status}
          busy={acting}
          error={actionError}
          onConfirm={handleConfirm}
          onCancel={() => {
            setPending(null);
            setActionError(null);
          }}
        />
      )}
    </div>
  );
}
