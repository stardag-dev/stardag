import { describe, expect, it } from "vitest";
import { viewFromPath } from "./routes";

describe("viewFromPath", () => {
  it("lands a direct load of the Concurrency page on it, in every scoped form", () => {
    expect(viewFromPath("/limits")).toBe("limits");
    expect(viewFromPath("/acme/limits")).toBe("limits");
    expect(viewFromPath("/acme/prod/limits")).toBe("limits");
  });

  it("routes the other environment-scoped views the same way", () => {
    expect(viewFromPath("/acme/prod/deployments")).toBe("deployments");
    expect(viewFromPath("/acme/prod/tasks")).toBe("tasks");
    expect(viewFromPath("/acme/prod/tasks/abc")).toBe("tasks");
    expect(viewFromPath("/acme/prod/builds/0199")).toBe("build");
    expect(viewFromPath("/acme/prod")).toBe("builds");
  });

  it("routes the task diff in every scoped form, apart from task pages", () => {
    expect(viewFromPath("/task-diff")).toBe("task-diff");
    expect(viewFromPath("/acme/task-diff")).toBe("task-diff");
    expect(viewFromPath("/acme/prod/task-diff")).toBe("task-diff");
    expect(viewFromPath("/acme/prod/tasks/task-diff")).toBe("tasks");
  });
});
