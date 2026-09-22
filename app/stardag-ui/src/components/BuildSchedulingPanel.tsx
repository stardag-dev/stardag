import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
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
  FrontierTaskRef,
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
import { Modal } from "./Modal";
import { ToolbarButton } from "./ui/ToolbarButton";

// How many ticks to pull. Enough to see a repeating outcome without
// turning the panel into a log viewer.
const TICK_LIMIT = 20;

// Order the status chips read in, rather than whatever order the server's
// GROUP BY produced. Unknown statuses (a newer SDK) are appended.
const STATUS_ORDER: TaskStatus[] = [
  "running",
  "suspended",
  "interrupted",
  "pending",
  "failed",
  "cancelled",
  "skipped",
  "completed",
];

/**
 * A cancelled or skipped task the frontier lists as actionable: every
 * upstream in the build's scope is complete, so the scheduler resets it
 * within its attempt budget and runs it. A cancel is a revocation, not a
 * verdict; a skip whose upstreams have since completed is a skip whose
 * reason no longer holds. Rendered so a reader sees the reset coming rather
 * than a dead-looking status.
 */
function resetPendingLabel(status: TaskStatus): string | null {
  if (status === "cancelled") return "reset pending (revocation)";
  if (status === "skipped") return "reset pending (stale skip)";
  return null;
}

function AwaitingReset({ actionable }: { actionable: FrontierTaskRef[] }) {
  const awaiting = actionable.filter((t) => resetPendingLabel(t.latest_status));
  if (awaiting.length === 0) return null;
  return (
    <ul className="space-y-0.5">
      {awaiting.map((t) => (
        <li
          key={t.task_id}
          className="flex flex-wrap items-center gap-x-1.5 text-xs text-gray-700 dark:text-gray-300"
        >
          <code
            title={t.task_id}
            className="rounded bg-gray-100 px-1 py-0.5 font-mono text-[11px] dark:bg-gray-700"
          >
            {shortId(t.task_id)}
          </code>
          <StatusBadge status={t.latest_status} />
          <span className="text-gray-600 dark:text-gray-400">
            {resetPendingLabel(t.latest_status)}
          </span>
        </li>
      ))}
    </ul>
  );
}

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
  const [open, setOpen] = useState(false);
  const ownerBuildId = blocker.blocking_status_build_id ?? null;
  const ownedByThisBuild = ownerBuildId === buildId;
  const held = heldFor(blocker.blocking_status_at);
  const actions = availableClaimActions(blocker.blocking_status);
  const qualifiedName = blocker.blocking_task_namespace
    ? `${blocker.blocking_task_namespace}/${blocker.blocking_task_name}`
    : blocker.blocking_task_name;

  // What happens next is a function of the blocker's status, not of who owns
  // it or whether it is in this build's plan. Plan membership is still worth
  // reporting — the chip below does that — but it does not change the copy:
  // a RUNNING blocker resolves when its claim does either way.
  let explanation: string;
  if (ownedByThisBuild) {
    explanation =
      "The blocking task is in this build's own task set and this build put it into that status, but it is not actionable, so nothing here will move it on.";
  } else if (blocker.blocking_status === "cancelled") {
    explanation =
      "Another build cancelled the blocking task. That revoked permission to run it, not the task itself, and it is in this build's plan — so this build's next tick resets it and runs it, bounded by the per-task attempt budget.";
  } else if (blocker.blocking_status === "running") {
    explanation =
      "Another build holds the execution claim on the blocking task. It resolves when that build finishes the task or the claim expires — until then, running it here would be a duplicate execution.";
  } else if (blocker.blocking_status === "suspended") {
    explanation =
      "The blocking task yielded dynamic dependencies and is waiting for them. The build that owns it is working through them; this build resolves as that one progresses.";
  } else if (blocker.blocking_status === "interrupted") {
    explanation =
      "The platform took the blocking task's execution away and the task asked to be resumed. The build that owns it will start it again; this build resolves when that run completes.";
  } else {
    explanation =
      "The blocking task's status is a result, not a revocation, so a tick leaves it to this build's fail_mode rather than overriding the policy the build was triggered with. Re-trigger this build to reset it and run it here.";
  }

  return (
    <li className="border-t border-amber-200/70 first:border-t-0 dark:border-amber-900/50">
      {/* The whole blocker on one line: which task, waiting on what, in
          what status, for how long, under whose build — and the remedy.
          Everything that explains *why* sits behind the disclosure, since
          it is the same three sentences every time and this panel shares
          the viewport with the DAG and the task table. */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 py-1 text-xs">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-label={`Explain why ${blocker.blocking_task_name} is blocking`}
          className="rounded text-amber-900/70 hover:text-amber-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-amber-200/70 dark:hover:text-amber-100"
        >
          <svg
            className={`h-3 w-3 transition-transform ${open ? "rotate-90" : ""}`}
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
        </button>
        <code
          title={`This build's task ${blocker.task_id}`}
          className="rounded bg-amber-100/60 px-1 py-0.5 font-mono text-[11px] text-gray-700 dark:bg-gray-700/60 dark:text-gray-200"
        >
          {shortId(blocker.task_id)}
        </code>
        <span className="text-gray-600 dark:text-gray-400">waits on</span>
        <span
          title={blocker.blocking_task_id}
          className="max-w-[18rem] truncate font-medium text-gray-900 dark:text-gray-100"
        >
          {qualifiedName}
        </span>
        <StatusBadge status={blocker.blocking_status} />
        {held && (
          <span
            className="text-gray-600 dark:text-gray-400"
            title={formatAbsoluteTime(blocker.blocking_status_at)}
          >
            {held}
          </span>
        )}
        {ownerBuildId ? (
          <BuildLink buildId={ownerBuildId} onNavigateToBuild={onNavigateToBuild} />
        ) : (
          <span className="text-gray-500 dark:text-gray-400">no owning build</span>
        )}
        {actions.length > 0 && isAdmin && ownerBuildId && (
          <span className="ml-auto flex items-center gap-1.5">
            {actions.map((action) => (
              <button
                key={action}
                type="button"
                disabled={busyAction}
                onClick={() => onAct(blocker, action)}
                // Several identical-looking buttons can sit in this list,
                // so each names its target rather than just its verb.
                aria-label={`${CLAIM_ACTION_LABELS[action]} on ${blocker.blocking_task_name}`}
                className="rounded border border-red-300 px-1.5 py-0.5 font-medium text-red-700 hover:bg-red-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500 disabled:opacity-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-900/30"
              >
                {CLAIM_ACTION_LABELS[action]}
              </button>
            ))}
          </span>
        )}
      </div>

      {open && (
        <div className="pb-1.5 pl-5 text-xs text-gray-600 dark:text-gray-400">
          <p>{explanation}</p>
          <p className="mt-1 font-mono text-[11px] text-gray-500 dark:text-gray-500">
            {blocker.task_id} → {blocker.blocking_task_id}
          </p>
          {ownedByThisBuild && (
            <p className="mt-1">This build owns the blocking status itself.</p>
          )}
          {actions.length > 0 && !ownerBuildId && (
            <p className="mt-1">
              No remedy can be offered: the build that set this status was not recorded,
              so there is nothing to address a cancel or retry to.
            </p>
          )}
          {actions.length > 0 && ownerBuildId && !isAdmin && (
            <p className="mt-1">
              Releasing or resetting another build&rsquo;s task requires the workspace
              admin role.
            </p>
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
  // Tracked separately from `frontier === null`, because the previous
  // frontier is deliberately kept on screen while the next read is in
  // flight. Without this the spinner appeared on the first load only,
  // and every refresh after it showed a static clock.
  const [frontierLoading, setFrontierLoading] = useState(true);

  const [summaries, setSummaries] = useState<BuildTickSummary[]>([]);
  const [ticksLoading, setTicksLoading] = useState(false);
  const [ticksUnavailable, setTicksUnavailable] = useState(false);
  const [ticksError, setTicksError] = useState<string | null>(null);

  // Whether the dialog is open. It replaces the two disclosures this
  // panel used to carry: they existed because the panel sat above the DAG
  // and the task table and at full height pushed both off the viewport.
  // In a dialog there is room, so everything it knows is simply shown.
  const [open, setOpen] = useState(false);

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
    setFrontierLoading(true);
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
      })
      .finally(() => {
        if (fresh()) setFrontierLoading(false);
      });
  }, [buildId, environmentId, refreshToken, localNonce]);

  // Reset when either half of the identity changes. Not just the build:
  // this component stays mounted across an environment switch too, and
  // `buildId` does not move through one — so keying the reset on the
  // build alone left the previous environment's frontier, ticks and open
  // dialog in place under the new one. Same defect the controls dialog
  // had in its key.
  useEffect(() => {
    setFrontier(null);
    setFrontierError(null);
    setFrontierLoading(true);
    setSummaries([]);
    setTicksUnavailable(false);
    setTicksError(null);
    setOpen(false);
    setNotice(null);
    setActionError(null);
    // `pending` renders ClaimActionDialog, and confirming it writes —
    // with the *old* blocker's ids and the *new* environment. Today the
    // parent's loader unmounts this subtree on either change so it is
    // unreachable, but this effect exists precisely for the case where
    // it is not, and leaving the one write out of it is the wrong thing
    // to forget.
    setPending(null);
  }, [buildId, environmentId]);

  const form = frontier ? schedulingPanelForm(frontier, buildStatus) : "hidden";
  // Tick history is fetched only when it will actually be read, which now
  // means simply "the dialog is open".
  const wantTicks = open && (form === "stalled" || form === "collapsed");

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
        const resulting = await cancelTask(
          ownerBuildId,
          blocker.blocking_task_id,
          environmentId,
        );
        // See TaskDetail: the event is recorded whatever the task's status,
        // but only a task that was actually holding the claim ends up
        // CANCELLED. Reporting a release that did not happen would send
        // someone off looking for a second cause.
        if (resulting !== "cancelled") {
          setActionError(
            `${blocker.blocking_task_name} is already ${resulting}, so there was no claim to release. If this build is still stuck, something else is holding it.`,
          );
          return;
        }
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

  // The icon is always present, and carries the state the panel used to
  // carry by existing or not: a spinner while the frontier is being read,
  // a dot when something is wrong. An icon that came and went would be
  // worse than one that says what it knows — a control that disappears
  // reads as a bug, and a toolbar whose buttons move is hard to aim at.
  const loading = frontierLoading;
  const unhealthy = frontierError !== null || form === "stalled";
  const trigger = (
    <ToolbarButton
      label="Scheduling"
      hint={
        frontierError
          ? "The scheduler state could not be read"
          : form === "stalled"
            ? "This build is not progressing"
            : "What the scheduler thinks this build is doing"
      }
      onClick={() => setOpen(true)}
      badge={
        unhealthy ? (
          <span
            aria-hidden="true"
            className="absolute -top-0.5 -right-0.5 h-2 w-2 rounded-full bg-red-500 ring-2 ring-white dark:ring-gray-800"
          />
        ) : undefined
      }
    >
      {loading ? (
        <svg
          aria-hidden="true"
          className="h-4 w-4 animate-spin"
          fill="none"
          viewBox="0 0 24 24"
        >
          <circle
            className="opacity-25"
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="3"
          />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
          />
        </svg>
      ) : (
        <svg
          aria-hidden="true"
          className="h-4 w-4"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"
          />
        </svg>
      )}
    </ToolbarButton>
  );

  if (!frontier) {
    return (
      <>
        {trigger}
        <Modal
          isOpen={open}
          onClose={() => setOpen(false)}
          title="Scheduling"
          maxWidthClass="max-w-3xl"
        >
          {frontierError ? (
            <ResultBanner tone="warning">
              Could not read this build&rsquo;s scheduler state, so what would explain a
              stalled build is unavailable: {frontierError}
            </ResultBanner>
          ) : (
            <p role="status" className="text-sm text-gray-600 dark:text-gray-400">
              Reading this build&rsquo;s scheduler state…
            </p>
          )}
        </Modal>
      </>
    );
  }

  // Always empty from a current server (edges are scoped to the build, and
  // a stalled build re-closes its plan before it is reported as stalled);
  // rendered defensively for servers predating that.
  const blockers = frontier.blocked_by_external;
  const awaitingResetCount = frontier.actionable.filter((t) =>
    resetPendingLabel(t.latest_status),
  ).length;
  // A status nothing is in is not information about this build.
  const counts = Object.entries(frontier.status_counts)
    .filter(([, count]) => count > 0)
    .sort((a, b) => {
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

  let body: ReactNode;

  if (form === "hidden") {
    // A healthy build with no scheduler to reason about. As a panel this
    // rendered nothing, which was right for a band that appeared unbidden;
    // behind an icon somebody chose to click, saying so is the answer.
    body = (
      <div className="space-y-3 text-sm text-gray-700 dark:text-gray-300">
        <p>
          Nothing to report. This build is progressing and no scheduler tick drives it,
          so there is no scheduler state to explain.
        </p>
        {countChips}
      </div>
    );
  } else if (form === "satisfied") {
    // Every root is complete, so there is nothing to diagnose and nothing to
    // intervene in — however the build's own status reads. Green rather than
    // amber, and deliberately short: the interesting question ("why does the
    // status still say failed?") is answered by the failure reason above this
    // panel, not by repeating it here.
    const completedElsewhere = buildStatus !== "completed";
    body = (
      <div className="rounded-md border border-green-200 bg-green-50 px-3 py-2 dark:border-green-900/60 dark:bg-green-950/30">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span aria-hidden="true" className="text-green-700 dark:text-green-400">
            ✓
          </span>
          <h3 className="text-xs font-semibold text-green-900 dark:text-green-200">
            Everything this build asked for is complete
          </h3>
          <span className="text-xs text-green-900/80 dark:text-green-100/80">
            {buildStatus === "running"
              ? "— its roots finished, so a scheduler tick will complete it"
              : `— its roots finished after the build was recorded as ${buildStatus}, ` +
                "so nothing is outstanding. Re-trigger it to reconcile the record."}
          </span>
        </div>
        {completedElsewhere && (
          <p className="mt-1 text-xs text-green-900/70 dark:text-green-100/70">
            Tasks are shared across builds, so another build may have finished them.
            Which build ran a task does not change its result.
          </p>
        )}
      </div>
    );
  } else if (form === "collapsed") {
    // A progressing reactive build. It must not imply anything about
    // blockers — the server does not look for them while a build is
    // moving.
    body = (
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          {appChip}
          <span className="text-sm text-gray-700 dark:text-gray-300">
            {frontier.actionable.length} actionable · {frontier.running.length} running
            {awaitingResetCount > 0 ? ` · ${awaitingResetCount} awaiting reset` : ""}
            {frontier.needs_tick ? " · wake-up pending" : ""}
          </span>
        </div>
        {countChips}
        <AwaitingReset actionable={frontier.actionable} />
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Every upstream in this build&rsquo;s structure scope is part of its plan, so a
          stalled build is stalled on tasks of its own.
        </p>
        <div>
          <h4 className="mb-1 text-xs font-semibold tracking-wide text-gray-500 uppercase dark:text-gray-400">
            Recent scheduler ticks
          </h4>
          {tickTrail}
        </div>
      </div>
    );
  } else {
    // --- Stalled form ---

    const blockedTaskCount = new Set(blockers.map((b) => b.task_id)).size;

    // One line that says what is wrong, with the paragraph-length version
    // under it. The headline used to be all that was on screen, because the
    // panel sat above the DAG; in a dialog both fit.
    let headline: string;
    if (blockers.length > 0) {
      headline =
        `${blockedTaskCount} task${blockedTaskCount === 1 ? "" : "s"} blocked by ` +
        `${blockers.length} upstream${blockers.length === 1 ? "" : "s"} ` +
        `held outside this build`;
    } else if (frontier.needs_tick) {
      headline = "Nothing runnable — a scheduler wake-up is still pending";
    } else if (frontier.reactive_app_name) {
      headline = "Nothing runnable, and no wake-up pending — needs intervention";
    } else {
      headline = "Nothing runnable — this build is not reactively scheduled";
    }

    let verdict: string;
    if (blockers.length > 0) {
      verdict =
        `Nothing in this build is actionable and nothing is running. ` +
        `${blockedTaskCount} of its task${blockedTaskCount === 1 ? " is" : "s are"} ` +
        `held back by ${blockers.length} upstream${blockers.length === 1 ? "" : "s"} ` +
        `whose status ${
          blockers.length === 1 ? "was" : "were"
        } set outside this build.`;
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

    body = (
      <div className="space-y-2">
        {/* Headline row: the answer. */}
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span aria-hidden="true" className="text-amber-700 dark:text-amber-400">
            ⚠
          </span>
          <h3 className="text-xs font-semibold text-amber-900 dark:text-amber-200">
            Not progressing
          </h3>
          <span className="text-xs text-amber-900/90 dark:text-amber-100/90">
            — {headline}
          </span>
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

        {notice && (
          <ResultBanner
            tone="success"
            className="mt-1"
            onDismiss={() => setNotice(null)}
          >
            {notice}
          </ResultBanner>
        )}
        {actionError && !pending && (
          <ResultBanner
            tone="error"
            className="mt-1"
            onDismiss={() => setActionError(null)}
          >
            {actionError}
          </ResultBanner>
        )}

        <p className="text-xs text-gray-600 dark:text-gray-400">{verdict}</p>

        {/* Blockers carry the remedy, which is the whole point of noticing a
          stalled build. All of them: the dialog has the room the band
          above the DAG did not. */}
        {blockers.length > 0 && (
          <ul className="mt-1">
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
        )}

        {
          <div className="mt-2 space-y-2 border-t border-gray-200 pt-2 dark:border-gray-700">
            {countChips}
            {frontier.blocked_by_external_truncated && (
              <p className="text-xs text-amber-900/80 dark:text-amber-200/80">
                More blockers were found than are listed here — the list is capped
                because it is a diagnostic, not a work queue. Clearing the ones shown
                will reveal the rest.
              </p>
            )}
            <div>
              <h4 className="mb-1 text-xs font-semibold tracking-wide text-gray-500 uppercase dark:text-gray-400">
                Recent scheduler ticks
              </h4>
              {tickTrail}
            </div>
          </div>
        }
      </div>
    );
  }

  return (
    <>
      {trigger}
      <Modal
        isOpen={open}
        onClose={() => setOpen(false)}
        title="Scheduling"
        maxWidthClass="max-w-3xl"
      >
        <div className="max-h-[70vh] overflow-y-auto">
          {/* A failed read with a frontier already on screen. The
              previous frontier is deliberately kept rather than
              blanked, which makes saying so essential: otherwise the
              dialog shows a confident, ordinary-looking answer that is
              simply old, while the icon's own dot and tooltip say the
              read failed. Before this panel became a dialog the error
              check came first and so always showed; restructuring it
              put this branch behind `!frontier`. */}
          {frontierError && (
            <ResultBanner tone="warning" className="mb-3">
              Could not re-read this build&rsquo;s scheduler state, so what is below is
              from the last successful read and may be out of date: {frontierError}
            </ResultBanner>
          )}
          {body}
        </div>
      </Modal>
      {/* Rendered outside the dialog, and after it, so the confirmation
          stacks on top of the dialog it was triggered from rather than
          being clipped inside it. */}
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
    </>
  );
}
