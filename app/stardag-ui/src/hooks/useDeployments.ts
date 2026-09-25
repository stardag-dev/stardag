import { useEffect, useMemo, useState } from "react";
import { fetchDeployment, fetchDeployments } from "../api/registry";
import type { Deployment } from "../types/task";

/** The most the list route returns in one read. */
export const DEPLOYMENT_LIST_LIMIT = 500;

/**
 * The environment's deployments, for resolving a plan's or an instance's
 * `deployment_id` to an app and generation.
 *
 * The list is read once (its first `DEPLOYMENT_LIST_LIMIT` rows) and
 * looked up locally. Any id in `wanted` the list does not hold — an old
 * generation past the first page — is then read on its own with
 * `GET /deployments/{id}` and merged into `byId`, so a caller never falls
 * back to printing a bare id for a deployment the registry knows.
 */
export function useDeployments(
  environmentId: string | undefined,
  wanted: readonly (string | null | undefined)[] = [],
): {
  deployments: Deployment[];
  byId: Map<string, Deployment>;
  loading: boolean;
  error: string | null;
} {
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Looked up one by one, per environment; null marks a lookup that failed,
  // so it is not retried on every render.
  const [extra, setExtra] = useState<{
    env: string | null;
    rows: Map<string, Deployment | null>;
  }>({ env: null, rows: new Map() });

  useEffect(() => {
    if (!environmentId) return;
    let stale = false;
    fetchDeployments(environmentId, { limit: DEPLOYMENT_LIST_LIMIT })
      .then((rows) => {
        if (stale) return;
        setDeployments(rows);
        setError(null);
      })
      .catch((err: unknown) => {
        if (stale) return;
        setDeployments([]);
        setError(err instanceof Error ? err.message : "Failed to load deployments");
      })
      .finally(() => {
        if (!stale) setLoadedFor(environmentId);
      });
    return () => {
      stale = true;
    };
  }, [environmentId]);

  const listed = useMemo(
    () => new Map(deployments.map((d) => [d.id, d])),
    [deployments],
  );
  const extraRows = extra.env === environmentId ? extra.rows : null;
  const wantedKey = [...new Set(wanted.filter((id): id is string => Boolean(id)))]
    .sort()
    .join(",");

  useEffect(() => {
    if (!environmentId || loadedFor !== environmentId || !wantedKey) return;
    const missing = wantedKey
      .split(",")
      .filter((id) => !listed.has(id) && !extraRows?.has(id));
    if (missing.length === 0) return;
    let stale = false;
    Promise.all(
      missing.map((id) =>
        fetchDeployment(id, environmentId).then(
          (row): [string, Deployment | null] => [id, row],
          (): [string, Deployment | null] => [id, null],
        ),
      ),
    ).then((found) => {
      if (stale) return;
      setExtra((previous) => {
        const rows = new Map(previous.env === environmentId ? previous.rows : []);
        for (const [id, row] of found) rows.set(id, row);
        return { env: environmentId, rows };
      });
    });
    return () => {
      stale = true;
    };
  }, [environmentId, loadedFor, wantedKey, listed, extraRows]);

  const byId = useMemo(() => {
    const merged = new Map(listed);
    for (const [id, row] of extraRows ?? []) if (row) merged.set(id, row);
    return merged;
  }, [listed, extraRows]);
  return { deployments, byId, loading: loadedFor !== environmentId, error };
}
