import { useMemo, useState } from "react";
import type { Deployment, TaskInstance } from "../types/task";
import { deploymentLabel } from "../utils/deployments";
import { shortHash } from "../utils/ids";
import { differingParameters, parametersOf } from "../utils/instances";
import { formatAbsoluteTime, formatRelativeTime } from "../utils/time";
import { ExpandButton } from "./ArtifactViewer";
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
  // v1's two expand icons: the card's upper-right opens the whole instance,
  // the parameters block's upper-right only its parameters.
  const [fullscreen, setFullscreen] = useState<"instance" | "parameters" | null>(null);
  const parameters = parametersOf(instance.body);
  const json = JSON.stringify(parameters, null, 2);
  const scopeLabel = deployment
    ? deploymentLabel(deployment)
    : instance.deployment_id.slice(0, 8);
  const parametersPre = (
    <pre className="overflow-auto rounded-md bg-gray-50 p-3 text-sm text-gray-800 dark:bg-gray-900 dark:text-gray-200">
      {json}
    </pre>
  );
  return (
    <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-gray-200 bg-gray-50 px-3 py-1.5 text-xs dark:border-gray-700 dark:bg-gray-800">
        <span className="text-gray-500 dark:text-gray-400">Scope</span>
        <span
          className="font-medium text-gray-800 dark:text-gray-200"
          title={instance.deployment_id}
        >
          {scopeLabel}
        </span>
        <span className="text-gray-500 dark:text-gray-400">settings</span>
        <code className="font-mono" title={instance.settings_hash}>
          {shortHash(instance.settings_hash)}
        </code>
        <span className="text-gray-500 dark:text-gray-400">instance hash</span>
        <code className="font-mono" title={instance.instance_hash}>
          {shortHash(instance.instance_hash)}
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
        <ExpandButton
          onClick={() => setFullscreen("instance")}
          title="View instance fullscreen"
        />
      </div>
      <div className="space-y-1 p-2">
        {differing.length > 0 && (
          <p className="text-xs text-gray-600 dark:text-gray-400">
            {differing.map((key) => (
              <span key={key} className="mr-2">
                <code>{key}</code>={JSON.stringify(parameters[key]) ?? "unset"}
              </span>
            ))}
          </p>
        )}
        <div className="overflow-hidden rounded-md border border-gray-200 dark:border-gray-700">
          <div className="flex items-center justify-between border-b border-gray-200 bg-gray-50 px-2 py-1 dark:border-gray-700 dark:bg-gray-800">
            <span className="text-xs font-medium text-gray-700 dark:text-gray-300">
              Parameters
            </span>
            <div className="flex items-center gap-2">
              <span className="rounded bg-gray-200 px-2 py-0.5 text-xs text-gray-500 dark:bg-gray-700 dark:text-gray-400">
                json
              </span>
              <ExpandButton
                onClick={() => setFullscreen("parameters")}
                title="View parameters fullscreen"
              />
            </div>
          </div>
          <pre className="max-h-48 overflow-auto bg-gray-50 p-2 text-xs text-gray-800 dark:bg-gray-900 dark:text-gray-200">
            {json}
          </pre>
        </div>
      </div>
      <FullscreenModal
        isOpen={fullscreen === "parameters"}
        onClose={() => setFullscreen(null)}
        title="Instance parameters"
      >
        {parametersPre}
      </FullscreenModal>
      <FullscreenModal
        isOpen={fullscreen === "instance"}
        onClose={() => setFullscreen(null)}
        title="Instance"
      >
        <div className="space-y-4">
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-gray-500 dark:text-gray-400">Deployment</dt>
            <dd className="text-gray-900 dark:text-gray-100">
              {deployment ? `${scopeLabel} · ${deployment.kind}` : scopeLabel}{" "}
              <code className="font-mono text-xs text-gray-500">
                {instance.deployment_id}
              </code>
            </dd>
            <dt className="text-gray-500 dark:text-gray-400">Settings hash</dt>
            <dd className="font-mono text-gray-900 dark:text-gray-100">
              {instance.settings_hash}
            </dd>
            <dt className="text-gray-500 dark:text-gray-400">Instance hash</dt>
            <dd className="font-mono text-gray-900 dark:text-gray-100">
              {instance.instance_hash}
            </dd>
            <dt className="text-gray-500 dark:text-gray-400">Instance id</dt>
            <dd className="font-mono text-gray-900 dark:text-gray-100">
              {instance.id}
            </dd>
            <dt className="text-gray-500 dark:text-gray-400">Created</dt>
            <dd className="text-gray-900 dark:text-gray-100">
              {formatAbsoluteTime(instance.created_at)}
            </dd>
            <dt className="text-gray-500 dark:text-gray-400">Expanded</dt>
            <dd className="text-gray-900 dark:text-gray-100">
              {instance.expanded_at
                ? formatAbsoluteTime(instance.expanded_at)
                : "not expanded: requires() not evaluated under this scope"}
            </dd>
            {inPlan && (
              <>
                <dt className="text-gray-500 dark:text-gray-400">Plan</dt>
                <dd className="text-gray-900 dark:text-gray-100">
                  the instance this plan holds
                </dd>
              </>
            )}
          </dl>
          <div>
            <h3 className="mb-1 text-sm font-medium text-gray-500 dark:text-gray-400">
              Parameters
            </h3>
            {parametersPre}
          </div>
        </div>
      </FullscreenModal>
    </div>
  );
}
