import { useState, type ReactNode } from "react";
import type { Build } from "../types/task";
import { isModalMetadata, modalAppUrl } from "../utils/modalLinks";
import { isSyntheticScope } from "../utils/scope";
import { formatAbsoluteTime } from "../utils/time";
import { BuildStatusBadge } from "./BuildStatusBadge";
import { Modal } from "./Modal";
import { CopyChip } from "./ui/CopyChip";
import { ToolbarButton } from "./ui/ToolbarButton";

/**
 * Everything about a build that is not "where am I" or "what can I do".
 *
 * These facts were four coloured pills — the Modal app, a `reactive`
 * badge, a truncated structure scope and a build-config count — sitting
 * first in the breadcrumb and then in the toolbar. As pills they were
 * loud enough to read as status while saying nothing that changes during
 * a build, and each had to be truncated to fit, which is how a scope key
 * ends up as `scope: 5c6ed85f155d…` and tells you nothing.
 *
 * Behind one icon they can be shown properly: full values, room to say
 * what each one means, and a copy affordance on the two that get pasted
 * into commands.
 */
export function BuildInfoDialog({ build }: { build: Build }) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <ToolbarButton
        label="Build info"
        hint="Id, execution, structure scope and config"
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
          <ScopeField scopeKey={build.scope_key} />

          {build.commit_hash && (
            <Field label="Commit">
              <CopyChip
                label={build.commit_hash}
                value={build.commit_hash}
                title="Commit"
              />
            </Field>
          )}

          <Field label="Created">{formatAbsoluteTime(build.created_at)}</Field>
          {build.started_at && (
            <Field label="Started">{formatAbsoluteTime(build.started_at)}</Field>
          )}
          {build.completed_at && (
            <Field label="Ended">{formatAbsoluteTime(build.completed_at)}</Field>
          )}

          <ConfigField config={build.build_config} />
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

/**
 * The structure scope, at full length and explained.
 *
 * As a pill it was `scope: 5c6ed85f155d…`, which is neither readable nor
 * comparable — and comparing two of them is the only thing anyone does
 * with a scope key.
 */
function ScopeField({ scopeKey }: { scopeKey?: string | null }) {
  if (!scopeKey) return null;
  const synthetic = isSyntheticScope(scopeKey);
  return (
    <Field
      label="Structure scope"
      hint={
        synthetic
          ? "Synthetic: this build shares its dependency edges with no other build."
          : "The code version and structure config this build is currently planned under. It moves when the app is redeployed — the next scheduler pass re-plans the build under the new code."
      }
    >
      {synthetic ? (
        <span className="text-gray-500 dark:text-gray-400">
          Per-build <code className="font-mono text-xs">({scopeKey})</code>
        </span>
      ) : (
        <CopyChip label={scopeKey} value={scopeKey} title="Structure scope" />
      )}
    </Field>
  );
}

/** The central values levels 2 and 3 parameters were read from. */
function ConfigField({
  config,
}: {
  config?: Record<string, Record<string, unknown>> | null;
}) {
  const classCount = config ? Object.keys(config).length : 0;
  if (classCount === 0) return null;
  return (
    <Field
      label={`Build config — ${classCount} task class${classCount === 1 ? "" : "es"}`}
      hint="Part of the structure scope, so two builds that disagree here do not share dependency edges."
    >
      <pre className="max-h-64 w-full overflow-auto rounded bg-gray-50 p-3 font-mono text-[11px] text-gray-700 dark:bg-gray-900 dark:text-gray-300">
        {JSON.stringify(config, null, 2)}
      </pre>
    </Field>
  );
}
