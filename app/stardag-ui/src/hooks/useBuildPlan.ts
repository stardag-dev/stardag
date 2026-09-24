import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchBuild,
  fetchBuildFrontier,
  fetchPlanGraph,
  RegistryError,
} from "../api/registry";
import type { Build, BuildFrontier } from "../types/task";
import { fullPlanView, type PlanView } from "../utils/planGraph";

export interface BuildPlanState {
  build: Build | null;
  frontier: BuildFrontier | null;
  frontierError: string | null;
  view: PlanView | null;
  // The active plan's graph could not be read (a 404: the plan is gone).
  planError: string | null;
  loading: boolean;
  error: string | null;
  // The environment:build pair what is on screen was loaded for.
  loadedKey: string | null;
}

const EMPTY_VIEW: PlanView = { members: [], edges: [] };

/**
 * A build, the frontier of its active plan, and the plan's members and
 * edges — read together, with a stale-response guard, since the build
 * view stays mounted across a change of build or environment.
 *
 * Membership and edges come from `GET /plans/{id}/graph`. A 404 there is
 * a plan that does not exist (deleted between the frontier read and this
 * one), reported as such — never papered over with a partial view.
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
    planError: null,
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
      let planError: string | null = null;
      if (frontier?.plan_id) {
        try {
          view = fullPlanView(await fetchPlanGraph(frontier.plan_id, environmentId));
        } catch (err) {
          if (!(err instanceof RegistryError && err.status === 404)) throw err;
          planError = `The build's active plan ${frontier.plan_id} was not found: it may have been deleted since the build was read.`;
        }
      }
      if (!fresh()) return;
      setState({
        build,
        frontier,
        frontierError,
        view,
        planError,
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
