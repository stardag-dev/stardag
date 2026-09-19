import { describe, expect, it } from "vitest";

import { reactFlowEdgeId } from "./graphEdges";

describe("reactFlowEdgeId", () => {
  it("gives one source/target pair a distinct edge per scope", () => {
    const own = reactFlowEdgeId({
      source: 1,
      target: 2,
      scope_key: "cafe:0123456789abcdef",
    });
    const provenance = reactFlowEdgeId({
      source: 1,
      target: 2,
      scope_key: "beef:0123456789abcdef",
    });
    expect(own).not.toBe(provenance);
    expect(new Set([own, provenance]).size).toBe(2);
  });

  it("names a pre-scope edge legacy", () => {
    expect(reactFlowEdgeId({ source: 1, target: 2, scope_key: null })).toBe(
      "1-2-legacy",
    );
    expect(reactFlowEdgeId({ source: 1, target: 2 })).toBe("1-2-legacy");
  });

  it("is stable for the same edge", () => {
    const edge = {
      source: "a",
      target: "b",
      scope_key: "cafe:0123456789abcdef",
    };
    expect(reactFlowEdgeId(edge)).toBe(reactFlowEdgeId({ ...edge }));
  });
});
