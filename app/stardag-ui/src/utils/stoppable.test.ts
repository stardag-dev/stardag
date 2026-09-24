import { describe, expect, it } from "vitest";
import type { Execution } from "../types/task";
import {
  executorOf,
  executorsIn,
  matchesFilters,
  NO_EXECUTOR,
  NO_REF_YET,
  notStoppableReason,
  stopCommand,
  stopCommandEffect,
  workerOf,
  workersIn,
} from "./stoppable";

const BUILD = "01a0c5c3-f18e-7d22-bcaf-add71bd0287c";
const NOW = Date.parse("2026-09-24T12:00:00Z");

function execution(overrides: Partial<Execution> = {}): Execution {
  return {
    id: "e-1",
    task_id: "t-1",
    build_id: BUILD,
    plan_id: "p-1",
    instance_id: "i-1",
    executor: "modal",
    executor_ref: "fc-01",
    executor_metadata: { kind: "modal", function_name: "worker_gpu" },
    started_at: "2026-09-24T11:00:00Z",
    claim_released_at: null,
    claim_outcome: null,
    ended_at: null,
    outcome: null,
    in_current_plan: true,
    ...overrides,
  };
}

describe("notStoppableReason", () => {
  it("is null for a Modal execution with a call id", () => {
    expect(notStoppableReason(execution())).toBeNull();
  });

  it("lists a claimed-but-not-spawned execution rather than dropping it", () => {
    expect(notStoppableReason(execution({ executor_ref: null }))).toBe(NO_REF_YET);
  });

  it("names the ambiguity of an execution with no executor", () => {
    expect(
      notStoppableReason(
        execution({ executor: null, executor_ref: null, executor_metadata: null }),
      ),
    ).toBe(NO_EXECUTOR);
  });

  it("refuses other executors permanently", () => {
    expect(notStoppableReason(execution({ executor: "local" }))).toMatch(/'local'/);
  });
});

describe("workerOf", () => {
  it("strips Modal's worker_ prefix", () => {
    expect(workerOf(execution())).toBe("gpu");
    expect(workersIn([execution(), execution({ id: "e-2" })])).toEqual(["gpu"]);
  });
});

describe("matchesFilters", () => {
  it("keeps only orphans under notInCurrentPlan", () => {
    expect(matchesFilters(execution(), { notInCurrentPlan: true })).toBe(false);
    expect(
      matchesFilters(execution({ in_current_plan: false }), { notInCurrentPlan: true }),
    ).toBe(true);
  });

  it("measures age from the execution's start", () => {
    expect(matchesFilters(execution(), { olderThanSeconds: 1800 }, NOW)).toBe(true);
    expect(matchesFilters(execution(), { olderThanSeconds: 7200 }, NOW)).toBe(false);
  });
});

describe("stopCommand", () => {
  it("carries the orphan flag and the narrowing flags", () => {
    expect(
      stopCommand(BUILD, {
        notInCurrentPlan: true,
        worker: "gpu",
        olderThanSeconds: 7200,
      }),
    ).toBe(
      `stardag builds stop ${BUILD} --not-in-current-plan --worker gpu --older-than 2h`,
    );
  });

  it("keeps the active filters alongside ticked tasks (conjunctive, as the CLI)", () => {
    expect(stopCommand(BUILD, { taskIds: ["t-1", "t-2"], worker: "gpu" })).toBe(
      `stardag builds stop ${BUILD} --task-id t-1 --task-id t-2 --worker gpu`,
    );
  });

  it("refuses an empty selection rather than widening it", () => {
    expect(() => stopCommand(BUILD, { taskIds: [] })).toThrow(/stop nothing/);
  });
});

describe("stopCommandEffect", () => {
  it("says the default command cancels the build", () => {
    expect(stopCommandEffect({})).toMatch(/then cancels the build/);
    // Narrowing does not spare the build: only the orphan flag does.
    expect(stopCommandEffect({ taskIds: ["t-1"], worker: "gpu" })).toMatch(
      /then cancels the build/,
    );
  });

  it("says --not-in-current-plan leaves the build running", () => {
    const effect = stopCommandEffect({ notInCurrentPlan: true });
    expect(effect).not.toMatch(/then cancels the build/);
    expect(effect).toMatch(/does not cancel the build/);
    expect(effect).toMatch(/keeps running on its active plan/);
  });
});

describe("executorOf (mirrors _stop.executor_of)", () => {
  // Claimed before the spawn reported: no `executor`, only the kind the
  // metadata declares. The CLI selects it under --executor modal.
  const metadataOnly = execution({
    executor: null,
    executor_ref: null,
    executor_metadata: { kind: "modal", function_name: "worker_gpu" },
  });

  it("falls back to the metadata kind", () => {
    expect(executorOf(metadataOnly)).toBe("modal");
    expect(executorOf(execution({ executor: null, executor_metadata: null }))).toBe(
      null,
    );
  });

  it("keeps a metadata-only row under the executor filter, as the CLI does", () => {
    expect(matchesFilters(metadataOnly, { executor: "modal" })).toBe(true);
    expect(matchesFilters(metadataOnly, { executor: "local" })).toBe(false);
  });

  it("offers the metadata kind as an executor choice", () => {
    expect(
      executorsIn([metadataOnly, execution({ id: "e-2", executor: "local" })]),
    ).toEqual(["local", "modal"]);
  });

  it("reads a metadata-only row as not spawned yet, not as unattributed", () => {
    expect(notStoppableReason(metadataOnly)).toBe(NO_REF_YET);
  });
});
