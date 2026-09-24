import { useEffect, useMemo, useState } from "react";
import { fetchDeployments } from "../api/registry";
import type { Deployment } from "../types/task";

/** The most the list route returns in one read. */
export const DEPLOYMENT_LIST_LIMIT = 500;

/**
 * The environment's deployments, for resolving a plan's or an instance's
 * `deployment_id` to an app and generation. There is no `GET
 * /deployments/{id}`, so the list is read once and looked up locally.
 */
export function useDeployments(environmentId: string | undefined): {
  deployments: Deployment[];
  byId: Map<string, Deployment>;
  loading: boolean;
  error: string | null;
} {
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!environmentId) return;
    let stale = false;
    setLoading(true);
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
        if (!stale) setLoading(false);
      });
    return () => {
      stale = true;
    };
  }, [environmentId]);

  const byId = useMemo(() => new Map(deployments.map((d) => [d.id, d])), [deployments]);
  return { deployments, byId, loading, error };
}
