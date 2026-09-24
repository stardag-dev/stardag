import { useMemo, useState } from "react";
import type { Deployment, TaskInstance } from "../types/task";
import { deploymentLabel } from "../utils/deployments";
import { shortHash } from "../utils/ids";
import { differingParameters, parametersOf } from "../utils/instances";
import { formatAbsoluteTime, formatRelativeTime } from "../utils/time";
import { ExpandButton } from "./ArtifactViewer";
import { FullscreenModal } from "./FullscreenModal";
import { Modal } from "./Modal";
import { Tooltip } from "./ui/Tooltip";

interface TaskParametersProps {
  // Newest first, as `GET /tasks/{id}` returns them.
  instances: TaskInstance[];
  deploymentsById: Map<string, Deployment>;
  // The instance the viewed plan holds; the newest when absent.
  planInstanceId?: string | null;
}

/**
 * v1's "Task Parameters" box, over the task's instances.
 *
 * Shows the parameters of one instance — the one the viewed plan holds,
 * or else the newest. The info icon opens that instance's scope
 * (deployment and settings hash), hashes and times, with the task's other
 * instances listed below as history. The expand icon opens every instance
 * in a table with the parameters of the selected one under it.
 *
 * An instance is a construction of the task under a scope; the instance
 * hash is printed only next to its scope, since on its own it identifies
 * nothing — two scopes may hold the same hash.
 */
export function TaskParameters({
  instances,
  deploymentsById,
  planInstanceId,
}: TaskParametersProps) {
  const [infoOpen, setInfoOpen] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const differing = useMemo(
    () => differingParameters(instances.map((instance) => instance.body)),
    [instances],
  );
  const current =
    instances.find((instance) => instance.id === planInstanceId) ?? instances[0];
  const inPlan = current !== undefined && current.id === planInstanceId;

  return (
    <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
      <div className="flex items-center justify-between border-b border-gray-200 bg-gray-50 px-3 py-2 dark:border-gray-700 dark:bg-gray-800">
        <div className="flex items-center gap-1.5">
          <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
            Task Parameters
          </span>
          {current && (
            <Tooltip
              content={
                instances.length > 1
                  ? `Instance info, and the ${instances.length - 1} other instance${
                      instances.length === 2 ? "" : "s"
                    } of this task`
                  : "Instance info: scope, hashes and times"
              }
            >
              <button
                type="button"
                onClick={() => setInfoOpen(true)}
                aria-label="Instance info"
                className="rounded p-0.5 text-gray-400 hover:bg-gray-200 hover:text-gray-600 dark:hover:bg-gray-600 dark:hover:text-gray-300"
              >
                <InfoIcon />
              </button>
            </Tooltip>
          )}
          {instances.length > 1 && (
            <span className="text-xs text-gray-500 dark:text-gray-400">
              {instances.length} instances
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="rounded bg-gray-200 px-2 py-0.5 text-xs text-gray-500 dark:bg-gray-700 dark:text-gray-400">
            json
          </span>
          {current && (
            <ExpandButton
              onClick={() => setFullscreen(true)}
              title="View parameters fullscreen"
            />
          )}
        </div>
      </div>
      {current ? (
        <ParametersPre instance={current} className="max-h-64" />
      ) : (
        <p className="p-3 text-sm text-gray-500 dark:text-gray-400">
          No instance recorded.
        </p>
      )}

      {current && (
        <Modal
          isOpen={infoOpen}
          onClose={() => setInfoOpen(false)}
          title="Task instance"
          maxWidthClass="max-w-3xl"
        >
          <div className="max-h-[70vh] space-y-4 overflow-y-auto">
            <div>
              <h3 className="mb-1 text-sm font-medium text-gray-500 dark:text-gray-400">
                {inPlan ? "In this plan" : "Latest"}
              </h3>
              <InstanceFacts
                instance={current}
                deployment={deploymentsById.get(current.deployment_id) ?? null}
              />
            </div>
            <div>
              <h3 className="mb-1 text-sm font-medium text-gray-500 dark:text-gray-400">
                Other instances ({instances.length - 1})
              </h3>
              {instances.length > 1 ? (
                <>
                  <InstanceTable
                    instances={instances.filter((i) => i.id !== current.id)}
                    deploymentsById={deploymentsById}
                    differing={differing}
                  />
                  <DifferingNote differing={differing} />
                </>
              ) : (
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  None: this is the task&rsquo;s only instance.
                </p>
              )}
            </div>
          </div>
        </Modal>
      )}

      {current && fullscreen && (
        <FullscreenParameters
          instances={instances}
          deploymentsById={deploymentsById}
          initial={current}
          planInstanceId={planInstanceId}
          differing={differing}
          onClose={() => setFullscreen(false)}
        />
      )}
    </div>
  );
}

/** The fullscreen view: every instance on top, the selected one's parameters below. */
function FullscreenParameters({
  instances,
  deploymentsById,
  initial,
  planInstanceId,
  differing,
  onClose,
}: {
  instances: TaskInstance[];
  deploymentsById: Map<string, Deployment>;
  initial: TaskInstance;
  planInstanceId?: string | null;
  differing: string[];
  onClose: () => void;
}) {
  const [selectedId, setSelectedId] = useState(initial.id);
  const selected = instances.find((i) => i.id === selectedId) ?? initial;
  return (
    <FullscreenModal isOpen onClose={onClose} title="Task Parameters">
      <div className="flex h-full flex-col gap-3">
        <div>
          <InstanceTable
            instances={instances}
            deploymentsById={deploymentsById}
            differing={differing}
            selectedId={selected.id}
            onSelect={setSelectedId}
            planInstanceId={planInstanceId}
          />
          {instances.length > 1 && <DifferingNote differing={differing} />}
        </div>
        <div className="min-h-0 flex-1 overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
          <ParametersPre instance={selected} className="h-full" />
        </div>
      </div>
    </FullscreenModal>
  );
}

function ParametersPre({
  instance,
  className = "",
}: {
  instance: TaskInstance;
  className?: string;
}) {
  return (
    <pre
      className={`overflow-auto bg-gray-50 p-3 text-sm text-gray-800 dark:bg-gray-900 dark:text-gray-200 ${className}`}
    >
      {JSON.stringify(parametersOf(instance.body), null, 2)}
    </pre>
  );
}

function scopeLabel(instance: TaskInstance, deployment: Deployment | null): string {
  return deployment ? deploymentLabel(deployment) : instance.deployment_id.slice(0, 8);
}

function InstanceFacts({
  instance,
  deployment,
}: {
  instance: TaskInstance;
  deployment: Deployment | null;
}) {
  const label = scopeLabel(instance, deployment);
  const dt = "text-gray-500 dark:text-gray-400";
  const dd = "text-gray-900 dark:text-gray-100";
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
      <dt className={dt}>Deployment</dt>
      <dd className={dd}>
        {deployment ? `${label} · ${deployment.kind}` : label}{" "}
        <code className="font-mono text-xs text-gray-500">
          {instance.deployment_id}
        </code>
      </dd>
      <dt className={dt}>Settings hash</dt>
      <dd className={`${dd} font-mono text-xs break-all`}>{instance.settings_hash}</dd>
      <dt className={dt}>Instance hash</dt>
      <dd className={`${dd} font-mono text-xs break-all`}>{instance.instance_hash}</dd>
      <dt className={dt}>Instance id</dt>
      <dd className={`${dd} font-mono text-xs`}>{instance.id}</dd>
      <dt className={dt}>Created</dt>
      <dd className={dd}>{formatAbsoluteTime(instance.created_at)}</dd>
      <dt className={dt}>Expanded</dt>
      <dd className={dd}>
        {instance.expanded_at
          ? formatAbsoluteTime(instance.expanded_at)
          : "not expanded: requires() not evaluated under this scope"}
      </dd>
    </dl>
  );
}

const TH =
  "px-2 py-1 text-left text-[11px] font-medium tracking-wider text-gray-500 uppercase dark:text-gray-400";
const TD = "px-2 py-1 whitespace-nowrap";

/** Instances, one compact row each; selectable when `onSelect` is given. */
function InstanceTable({
  instances,
  deploymentsById,
  differing,
  selectedId,
  onSelect,
  planInstanceId,
}: {
  instances: TaskInstance[];
  deploymentsById: Map<string, Deployment>;
  differing: string[];
  selectedId?: string;
  onSelect?: (id: string) => void;
  planInstanceId?: string | null;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
      <table className="min-w-full divide-y divide-gray-200 text-xs dark:divide-gray-700">
        <thead className="bg-gray-50 dark:bg-gray-800">
          <tr>
            <th className={TH}>Created</th>
            <th className={TH}>Deployment</th>
            <th className={TH}>Settings</th>
            <th className={TH}>Instance hash</th>
            <th className={TH}>Expanded</th>
            {differing.length > 0 && <th className={TH}>Differs in</th>}
            {planInstanceId && <th className={TH} />}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-200 bg-white text-gray-800 dark:divide-gray-700 dark:bg-gray-900 dark:text-gray-200">
          {instances.map((instance) => {
            const selected = instance.id === selectedId;
            const parameters = parametersOf(instance.body);
            return (
              <tr
                key={instance.id}
                onClick={onSelect ? () => onSelect(instance.id) : undefined}
                // Selectable from the keyboard too: Tab to a row, Enter or
                // Space to show its parameters.
                tabIndex={onSelect ? 0 : undefined}
                onKeyDown={
                  onSelect
                    ? (e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onSelect(instance.id);
                        }
                      }
                    : undefined
                }
                aria-selected={onSelect ? selected : undefined}
                className={
                  onSelect
                    ? `cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-inset ${
                        selected
                          ? "bg-blue-50 dark:bg-blue-950/40"
                          : "hover:bg-gray-50 dark:hover:bg-gray-800"
                      }`
                    : undefined
                }
              >
                <td className={TD} title={formatAbsoluteTime(instance.created_at)}>
                  {formatRelativeTime(instance.created_at)}
                </td>
                <td className={TD} title={instance.deployment_id}>
                  {scopeLabel(
                    instance,
                    deploymentsById.get(instance.deployment_id) ?? null,
                  )}
                </td>
                <td className={`${TD} font-mono`} title={instance.settings_hash}>
                  {shortHash(instance.settings_hash)}
                </td>
                <td className={`${TD} font-mono`} title={instance.instance_hash}>
                  {shortHash(instance.instance_hash)}
                </td>
                <td className={TD}>{instance.expanded_at ? "yes" : "no"}</td>
                {differing.length > 0 && (
                  <td className={`${TD} font-mono`}>
                    {differing.map((key) => (
                      <span key={key} className="mr-2">
                        {key}={JSON.stringify(parameters[key]) ?? "unset"}
                      </span>
                    ))}
                  </td>
                )}
                {planInstanceId && (
                  <td className={TD}>
                    {instance.id === planInstanceId && (
                      <span className="rounded bg-blue-100 px-1.5 py-0.5 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300">
                        in this plan
                      </span>
                    )}
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DifferingNote({ differing }: { differing: string[] }) {
  return (
    <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
      {differing.length > 0
        ? "Instances of one task differ only in non-significant parameters, or in how a nested task was built."
        : "Same parameters throughout: the instances differ only in scope."}
    </p>
  );
}

// The build toolbar's info icon (BuildInfoDialog), at the box's size.
function InfoIcon() {
  return (
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
        d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
      />
    </svg>
  );
}
