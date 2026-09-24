import type { BuildFrontier, Deployment } from "../types/task";
import { deploymentLabel } from "../utils/deployments";

interface PlanHeaderProps {
  frontier: BuildFrontier | null;
  deployment: Deployment | null;
  // False when the member list is built from the roots and frontier only.
  complete: boolean;
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

/**
 * One line about the build's active plan: its scope (deployment, settings
 * hash), whether it is sealed, whether it is complete — and, until the
 * registry serves plan membership, that the list below is partial.
 */
export function PlanHeader({ frontier, deployment, complete }: PlanHeaderProps) {
  if (!frontier) return null;
  if (!frontier.plan_id) {
    return (
      <div className="border-b border-gray-200 px-4 py-1.5 text-xs text-gray-600 dark:border-gray-700 dark:text-gray-400">
        No active plan yet.
      </div>
    );
  }
  return (
    <div className="space-y-1 border-b border-gray-200 px-4 py-1.5 dark:border-gray-700">
      <div className="flex flex-wrap items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400">
        <span className="font-medium text-gray-700 dark:text-gray-300">Active plan</span>
        <Chip title={frontier.deployment_id ?? undefined}>
          {deployment ? deploymentLabel(deployment) : `deployment ${frontier.deployment_id?.slice(0, 8)}`}
          {deployment && !deployment.is_current ? " (not current)" : ""}
        </Chip>
        <Chip title={frontier.settings_hash ?? undefined}>
          settings {frontier.settings_hash?.slice(0, 8)}
        </Chip>
        <Chip title="Activated plans are the build's one active request">activated</Chip>
        <Chip
          title={
            frontier.sealed
              ? "The static phase is fully stated and verified"
              : "The static phase is still being stated"
          }
        >
          {frontier.sealed ? "sealed" : "not sealed"}
        </Chip>
        {frontier.plan_complete && <Chip>plan complete</Chip>}
      </div>
      {!complete && (
        <p className="text-xs text-amber-800 dark:text-amber-300">
          Partial: this registry does not serve plan membership yet, so only the roots
          and the frontier&rsquo;s runnable, running and discovery-job members are
          listed, and no edges are drawn.
        </p>
      )}
    </div>
  );
}
