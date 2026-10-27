import { describe, expect, it } from "vitest";
import {
  allContainerIds,
  ancestorIds,
  copyValueText,
  defaultExpanded,
  findMatches,
  foldCase,
  hiddenCount,
  fuzzyMatch,
  MAX_FUZZY_TEXT,
  segmentsOf,
  indexJson,
  moreOf,
  moreRow,
  nthChild,
  PAGE_SIZE,
  revealInPages,
  showMore,
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
    expect(matches.map((m) => [nodes[m.index].id, m.keyRanges, m.valueRanges])).toEqual(
      [
        ["$.name", null, [[0, 5]]],
        ["$.model.note", null, [[0, 5]]],
      ],
    );
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

describe("fuzzyMatch", () => {
  const ranges = (text: string, query: string) => fuzzyMatch(text, query)?.ranges;

  it("matches the query's characters in order, case-insensitively", () => {
    expect(ranges("learning_rate", "LR")).toEqual([
      [0, 1],
      [9, 10],
    ]);
    expect(ranges("units", "unts")).toEqual([
      [0, 2],
      [3, 5],
    ]);
    expect(fuzzyMatch("units", "stun")).toBeNull();
    expect(fuzzyMatch("units", "unitsx")).toBeNull();
    expect(fuzzyMatch("units", " ")).toBeNull();
  });

  it("prefers word starts and contiguous runs when placing the characters", () => {
    // `b` and `s` at the word starts, not the first `b`/`s` in the text.
    expect(ranges("xbxs_batch_size", "bs")).toEqual([
      [5, 6],
      [11, 12],
    ]);
    // camelCase humps.
    expect(ranges("maxBatchSize", "mbs")).toEqual([
      [0, 1],
      [3, 4],
      [8, 9],
    ]);
    // The contiguous occurrence over a scattered one.
    expect(ranges("t_r_a_i_n train", "train")).toEqual([[10, 15]]);
  });

  it("needs the first character at a word start, unless the query is contiguous", () => {
    expect(fuzzyMatch("alpha", "pa")).toBeNull();
    expect(ranges("alpha", "pha")).toEqual([[2, 5]]);
    // `a` at the word start of `alpha`, not the `a` inside `max`.
    expect(ranges("max_alpha", "ap")).toEqual([
      [4, 5],
      [6, 7],
    ]);
  });

  it("scores contiguous above scattered, and word starts above mid-word", () => {
    const score = (text: string, query: string) => fuzzyMatch(text, query)!.score;
    expect(score("batch", "bat")).toBeGreaterThan(score("b_a_t", "bat"));
    expect(score("batch", "bat")).toBeGreaterThan(score("acrobat", "bat"));
    expect(score("batch_size", "bs")).toBeGreaterThan(score("abs", "bs"));
  });

  it("rejects characters strewn far apart mid-word", () => {
    expect(fuzzyMatch("cxxxxxxxxxxxxxxxe", "ce")).toBeNull();
    // A hash has one word start, so a short query does not match most of them.
    expect(fuzzyMatch("9f8a7c6b5e4d3c2b1a0f9e8d", "abc")).toBeNull();
    expect(fuzzyMatch("9f8a7c6b5e4d3c2b1a0f9e8d", "f8a")).not.toBeNull();
    // ...but keeps initials however far apart.
    expect(fuzzyMatch("first_quite_long_segment_last", "fl")).not.toBeNull();
  });

  it("highlights whole characters when lower-casing changes the length", () => {
    // "İ" lower-cases to two code units.
    expect(ranges("İstanbul", "stan")).toEqual([[1, 5]]);
    expect(ranges("xİy", "i")).toEqual([[1, 2]]);
    expect(ranges("ΟΔΟΣ", "οδοσ")).toEqual([[0, 4]]);
  });

  it("falls back to a substring match on very long texts", () => {
    const long = "x".repeat(MAX_FUZZY_TEXT) + "needle";
    expect(ranges(long, "needle")).toEqual([[MAX_FUZZY_TEXT, MAX_FUZZY_TEXT + 6]]);
    expect(fuzzyMatch(long, "nedl")).toBeNull();
  });
});

describe("findMatches ordering", () => {
  it("orders matches best first, document order breaking ties", () => {
    const nodes = indexJson({
      a: "b_a_t",
      b: "acrobat",
      c: "batch",
      d: "batch",
    });
    expect(findMatches(nodes, "bat").map((m) => nodes[m.index].id)).toEqual([
      "$.c",
      "$.d",
      "$.b",
      "$.a",
    ]);
  });

  it("scores a node by the better of its key and value, and highlights each", () => {
    const nodes = indexJson({ batch_size: "large", other: "batch" });
    const [first, second] = findMatches(nodes, "bs");
    expect(nodes[first.index].id).toBe("$.batch_size");
    expect(first.keyRanges).toEqual([
      [0, 1],
      [6, 7],
    ]);
    expect(first.valueRanges).toBeNull();
    expect(second).toBeUndefined();
  });
});

describe("segmentsOf", () => {
  it("splits a text at the matched ranges", () => {
    expect(
      segmentsOf("learning_rate", [
        [0, 1],
        [9, 10],
      ]),
    ).toEqual([
      { text: "l", match: true },
      { text: "earning_", match: false },
      { text: "r", match: true },
      { text: "ate", match: false },
    ]);
    expect(segmentsOf("abc", [])).toEqual([{ text: "abc", match: false }]);
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

describe("paging wide containers", () => {
  const nodes = indexJson({ wide: Array.from({ length: 450 }, (_, i) => i), x: 1 });
  const expanded = new Set(["$", "$.wide"]);

  it("draws the first page of children, then a show-more row", () => {
    const rows = visibleRows(nodes, expanded);
    expect(rows).toHaveLength(1 + PAGE_SIZE + 1 + 1);
    expect(rows[PAGE_SIZE + 1]).toBe(moreRow(1, "after"));
    expect(moreOf(rows[PAGE_SIZE + 1])).toEqual({ parent: 1, direction: "after" });
    expect(moreOf(moreRow(0, "before"))).toEqual({ parent: 0, direction: "before" });
    expect(nodes[rows.at(-1)!].id).toBe("$.x");
    expect(hiddenCount(nodes, new Map(), 1, "after")).toBe(250);
    expect(hiddenCount(nodes, new Map(), 1, "before")).toBe(0);
  });

  it("grows the window a page at a time", () => {
    const windows = showMore(nodes, new Map(), 1, "after");
    expect(visibleRows(nodes, expanded, windows)).toHaveLength(1 + 400 + 1 + 1);
    expect(hiddenCount(nodes, windows, 1, "after")).toBe(50);
    expect(nodes[nthChild(nodes, 1, 400)].id).toBe("$.wide[400]");
    expect(nthChild(nodes, 1, 450)).toBe(-1);
  });

  it("moves the window to the page holding a node, and leaves it alone otherwise", () => {
    const deep = nodes.findIndex((n) => n.id === "$.wide[420]");
    const windows = revealInPages(nodes, new Map(), deep);
    expect(windows.get("$.wide")).toEqual([400, 450]);
    const rows = visibleRows(nodes, expanded, windows);
    // A show-earlier row, the last 50 children, and the next top-level key.
    expect(rows).toHaveLength(1 + 1 + 50 + 1);
    expect(rows[1]).toBe(moreRow(1, "before"));
    expect(hiddenCount(nodes, windows, 1, "before")).toBe(400);
    expect(showMore(nodes, windows, 1, "before").get("$.wide")).toEqual([200, 450]);

    const near = nodes.findIndex((n) => n.id === "$.wide[3]");
    const same = new Map();
    expect(revealInPages(nodes, same, near)).toBe(same);
  });
});

describe("case folding", () => {
  it("folds per character, so a final sigma matches its query", () => {
    expect(foldCase("ΟΔΟΣ")).toBe(foldCase("οδοσ"));
    const nodes = indexJson({ street: "ΟΔΟΣ" });
    expect(findMatches(nodes, "οδοσ")).toHaveLength(1);
  });
});
