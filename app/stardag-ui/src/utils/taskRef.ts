/**
 * The code that references a registered task: the SDK's
 * `MyTask.ref(task_id, instance_id=...)`, as `stardag tasks ref` prints it.
 *
 * The class name is the task's name — its instances' `__name`
 * discriminator, which the task id hashes, so every instance of a task
 * names the same class. No import line is offered: the class's module is
 * not recorded with the task, and a task's namespace is an identity, not an
 * import path, so it must not be read as one.
 */

/** The `ref(...)` line, pinned to `instanceId` when one is given. */
export function taskRefSnippet(
  taskId: string,
  instanceId: string | null,
  className: string,
): string {
  const pin = instanceId ? `, instance_id="${instanceId}"` : "";
  return `${className}.ref("${taskId}"${pin})`;
}

/** The docs page explaining task references. */
export const TASK_REF_DOCS_URL =
  "https://docs.stardag.com/how-to/reference-existing-tasks/";
