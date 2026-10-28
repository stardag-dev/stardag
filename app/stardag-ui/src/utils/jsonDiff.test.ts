import { describe, expect, it } from "vitest";
import {
  alignArrays,
  canonicalJson,
  countsLabel,
  countsSentence,
  defaultDiffExpanded,
  diffBodies,
  diffJson,
  diffPath,
  diffValueText,
  findDiffMatches,
  MAX_ALIGN_CELLS,
  onlyDifferences,
  type DiffNode,
} from "./jsonDiff";
import { visibleRows } from "./jsonTree";

function byId(nodes: DiffNode[], id: string): DiffNode {
  const node = nodes.find((n) => n.id === id);
  if (!node) throw new Error(`no node ${id}: ${nodes.map((n) => n.id).join(" ")}`);
  return node;
}

function statuses(nodes: DiffNode[]): Record<string, string> {
  return Object.fromEntries(nodes.slice(1).map((n) => [n.id, n.status]));
}

/** The preorder invariants every index must keep. */
function checkIndex(nodes: DiffNode[]) {
  nodes.forEach((node, i) => {
    expect(node.end).toBeGreaterThan(i);
    let children = 0;
    for (let c = i + 1; c < node.end; c = nodes[c].end) {
      expect(nodes[c].parent).toBe(i);
      expect(nodes[c].depth).toBe(node.depth + 1);
      expect(nodes[c].posInSet).toBe(++children);
      expect(nodes[c].setSize).toBe(node.size);
    }
    expect(children).toBe(node.size);
  });
  expect(new Set(nodes.map((n) => n.id)).size).toBe(nodes.length);
}

describe("diffJson", () => {
  it("marks added, removed, changed and unchanged keys, with counts", () => {
    const nodes = diffJson(
      { same: 1, gone: "x", edited: 2.5 },
      { same: 1, edited: 3, fresh: true },
    );
    checkIndex(nodes);
    expect(statuses(nodes)).toEqual({
      "$.same": "unchanged",
      "$.gone": "removed",
      "$.edited": "changed",
      "$.fresh": "added",
    });
    expect(nodes[0].status).toBe("changed");
    expect(nodes[0].counts).toEqual({ added: 1, removed: 1, changed: 1 });
    const edited = byId(nodes, "$.edited");
    expect([edited.left, edited.right]).toEqual([2.5, 3]);
    expect(byId(nodes, "$.gone").hasRight).toBe(false);
    expect(byId(nodes, "$.fresh").hasLeft).toBe(false);
  });

  it("orders keys left first, then the right side's new ones", () => {
    const nodes = diffJson({ b: 1, a: 1 }, { c: 1, a: 1, b: 1 });
    expect(nodes.slice(1).map((n) => n.key)).toEqual(["b", "a", "c"]);
  });

  it("treats a type change as a change, not equal text", () => {
    const nodes = diffJson(
      { n: 3, b: false, z: null, o: { a: 1 } },
      { n: "3", b: "false", z: 0, o: 5 },
    );
    expect(statuses(nodes)).toMatchObject({
      "$.n": "changed",
      "$.b": "changed",
      "$.z": "changed",
      "$.o": "changed",
    });
    const n = byId(nodes, "$.n");
    expect([n.leftKind, n.rightKind]).toEqual(["number", "string"]);
    // The object side keeps its children, removed; the change counts once.
    const o = byId(nodes, "$.o");
    expect(o.kind).toBe("object");
    expect(o.size).toBe(1);
    expect(o.counts).toEqual({ added: 0, removed: 0, changed: 1 });
    expect(nodes[0].counts.changed).toBe(4);
    const child = nodes[nodes.indexOf(o) + 1];
    expect(child.status).toBe("removed");
    expect(child.leftPath).toBe("$.o.a");
    expect(child.rightPath).toBeNull();
    checkIndex(nodes);
  });

  it("treats an object turned array as a type change with both sides' children", () => {
    const nodes = diffJson({ v: { a: 1 } }, { v: [1, 2] });
    const v = byId(nodes, "$.v");
    expect(v.status).toBe("changed");
    expect(v.size).toBe(3);
    const kids = nodes.slice(nodes.indexOf(v) + 1, v.end);
    expect(kids.map((k) => k.status)).toEqual(["removed", "added", "added"]);
    checkIndex(nodes);
  });

  it("keeps an added or removed container's children, counting it once", () => {
    const nodes = diffJson({}, { opts: { lr: 0.1, layers: [64, 128] } });
    const opts = byId(nodes, "$.opts");
    expect(opts.status).toBe("added");
    expect(opts.counts).toEqual({ added: 1, removed: 0, changed: 0 });
    expect(nodes[0].counts).toEqual({ added: 1, removed: 0, changed: 0 });
    expect(byId(nodes, "$.opts.layers[1]").status).toBe("added");
    checkIndex(nodes);
  });

  it("sums counts up through containers on both sides", () => {
    const nodes = diffJson(
      { a: { b: { c: 1, d: 2 }, e: [1] } },
      { a: { b: { c: 9, d: 2, f: 0 }, e: [] } },
    );
    expect(byId(nodes, "$.a.b").counts).toEqual({
      added: 1,
      removed: 0,
      changed: 1,
    });
    expect(byId(nodes, "$.a.e").counts).toEqual({
      added: 0,
      removed: 1,
      changed: 0,
    });
    expect(byId(nodes, "$.a").counts).toEqual({
      added: 1,
      removed: 1,
      changed: 1,
    });
    expect(byId(nodes, "$.a").status).toBe("changed");
    expect(byId(nodes, "$.a.b.d").status).toBe("unchanged");
  });

  it("reads equal objects with keys in another order as unchanged", () => {
    const nodes = diffJson({ o: { a: 1, b: 2 } }, { o: { b: 2, a: 1 } });
    expect(byId(nodes, "$.o").status).toBe("unchanged");
    expect(nodes[0].counts).toEqual({ added: 0, removed: 0, changed: 0 });
  });

  it("handles unicode keys and whitespace in keys", () => {
    const nodes = diffJson(
      { größe: 1, "a b": "x", " lead": 1, "": 0, "😀": [1] },
      { größe: 2, "a b": "x", " lead": 2, "": 0, "😀": [1, 2] },
    );
    checkIndex(nodes);
    expect(byId(nodes, '$["größe"]').status).toBe("changed");
    expect(byId(nodes, '$["a b"]').status).toBe("unchanged");
    expect(byId(nodes, '$[" lead"]').status).toBe("changed");
    expect(byId(nodes, '$[""]').status).toBe("unchanged");
    expect(byId(nodes, '$["😀"][1]').status).toBe("added");
    expect(diffPath(byId(nodes, '$["😀"][1]'))).toBe('$["😀"][1]');
  });

  it("does not equate a string with the same text in another type", () => {
    const nodes = diffJson({ x: "1" }, { x: 1 });
    expect(byId(nodes, "$.x").status).toBe("changed");
  });
});

describe("nested tasks", () => {
  const dataset = (split: string) => ({
    __namespace: "examples.ml",
    __name: "Dataset",
    split,
    seed: 0,
  });
  const model = (lr: number, data = dataset("train")) => ({
    __namespace: "examples.ml",
    __name: "TrainModel",
    lr,
    epochs: 10,
    dataset: data,
  });
  const metrics = (m: object, data: object) => ({
    __namespace: "examples.ml",
    __name: "Metrics",
    model: m,
    dataset: data,
  });

  it("strips only the top-level discriminators, and labels nested tasks", () => {
    const nodes = diffBodies(
      metrics(model(0.1), dataset("test")),
      metrics(model(0.01), dataset("test")),
    );
    expect(nodes.find((n) => n.id === "$.__name")).toBeUndefined();
    const m = byId(nodes, "$.model");
    expect(m.leftTaskLabel).toBe("examples.ml.TrainModel");
    expect(m.rightTaskLabel).toBe("examples.ml.TrainModel");
    expect(m.status).toBe("changed");
    expect(m.counts).toEqual({ added: 0, removed: 0, changed: 1 });
    expect(byId(nodes, "$.model.lr").status).toBe("changed");
    // The identical nested task is one unchanged node.
    const d = byId(nodes, "$.dataset");
    expect(d.status).toBe("unchanged");
    expect(d.counts).toEqual({ added: 0, removed: 0, changed: 0 });
    // Nested discriminators are still nodes (they identify the nested task).
    expect(byId(nodes, "$.model.__name").status).toBe("unchanged");
  });

  it("reads a nested task of another type as a changed task, with its labels", () => {
    const other = { __namespace: "examples.ml", __name: "FineTune", lr: 0.1 };
    const nodes = diffBodies(
      metrics(model(0.1), dataset("test")),
      metrics(other, dataset("test")),
    );
    const m = byId(nodes, "$.model");
    expect(m.leftTaskLabel).toBe("examples.ml.TrainModel");
    expect(m.rightTaskLabel).toBe("examples.ml.FineTune");
    expect(byId(nodes, "$.model.__name").status).toBe("changed");
    expect(byId(nodes, "$.model.dataset").status).toBe("removed");
  });

  it("drills into a deeper nested task that differs", () => {
    const nodes = diffBodies(
      metrics(model(0.1, dataset("train")), dataset("test")),
      metrics(model(0.1, dataset("train-v2")), dataset("test")),
    );
    expect(byId(nodes, "$.model.dataset.split").status).toBe("changed");
    expect(byId(nodes, "$.model.dataset").counts.changed).toBe(1);
    expect(byId(nodes, "$.model").counts.changed).toBe(1);
  });
});

describe("arrays", () => {
  it("pairs by index when nothing is inserted", () => {
    expect(alignArrays([1, 2, 3], [1, 9, 3])).toEqual([
      [0, 0],
      [1, 1],
      [2, 2],
    ]);
    const nodes = diffJson({ a: [1, 2, 3] }, { a: [1, 9, 3] });
    expect(statuses(nodes)).toMatchObject({
      "$.a[0]": "unchanged",
      "$.a[1]": "changed",
      "$.a[2]": "unchanged",
    });
  });

  it("reads one insertion at the front as one addition, not a shift", () => {
    expect(alignArrays(["b", "c"], ["a", "b", "c"])).toEqual([
      [null, 0],
      [0, 1],
      [1, 2],
    ]);
    const nodes = diffJson({ a: ["b", "c"] }, { a: ["a", "b", "c"] });
    expect(byId(nodes, "$.a").counts).toEqual({
      added: 1,
      removed: 0,
      changed: 0,
    });
  });

  it("reads a removal in the middle as one removal", () => {
    const nodes = diffJson({ a: [1, 2, 3, 4] }, { a: [1, 2, 4] });
    expect(byId(nodes, "$.a").counts).toEqual({
      added: 0,
      removed: 1,
      changed: 0,
    });
    // The removed item keeps its left path, and has an id of its own.
    const removed = nodes.find((n) => n.status === "removed") as DiffNode;
    expect(removed.leftPath).toBe("$.a[2]");
    expect(removed.key).toBe(2);
    expect(diffPath(removed)).toBe("$.a[2]");
    checkIndex(nodes);
  });

  it("gives a removed and an added item at one index distinct ids", () => {
    // 1 removed from the front and 3 added at the end: index 0 on the
    // left is removed while index 0 on the right is the kept `2`.
    const nodes = diffJson([1, 2], [2, 3]);
    checkIndex(nodes);
    expect(nodes.slice(1).map((n) => [n.id, n.status])).toEqual([
      ["$-[0]", "removed"],
      ["$[0]", "unchanged"],
      ["$[1]", "added"],
    ]);
  });

  it("pairs edited tasks in an array of tasks in place, around an insertion", () => {
    const t = (name: string, seed: number) => ({ __name: name, seed });
    const nodes = diffJson(
      { inputs: [t("A", 1), t("B", 1), t("C", 1)] },
      { inputs: [t("New", 0), t("A", 1), t("B", 2), t("C", 1)] },
    );
    const inputs = byId(nodes, "$.inputs");
    expect(inputs.counts).toEqual({ added: 1, removed: 0, changed: 1 });
    expect(byId(nodes, "$.inputs[0]").status).toBe("added");
    expect(byId(nodes, "$.inputs[1]").status).toBe("unchanged");
    expect(byId(nodes, "$.inputs[2].seed").status).toBe("changed");
    expect(byId(nodes, "$.inputs[2]").leftPath).toBe("$.inputs[1]");
  });

  it("falls back to index pairing beyond the alignment budget", () => {
    const side = Math.ceil(Math.sqrt(MAX_ALIGN_CELLS)) + 2;
    const left = Array.from({ length: side }, (_, i) => `l${i}`);
    const right = Array.from({ length: side }, (_, i) => `r${i}`);
    const out = alignArrays(left, right);
    expect(out).toHaveLength(side);
    expect(out[5]).toEqual([5, 5]);
  });

  it("handles empty arrays on either side", () => {
    expect(alignArrays([], [])).toEqual([]);
    expect(alignArrays([], [1])).toEqual([[null, 0]]);
    expect(alignArrays([1], [])).toEqual([[0, null]]);
  });
});

describe("canonicalJson", () => {
  it("ignores key order and keeps types apart", () => {
    expect(canonicalJson({ a: 1, b: [1, { c: 2, d: 3 }] })).toBe(
      canonicalJson({ b: [1, { d: 3, c: 2 }], a: 1 }),
    );
    expect(canonicalJson("1")).not.toBe(canonicalJson(1));
  });
});

describe("onlyDifferences", () => {
  it("drops unchanged nodes and re-indexes", () => {
    const nodes = diffJson(
      { a: 1, b: { c: 1, d: 2 }, e: [1, 2] },
      { a: 1, b: { c: 1, d: 3 }, e: [1, 2, 3] },
    );
    const only = onlyDifferences(nodes);
    checkIndex(only);
    expect(only.slice(1).map((n) => n.id)).toEqual(["$.b", "$.b.d", "$.e", "$.e[2]"]);
    expect(only[0].size).toBe(2);
  });

  it("keeps an added container's children", () => {
    const only = onlyDifferences(diffJson({}, { o: { a: 1 } }));
    expect(only.map((n) => n.id)).toEqual(["$", "$.o", "$.o.a"]);
  });

  it("reduces identical values to the root", () => {
    expect(onlyDifferences(diffJson({ a: 1 }, { a: 1 }))).toHaveLength(1);
  });
});

describe("defaultDiffExpanded", () => {
  it("expands changed containers and leaves unchanged and added ones collapsed", () => {
    const nodes = diffJson(
      { same: { x: 1 }, diff: { inner: { y: 1 } }, typed: { a: 1 } },
      { same: { x: 1 }, diff: { inner: { y: 2 } }, typed: 3, added: { z: 1 } },
    );
    const expanded = defaultDiffExpanded(nodes);
    expect(expanded.has("$.diff")).toBe(true);
    expect(expanded.has("$.diff.inner")).toBe(true);
    expect(expanded.has("$.same")).toBe(false);
    expect(expanded.has("$.typed")).toBe(false);
    expect(expanded.has("$.added")).toBe(false);
    const rows = visibleRows(nodes, expanded).map((i) => nodes[i].id);
    expect(rows).toContain("$.diff.inner.y");
    expect(rows).not.toContain("$.same.x");
  });

  it("stops at the row budget", () => {
    const wide = Object.fromEntries(
      Array.from({ length: 30 }, (_, i) => [`k${i}`, { v: i, w: Array(20).fill(i) }]),
    );
    const changed = Object.fromEntries(
      Array.from({ length: 30 }, (_, i) => [
        `k${i}`,
        { v: i + 1, w: Array(20).fill(i) },
      ]),
    );
    const nodes = diffJson({ wide }, { wide: changed });
    const expanded = defaultDiffExpanded(nodes, 40);
    expect(expanded.has("$.wide")).toBe(true);
    expect(expanded.has("$.wide.k0")).toBe(false);
  });
});

describe("findDiffMatches", () => {
  const nodes = diffJson(
    { learning_rate: 0.1, name: "alpha", same: "shared" },
    { learning_rate: 0.01, name: "beta", same: "shared" },
  );

  it("matches keys and values on either side", () => {
    const lr = findDiffMatches(nodes, "lrate");
    expect(lr.map((m) => nodes[m.index].id)).toEqual(["$.learning_rate"]);
    expect(lr[0].keyRanges).not.toBeNull();

    const left = findDiffMatches(nodes, "alpha");
    expect(left).toHaveLength(1);
    expect(left[0].leftRanges).toEqual([[0, 5]]);
    expect(left[0].rightRanges).toBeNull();

    const right = findDiffMatches(nodes, "beta");
    expect(right[0].rightRanges).toEqual([[0, 4]]);
  });

  it("matches an unchanged value once, on the side it is drawn", () => {
    const m = findDiffMatches(nodes, "shared");
    expect(m).toHaveLength(1);
    expect(m[0].leftRanges).toBeNull();
    expect(m[0].rightRanges).not.toBeNull();
  });

  it("matches nothing for an empty query", () => {
    expect(findDiffMatches(nodes, "  ")).toEqual([]);
  });
});

describe("labels and copying", () => {
  it("formats counts", () => {
    expect(countsLabel({ added: 2, removed: 1, changed: 3 })).toBe("+2 −1 ~3");
    expect(countsLabel({ added: 0, removed: 0, changed: 1 })).toBe("~1");
    expect(countsLabel({ added: 0, removed: 0, changed: 0 })).toBe("");
    expect(countsSentence({ added: 1, removed: 0, changed: 2 })).toBe(
      "1 added, 2 changed",
    );
  });

  it("copies each side's value", () => {
    const nodes = diffJson({ s: "old", o: { a: 1 } }, { s: "new", o: 2 });
    const s = byId(nodes, "$.s");
    expect(diffValueText(s, "left")).toBe("old");
    expect(diffValueText(s, "right")).toBe("new");
    expect(diffValueText(byId(nodes, "$.o"), "left")).toBe('{\n  "a": 1\n}');
  });
});

describe("scale", () => {
  it("diffs bodies of tens of thousands of nodes quickly", () => {
    const big = (shift: number) => ({
      rows: Array.from({ length: 5000 }, (_, i) => ({
        id: i,
        value: i % 97 === 0 ? i + shift : i,
        tags: ["a", "b"],
      })),
    });
    const start = performance.now();
    const nodes = diffJson(big(0), big(1));
    const elapsed = performance.now() - start;
    expect(nodes.length).toBeGreaterThan(20000);
    expect(byId(nodes, "$.rows").counts.changed).toBe(52);
    // Generous: CI machines vary; the point is "not quadratic".
    expect(elapsed).toBeLessThan(2000);
  });
});
