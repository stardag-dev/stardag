import { useEffect, useRef, useState } from "react";
import { fetchTask } from "../api/registry";
import { useBreadcrumb } from "../context/BreadcrumbContext";
import { useTaskDiffUrl } from "../hooks/useTaskDiffUrl";
import { useTaskSearchUrl } from "../hooks/useTaskSearchUrl";
import type { Task, TaskInstance } from "../types/task";
import { shortHash, shortTaskId } from "../utils/ids";
import { qualifiedName } from "../utils/instances";
import {
  isSameTypeSearch,
  pickInstance,
  pickOtherInstance,
  sameTypeSearch,
  TASK_DIFF_PARAMS,
  type TaskDiffSelection,
} from "../utils/taskDiff";
import { isEmptySearch } from "../utils/taskSearch";
import { formatAbsoluteTime, formatRelativeTime } from "../utils/time";
import { JsonDiffView } from "./JsonDiffView";
import { TaskSearchPanel } from "./TaskSearch";
import { CopyChip } from "./ui/CopyChip";
import { ResultBanner } from "./ui/ResultBanner";
import { Tooltip } from "./ui/Tooltip";

interface TaskDiffPageProps {
  environmentId: string;
  /** Open a task's page; `""` opens the task search. */
  onOpenTask: (taskId: string) => void;
}

interface TaskRead {
  task: Task | null;
  error: string | null;
}

/** `GET /tasks/{id}`, re-read when the id changes; `null` id reads nothing. */
function useTask(taskId: string | null, environmentId: string): TaskRead {
  const [state, setState] = useState<TaskRead & { key: string | null }>({
    key: null,
    task: null,
    error: null,
  });
  const key = taskId ? `${environmentId}\u0000${taskId}` : null;
  useEffect(() => {
    if (!taskId || key === null) return;
    let live = true;
    fetchTask(taskId, environmentId)
      .then((task) => live && setState({ key, task, error: null }))
      .catch(
        (err: unknown) =>
          live &&
          setState({
            key,
            task: null,
            error: err instanceof Error ? err.message : "Failed to load task",
          }),
      );
    return () => {
      live = false;
    };
  }, [taskId, environmentId, key]);
  // A read for another id is not this one's: report it as loading.
  return state.key === key ? state : { task: null, error: null };
}

const BUTTON =
  "rounded-md border border-gray-300 px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700";
const LINK_BUTTON =
  "rounded px-1 text-xs font-medium text-blue-600 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-400";

/**
 * `/task-diff`: two task instances side by side as a structural diff of
 * their parameters.
 *
 * The left side arrives pre-selected (from a task's "Compare" action);
 * the right is picked with the `/tasks` search, pre-filtered to the left
 * task's type, since a diff between two types is mostly additions and
 * removals. Either side's instance can be switched, so two instances of
 * one task compare too. Everything is in the URL: shareable, and Back
 * steps through the picks.
 */
export function TaskDiffPage({ environmentId, onOpenTask }: TaskDiffPageProps) {
  const { selection, select } = useTaskDiffUrl();
  const searchUrl = useTaskSearchUrl({
    keep: TASK_DIFF_PARAMS,
    remember: false,
  });
  const { search, setSearch } = searchUrl;
  const left = useTask(selection.left, environmentId);
  const rightIsLeft = selection.right !== null && selection.right === selection.left;
  const rightRead = useTask(rightIsLeft ? null : selection.right, environmentId);
  const right = rightIsLeft ? left : rightRead;
  const { setItems: setBreadcrumb } = useBreadcrumb();

  useEffect(() => {
    setBreadcrumb([
      { label: "Tasks", onClick: () => onOpenTask("") },
      { label: "Compare" },
    ]);
    return () => setBreadcrumb([]);
  }, [onOpenTask, setBreadcrumb]);

  // A link that names the left task but no search (typed, or shared from
  // somewhere that dropped it) gets the same-type pre-filter, once.
  const prefilled = useRef<string | null>(null);
  const leftTask = left.task;
  useEffect(() => {
    if (!leftTask || prefilled.current === leftTask.task_id) return;
    prefilled.current = leftTask.task_id;
    if (isEmptySearch(search)) {
      setSearch(sameTypeSearch(leftTask.task_namespace, leftTask.task_name), {
        replace: true,
      });
    }
  }, [leftTask, search, setSearch]);

  const leftInstance = leftTask
    ? pickInstance(leftTask.instances, selection.leftInstance)
    : null;
  const rightTask = right.task;
  const rightInstance = rightTask
    ? rightIsLeft
      ? pickOtherInstance(
          rightTask.instances,
          selection.rightInstance,
          leftInstance?.id ?? null,
        )
      : pickInstance(rightTask.instances, selection.rightInstance)
    : null;

  const update = (patch: Partial<TaskDiffSelection>) =>
    select({ ...selection, ...patch });

  if (!selection.left) {
    return (
      <div className="flex h-full flex-col items-center justify-center p-8 text-center text-gray-500 dark:text-gray-400">
        <p className="text-lg font-medium">Nothing to compare yet</p>
        <p className="mt-1 max-w-md text-sm">
          Open a task and use &ldquo;Compare&rdquo; in its Task Parameters to pick it as
          the left side.
        </p>
        <button
          type="button"
          onClick={() => onOpenTask("")}
          className={`mt-3 ${LINK_BUTTON} text-sm`}
        >
          Search tasks
        </button>
      </div>
    );
  }

  const sameType = leftTask
    ? isSameTypeSearch(search, leftTask.task_namespace, leftTask.task_name)
    : true;
  const leftType = leftTask
    ? qualifiedName(leftTask.task_namespace, leftTask.task_name)
    : "";
  const otherInstance =
    leftTask && leftInstance
      ? leftTask.instances.find((i) => i.id !== leftInstance.id) ?? null
      : null;

  const notice = leftTask && (
    <p
      role="note"
      className={`text-xs ${
        sameType
          ? "text-gray-500 dark:text-gray-400"
          : "text-amber-700 dark:text-amber-400"
      }`}
    >
      {sameType ? (
        <>
          Showing <span className="font-medium">{leftType}</span> tasks only: a diff is
          most useful between tasks of one type. The filter can be removed, but a diff
          across types is mostly additions and removals.
        </>
      ) : (
        <>
          The search is no longer limited to{" "}
          <span className="font-medium">{leftType}</span>: a diff against another type
          is mostly additions and removals.{" "}
          <button
            type="button"
            onClick={() =>
              setSearch({
                ...search,
                namespace: leftTask.task_namespace,
                name: leftTask.task_name,
              })
            }
            className={LINK_BUTTON}
          >
            Limit to {leftType} again
          </button>
        </>
      )}
    </p>
  );

  return (
    <div className="flex h-full flex-col">
      <div className="grid grid-cols-1 gap-3 border-b border-gray-200 bg-white p-4 md:grid-cols-[1fr_auto_1fr] dark:border-gray-700 dark:bg-gray-800">
        <SideCard
          side="left"
          read={left}
          taskId={selection.left}
          instance={leftInstance}
          onInstance={(id) => update({ leftInstance: id })}
          onOpenTask={onOpenTask}
        />
        <div className="flex items-center justify-center">
          <Tooltip content="Swap left and right">
            <button
              type="button"
              aria-label="Swap left and right"
              disabled={!selection.right}
              onClick={() =>
                select({
                  left: selection.right,
                  leftInstance: rightInstance?.id ?? selection.rightInstance,
                  right: selection.left,
                  rightInstance: leftInstance?.id ?? selection.leftInstance,
                })
              }
              className="rounded-full p-1.5 text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-40 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
            >
              <svg
                aria-hidden="true"
                className="h-5 w-5"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M8 7h12m0 0l-4-4m4 4l-4 4M16 17H4m0 0l4 4m-4-4l4-4"
                />
              </svg>
            </button>
          </Tooltip>
        </div>
        {selection.right ? (
          <SideCard
            side="right"
            read={right}
            taskId={selection.right}
            instance={rightInstance}
            onInstance={(id) => update({ rightInstance: id })}
            onOpenTask={onOpenTask}
            onChange={() => update({ right: null, rightInstance: null })}
            avoidInstanceId={rightIsLeft ? leftInstance?.id ?? null : null}
          />
        ) : (
          <div className="flex flex-col justify-center rounded-lg border border-dashed border-gray-300 p-3 text-sm text-gray-500 dark:border-gray-600 dark:text-gray-400">
            <span className="text-xs font-medium tracking-wider uppercase">Right</span>
            <p>Pick a task to compare with from the search below.</p>
            {otherInstance && leftTask && (
              <button
                type="button"
                onClick={() =>
                  update({
                    right: leftTask.task_id,
                    rightInstance: otherInstance.id,
                  })
                }
                className={`mt-1 self-start ${LINK_BUTTON}`}
              >
                Or compare with another instance of this task
              </button>
            )}
          </div>
        )}
      </div>

      {left.error ? (
        <div className="p-4">
          <ResultBanner tone="error">
            Could not read the left task: {left.error}
          </ResultBanner>
        </div>
      ) : !selection.right ? (
        <div className="min-h-0 flex-1">
          <TaskSearchPanel
            url={searchUrl}
            environmentId={environmentId}
            onOpenTask={(taskId) => update({ right: taskId, rightInstance: null })}
            notice={notice}
            selectedTaskId={selection.left}
            openVerb="Compare with"
            rememberSearches={false}
          />
        </div>
      ) : right.error ? (
        <div className="p-4">
          <ResultBanner tone="error">
            Could not read the right task: {right.error}
          </ResultBanner>
        </div>
      ) : leftInstance && rightInstance ? (
        <div className="flex min-h-0 flex-1 flex-col p-4">
          {leftInstance.id === rightInstance.id && (
            <p role="note" className="mb-2 text-xs text-gray-500 dark:text-gray-400">
              Both sides are the same instance. Pick another instance on either side to
              see a difference.
            </p>
          )}
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
            <JsonDiffView
              key={`${leftInstance.id}\u0000${rightInstance.id}`}
              left={leftInstance.body}
              right={rightInstance.body}
              className="min-h-0 flex-1"
            />
          </div>
        </div>
      ) : leftTask && rightTask ? (
        <p className="p-4 text-sm text-gray-500 dark:text-gray-400">
          {leftInstance ? "The right task" : "The left task"} has no recorded instance
          to compare.
        </p>
      ) : (
        <p role="status" className="p-4 text-sm text-gray-500 dark:text-gray-400">
          Loading…
        </p>
      )}
    </div>
  );
}

function instanceLabel(instance: TaskInstance): string {
  return `${formatRelativeTime(instance.created_at)} · ${shortHash(
    instance.instance_hash,
  )}`;
}

function SideCard({
  side,
  read,
  taskId,
  instance,
  onInstance,
  onOpenTask,
  onChange,
  avoidInstanceId = null,
}: {
  side: "left" | "right";
  read: TaskRead;
  taskId: string;
  instance: TaskInstance | null;
  onInstance: (instanceId: string) => void;
  onOpenTask: (taskId: string) => void;
  onChange?: () => void;
  /** The other side's instance, marked in the picker when both sides are one task. */
  avoidInstanceId?: string | null;
}) {
  const { task } = read;
  const Side = side === "left" ? "Left" : "Right";
  const edge =
    side === "left"
      ? "border-l-4 border-l-red-400 dark:border-l-red-500"
      : "border-l-4 border-l-green-500 dark:border-l-green-500";
  return (
    <section
      aria-label={`${Side} side`}
      className={`min-w-0 rounded-lg border border-gray-200 p-3 dark:border-gray-700 ${edge}`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium tracking-wider text-gray-500 uppercase dark:text-gray-400">
          {Side}
        </span>
        {onChange && (
          <button type="button" onClick={onChange} className={BUTTON}>
            Change
          </button>
        )}
      </div>
      <div className="mt-1 flex min-w-0 flex-wrap items-center gap-1.5">
        <button
          type="button"
          onClick={() => onOpenTask(taskId)}
          className="truncate rounded text-left text-sm font-medium text-gray-900 hover:text-blue-700 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-gray-100 dark:hover:text-blue-400"
          title="Open task page"
        >
          {task
            ? qualifiedName(task.task_namespace, task.task_name)
            : read.error
              ? "Could not read task"
              : "Loading…"}
        </button>
        <CopyChip label={shortTaskId(taskId)} value={taskId} title="Task id" />
      </div>
      {task && task.instances.length > 1 && instance ? (
        <label className="mt-1.5 flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
          Instance
          <select
            aria-label={`${Side} instance`}
            value={instance.id}
            onChange={(e) => onInstance(e.target.value)}
            className="min-w-0 rounded-md border border-gray-300 px-1.5 py-0.5 font-mono text-xs text-gray-900 focus:border-blue-500 focus:outline-none dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100"
          >
            {task.instances.map((i) => (
              <option key={i.id} value={i.id}>
                {instanceLabel(i)}
                {i.id === avoidInstanceId ? " (left)" : ""}
              </option>
            ))}
          </select>
        </label>
      ) : instance ? (
        <p
          className="mt-1.5 text-xs text-gray-500 dark:text-gray-400"
          title={formatAbsoluteTime(instance.created_at)}
        >
          Instance <span className="font-mono">{instanceLabel(instance)}</span>
        </p>
      ) : null}
    </section>
  );
}
