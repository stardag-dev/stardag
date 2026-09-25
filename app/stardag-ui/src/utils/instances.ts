/**
 * Reading task-instance bodies.
 *
 * A body is the registry-mode dump of a task object: every parameter,
 * nested tasks as full dumps, and the `__namespace` / `__name`
 * discriminator keys. The completion identity (`task_id`) hashes only the
 * significant parameters; the body carries all of them.
 */

import { shortTaskId } from "./ids";

/** The discriminator keys the SDK writes into every task body. */
const NAMESPACE_KEY = "__namespace";
const NAME_KEY = "__name";

export interface TaskIdentity {
  namespace: string;
  name: string;
}

/** The task class a body was dumped from; empty strings when absent. */
export function identityOf(body: Record<string, unknown>): TaskIdentity {
  const namespace = body[NAMESPACE_KEY];
  const name = body[NAME_KEY];
  return {
    namespace: typeof namespace === "string" ? namespace : "",
    name: typeof name === "string" ? name : "",
  };
}

/** `namespace.Name`, or `Name` in the default namespace. */
export function qualifiedName(namespace: string, name: string): string {
  return namespace ? `${namespace}.${name}` : name;
}

/**
 * The display label of a member: its task name and short task id, or the
 * short id alone when the body names no class.
 */
export function memberLabel(taskId: string, body: Record<string, unknown>): string {
  const { namespace, name } = identityOf(body);
  const short = shortTaskId(taskId);
  return name ? `${qualifiedName(namespace, name)} ${short}` : short;
}

/**
 * The parameters of a body: everything but the `__`-prefixed keys at its
 * top level. Nested task dumps keep theirs — they are what identifies the
 * nested task.
 */
export function parametersOf(body: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(body).filter(([key]) => !key.startsWith("__")),
  );
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) =>
      a < b ? -1 : a > b ? 1 : 0,
    );
    return `{${entries
      .map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

/**
 * The top-level parameters on which a body differs from any of the
 * others, sorted. Two instances of one completion differ only in
 * non-significant parameters (or in how a nested task was built), so this
 * is what "why are there two instances?" is answered with.
 */
export function differingParameters(bodies: Record<string, unknown>[]): string[] {
  if (bodies.length < 2) return [];
  const keys = new Set<string>();
  for (const body of bodies) {
    for (const key of Object.keys(parametersOf(body))) keys.add(key);
  }
  const differing: string[] = [];
  for (const key of keys) {
    const first = canonical(bodies[0][key]);
    if (bodies.some((body) => canonical(body[key]) !== first)) differing.push(key);
  }
  return differing.sort();
}
