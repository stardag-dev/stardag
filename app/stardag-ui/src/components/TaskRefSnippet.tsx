import { useId, useState } from "react";
import type { TaskInstance } from "../types/task";
import { shortHash } from "../utils/ids";
import { refIsShadowed, TASK_REF_DOCS_URL, taskRefSnippet } from "../utils/taskRef";
import { formatRelativeTime } from "../utils/time";
import { CopyButton } from "./ModalExecution";
import { InfoIcon } from "./ui/InfoIcon";
import { Tooltip } from "./ui/Tooltip";

interface TaskRefSnippetProps {
  taskId: string;
  taskName: string;
  // Newest first, as the task read returns them (at most its page of them).
  instances: TaskInstance[];
  // The instance to pin by default (the one a build's plan holds). It is
  // pinned even when it is older than the instances the task read
  // returned: the id is all the reference needs.
  preferredInstanceId?: string | null;
}

/**
 * "Reference in code": the `MyTask.ref(task_id, instance_id=...)` line that
 * points code at this exact task, pinned to an instance. What
 * `stardag tasks ref <task-id>` prints. No import line: the class's module
 * is not recorded, and the namespace is not an import path.
 */
export function TaskRefSnippet({
  taskId,
  taskName,
  instances,
  preferredInstanceId,
}: TaskRefSnippetProps) {
  const selectId = useId();
  const options: { id: string; label: string }[] = instances.map((i, index) => ({
    id: i.id,
    label: `${shortHash(i.id)} — ${formatRelativeTime(i.created_at)}${
      index === 0 ? " (newest)" : ""
    }${i.id === preferredInstanceId ? " (this plan)" : ""}`,
  }));
  if (preferredInstanceId && !options.some((o) => o.id === preferredInstanceId)) {
    options.push({
      id: preferredInstanceId,
      label: `${shortHash(preferredInstanceId)} (this plan)`,
    });
  }
  const [chosen, setChosen] = useState<string | null>(null);
  const instanceId =
    options.find((o) => o.id === chosen)?.id ??
    preferredInstanceId ??
    options[0]?.id ??
    null;
  // Every instance of a task is the same class, so any body tells whether a
  // `ref` field shadows the method; the chosen one is read when it is here.
  const body = (instances.find((i) => i.id === instanceId) ?? instances[0])?.body;
  const snippet = taskRefSnippet(taskId, instanceId, taskName, refIsShadowed(body));

  return (
    <div>
      <div className="mb-1 flex flex-wrap items-center gap-1.5">
        <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">
          Reference in code
        </h3>
        <Tooltip content="Points code at this exact task instead of re-composing it, so it keeps resolving to the same task as the code evolves. Uses the registered task name as the class: for a renamed or nested class, `stardag tasks ref <task-id> -m <module>` prints the exact expression. Click for the docs.">
          <a
            href={TASK_REF_DOCS_URL}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="About task references (opens the docs)"
            className="rounded p-0.5 text-gray-400 hover:bg-gray-200 hover:text-gray-600 dark:hover:bg-gray-600 dark:hover:text-gray-300"
          >
            <InfoIcon />
          </a>
        </Tooltip>
        {options.length > 1 && instanceId && (
          <label
            htmlFor={selectId}
            className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400"
          >
            pinned to instance
            <select
              id={selectId}
              value={instanceId}
              onChange={(e) => setChosen(e.target.value)}
              className="rounded border border-gray-300 px-1 py-0.5 font-mono text-xs text-gray-900 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100"
            >
              {options.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
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
          {snippet}
        </pre>
        <CopyButton text={snippet} className="flex-shrink-0" />
      </div>
    </div>
  );
}
