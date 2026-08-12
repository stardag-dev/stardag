import { describe, expect, it } from "vitest";
import type { BuildFrontier, FrontierTaskRef } from "../types/task";
import { rootsSatisfied, rootsSatisfiedFrom, schedulingPanelForm } from "./claims";

function ref(
  taskId: string,
  status: FrontierTaskRef["latest_status"],
): FrontierTaskRef {
  return { task_id: taskId, latest_status: status };
}

function frontier(overrides: Partial<BuildFrontier> = {}): BuildFrontier {
  return {
    build_id: "b-1",
    build_status: "running",
    needs_tick: false,
    root_task_ids: ["root-1"],
    roots: [ref("root-1", "completed")],
    status_counts: { completed: 3 },
    actionable: [],
    running: [],
    blocked_by_external: [],
    blocked_by_external_truncated: false,
    reactive_app_name: "some-app",
    ...overrides,
  };
}

describe("rootsSatisfied", () => {
  it("is true when every root is complete", () => {
    expect(rootsSatisfied(frontier())).toBe(true);
  });

  it("is false for a build with no roots", () => {
    // Not a corner case: `POST /builds` defaults `root_task_ids` to `[]`, so
    // every build is briefly rootless between being minted and having its roots
    // registered. `every()` over an empty list is vacuously true, so without an
    // explicit guard a build mid-creation — or an abandoned one that never got
    // roots — would report its request satisfied and be told so.
    expect(rootsSatisfied(frontier({ root_task_ids: [], roots: [] }))).toBe(false);
    expect(rootsSatisfiedFrom([], () => "completed")).toBe(false);
  });

  it("is false when a root cannot be resolved", () => {
    // The server reports `roots` by looking the ids up, so a shorter list means
    // a root is missing rather than complete. Unknown is not complete.
    expect(rootsSatisfied(frontier({ root_task_ids: ["root-1", "root-2"] }))).toBe(
      false,
    );
    expect(rootsSatisfiedFrom(["root-1"], () => undefined)).toBe(false);
  });

  it("is false when any root is still incomplete", () => {
    expect(
      rootsSatisfied(
        frontier({
          root_task_ids: ["root-1", "root-2"],
          roots: [ref("root-1", "completed"), ref("root-2", "suspended")],
        }),
      ),
    ).toBe(false);
  });
});

describe("schedulingPanelForm", () => {
  // The build that started the whole effort: it failed waiting on a shared task,
  // another build completed that task later, and the panel went on insisting
  // "nothing is going to happen without intervention" over finished work.
  const supersededProd = frontier({
    build_status: "failed",
    status_counts: { completed: 3 },
    root_task_ids: ["d61cba54"],
    roots: [ref("d61cba54", "completed")],
    reactive_app_name: "mmt-train-dev-3261",
  });

  it("does not call a failed build stalled once its roots have completed", () => {
    expect(schedulingPanelForm(supersededProd, "failed")).toBe("satisfied");
  });

  it("does not call a running build stalled once its roots have completed", () => {
    // Same shape, reachable because a cross-build completion does not wake the
    // waiting build: only its own worker notifies it, so without a watchdog it
    // sits RUNNING with its work done.
    expect(
      schedulingPanelForm({ ...supersededProd, build_status: "running" }, "running"),
    ).toBe("satisfied");
  });

  it("still flags a genuinely stuck build that has no roots", () => {
    // An abandoned build whose only task was cancelled, with no roots recorded.
    // Nothing will advance it, so "needs intervention" is the correct message —
    // and the roots guard is what keeps it.
    expect(
      schedulingPanelForm(
        frontier({
          build_status: "running",
          root_task_ids: [],
          roots: [],
          status_counts: { cancelled: 1 },
          reactive_app_name: null,
        }),
        "running",
      ),
    ).toBe("stalled");
  });

  it("leaves a cancelled build alone", () => {
    // Stopped on purpose. Announcing that its roots finished anyway is noise,
    // and the form tells the reader to re-trigger to reconcile the record —
    // which contradicts the intent of cancelling. Before the satisfied form
    // existed a cancelled build got the quiet strip; it still does.
    expect(schedulingPanelForm(supersededProd, "cancelled")).toBe("collapsed");
  });

  it("leaves a completed build alone", () => {
    // Already done; the panel has nothing to add, and a green "work is done"
    // banner on a completed build is noise.
    expect(schedulingPanelForm(supersededProd, "completed")).toBe("collapsed");
  });

  it("still flags a failed build whose roots are not complete", () => {
    expect(
      schedulingPanelForm(
        frontier({
          build_status: "failed",
          roots: [ref("root-1", "suspended")],
          status_counts: { completed: 2, suspended: 1 },
        }),
        "failed",
      ),
    ).toBe("stalled");
  });
});
