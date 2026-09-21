import { describe, expect, it, vi } from "vitest";
import type { Task, TaskStatus } from "../types/task";
import {
  CLAIM_PAGE_SIZE,
  MAX_CLAIM_PAGES,
  collectExecutions,
  executionFromTask,
  executionsForBuild,
  executorsIn,
  matchesFilters,
  stopCommand,
  workersIn,
} from "./stoppable";

const BUILD = "11111111-1111-1111-1111-111111111111";
const OTHER_BUILD = "22222222-2222-2222-2222-222222222222";

const MINUTE = 60 * 1000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "row-1",
    task_id: "tid-grind-beans",
    environment_id: "env-1",
    task_namespace: "acme.features",
    task_name: "Featurise",
    task_data: {},
    version: null,
    output_uri: null,
    created_at: ago(90 * MINUTE),
    status: "running",
    started_at: ago(60 * MINUTE),
    completed_at: null,
    error_message: null,
    artifact_count: 0,
    latest_status: "running",
    latest_status_at: ago(20 * MINUTE),
    latest_status_build_id: BUILD,
    latest_executor: "modal",
    latest_executor_ref: "fc-abc123",
    latest_executor_metadata: {
      kind: "modal",
      workspace: "acme",
      app_name: "pipeline",
      function_name: "worker_gpu",
    },
    ...overrides,
  };
}

describe("executionFromTask", () => {
  it.each<TaskStatus>(["running", "interrupted"])(
    "selects %s, whose row may still name a live container",
    (status) => {
      expect(executionFromTask(makeTask({ latest_status: status }), BUILD)).not.toBe(
        null,
      );
    },
  );

  it.each<TaskStatus>(["suspended", "pending", "completed", "failed", "cancelled"])(
    "does not select %s",
    (status) => {
      // SUSPENDED is the interesting one: it keeps its executor ref, but
      // that execution yielded and returned, so there is nothing to stop.
      expect(executionFromTask(makeTask({ latest_status: status }), BUILD)).toBe(null);
    },
  );

  it("does not select a row whose status another build produced", () => {
    // The check that makes the list safe as well as exact: acting on one of
    // these would kill somebody else's worker.
    const task = makeTask({ latest_status_build_id: OTHER_BUILD });
    expect(executionFromTask(task, BUILD)).toBe(null);
  });

  it("does not select a row with no executor ref", () => {
    expect(executionFromTask(makeTask({ latest_executor_ref: null }), BUILD)).toBe(
      null,
    );
  });

  it("treats a ref with no executor named as Modal", () => {
    // Data from before `latest_executor` existed. Modal is the only
    // executor that has ever recorded a ref, and dropping the row would
    // hide a live container from a list that is meant to be exact.
    const execution = executionFromTask(makeTask({ latest_executor: null }), BUILD);
    expect(execution?.executor).toBe("modal");
    expect(execution?.stoppable).toBe(true);
  });

  it("lists a non-Modal execution but does not call it stoppable", () => {
    const execution = executionFromTask(
      makeTask({ latest_executor: "prefect" }),
      BUILD,
    );
    expect(execution?.stoppable).toBe(false);
  });

  it("strips Modal's worker_ prefix from the worker name", () => {
    expect(executionFromTask(makeTask(), BUILD)?.worker).toBe("gpu");
  });

  it("qualifies the task name with its namespace", () => {
    expect(executionFromTask(makeTask(), BUILD)?.qualifiedName).toBe(
      "acme.features.Featurise",
    );
    expect(
      executionFromTask(makeTask({ task_namespace: "" }), BUILD)?.qualifiedName,
    ).toBe("Featurise");
  });

  it("reports a restart as due only while the preemption is outstanding", () => {
    // The restart records its own start, which moves latest_status_at past
    // the preemption — so this goes false with nothing to clear.
    const outstanding = makeTask({
      latest_status_at: ago(20 * MINUTE),
      latest_preempted_at: ago(2 * MINUTE),
    });
    const landed = makeTask({
      latest_status_at: ago(1 * MINUTE),
      latest_preempted_at: ago(2 * MINUTE),
    });
    expect(executionFromTask(outstanding, BUILD)?.restartDue).toBe(true);
    expect(executionFromTask(landed, BUILD)?.restartDue).toBe(false);
    expect(executionFromTask(makeTask(), BUILD)?.restartDue).toBe(false);
  });
});

describe("executionsForBuild", () => {
  it("keeps only this build's live executions", () => {
    const rows = [
      makeTask({ task_id: "mine" }),
      makeTask({ task_id: "theirs", latest_status_build_id: OTHER_BUILD }),
      makeTask({ task_id: "done", latest_status: "completed" }),
    ];
    expect(executionsForBuild(rows, BUILD).map((e) => e.taskId)).toEqual(["mine"]);
  });
});

describe("matchesFilters", () => {
  const execution = executionFromTask(makeTask(), BUILD)!;

  it("matches everything when nothing is set", () => {
    expect(matchesFilters(execution, {})).toBe(true);
  });

  it("filters by worker", () => {
    expect(matchesFilters(execution, { worker: "gpu" })).toBe(true);
    expect(matchesFilters(execution, { worker: "cpu" })).toBe(false);
  });

  it("filters by executor", () => {
    expect(matchesFilters(execution, { executor: "modal" })).toBe(true);
    expect(matchesFilters(execution, { executor: "prefect" })).toBe(false);
  });

  it("treats namespace as a prefix", () => {
    expect(matchesFilters(execution, { namespace: "acme" })).toBe(true);
    expect(matchesFilters(execution, { namespace: "acme.features" })).toBe(true);
    expect(matchesFilters(execution, { namespace: "acme.labels" })).toBe(false);
  });

  it("filters by how long the execution has been in this status", () => {
    expect(matchesFilters(execution, { olderThanSeconds: 10 * 60 })).toBe(true);
    expect(matchesFilters(execution, { olderThanSeconds: 60 * 60 })).toBe(false);
  });

  it("never matches an undatable row against a staleness filter", () => {
    // An age that cannot be established is not evidence of age — the same
    // rule the server applies to `status_older_than`.
    const undatable = executionFromTask(makeTask({ latest_status_at: null }), BUILD)!;
    expect(matchesFilters(undatable, { olderThanSeconds: 60 })).toBe(false);
  });

  it("is conjunctive", () => {
    expect(matchesFilters(execution, { worker: "gpu", namespace: "other" })).toBe(
      false,
    );
  });
});

describe("workersIn", () => {
  it("lists the distinct worker names, sorted", () => {
    const rows = [
      makeTask({ latest_executor_metadata: { function_name: "worker_gpu" } }),
      makeTask({ latest_executor_metadata: { function_name: "worker_cpu" } }),
      makeTask({ latest_executor_metadata: { function_name: "worker_gpu" } }),
      makeTask({ latest_executor_metadata: null }),
    ];
    expect(workersIn(executionsForBuild(rows, BUILD))).toEqual(["cpu", "gpu"]);
  });
});

describe("collectExecutions", () => {
  const page = (tasks: Task[], total: number) => ({ tasks, total });

  it("stops once the server's total is accounted for", async () => {
    const fetchPage = vi.fn(async () => page([makeTask()], 1));
    const result = await collectExecutions(fetchPage, BUILD);
    expect(fetchPage).toHaveBeenCalledTimes(1);
    expect(result.executions).toHaveLength(1);
    expect(result.truncated).toBe(false);
  });

  it("keeps paging while the total says there is more", async () => {
    // The whole reason this exists: a build's executions can sit entirely
    // on a later page, and a single-page read would report none — which
    // the panel renders as "nothing running".
    const others = Array.from({ length: CLAIM_PAGE_SIZE }, (_, i) =>
      makeTask({ task_id: `other-${i}`, latest_status_build_id: OTHER_BUILD }),
    );
    const fetchPage = vi
      .fn()
      .mockResolvedValueOnce(page(others, CLAIM_PAGE_SIZE + 1))
      .mockResolvedValueOnce(
        page([makeTask({ task_id: "mine" })], CLAIM_PAGE_SIZE + 1),
      );

    const result = await collectExecutions(fetchPage, BUILD);

    expect(fetchPage.mock.calls.map((c) => c[0])).toEqual([1, 2]);
    expect(result.executions.map((e) => e.taskId)).toEqual(["mine"]);
    expect(result.truncated).toBe(false);
  });

  it("gives up after the cap and says it did", async () => {
    // Reported, never silent: "found none" and "stopped looking" have to
    // be different answers, because only one of them is safe to act on.
    const full = Array.from({ length: CLAIM_PAGE_SIZE }, (_, i) =>
      makeTask({ task_id: `other-${i}`, latest_status_build_id: OTHER_BUILD }),
    );
    const fetchPage = vi.fn(async () => page(full, 10_000_000));

    const result = await collectExecutions(fetchPage, BUILD);

    expect(fetchPage).toHaveBeenCalledTimes(MAX_CLAIM_PAGES);
    expect(result.truncated).toBe(true);
    expect(result.executions).toHaveLength(0);
  });

  it("stops on a short page even if the total disagrees", async () => {
    // A total that overcounts (rows finishing under the scan) must not
    // turn into an endless walk of empty pages.
    const fetchPage = vi.fn(async () => page([], 500));
    const result = await collectExecutions(fetchPage, BUILD);
    expect(fetchPage).toHaveBeenCalledTimes(1);
    expect(result.truncated).toBe(false);
  });
});

describe("executorsIn", () => {
  it("lists the distinct executors, sorted", () => {
    const rows = [
      makeTask(),
      makeTask({ latest_executor: "prefect" }),
      makeTask({ latest_executor: null }),
    ];
    expect(executorsIn(executionsForBuild(rows, BUILD))).toEqual(["modal", "prefect"]);
  });
});

describe("stopCommand", () => {
  it("is the bare command when nothing is filtered", () => {
    expect(stopCommand(BUILD, {})).toBe(`stardag builds stop ${BUILD}`);
  });

  it("carries every filter the panel applied", () => {
    // The panel's actual output: the command has to act on exactly the
    // list the operator just read, or showing them both is worse than
    // showing neither.
    expect(
      stopCommand(BUILD, {
        worker: "gpu",
        executor: "modal",
        namespace: "acme",
        olderThanSeconds: 1800,
      }),
    ).toBe(
      `stardag builds stop ${BUILD} --worker gpu --executor modal ` +
        `--namespace acme --older-than 30m`,
    );
  });

  it("names exact task ids instead of the narrowing flags", () => {
    // The CLI's filters are conjunctive and --task-id is exact, so a list
    // of ids is the whole selection; restating the others would only
    // invite the two to drift apart.
    expect(stopCommand(BUILD, { worker: "gpu", taskIds: ["one", "two"] })).toBe(
      `stardag builds stop ${BUILD} --task-id one --task-id two`,
    );
  });

  it("refuses an empty task-id list rather than widening", () => {
    // Loud rather than quiet: emitting no `--task-id` flags would produce
    // a command with no filters at all, which stops every execution the
    // build holds. There is no command string meaning "stop nothing".
    expect(() => stopCommand(BUILD, { taskIds: [] })).toThrow(/stop nothing/);
  });

  it("renders durations the way --older-than takes them", () => {
    expect(stopCommand(BUILD, { olderThanSeconds: 300 })).toContain("--older-than 5m");
    expect(stopCommand(BUILD, { olderThanSeconds: 7200 })).toContain("--older-than 2h");
    expect(stopCommand(BUILD, { olderThanSeconds: 86400 })).toContain(
      "--older-than 1d",
    );
  });
});
