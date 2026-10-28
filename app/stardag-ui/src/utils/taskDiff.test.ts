import { describe, expect, it } from "vitest";
import { withKeptParams } from "../hooks/useTaskSearchUrl";
import type { TaskInstance } from "../types/task";
import {
  compareQuery,
  isSameTypeSearch,
  NO_SELECTION,
  parseTaskDiff,
  pickInstance,
  pickOtherInstance,
  sameTypeSearch,
  TASK_DIFF_PARAMS,
  taskDiffQuery,
  withSelection,
} from "./taskDiff";
import { EMPTY_SEARCH, parseSearch } from "./taskSearch";

function instance(id: string): TaskInstance {
  return {
    id,
    deployment_id: "dep-1",
    settings_hash: "s",
    instance_hash: `h-${id}`,
    body: {},
    expanded_at: null,
    created_at: "2026-10-01T00:00:00Z",
  };
}

describe("task diff URL", () => {
  it("round-trips a selection next to the picker's search", () => {
    const query = taskDiffQuery(
      { left: "t-1", leftInstance: "i-1", right: "t-2", rightInstance: null },
      sameTypeSearch("examples.ml", "Metrics"),
    );
    expect(query).toBe(
      "?left=t-1&leftInstance=i-1&right=t-2&namespace=examples.ml&name=Metrics",
    );
    expect(parseTaskDiff(query)).toEqual({
      left: "t-1",
      leftInstance: "i-1",
      right: "t-2",
      rightInstance: null,
    });
    expect(parseSearch(query)).toMatchObject({
      namespace: "examples.ml",
      name: "Metrics",
    });
  });

  it("writes a selection without touching the search, and the search without the selection", () => {
    const current = "?left=a&namespace=&q=abc&tag=x&tag=y";
    expect(withSelection(current, { ...NO_SELECTION, left: "a", right: "b" })).toBe(
      "?left=a&right=b&namespace=&q=abc&tag=x&tag=y",
    );
    expect(withKeptParams(current, "?status=failed", TASK_DIFF_PARAMS)).toBe(
      "?left=a&status=failed",
    );
    expect(withKeptParams("", "", TASK_DIFF_PARAMS)).toBe("");
  });

  it("opens the compare action on the left task, pre-filtered to its type", () => {
    const query = compareQuery({
      taskId: "t-1",
      instanceId: "i-1",
      namespace: "",
      name: "Train",
    });
    expect(parseTaskDiff(query)).toMatchObject({ left: "t-1", leftInstance: "i-1" });
    // The root namespace is a filter too.
    expect(parseSearch(query)).toMatchObject({ namespace: "", name: "Train" });
    expect(isSameTypeSearch(parseSearch(query), "", "Train")).toBe(true);
    expect(isSameTypeSearch(EMPTY_SEARCH, "", "Train")).toBe(false);
  });

  it("ignores blank selection parameters", () => {
    expect(parseTaskDiff("?left=&right=%20")).toEqual(NO_SELECTION);
  });
});

describe("instance picking", () => {
  const instances = [instance("new"), instance("mid"), instance("old")];

  it("shows the asked-for instance, else the newest", () => {
    expect(pickInstance(instances, "mid")?.id).toBe("mid");
    expect(pickInstance(instances, null)?.id).toBe("new");
    expect(pickInstance(instances, "gone")?.id).toBe("new");
    expect(pickInstance([], null)).toBeNull();
  });

  it("defaults the other side of one task to a different instance", () => {
    expect(pickOtherInstance(instances, null, "new")?.id).toBe("mid");
    expect(pickOtherInstance(instances, "new", "new")?.id).toBe("new");
    expect(pickOtherInstance([instance("only")], null, "only")?.id).toBe("only");
  });
});
