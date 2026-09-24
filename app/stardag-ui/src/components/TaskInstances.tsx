import { useMemo, useState } from "react";
import type { Deployment, TaskInstance } from "../types/task";
import { deploymentLabel } from "../utils/deployments";
import { differingParameters, parametersOf } from "../utils/instances";
import { formatAbsoluteTime, formatRelativeTime } from "../utils/time";
import { FullscreenModal } from "./FullscreenModal";

interface TaskInstancesProps {
  instances: TaskInstance[];
  deploymentsById: Map<string, Deployment>;
  // The instance the viewed plan holds, marked "in this plan".
  planInstanceId?: string | null;
}

/**
 * The instances that realise one completion, newest first.
 *
 * An instance is a construction of the task under a scope — a deployment
 * and a settings hash — and its body holds every parameter. The instance
 * hash is printed only next to its scope: on its own it identifies
 * nothing, since two scopes may hold the same hash.
 */
export function TaskInstances({
  instances,
  deploymentsById,
  planInstanceId,
}: TaskInstancesProps) {
  const differing = useMemo(
    () => differingParameters(instances.map((instance) => instance.body)),
    [instances],
  );
  if (instances.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400">No instance recorded.</p>
    );
  }
  return (
    <div className="space-y-2">
      {instances.length > 1 && (
        <p className="text-xs text-gray-600 dark:text-gray-400">
          {instances.length} instances of this completion
          {differing.length > 0
            ? `, differing in ${differing.join(", ")}.`
            : ", with the same parameters under different scopes."}
        </p>
      )}
      {instances.map((instance) => (
        <InstanceCard
          key={instance.id}
          instance={instance}
          deployment={deploymentsById.get(instance.deployment_id) ?? null}
          inPlan={instance.id === planInstanceId}
          differing={differing}
        />
      ))}
    </div>
  );
}

function InstanceCard({
  instance,
  deployment,
  inPlan,
  differing,
}: {
  instance: TaskInstance;
  deployment: Deployment | null;
  inPlan: boolean;
  differing: string[];
}) {
  const [full, setFull] = useState(false);
  const parameters = parametersOf(instance.body);
  const json = JSON.stringify(parameters, null, 2);
  return (
    <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-gray-200 bg-gray-50 px-3 py-1.5 text-xs dark:border-gray-700 dark:bg-gray-800">
        <span className="text-gray-500 dark:text-gray-400">Scope</span>
        <span
          className="font-medium text-gray-800 dark:text-gray-200"
          title={instance.deployment_id}
        >
          {deployment
            ? deploymentLabel(deployment)
            : instance.deployment_id.slice(0, 8)}
        </span>
        <span className="text-gray-500 dark:text-gray-400">settings</span>
        <code className="font-mono" title={instance.settings_hash}>
          {instance.settings_hash.slice(0, 12)}
        </code>
        <span className="text-gray-500 dark:text-gray-400">instance hash</span>
        <code className="font-mono" title={instance.instance_hash}>
          {instance.instance_hash.slice(0, 12)}
        </code>
        {inPlan && (
          <span className="rounded bg-blue-100 px-1.5 py-0.5 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300">
            in this plan
          </span>
        )}
        <span
          className="ml-auto text-gray-500 dark:text-gray-400"
          title={formatAbsoluteTime(instance.created_at)}
        >
          {formatRelativeTime(instance.created_at)}
          {instance.expanded_at ? "" : " · not expanded"}
        </span>
      </div>
      <div className="p-2">
        {differing.length > 0 && (
          <p className="mb-1 text-xs text-gray-600 dark:text-gray-400">
            {differing.map((key) => (
              <span key={key} className="mr-2">
                <code>{key}</code>={JSON.stringify(parameters[key]) ?? "unset"}
              </span>
            ))}
          </p>
        )}
        <pre className="max-h-48 overflow-auto rounded-md bg-gray-50 p-2 text-xs text-gray-800 dark:bg-gray-900 dark:text-gray-200">
          {json}
        </pre>
        <button
          type="button"
          onClick={() => setFull(true)}
          className="mt-1 text-xs text-blue-600 hover:underline dark:text-blue-400"
        >
          View fullscreen
        </button>
      </div>
      <FullscreenModal
        isOpen={full}
        onClose={() => setFull(false)}
        title="Instance parameters"
      >
        <pre className="overflow-auto rounded-md bg-gray-50 p-3 text-sm text-gray-800 dark:bg-gray-900 dark:text-gray-200">
          {json}
        </pre>
      </FullscreenModal>
    </div>
  );
}
