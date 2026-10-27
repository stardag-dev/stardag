import { describe, expect, it } from "vitest";
import { taskRefSnippet } from "./taskRef";

const TASK_ID = "df0c8b03-fab2-5ddd-9743-09fb4a634cf5";
const INSTANCE_ID = "0193b0c4-7a1e-7c3d-9f00-2b6f1c9d8e7a";

describe("the reference snippet", () => {
  it("pins the instance", () => {
    expect(taskRefSnippet(TASK_ID, INSTANCE_ID, "TrainModel")).toBe(
      `TrainModel.ref("${TASK_ID}", instance_id="${INSTANCE_ID}")`,
    );
  });

  it("omits a missing instance", () => {
    expect(taskRefSnippet(TASK_ID, null, "Train")).toBe(`Train.ref("${TASK_ID}")`);
  });

  it("keeps a nested class's qualified name", () => {
    expect(taskRefSnippet(TASK_ID, null, "Outer.Inner")).toBe(
      `Outer.Inner.ref("${TASK_ID}")`,
    );
  });
});
