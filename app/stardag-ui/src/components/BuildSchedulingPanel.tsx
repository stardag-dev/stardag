import { useEffect, useRef, useState } from "react";
import {
  fetchBuildNotify,
  fetchBuildPlans,
  fetchBuildTickSummaries,
} from "../api/registry";
import type {
  BuildFrontier,
  BuildStatus,
  BuildTickSummary,
  FrontierItem,
  FrontierMember,
  PlanDetail,
} from "../types/task";
import { memberLabel } from "../utils/instances";
import {
  orderedCounts,
  schedulingState,
  type SchedulingState,
} from "../utils/scheduling";
import { BuildPlans } from "./BuildPlans";
import { Modal } from "./Modal";
import { StatusBadge } from "./StatusBadge";
import { TickSummaryTrail } from "./TickSummaryTrail";
import { ResultBanner } from "./ui/ResultBanner";
import { Spinner } from "./ui/Spinner";
import { ToolbarButton } from "./ui/ToolbarButton";
import { Tooltip } from "./ui/Tooltip";

// Enough ticks to see a repeating outcome without becoming a log viewer.
const TICK_LIMIT = 20;

function MemberList({
  title,
  help,
  items,
  onOpenTask,
}: {
  title: string;
  help: string;
  items: (FrontierMember | FrontierItem)[];
  onOpenTask?: (taskId: string) => void;
}) {
  return (
    <div>
      <Tooltip content={help}>
        <h4 className="mb-1 text-xs font-semibold tracking-wide text-gray-500 uppercase dark:text-gray-400">
          {title} ({items.length})
        </h4>
      </Tooltip>
      {items.length === 0 ? (
        <p className="text-xs text-gray-500 dark:text-gray-400">None.</p>
      ) : (
        <ul className="space-y-0.5">
          {items.map((item) => (
            <li
              key={item.instance_id}
              className="flex flex-wrap items-center gap-x-2 text-xs text-gray-700 dark:text-gray-300"
            >
              <button
                type="button"
                onClick={() => onOpenTask?.(item.task_id)}
                title={item.task_id}
                className="font-medium text-blue-700 hover:underline dark:text-blue-300"
              >
                {memberLabel(item.task_id, item.body)}
              </button>
              <StatusBadge status={item.status} />
              {item.is_root && <span className="text-gray-500">root</span>}
              {"attempts" in item && (
                <span className="text-gray-600 dark:text-gray-400">
                  {item.attempts} attempt{item.attempts === 1 ? "" : "s"}
                  {item.interruptions > 0 ? `, ${item.interruptions} interrupted` : ""}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

interface BuildSchedulingPanelProps {
  buildId: string;
  environmentId: string;
  buildStatus: BuildStatus;
  // Read by the build view, which also draws the plan from it.
  frontier: BuildFrontier | null;
  frontierError: string | null;
  refreshToken?: number;
  onOpenTask?: (taskId: string) => void;
}

// The toolbar button's name, the tooltip's first line and the heading.
export const PLANS_AND_SCHEDULING = "Plans and scheduling";

/**
 * "Which plans does this build have, and what does the scheduler see?" —
 * the build's plans (scope, lifecycle, the active one marked), then the
 * frontier of the active plan: what is runnable, what awaits discovery,
 * what is running (with the attempt and interruption counts the tick
 * budgets on), any closure conflict, and the reactive scheduler's recent
 * ticks. The plan row lives here rather than above the DAG, which keeps
 * the build overview from growing.
 */
export function BuildSchedulingPanel({
  buildId,
  environmentId,
  buildStatus,
  frontier,
  frontierError,
  refreshToken = 0,
  onOpenTask,
}: BuildSchedulingPanelProps) {
  const [open, setOpen] = useState(false);
  const [plans, setPlans] = useState<PlanDetail[] | null>(null);
  const [plansError, setPlansError] = useState<string | null>(null);
  const [summaries, setSummaries] = useState<BuildTickSummary[]>([]);
  const [ticksRead, setTicksRead] = useState(false);
  const [ticksError, setTicksError] = useState<string | null>(null);
  const epochRef = useRef(0);

  useEffect(() => {
    if (!open) return;
    const epoch = ++epochRef.current;
    const fresh = () => epochRef.current === epoch;
    fetchBuildPlans(buildId, environmentId)
      .then((rows) => {
        if (!fresh()) return;
        setPlans(rows);
        setPlansError(null);
      })
      .catch((err: unknown) => {
        if (!fresh()) return;
        setPlansError(err instanceof Error ? err.message : "Failed to load plans");
      });
    fetchBuildTickSummaries(buildId, environmentId, TICK_LIMIT)
      .then((data) => {
        if (!fresh()) return;
        setSummaries(data.summaries);
        setTicksError(null);
      })
      .catch((err: unknown) => {
        if (!fresh()) return;
        setTicksError(
          err instanceof Error ? err.message : "Failed to load tick history",
        );
      })
      .finally(() => {
        if (fresh()) setTicksRead(true);
      });
  }, [open, buildId, environmentId, refreshToken]);

  // A reactive build that looks stalled may simply have a wake-up queued:
  // the next tick re-evaluates it. Read the flag only then, and with every
  // refresh, so the toolbar dot does not cry wolf.
  const [wake, setWake] = useState<{ key: string; pending: boolean } | null>(null);
  const idleStalled = schedulingState(frontier, buildStatus) === "stalled";
  const reactive = Boolean(frontier?.reactive_app_name);
  const wakeKey = `${environmentId}:${buildId}:${refreshToken}`;
  useEffect(() => {
    if (!idleStalled || !reactive) return;
    let stale = false;
    fetchBuildNotify(buildId, environmentId)
      .then((r) => !stale && setWake({ key: wakeKey, pending: r.needs_tick }))
      .catch(() => !stale && setWake({ key: wakeKey, pending: false }));
    return () => {
      stale = true;
    };
  }, [idleStalled, reactive, buildId, environmentId, wakeKey]);
  const wakeUpPending = wake?.key === wakeKey && wake.pending;

  const state = schedulingState(frontier, buildStatus, wakeUpPending);
  const activePlan = plans?.find((p) => p.is_active) ?? null;
  const conflicts = frontier?.closure?.conflicts ?? [];
  const unhealthy =
    frontierError !== null || state === "stalled" || conflicts.length > 0;

  return (
    <>
      <ToolbarButton
        label={PLANS_AND_SCHEDULING}
        hint={
          frontierError
            ? "The scheduler state could not be read"
            : state === "stalled"
              ? "This build is not progressing"
              : "The build's plans, and what the scheduler sees of the active one"
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
      </ToolbarButton>

      <Modal
        isOpen={open}
        onClose={() => setOpen(false)}
        title={PLANS_AND_SCHEDULING}
        maxWidthClass="max-w-3xl"
      >
        <div className="max-h-[70vh] space-y-3 overflow-y-auto">
          <BuildPlans
            plans={plans}
            error={plansError}
            activePlanComplete={frontier?.plan_complete ?? false}
          />
          <h4 className="border-t border-gray-200 pt-3 text-xs font-semibold tracking-wide text-gray-500 uppercase dark:border-gray-700 dark:text-gray-400">
            Scheduling
          </h4>
          {frontierError && (
            <ResultBanner tone="warning">
              Could not read this build&rsquo;s frontier
              {frontier ? "; what is below is from the last successful read" : ""}:{" "}
              {frontierError}
            </ResultBanner>
          )}
          {!frontier ? (
            !frontierError && <Spinner>Reading this build&rsquo;s frontier…</Spinner>
          ) : !frontier.plan_id ? (
            <p className="text-sm text-gray-700 dark:text-gray-300">
              This build has no active plan yet: its first registration has not landed.
            </p>
          ) : (
            <>
              <StateLine state={state} frontier={frontier} buildStatus={buildStatus} />
              {activePlan && <MemberCounts plan={activePlan} />}
              {conflicts.length > 0 && (
                <ResultBanner tone="error">
                  {conflicts.length} closure conflict{conflicts.length === 1 ? "" : "s"}
                  : another instance of a member&rsquo;s completion reached this plan
                  with different parameters (
                  {conflicts
                    .map(
                      (c) =>
                        `${c.task_id.slice(0, 8)}: ${c.fields.join(", ") || "body"}`,
                    )
                    .join("; ")}
                  ).{frontier.closure?.build_failed ? " The build was failed." : ""}
                </ResultBanner>
              )}
              <MemberList
                title="Running"
                help="Members whose task holds a claim, under any build"
                items={frontier.running}
                onOpenTask={onOpenTask}
              />
              <MemberList
                title="Runnable"
                help="Members the next tick may claim and start"
                items={frontier.runnable}
                onOpenTask={onOpenTask}
              />
              <MemberList
                title="Discovery jobs"
                help="Members admitted unexpanded: their requires() has not been evaluated under this scope"
                items={frontier.discovery_jobs}
                onOpenTask={onOpenTask}
              />
            </>
          )}
          {(frontier?.reactive_app_name || summaries.length > 0) && (
            <div>
              <h4 className="mb-1 text-xs font-semibold tracking-wide text-gray-500 uppercase dark:text-gray-400">
                Recent scheduler ticks
              </h4>
              <TickSummaryTrail
                summaries={summaries}
                loading={!ticksRead}
                unavailable={false}
                error={ticksError}
              />
            </div>
          )}
        </div>
      </Modal>
    </>
  );
}

/**
 * The active plan's members by their task's global status
 * (`member_counts`, from the plan read), excluded ones apart — v1's
 * status-count chips.
 */
function MemberCounts({ plan }: { plan: PlanDetail }) {
  const counts = orderedCounts(plan.member_counts);
  if (counts.length === 0 && plan.excluded_count === 0) return null;
  return (
    <div
      className="flex flex-wrap items-center gap-1"
      aria-label="Active plan members by status"
    >
      {counts.map(([status, count]) => (
        <span
          key={status}
          className="inline-flex items-baseline gap-1 rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-700 dark:bg-gray-700 dark:text-gray-200"
        >
          <span>{status}</span>
          <span className="font-medium">{count}</span>
        </span>
      ))}
      {plan.excluded_count > 0 && (
        <Tooltip content="Given up on: not scheduled, and not gating the build">
          <span className="inline-flex items-baseline gap-1 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
            <span>excluded</span>
            <span className="font-medium">{plan.excluded_count}</span>
          </span>
        </Tooltip>
      )}
    </div>
  );
}

function StateLine({
  state,
  frontier,
  buildStatus,
}: {
  state: SchedulingState;
  frontier: BuildFrontier;
  buildStatus: BuildStatus;
}) {
  const text =
    state === "complete"
      ? "Every member of the active plan is satisfied; the next tick completes the build."
      : state === "waking"
        ? "Nothing runnable, running or awaiting discovery right now, but a wake-up is queued: the next tick re-evaluates the build."
        : state === "stalled"
          ? frontier.reactive_app_name
            ? "Nothing runnable, running or awaiting discovery, no wake-up queued, and the plan is not complete — needs intervention."
            : "Nothing runnable, running or awaiting discovery, and this build is not reactively scheduled."
          : state === "settled"
            ? frontier.plan_complete
              ? `Every member of the active plan is satisfied; the build is recorded as ${buildStatus}, so no tick acts on it.`
              : `The build is recorded as ${buildStatus}, so no tick acts on it.`
            : frontier.sealed
              ? "Progressing."
              : "Progressing. The plan is not sealed yet: its static phase is still being stated.";
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
      {state === "stalled" && (
        <span aria-hidden="true" className="text-amber-700 dark:text-amber-400">
          ⚠
        </span>
      )}
      <span>{text}</span>
      {frontier.reactive_app_name && (
        <Tooltip content="The reactive app whose scheduler ticks drive this build">
          <span className="rounded bg-indigo-100 px-1.5 py-0.5 text-xs text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-300">
            ⚡ {frontier.reactive_app_name}
          </span>
        </Tooltip>
      )}
    </div>
  );
}
