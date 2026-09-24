import { describe, expect, it } from "vitest";
import type { Deployment } from "../types/task";
import { deploymentLabel, groupByApp } from "./deployments";

function deployment(overrides: Partial<Deployment>): Deployment {
  return {
    id: "d",
    kind: "modal",
    app_name: "app",
    code_id: "c",
    image_id: null,
    modal_app_id: null,
    generation: 1,
    deployed_at: "2026-09-24T00:00:00Z",
    activated_at: "2026-09-24T00:00:01Z",
    is_current: false,
    ...overrides,
  };
}

describe("groupByApp", () => {
  it("groups per kind and app, newest generation first, current marked", () => {
    const groups = groupByApp([
      deployment({ id: "a1", app_name: "etl", generation: 1 }),
      deployment({ id: "a3", app_name: "etl", generation: 3, is_current: true }),
      deployment({ id: "a2", app_name: "etl", generation: 2, activated_at: null }),
      deployment({ id: "l1", kind: "local", app_name: "etl" }),
      deployment({ id: "b1", app_name: "backfill", is_current: true }),
    ]);
    expect(groups.map((g) => [g.appName, g.kind, g.current?.id])).toEqual([
      ["backfill", "modal", "b1"],
      ["etl", "local", undefined],
      ["etl", "modal", "a3"],
    ]);
    expect(groups[2].generations.map((d) => d.id)).toEqual(["a3", "a2", "a1"]);
  });
});

describe("deploymentLabel", () => {
  it("names the app and generation", () => {
    expect(deploymentLabel(deployment({ app_name: "etl", generation: 4 }))).toBe(
      "etl gen 4",
    );
  });
});
