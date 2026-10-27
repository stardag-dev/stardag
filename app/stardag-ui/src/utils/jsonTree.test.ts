import { describe, expect, it } from "vitest";
import {
  allContainerIds,
  ancestorIds,
  copyValueText,
  defaultExpanded,
  findMatches,
  highlightSegments,
  indexJson,
  pathSegment,
  sizeLabel,
  union,
  visibleRows,
} from "./jsonTree";

const BODY = {
  epochs: 3,
  name: "Alpha",
  "odd key": null,
  flags: [true, false],
  model: {
    __namespace: "demo",
    __name: "Train",
    layers: [{ units: 64 }, { units: 32 }],
    note: "alpha release",
  },
  empty: {},
};

function ids(nodes: ReturnType<typeof indexJson>, rows: number[]): string[] {
  return rows.map((i) => nodes[i].id);
}

describe("indexJson", () => {
  const nodes = indexJson(BODY);

  it("indexes in preorder with JSON paths as ids", () => {
    expect(nodes.map((n) => n.id)).toEqual([
      "$",
      "$.epochs",
      "$.name",
      '$["odd key"]',
      "$.flags",
      "$.flags[0]",
      "$.flags[1]",
      "$.model",
      "$.model.__namespace",
      "$.model.__name",
      "$.model.layers",
      "$.model.layers[0]",
      "$.model.layers[0].units",
      "$.model.layers[1]",
      "$.model.layers[1].units",
      "$.model.note",
      "$.empty",
    ]);
  });

  it("records kinds, sizes, subtree ends and sibling positions", () => {
    const model = nodes.find((n) => n.id === "$.model")!;
    expect(model.kind).toBe("object");
    expect(model.size).toBe(4);
    expect(nodes[model.end].id).toBe("$.empty");
    expect(nodes[0].size).toBe(6);
    const flags = nodes.find((n) => n.id === "$.flags")!;
    expect(flags).toMatchObject({
      kind: "array",
      size: 2,
      posInSet: 4,
      setSize: 6,
    });
    expect(nodes.find((n) => n.id === '$["odd key"]')!.kind).toBe("null");
    expect(nodes.find((n) => n.id === "$.flags[1]")!.kind).toBe("boolean");
  });

  it("labels nested task bodies by their discriminators", () => {
    expect(nodes.find((n) => n.id === "$.model")!.taskLabel).toBe("demo.Train");
    expect(nodes.find((n) => n.id === "$.model.layers[0]")!.taskLabel).toBeNull();
  });

  it("indexes a scalar root as a single node", () => {
    expect(indexJson(5)).toHaveLength(1);
  });
});

describe("pathSegment", () => {
  it("uses dot notation for identifiers and brackets otherwise", () => {
    expect(pathSegment("a_1")).toBe(".a_1");
    expect(pathSegment(2)).toBe("[2]");
    expect(pathSegment("1a")).toBe('["1a"]');
    expect(pathSegment('say "hi"')).toBe('["say \\"hi\\""]');
  });
});

describe("visibleRows / defaultExpanded", () => {
  const nodes = indexJson(BODY);

  it("skips the descendants of collapsed containers, never drawing the root", () => {
    expect(ids(nodes, visibleRows(nodes, new Set(["$"])))).toEqual([
      "$.epochs",
      "$.name",
      '$["odd key"]',
      "$.flags",
      "$.model",
      "$.empty",
    ]);
    expect(ids(nodes, visibleRows(nodes, new Set(["$", "$.model"])))).toContain(
      "$.model.layers",
    );
    expect(ids(nodes, visibleRows(nodes, new Set(["$", "$.model"])))).not.toContain(
      "$.model.layers[0]",
    );
  });

  it("shows everything when every container is expanded", () => {
    expect(visibleRows(nodes, allContainerIds(nodes))).toHaveLength(nodes.length - 1);
  });

  it("expands levels shallower than the default depth", () => {
    const expanded = defaultExpanded(nodes, 2);
    expect([...expanded].sort()).toEqual(["$", "$.empty", "$.flags", "$.model"]);
  });

  it("stops expanding at the level that would exceed the row budget", () => {
    const wide = indexJson({
      a: Array.from({ length: 500 }, (_, i) => i),
      b: 1,
    });
    expect([...defaultExpanded(wide, 3)]).toEqual(["$"]);
  });
});

describe("findMatches / ancestorIds", () => {
  const nodes = indexJson(BODY);

  it("matches keys and scalar values case-insensitively, in display order", () => {
    const matches = findMatches(nodes, "ALPHA");
    expect(matches.map((m) => [nodes[m.index].id, m.inKey, m.inValue])).toEqual([
      ["$.name", false, true],
      ["$.model.note", false, true],
    ]);
    expect(findMatches(nodes, "units").map((m) => nodes[m.index].id)).toEqual([
      "$.model.layers[0].units",
      "$.model.layers[1].units",
    ]);
  });

  it("matches numbers, booleans and null by their text, but not array indices", () => {
    expect(findMatches(nodes, "64").map((m) => nodes[m.index].id)).toEqual([
      "$.model.layers[0].units",
    ]);
    expect(findMatches(nodes, "null").map((m) => nodes[m.index].id)).toEqual([
      '$["odd key"]',
    ]);
    expect(findMatches(nodes, "0")).toHaveLength(0);
  });

  it("matches nothing on a blank query", () => {
    expect(findMatches(nodes, "  ")).toEqual([]);
  });

  it("collects every ancestor of the matches", () => {
    const deep = nodes.findIndex((n) => n.id === "$.model.layers[1].units");
    expect([...ancestorIds(nodes, [deep])].sort()).toEqual([
      "$",
      "$.model",
      "$.model.layers",
      "$.model.layers[1]",
    ]);
  });
});

describe("union", () => {
  it("returns the original set when nothing is added", () => {
    const a = new Set(["x"]);
    expect(union(a, ["x"])).toBe(a);
    expect([...union(a, ["y"])]).toEqual(["x", "y"]);
  });
});

describe("highlightSegments", () => {
  it("splits every case-insensitive occurrence", () => {
    expect(highlightSegments("Alpha alpha", "ALP")).toEqual([
      { text: "Alp", match: true },
      { text: "ha ", match: false },
      { text: "alp", match: true },
      { text: "ha", match: false },
    ]);
    expect(highlightSegments("abc", "")).toEqual([{ text: "abc", match: false }]);
  });
});

describe("copyValueText / sizeLabel", () => {
  const nodes = indexJson(BODY);
  const node = (id: string) => nodes.find((n) => n.id === id)!;

  it("copies strings raw and everything else as JSON", () => {
    expect(copyValueText(node("$.name"))).toBe("Alpha");
    expect(copyValueText(node("$.epochs"))).toBe("3");
    expect(copyValueText(node('$["odd key"]'))).toBe("null");
    expect(JSON.parse(copyValueText(node("$.model.layers")))).toEqual([
      { units: 64 },
      { units: 32 },
    ]);
  });

  it("pluralises sizes", () => {
    expect(sizeLabel(node("$.flags"))).toBe("2 items");
    expect(sizeLabel(node("$.empty"))).toBe("0 keys");
    expect(sizeLabel(indexJson({ a: 1 })[0])).toBe("1 key");
  });
});
