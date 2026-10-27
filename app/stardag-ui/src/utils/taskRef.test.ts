import { describe, expect, it } from "vitest";
import { isModulePath, taskRefSnippet } from "./taskRef";

const TASK_ID = "df0c8b03-fab2-5ddd-9743-09fb4a634cf5";
const INSTANCE_ID = "0193b0c4-7a1e-7c3d-9f00-2b6f1c9d8e7a";

describe("the reference snippet", () => {
  it("pins the instance and imports from a module-like namespace", () => {
    const snippet = taskRefSnippet(
      TASK_ID,
      INSTANCE_ID,
      { __namespace: "examples.ml", __name: "TrainModel", epochs: 3 },
      "TrainModel",
    );
    expect(snippet.importLine).toBe("from examples.ml import TrainModel");
    expect(snippet.refLine).toBe(
      `TrainModel.ref("${TASK_ID}", instance_id="${INSTANCE_ID}")`,
    );
    expect(snippet.code).toBe(`${snippet.importLine}\n${snippet.refLine}`);
  });

  it("gives just the ref line when the namespace is not a module path", () => {
    for (const namespace of ["", "my-project", "a..b"]) {
      const snippet = taskRefSnippet(
        TASK_ID,
        INSTANCE_ID,
        { __namespace: namespace, __name: "TrainModel" },
        "TrainModel",
      );
      expect(snippet.importLine).toBeNull();
      expect(snippet.code).toBe(snippet.refLine);
    }
  });

  it("falls back to the task name and omits a missing instance", () => {
    const snippet = taskRefSnippet(TASK_ID, null, {}, "Fallback");
    expect(snippet.code).toBe(`Fallback.ref("${TASK_ID}")`);
  });

  it("imports the outer class of a nested one", () => {
    const snippet = taskRefSnippet(
      TASK_ID,
      INSTANCE_ID,
      { __namespace: "pkg", __name: "Outer.Inner" },
      "Inner",
    );
    expect(snippet.importLine).toBe("from pkg import Outer");
    expect(snippet.refLine.startsWith("Outer.Inner.ref(")).toBe(true);
  });

  it("recognises dotted module paths", () => {
    expect(isModulePath("a.b_c.d1")).toBe(true);
    expect(isModulePath("1a")).toBe(false);
    expect(isModulePath("")).toBe(false);
  });
});
