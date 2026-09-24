import { useEffect, useState, type ReactNode } from "react";
import { fetchSettings } from "../api/registry";
import type { Build, BuildFrontier, Deployment, Settings } from "../types/task";
import { deploymentLabel } from "../utils/deployments";
import { isModalMetadata, modalAppUrl } from "../utils/modalLinks";
import { formatAbsoluteTime } from "../utils/time";
import { BuildStatusBadge } from "./BuildStatusBadge";
import { Modal } from "./Modal";
import { CopyChip } from "./ui/CopyChip";
import { ToolbarButton } from "./ui/ToolbarButton";

interface BuildInfoDialogProps {
  build: Build;
  environmentId: string;
  // The active plan, as the frontier reports it; null while unread.
  frontier: BuildFrontier | null;
  // The active plan's deployment, resolved from the deployment list.
  deployment: Deployment | null;
}

/**
 * Everything about a build that is not "where am I" or "what can I do":
 * its id, execution, the scope its active plan runs under (deployment and
 * settings), and its times. The settings body is read on open, by hash.
 */
export function BuildInfoDialog({
  build,
  environmentId,
  frontier,
  deployment,
}: BuildInfoDialogProps) {
  const [open, setOpen] = useState(false);
  const settingsHash = frontier?.settings_hash ?? null;
  const [settings, setSettings] = useState<Settings | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !settingsHash) return;
    let stale = false;
    fetchSettings(settingsHash, environmentId)
      .then((s) => {
        if (stale) return;
        setSettings(s);
        setSettingsError(null);
      })
      .catch((err: unknown) => {
        if (stale) return;
        setSettingsError(
          err instanceof Error ? err.message : "Failed to read settings",
        );
      });
    return () => {
      stale = true;
    };
  }, [open, settingsHash, environmentId]);

  return (
    <>
      <ToolbarButton
        label="Build info"
        hint="Id, execution, deployment and settings"
        onClick={() => setOpen(true)}
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
            d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
          />
        </svg>
      </ToolbarButton>

      <Modal
        isOpen={open}
        onClose={() => setOpen(false)}
        title="Build info"
        maxWidthClass="max-w-2xl"
      >
        <div className="max-h-[70vh] space-y-0 overflow-y-auto">
          <Field label="Name">
            <span className="font-medium text-gray-900 dark:text-gray-100">
              {build.name}
            </span>
            <BuildStatusBadge status={build.status} isResumed={build.is_resumed} />
          </Field>
          <Field
            label="Build id"
            hint="What every CLI command against this build takes"
          >
            <CopyChip label={build.id} value={build.id} title="Build id" />
          </Field>
          {build.description && <Field label="Description">{build.description}</Field>}
          <ExecutorField build={build} />
          <DeploymentField frontier={frontier} deployment={deployment} />
          <SettingsField
            settingsHash={settingsHash}
            settings={settings && settings.hash === settingsHash ? settings : null}
            error={settingsError}
          />
          <Field
            label="Roots"
            hint="The request, at completion-id level; stable across rollover"
          >
            <span>
              {build.root_task_ids.length} task
              {build.root_task_ids.length === 1 ? "" : "s"}
            </span>
          </Field>
          <Field label="Created">{formatAbsoluteTime(build.created_at)}</Field>
          {build.started_at && (
            <Field label="Started">{formatAbsoluteTime(build.started_at)}</Field>
          )}
          {build.completed_at && (
            <Field label="Ended">{formatAbsoluteTime(build.completed_at)}</Field>
          )}
        </div>
      </Modal>
    </>
  );
}

/**
 * One fact: a muted label, then the value.
 *
 * Label above value rather than beside it, so a long value — a scope key,
 * a full UUID — never has to be truncated to keep a column aligned. That
 * truncation is what made these unreadable as pills.
 */
function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="border-b border-gray-200 py-2.5 last:border-b-0 dark:border-gray-700">
      <p className="text-xs text-gray-500 dark:text-gray-400">{label}</p>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-gray-800 dark:text-gray-200">
        {children}
      </div>
      {hint && <p className="mt-1 text-xs text-gray-500 dark:text-gray-500">{hint}</p>}
    </div>
  );
}

/** Which backend ran this build, and whether ticks drive it. */
function ExecutorField({ build }: { build: Build }) {
  const metadata = build.executor_metadata;
  const appName =
    typeof metadata?.app_name === "string" && metadata.app_name.length > 0
      ? metadata.app_name
      : null;
  // `kind` decides what the app name is *called*. Without this check a
  // build on any other backend that happens to record an `app_name` is
  // announced as Modal — the same guard `BuildExecutorChips` has always
  // had, and the reason this dialog needs it too.
  const isModal = metadata ? isModalMetadata(metadata) : false;
  const kind = typeof metadata?.kind === "string" ? metadata.kind : null;
  const url = modalAppUrl(metadata);

  // `reactive_app_name` is the canonical marker: the reactive-meta
  // endpoint sets it independently of whatever trigger metadata the
  // build was created with, so a build can be tick-driven with nothing
  // in `executor_metadata` to show for it. The metadata flag stays as a
  // fallback for servers that predate the column.
  const reactiveApp = build.reactive_app_name ?? null;
  const reactive = reactiveApp !== null || metadata?.reactive === true;

  if (!appName && !reactive) {
    return (
      <Field
        label="Execution"
        hint={
          metadata
            ? "No app was recorded for this build's trigger."
            : "No executor was recorded. Either the build ran in its own process, or it predates the server recording one."
        }
      >
        <span className="text-gray-500 dark:text-gray-400">
          {kind ?? "Not recorded"}
        </span>
      </Field>
    );
  }

  const appLabel = appName
    ? isModal
      ? `Modal app ${appName}`
      : `${kind ?? "Executor"} app ${appName}`
    : null;

  return (
    <Field
      label="Execution"
      hint={
        reactive
          ? "Reactive: scheduler ticks drive this build, with no resident orchestrator."
          : "Driven by a resident orchestrator rather than by scheduler ticks."
      }
    >
      {appLabel &&
        (url ? (
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className="font-medium text-blue-700 hover:underline dark:text-blue-400"
          >
            {appLabel}
          </a>
        ) : (
          <span className="font-medium">{appLabel}</span>
        ))}
      {/* Named separately when the reactive app is not the trigger's
          app — they are different facts and can disagree. */}
      {reactiveApp && reactiveApp !== appName && (
        <span className="text-gray-600 dark:text-gray-400">
          ticked by {reactiveApp}
        </span>
      )}
      {!appLabel && reactive && <span>Reactive</span>}
    </Field>
  );
}

/** The active plan's deployment: app, generation, code id. */
function DeploymentField({
  frontier,
  deployment,
}: {
  frontier: BuildFrontier | null;
  deployment: Deployment | null;
}) {
  const deploymentId = frontier?.deployment_id ?? null;
  if (!deploymentId) {
    return (
      <Field label="Deployment" hint="The build has no active plan yet.">
        <span className="text-gray-500 dark:text-gray-400">None</span>
      </Field>
    );
  }
  if (!deployment) {
    return (
      <Field label="Deployment" hint="Not among the environment's listed deployments.">
        <CopyChip label={deploymentId} value={deploymentId} title="Deployment id" />
      </Field>
    );
  }
  return (
    <Field
      label="Deployment"
      hint={
        deployment.is_current
          ? "The app's current deployment."
          : "Not the app's current deployment: the build's next tick rolls it over."
      }
    >
      <span className="font-medium">{deploymentLabel(deployment)}</span>
      <span className="text-gray-600 dark:text-gray-400">{deployment.kind}</span>
      <CopyChip label={deployment.code_id} value={deployment.code_id} title="Code id" />
      {deployment.is_current && (
        <span className="rounded bg-green-100 px-1.5 py-0.5 text-xs text-green-800 dark:bg-green-900/40 dark:text-green-300">
          current
        </span>
      )}
    </Field>
  );
}

/** The active plan's settings, by hash, with the body once read. */
function SettingsField({
  settingsHash,
  settings,
  error,
}: {
  settingsHash: string | null;
  settings: Settings | null;
  error: string | null;
}) {
  if (!settingsHash) return null;
  const keys = settings ? Object.keys(settings.body).length : null;
  return (
    <Field
      label={
        keys === null ? "Settings" : `Settings — ${keys} key${keys === 1 ? "" : "s"}`
      }
      hint="Part of the plan's scope: two plans under different settings share no instances."
    >
      <CopyChip
        label={settingsHash.slice(0, 16)}
        value={settingsHash}
        title="Settings hash"
      />
      {error ? (
        <span className="text-xs text-red-600 dark:text-red-400">{error}</span>
      ) : settings === null ? (
        <span className="text-xs text-gray-500">Reading…</span>
      ) : keys === 0 ? (
        <span className="text-xs text-gray-500 dark:text-gray-400">empty</span>
      ) : (
        <pre className="max-h-64 w-full overflow-auto rounded bg-gray-50 p-3 font-mono text-[11px] text-gray-700 dark:bg-gray-900 dark:text-gray-300">
          {JSON.stringify(settings.body, null, 2)}
        </pre>
      )}
    </Field>
  );
}
