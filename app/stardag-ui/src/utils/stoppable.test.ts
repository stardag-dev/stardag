import { describe, expect, it } from "vitest";
import type { Execution } from "../types/task";
import {
  matchesFilters,
  NO_EXECUTOR,
  NO_REF_YET,
  notStoppableReason,
  stopCommand,
  workerOf,
  workersIn,
} from "./stoppable";

const BUILD = "01a0c5c3-f18e-7d22-bcaf-add71bd0287c";
const NOW = Date.parse("2026-09-24T12:00:00Z");

function execution(overrides: Partial<Execution> = {}): Execution {
  return {
    id: "e-1",
    task_id: "t-1",
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
      stopCommand(BUILD, { notInCurrentPlan: true, worker: "gpu", olderThanSeconds: 7200 }),
    ).toBe(`stardag builds stop ${BUILD} --not-in-current-plan --worker gpu --older-than 2h`);
  });

  it("names ticked tasks exactly and nothing else", () => {
    expect(stopCommand(BUILD, { taskIds: ["t-1", "t-2"], worker: "gpu" })).toBe(
      `stardag builds stop ${BUILD} --task-id t-1 --task-id t-2`,
    );
  });

  it("refuses an empty selection rather than widening it", () => {
    expect(() => stopCommand(BUILD, { taskIds: [] })).toThrow(/stop nothing/);
  });
});
