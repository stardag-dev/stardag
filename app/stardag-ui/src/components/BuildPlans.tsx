import type { PlanDetail } from "../types/task";
import { deploymentLabel } from "../utils/deployments";
import { formatAbsoluteTime, formatRelativeTime } from "../utils/time";
import { Spinner } from "./ui/Spinner";
import { Tooltip } from "./ui/Tooltip";

interface BuildPlansProps {
  // Null while the first read is in flight.
  plans: PlanDetail[] | null;
  error: string | null;
  // The active plan's completeness, from the frontier.
  activePlanComplete: boolean;
}

function Chip({ children, title }: { children: React.ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-700 dark:bg-gray-700 dark:text-gray-300"
    >
      {children}
    </span>
  );
}

function Stamp({
  label,
  at,
  help,
}: {
  label: string;
  at: string | null;
  help: string;
}) {
  return (
    <Tooltip content={help}>
      <div>
        <dt className="text-gray-500 dark:text-gray-400">{label}</dt>
        <dd
          className="text-gray-800 dark:text-gray-200"
          title={at ? formatAbsoluteTime(at) : undefined}
        >
          {at ? formatRelativeTime(at) : "—"}
        </dd>
      </div>
    </Tooltip>
  );
}

/**
 * The build's plans, newest generation first: each one's scope (deployment
 * kind, app and generation; settings hash), its lifecycle timestamps and
 * member counts, with the active plan marked. Shown in the "Plans and
 * scheduling" dialog, so nothing is stacked above the DAG and task table.
 */
export function BuildPlans({ plans, error, activePlanComplete }: BuildPlansProps) {
  return (
    <div>
      <h4 className="mb-1 text-xs font-semibold tracking-wide text-gray-500 uppercase dark:text-gray-400">
        Plans{plans ? ` (${plans.length})` : ""}
      </h4>
      {error ? (
        <p className="text-xs text-red-600 dark:text-red-400">{error}</p>
      ) : plans === null ? (
        <Spinner>Reading this build&rsquo;s plans…</Spinner>
      ) : plans.length === 0 ? (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          No plan yet: the build&rsquo;s first registration has not landed.
        </p>
      ) : (
        <ul className="space-y-2">
          {plans.map((plan) => (
            <li
              key={plan.id}
              aria-label={`Plan generation ${plan.generation}${
                plan.is_active ? " (active)" : ""
              }`}
              className={`rounded-md border px-3 py-2 text-xs ${
                plan.is_active
                  ? "border-blue-300 bg-blue-50/60 dark:border-blue-700 dark:bg-blue-950/30"
                  : "border-gray-200 dark:border-gray-700"
              }`}
            >
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium text-gray-800 dark:text-gray-200">
                  Plan {plan.generation}
                </span>
                {plan.is_active && (
                  <span className="rounded bg-blue-100 px-1.5 py-0.5 text-[11px] font-medium text-blue-800 dark:bg-blue-900/50 dark:text-blue-300">
                    active
                  </span>
                )}
                <Chip title={`Deployment ${plan.deployment_id}`}>
                  {deploymentLabel(plan.deployment)} · {plan.deployment.kind}
                  {plan.deployment.kind !== "local" && !plan.deployment.is_current
                    ? " (not current)"
                    : ""}
                </Chip>
                <Chip title={plan.settings_hash}>
                  settings {plan.settings_hash.slice(0, 8)}
                </Chip>
                {plan.is_active && activePlanComplete && <Chip>plan complete</Chip>}
                <Tooltip content="Members: roots, and those given up on (excluded)">
                  <span className="ml-auto text-gray-500 dark:text-gray-400">
                    {plan.member_count} member{plan.member_count === 1 ? "" : "s"},{" "}
                    {plan.root_count} root{plan.root_count === 1 ? "" : "s"}
                    {plan.excluded_count > 0 ? `, ${plan.excluded_count} excluded` : ""}
                  </span>
                </Tooltip>
              </div>
              <dl className="mt-1.5 grid grid-cols-3 gap-2">
                <Stamp
                  label="Activated"
                  at={plan.activated_at}
                  help="From here the plan was the build's one active request"
                />
                <Stamp
                  label="Sealed"
                  at={plan.sealed_at}
                  help="The static phase was fully stated and verified"
                />
                <Stamp
                  label="Superseded"
                  at={plan.superseded_at}
                  help="A replacement plan activated"
                />
              </dl>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
