/** The views the router renders, by path. */
export type View =
  | "callback"
  | "settings"
  | "invites"
  | "new-workspace"
  | "tasks"
  | "deployments"
  | "limits"
  | "build"
  | "builds";

/**
 * Which view a path renders. Environment-scoped views take the same
 * forms: bare (`/limits`), under a workspace whose environment is the
 * default (`/<ws>/limits`), or under both (`/<ws>/<env>/limits`) — so a
 * deep link to any of them lands on that view, not on the builds list.
 */
export function viewFromPath(path: string): View {
  if (path === "/callback") return "callback";
  if (path === "/settings") return "settings";
  if (path === "/invites") return "invites";
  if (path === "/workspaces/new") return "new-workspace";

  // Tasks: /tasks[/:task_id], optionally under /:org[/:environment]
  if (/(^|\/)tasks(\/[^/]+)?$/.test(path)) return "tasks";

  // Deployments: /deployments (same env-scoped forms)
  if (path === "/deployments" || path.endsWith("/deployments")) return "deployments";

  // Concurrency limits: /limits (same env-scoped forms)
  if (path === "/limits" || path.endsWith("/limits")) return "limits";

  // /builds/:id or /:org/:environment/builds/:id
  if (/\/builds\/[^/]+/.test(path)) return "build";

  return "builds";
}
