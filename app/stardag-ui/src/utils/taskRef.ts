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

/**
 * Whether the class's `ref` classmethod is shadowed: a task may have a
 * parameter named `ref`, and then `MyTask.ref` is that field, not the
 * method. Read off an instance body's top-level keys (its fields).
 */
export function refIsShadowed(body: Record<string, unknown> | undefined): boolean {
  return body !== undefined && Object.prototype.hasOwnProperty.call(body, "ref");
}

/**
 * The reference, pinned to `instanceId` when one is given:
 * `MyTask.ref(...)`, or — when a `ref` field shadows the method —
 * `sd.task_ref(MyTask, ...)` with the `import stardag as sd` it needs.
 */
export function taskRefSnippet(
  taskId: string,
  instanceId: string | null,
  className: string,
  refShadowed = false,
): string {
  const pin = instanceId ? `, instance_id="${instanceId}"` : "";
  return refShadowed
    ? `import stardag as sd\n\nsd.task_ref(${className}, "${taskId}"${pin})`
    : `${className}.ref("${taskId}"${pin})`;
}

/** The docs page explaining task references. */
export const TASK_REF_DOCS_URL =
  "https://docs.stardag.com/how-to/reference-existing-tasks/";
