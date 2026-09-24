import { describe, expect, it } from "vitest";
import { eventTypeStyle, formatEventType } from "./events";

describe("event types", () => {
  it("formats as v1 did", () => {
    expect(formatEventType("task_structure_diverged")).toBe("Structure Diverged");
    expect(formatEventType("build_started")).toBe("Started");
  });

  it("colours a divergence as a failure and an observation as a completion", () => {
    expect(eventTypeStyle("task_structure_diverged")).toContain("red");
    expect(eventTypeStyle("task_observed_complete")).toContain("green");
    expect(eventTypeStyle("task_preempted")).toContain("orange");
  });
});
