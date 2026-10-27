import { useId, useState } from "react";
import type { TaskInstance } from "../types/task";
import { shortHash } from "../utils/ids";
import { taskRefSnippet } from "../utils/taskRef";
import { formatRelativeTime } from "../utils/time";
import { CopyButton } from "./ModalExecution";

interface TaskRefSnippetProps {
  taskId: string;
  taskName: string;
  // Newest first, as the task read returns them.
  instances: TaskInstance[];
  // The instance to pin by default (e.g. the one a build's plan holds).
  preferredInstanceId?: string | null;
}

/**
 * "Reference in code": the `MyTask.ref(task_id, instance_id=...)` line that
 * points code at this exact task, pinned to an instance, with the class's
 * import when the namespace reads as a module path. What
 * `stardag tasks ref <task-id>` prints.
 */
export function TaskRefSnippet({
  taskId,
  taskName,
  instances,
  preferredInstanceId,
}: TaskRefSnippetProps) {
  const selectId = useId();
  const initial =
    instances.find((i) => i.id === preferredInstanceId)?.id ?? instances[0]?.id ?? null;
  const [chosen, setChosen] = useState<string | null>(null);
  const instance =
    instances.find((i) => i.id === chosen) ??
    instances.find((i) => i.id === initial) ??
    null;
  const snippet = taskRefSnippet(
    taskId,
    instance?.id ?? null,
    instance?.body ?? {},
    taskName,
  );

  return (
    <div>
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">
          Reference in code
        </h3>
        {instances.length > 1 && instance && (
          <label
            htmlFor={selectId}
            className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400"
          >
            pinned to instance
            <select
              id={selectId}
              value={instance.id}
              onChange={(e) => setChosen(e.target.value)}
              className="rounded border border-gray-300 px-1 py-0.5 font-mono text-xs text-gray-900 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100"
            >
              {instances.map((i, index) => (
                <option key={i.id} value={i.id}>
                  {shortHash(i.id)} — {formatRelativeTime(i.created_at)}
                  {index === 0 ? " (newest)" : ""}
                  {i.id === preferredInstanceId ? " (this plan)" : ""}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <div className="flex items-start gap-1 rounded-md bg-gray-50 p-2 dark:bg-gray-900/60">
        <pre
          aria-label="Code referencing this task"
          className="min-w-0 flex-1 overflow-x-auto font-mono text-xs leading-relaxed text-gray-800 dark:text-gray-200"
        >
          {snippet.importLine && (
            <span className="text-gray-500 dark:text-gray-400">
              {snippet.importLine}
              {"\n"}
            </span>
          )}
          {snippet.refLine}
        </pre>
        <CopyButton text={snippet.code} className="flex-shrink-0" />
      </div>
      <p className="mt-1 text-xs text-gray-400 dark:text-gray-500">
        {snippet.importLine
          ? "The import assumes the namespace is the class's module; "
          : "The class's module is not recorded with the task; "}
        <code className="font-mono">
          stardag tasks ref &lt;task-id&gt; -m &lt;module&gt;
        </code>{" "}
        prints the exact import.
      </p>
    </div>
  );
}
