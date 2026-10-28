import { beforeEach, describe, expect, it } from "vitest";
import type { TaskSummary } from "../types/task";
import { EMPTY_SEARCH } from "./taskSearch";
import {
  harvest,
  harvestInto,
  loadStore,
  remember,
  rememberSearch,
  suggest,
  type SuggestionStore,
} from "./taskSuggestions";

function task(namespace: string, name: string, tags: string[] = []): TaskSummary {
  return {
    task_id: `${namespace}-${name}`,
    task_namespace: namespace,
    task_name: name,
    version: null,
    output_uri: null,
    status: "completed",
    status_at: null,
    started_at: null,
    completed_at: null,
    error_message: null,
    claim_expires_at: null,
    claim_plan_id: null,
    claim_build_id: null,
    execution_id: null,
    tags,
    description: null,
  };
}

const empty: SuggestionStore = { names: [], tags: [], recent: [] };

beforeEach(() => localStorage.clear());

describe("suggestions", () => {
  it("harvests names and tags from fetched tasks, without duplicates", () => {
    const store = harvest(empty, [
      task("examples.ml", "TrainModel", ["prod"]),
      task("examples.ml", "TrainModel", ["baseline"]),
      task("", "Evaluate"),
    ]);
    expect(store.names).toEqual([
      { namespace: "examples.ml", name: "TrainModel" },
      { namespace: "", name: "Evaluate" },
    ]);
    expect(store.tags).toEqual(["baseline", "prod"]);
  });

  it("suggests names, namespaces and tags, prefix matches first", () => {
    const store = harvest(empty, [
      task("examples.ml", "TrainModel", ["training"]),
      task("", "PreTrain"),
    ]);
    const labels = suggest(store, "train", EMPTY_SEARCH).map(
      (s) => `${s.kind}:${s.label}`,
    );
    expect(labels).toEqual([
      "name:examples.ml.TrainModel",
      "tag:training",
      "name:PreTrain",
    ]);
    expect(suggest(store, "exam", EMPTY_SEARCH).map((s) => s.kind)).toContain(
      "namespace",
    );
  });

  it("restricts to a kind before capping", () => {
    const store = harvest(
      empty,
      Array.from({ length: 12 }, (_, i) => task("", `Prod${i}`)).concat([
        task("", "Other", ["production"]),
      ]),
    );
    expect(suggest(store, "prod", EMPTY_SEARCH).some((s) => s.kind === "tag")).toBe(
      false,
    );
    expect(suggest(store, "prod", EMPTY_SEARCH, ["tag"])).toEqual([
      { kind: "tag", tag: "production", label: "production" },
    ]);
  });

  it("skips what the search already filters on", () => {
    const store = harvest(empty, [task("", "A", ["prod"])]);
    expect(suggest(store, "prod", { ...EMPTY_SEARCH, tags: ["prod"] })).toEqual([]);
  });

  it("offers recent searches for an empty box, most recent first", () => {
    let store = remember(empty, { ...EMPTY_SEARCH, text: "first" });
    store = remember(store, { ...EMPTY_SEARCH, tags: ["prod"] });
    store = remember(store, { ...EMPTY_SEARCH, text: "first" });
    const recent = suggest(store, "", EMPTY_SEARCH);
    expect(recent.map((s) => s.label)).toEqual(["“first”", "#prod"]);
  });

  it("persists per environment", () => {
    harvestInto("env-1", [task("", "A", ["x"])]);
    rememberSearch("env-1", { ...EMPTY_SEARCH, text: "a" });
    expect(loadStore("env-1").tags).toEqual(["x"]);
    expect(loadStore("env-1").recent).toEqual(["?q=a"]);
    expect(loadStore("env-2")).toEqual(empty);
  });

  it("survives corrupt storage", () => {
    localStorage.setItem("stardag.taskSearch.v1.env-1", "{not json");
    expect(loadStore("env-1")).toEqual(empty);
  });
});
