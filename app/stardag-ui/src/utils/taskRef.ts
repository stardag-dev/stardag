/**
 * The code that references a registered task: the SDK's
 * `MyTask.ref(task_id, instance_id=...)`, as `stardag tasks ref` prints it.
 *
 * The class name is the task's name — its instances' `__name`
 * discriminator, which the task id hashes, so every instance of a task
 * names the same class. The module is not recorded: the namespace defaults
 * to empty and is only a module path when the project chose one that is.
 * So the import line is offered only when the namespace reads as a dotted
 * Python module path, and is marked a guess — the CLI with `-m <module>`
 * prints the exact one.
 */

export interface TaskRefSnippet {
  // `from <namespace> import <Class>`, or null when the namespace is not a
  // plausible module path.
  importLine: string | null;
  refLine: string;
  // Both, newline-joined: what the copy button copies.
  code: string;
}

const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Whether `value` reads as a dotted Python module path. */
export function isModulePath(value: string): boolean {
  return value !== "" && value.split(".").every((part) => IDENTIFIER.test(part));
}

export function taskRefSnippet(
  taskId: string,
  instanceId: string | null,
  namespace: string,
  className: string,
): TaskRefSnippet {
  // `Outer.Inner` is imported as `Outer`, as the CLI does.
  const importedAs = className.split(".")[0];
  const importLine =
    isModulePath(namespace) && IDENTIFIER.test(importedAs)
      ? `from ${namespace} import ${importedAs}`
      : null;
  const pin = instanceId ? `, instance_id="${instanceId}"` : "";
  const refLine = `${className}.ref("${taskId}"${pin})`;
  return {
    importLine,
    refLine,
    code: importLine ? `${importLine}\n${refLine}` : refLine,
  };
}
