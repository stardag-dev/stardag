import { describe, expect, it } from "vitest";
import type { BuildFrontier, FrontierItem } from "../types/task";
import { schedulingState } from "./scheduling";

const RUNNING: FrontierItem = {
  task_id: "t",
  instance_id: "i",
  instance_hash: "h",
  status: "running",
  is_root: true,
  body: {},
  attempts: 1,
  interruptions: 0,
};

function frontier(overrides: Partial<BuildFrontier> = {}): BuildFrontier {
  return {
    build_id: "b",
    plan_id: "p",
    deployment_id: "d",
    settings_hash: "s",
    sealed: true,
    plan_complete: false,
    build_status: "running",
    reactive_app_name: "app",
    reactive_tick_kwargs: null,
    runnable: [],
    discovery_jobs: [],
    running: [],
    closure: null,
    ...overrides,
  };
}

describe("schedulingState", () => {
  it("is unknown before the frontier is read", () => {
    expect(schedulingState(null, "running")).toBe("unknown");
  });

  it("is complete when the plan is, whatever is left in the lists", () => {
    expect(schedulingState(frontier({ plan_complete: true }), "running")).toBe(
      "complete",
    );
  });

  it("is stalled for a running build with nothing to do", () => {
    expect(schedulingState(frontier(), "running")).toBe("stalled");
  });

  it("is not stalled once the build is terminal", () => {
    expect(schedulingState(frontier(), "failed")).toBe("settled");
  });

  it("is progressing with anything running", () => {
    expect(schedulingState(frontier({ running: [RUNNING] }), "running")).toBe(
      "progressing",
    );
  });
});
