import { useEffect, useMemo } from "react";
import { useBreadcrumb } from "../context/BreadcrumbContext";
import { useEnvironment } from "../context/EnvironmentContext";
import { DEPLOYMENT_LIST_LIMIT, useDeployments } from "../hooks/useDeployments";
import type { Deployment } from "../types/task";
import { groupByApp, type AppDeployments } from "../utils/deployments";
import { formatAbsoluteTime, formatRelativeTime } from "../utils/time";
import { CopyChip } from "./ui/CopyChip";
import { ResultBanner } from "./ui/ResultBanner";

/** A Modal app's dashboard link from its app id, when the id is known. */
function modalAppIdUrl(modalAppId: string | null): string | null {
  return modalAppId ? `https://modal.com/id/${modalAppId}` : null;
}

function Time({ at }: { at: string | null }) {
  if (!at) return <span className="text-gray-400">—</span>;
  return <span title={formatAbsoluteTime(at)}>{formatRelativeTime(at)}</span>;
}

function GenerationRow({ deployment }: { deployment: Deployment }) {
  const appUrl = modalAppIdUrl(deployment.modal_app_id);
  return (
    <tr className={deployment.is_current ? "bg-green-50/60 dark:bg-green-950/20" : ""}>
      <td className="px-3 py-1.5 font-mono text-xs">
        {deployment.generation}
        {deployment.is_current && (
          <span className="ml-2 rounded bg-green-100 px-1.5 py-0.5 font-sans text-green-800 dark:bg-green-900/40 dark:text-green-300">
            current
          </span>
        )}
      </td>
      <td className="px-3 py-1.5">
        <CopyChip label={deployment.code_id} value={deployment.code_id} title="Code id" />
      </td>
      <td className="px-3 py-1.5 text-xs text-gray-600 dark:text-gray-400">
        <Time at={deployment.deployed_at} />
      </td>
      <td className="px-3 py-1.5 text-xs text-gray-600 dark:text-gray-400">
        {deployment.activated_at ? (
          <Time at={deployment.activated_at} />
        ) : (
          <span
            className="text-amber-700 dark:text-amber-400"
            title="Recorded, but the deploy never reported success: never current, cannot host a plan"
          >
            not activated
          </span>
        )}
      </td>
      <td className="px-3 py-1.5 text-xs">
        {deployment.modal_app_id ? (
          appUrl ? (
            <a
              href={appUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="font-mono text-blue-700 hover:underline dark:text-blue-300"
            >
              {deployment.modal_app_id}
            </a>
          ) : (
            <code>{deployment.modal_app_id}</code>
          )
        ) : (
          <span className="text-gray-400">—</span>
        )}
      </td>
    </tr>
  );
}

function AppCard({ group }: { group: AppDeployments }) {
  return (
    <section className="overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
      <header className="flex flex-wrap items-center gap-2 border-b border-gray-200 px-4 py-2 dark:border-gray-700">
        <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
          {group.appName}
        </h2>
        <span className="rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-600 dark:bg-gray-700 dark:text-gray-300">
          {group.kind}
        </span>
        <span className="text-xs text-gray-500 dark:text-gray-400">
          {group.current
            ? `current: generation ${group.current.generation}`
            : "no current deployment"}
        </span>
      </header>
      <table className="min-w-full text-left">
        <thead className="text-xs text-gray-500 dark:text-gray-400">
          <tr>
            <th className="px-3 py-1 font-medium">Generation</th>
            <th className="px-3 py-1 font-medium">Code id</th>
            <th className="px-3 py-1 font-medium">Deployed</th>
            <th className="px-3 py-1 font-medium">Activated</th>
            <th className="px-3 py-1 font-medium">Modal app</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
          {group.generations.map((d) => (
            <GenerationRow key={d.id} deployment={d} />
          ))}
        </tbody>
      </table>
    </section>
  );
}

/**
 * Deployments: per app, every generation, the current one marked. A plan
 * runs under one deployment; a reactive build rolls over to its app's
 * current deployment at its next tick.
 */
export function DeploymentsPage() {
  const { activeEnvironment } = useEnvironment();
  const { setItems: setBreadcrumb } = useBreadcrumb();
  const { deployments, loading, error } = useDeployments(activeEnvironment?.id);
  const groups = useMemo(() => groupByApp(deployments), [deployments]);

  useEffect(() => {
    setBreadcrumb([{ label: "Deployments" }]);
    return () => setBreadcrumb([]);
  }, [setBreadcrumb]);

  if (!activeEnvironment) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500 dark:text-gray-400">
        Select an environment to view deployments
      </div>
    );
  }
  return (
    <div className="space-y-3 p-4">
      {error && <ResultBanner tone="error">{error}</ResultBanner>}
      {loading ? (
        <p role="status" className="text-sm text-gray-500 dark:text-gray-400">
          Loading deployments…
        </p>
      ) : groups.length === 0 ? (
        <p className="text-sm text-gray-500 dark:text-gray-400">
          No deployments recorded. <code>stardag modal deploy</code> records one.
        </p>
      ) : (
        <>
          {deployments.length === DEPLOYMENT_LIST_LIMIT && (
            <p className="text-xs text-amber-800 dark:text-amber-300">
              Showing the newest {DEPLOYMENT_LIST_LIMIT}; older generations are not
              listed.
            </p>
          )}
          {groups.map((group) => (
            <AppCard key={`${group.kind}:${group.appName}`} group={group} />
          ))}
        </>
      )}
    </div>
  );
}
