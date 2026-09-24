import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { fetchTask, fetchTaskArtifacts, fetchTaskExecutions } from "../api/registry";
import { useDeployments } from "../hooks/useDeployments";
import type { Execution, PlanMember, Task, TaskArtifact } from "../types/task";
import { qualifiedName } from "../utils/instances";
import { formatAbsoluteTime } from "../utils/time";
import { ArtifactList } from "./ArtifactViewer";
import { MembershipFacts } from "./MembershipFacts";
import { CopyButton } from "./ModalExecution";
import { TaskClaimPanel } from "./TaskClaimPanel";
import { TaskEventLog } from "./TaskEventLog";
import { TaskExecutions } from "./TaskExecutions";
import { TaskInstances } from "./TaskInstances";

/** The build a task is opened from, when it is. */
export interface TaskBuildContext {
  buildId: string;
  // The build's active plan; the remedies go through it.
  planId: string | null;
  // The instance that plan holds for this task.
  planInstanceId: string | null;
  // The task's membership of that plan, when the plan lists it.
  member?: PlanMember | null;
}

interface TaskDetailProps {
  taskId: string;
  environmentId: string;
  context?: TaskBuildContext;
  onClose?: () => void;
  // Shown as a link icon next to the header when given.
  onOpenTaskPage?: () => void;
  // Called after a remedy changed the task, so the parent re-reads.
  onChanged?: () => void;
  // A refresh of the parent, which re-reads this task too.
  refreshToken?: number;
  // Jump to another build (the claim holder, an execution's build).
  onOpenBuild?: (buildId: string) => void;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <h3 className="mb-1 text-sm font-medium text-gray-500 dark:text-gray-400">
        {title}
      </h3>
      {children}
    </div>
  );
}

/**
 * One completion: its status and claim, the instances that realise it,
 * every execution of it across builds (`GET /tasks/{id}/executions`,
 * ended ones included), and its artifacts.
 *
 * Keyed by `task_id`. From a build it also marks that build's plan
 * membership and instance, and offers a reset through its active plan.
 */
export function TaskDetail({
  taskId,
  environmentId,
  context,
  onClose,
  onOpenTaskPage,
  onChanged,
  refreshToken = 0,
  onOpenBuild,
}: TaskDetailProps) {
  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [artifacts, setArtifacts] = useState<TaskArtifact[] | null>(null);
  const [executions, setExecutions] = useState<Execution[] | null>(null);
  const [nonce, setNonce] = useState(0);
  const { byId: deploymentsById } = useDeployments(environmentId);
  const epochRef = useRef(0);
  const buildId = context?.buildId;

  // Reset on identity change only — never on a same-task refresh
  // (refreshToken/nonce) — so a remedy can never be issued against the
  // previous task's now-stale state while the new task is still loading.
  // Adjusting state during render (rather than in an effect) is the
  // pattern React recommends for "reset state when a prop changes": it
  // avoids an extra commit with the stale task still in state.
  const identity = `${taskId}\u0000${environmentId}`;
  const [prevIdentity, setPrevIdentity] = useState(identity);
  if (identity !== prevIdentity) {
    setPrevIdentity(identity);
    setTask(null);
    setError(null);
    setArtifacts(null);
    setExecutions(null);
  }

  useEffect(() => {
    const epoch = ++epochRef.current;
    const fresh = () => epochRef.current === epoch;
    fetchTask(taskId, environmentId)
      .then((t) => {
        if (!fresh()) return;
        setTask(t);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!fresh()) return;
        setTask(null);
        setError(err instanceof Error ? err.message : "Failed to load task");
      });
    fetchTaskArtifacts(taskId, environmentId)
      .then((r) => fresh() && setArtifacts(r.artifacts))
      .catch(() => fresh() && setArtifacts([]));
    fetchTaskExecutions(taskId, environmentId)
      .then((rows) => fresh() && setExecutions(rows))
      .catch(() => fresh() && setExecutions([]));
  }, [taskId, environmentId, refreshToken, nonce]);

  const handleChanged = useCallback(() => {
    setNonce((n) => n + 1);
    onChanged?.();
  }, [onChanged]);

  const current = executions?.find((e) => e.id === task?.execution_id) ?? null;

  return (
    <div className="h-full overflow-auto bg-white p-4 dark:bg-gray-800">
      <div className="mb-4 flex items-start justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-1.5">
            <h2 className="truncate text-lg font-semibold text-gray-900 dark:text-gray-100">
              {task ? qualifiedName(task.task_namespace, task.task_name) : "Task"}
            </h2>
            {onOpenTaskPage && (
              <button
                type="button"
                onClick={onOpenTaskPage}
                aria-label="Open task page"
                title="Open task page"
                className="flex-shrink-0 rounded p-0.5 text-gray-400 hover:text-blue-600 dark:hover:text-blue-400"
              >
                <svg
                  aria-hidden="true"
                  className="h-4 w-4"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={2}
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"
                  />
                </svg>
              </button>
            )}
          </div>
          <div className="flex items-center gap-1">
            <p
              className="truncate font-mono text-sm text-gray-500 dark:text-gray-400"
              title={taskId}
            >
              {taskId}
            </p>
            <CopyButton text={taskId} />
          </div>
          {task?.version && (
            <p className="text-sm text-gray-500 dark:text-gray-400">
              Version {task.version}
            </p>
          )}
          {context?.member && (
            <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
              <span>In this plan:</span>
              <MembershipFacts member={context.member} variant="chips" />
            </div>
          )}
        </div>
        {onClose && (
          <button
            onClick={onClose}
            className="ml-2 text-gray-400 hover:text-gray-500 dark:hover:text-gray-300"
          >
            <span className="sr-only">Close</span>
            <svg
              className="h-6 w-6"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth="1.5"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        )}
      </div>

      {error ? (
        <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
      ) : !task ? (
        <p role="status" className="text-sm text-gray-500 dark:text-gray-400">
          Loading task…
        </p>
      ) : (
        <div className="space-y-4">
          <Section title="Status">
            <TaskClaimPanel
              task={task}
              environmentId={environmentId}
              buildId={context?.buildId}
              planId={context?.planId}
              currentExecution={current}
              onChanged={handleChanged}
              onOpenBuild={onOpenBuild}
            />
          </Section>

          {task.status === "failed" && task.error_message && (
            <Section title="Error">
              <pre className="overflow-auto rounded-md bg-red-50 p-3 text-sm text-red-700 dark:bg-red-900/20 dark:text-red-400">
                {task.error_message}
              </pre>
            </Section>
          )}

          <TaskEventLog
            taskId={task.task_id}
            taskLabel={qualifiedName(task.task_namespace, task.task_name)}
            environmentId={environmentId}
            onOpenBuild={onOpenBuild}
          />

          {task.output_uri && (
            <Section title="Output URI">
              <div className="flex items-center gap-1">
                <p
                  className="truncate font-mono text-sm text-gray-900 dark:text-gray-100"
                  title={task.output_uri}
                >
                  {task.output_uri}
                </p>
                <CopyButton text={task.output_uri} className="flex-shrink-0" />
              </div>
            </Section>
          )}

          {(task.started_at || task.completed_at) && (
            <div className="grid grid-cols-2 gap-2 text-sm text-gray-900 dark:text-gray-100">
              {task.started_at && (
                <Section title="Started">{formatAbsoluteTime(task.started_at)}</Section>
              )}
              {task.completed_at && (
                <Section title="Completed">
                  {formatAbsoluteTime(task.completed_at)}
                </Section>
              )}
            </div>
          )}

          <Section title={`Instances (${task.instances.length})`}>
            <TaskInstances
              instances={task.instances}
              deploymentsById={deploymentsById}
              planInstanceId={context?.planInstanceId}
            />
          </Section>

          <Section
            title={
              executions === null ? "Executions" : `Executions (${executions.length})`
            }
          >
            {executions === null ? (
              <p role="status" className="text-xs text-gray-500 dark:text-gray-400">
                Loading executions…
              </p>
            ) : (
              <TaskExecutions
                executions={executions}
                currentBuildId={buildId}
                currentExecutionId={task.execution_id}
                onOpenBuild={onOpenBuild}
              />
            )}
          </Section>

          {artifacts === null ? (
            <p className="text-sm text-gray-500 dark:text-gray-400">
              Loading artifacts…
            </p>
          ) : (
            <ArtifactList artifacts={artifacts} />
          )}
        </div>
      )}
    </div>
  );
}
