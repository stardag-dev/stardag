import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchBuild,
  fetchBuildFrontier,
  fetchPlanGraph,
  fetchPlanRoots,
} from "../api/registry";
import type { Build, BuildFrontier } from "../types/task";
import { fullPlanView, partialPlanView, type PlanView } from "../utils/planGraph";

export interface BuildPlanState {
  build: Build | null;
  frontier: BuildFrontier | null;
  frontierError: string | null;
  view: PlanView | null;
  loading: boolean;
  error: string | null;
  // The environment:build pair what is on screen was loaded for.
  loadedKey: string | null;
}

const EMPTY_VIEW: PlanView = { members: [], edges: [], complete: true };

/**
 * A build, the frontier of its active plan, and the plan's members and
 * edges — read together, with a stale-response guard, since the build
 * view stays mounted across a change of build or environment.
 *
 * Membership comes from `GET /plans/{id}/graph` when the registry serves
 * it, and otherwise from the plan's roots plus the frontier (partial).
 */
export function useBuildPlan(
  buildId: string,
  environmentId: string | undefined,
): BuildPlanState & { reload: () => Promise<void>; setBuild: (b: Build) => void } {
  const [state, setState] = useState<BuildPlanState>({
    build: null,
    frontier: null,
    frontierError: null,
    view: null,
    loading: true,
    error: null,
    loadedKey: null,
  });
  const epochRef = useRef(0);

  const reload = useCallback(async () => {
    if (!environmentId) return;
    const epoch = ++epochRef.current;
    const fresh = () => epochRef.current === epoch;
    const key = `${environmentId}:${buildId}`;
    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const build = await fetchBuild(buildId, environmentId);
      let frontier: BuildFrontier | null = null;
      let frontierError: string | null = null;
      try {
        frontier = await fetchBuildFrontier(buildId, environmentId);
      } catch (err) {
        frontierError = err instanceof Error ? err.message : "Failed to read frontier";
      }
      let view: PlanView = EMPTY_VIEW;
      if (frontier?.plan_id) {
        const graph = await fetchPlanGraph(frontier.plan_id, environmentId);
        if (graph) {
          view = fullPlanView(graph);
        } else {
          const roots = await fetchPlanRoots(frontier.plan_id, environmentId);
          view = partialPlanView(roots.roots, frontier);
        }
      }
      if (!fresh()) return;
      setState({
        build,
        frontier,
        frontierError,
        view,
        loading: false,
        error: null,
        loadedKey: key,
      });
    } catch (err) {
      if (!fresh()) return;
      setState((s) => ({
        ...s,
        loading: false,
        error: err instanceof Error ? err.message : "Failed to load build",
      }));
    }
  }, [buildId, environmentId]);

  useEffect(() => {
    reload();
  }, [reload]);

  // An override's response is newer than any read in flight: supersede it.
  const setBuild = useCallback((build: Build) => {
    epochRef.current += 1;
    setState((s) => ({ ...s, build, loading: false }));
  }, []);

  return { ...state, reload, setBuild };
}
