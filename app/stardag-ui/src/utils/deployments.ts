import type { Deployment, DeploymentKind } from "../types/task";

/** One app's deployments: every generation, newest first. */
export interface AppDeployments {
  kind: DeploymentKind;
  appName: string;
  current: Deployment | null;
  generations: Deployment[];
}

/**
 * Group deployments by `(kind, app_name)` — generations are monotonic per
 * that pair, not per app name alone, so a local and a Modal app of one
 * name are two groups. Groups are sorted by app name, then kind.
 */
export function groupByApp(deployments: Deployment[]): AppDeployments[] {
  const groups = new Map<string, AppDeployments>();
  for (const deployment of deployments) {
    const key = `${deployment.kind}\u0000${deployment.app_name}`;
    let group = groups.get(key);
    if (!group) {
      group = {
        kind: deployment.kind,
        appName: deployment.app_name,
        current: null,
        generations: [],
      };
      groups.set(key, group);
    }
    group.generations.push(deployment);
    if (deployment.is_current) group.current = deployment;
  }
  const result = [...groups.values()];
  for (const group of result) {
    group.generations.sort((a, b) => b.generation - a.generation);
  }
  return result.sort(
    (a, b) => a.appName.localeCompare(b.appName) || a.kind.localeCompare(b.kind),
  );
}

/** "app gen 3", the short form a plan names its deployment by. */
export function deploymentLabel(deployment: Deployment): string {
  return `${deployment.app_name} gen ${deployment.generation}`;
}
