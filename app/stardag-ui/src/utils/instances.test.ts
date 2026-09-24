import { describe, expect, it } from "vitest";
import {
  differingParameters,
  identityOf,
  memberLabel,
  parametersOf,
  qualifiedName,
} from "./instances";

const TASK_ID = "4c2a9e1b-7d3f-5a60-8b21-0e9f4d6c1a37";

describe("identityOf / memberLabel", () => {
  it("reads the class from the discriminator keys", () => {
    const body = { __namespace: "demo", __name: "Train", epochs: 3 };
    expect(identityOf(body)).toEqual({ namespace: "demo", name: "Train" });
    expect(memberLabel(TASK_ID, body)).toBe("demo.Train 4c2a9e1b");
  });

  it("falls back to the short task id when the body names no class", () => {
    expect(memberLabel(TASK_ID, { epochs: 3 })).toBe("4c2a9e1b");
  });

  it("omits the default namespace", () => {
    expect(qualifiedName("", "Train")).toBe("Train");
  });
});

describe("parametersOf", () => {
  it("drops the top-level discriminators and keeps nested ones", () => {
    const body = {
      __namespace: "demo",
      __name: "Train",
      data: { __namespace: "demo", __name: "Load", path: "s3://x" },
    };
    expect(parametersOf(body)).toEqual({
      data: { __namespace: "demo", __name: "Load", path: "s3://x" },
    });
  });
});

describe("differingParameters", () => {
  it("names the parameters two instances disagree on, key order ignored", () => {
    const a = { __name: "Train", epochs: 3, opts: { a: 1, b: 2 }, log: "info" };
    const b = { __name: "Train", epochs: 3, opts: { b: 2, a: 1 }, log: "debug" };
    expect(differingParameters([a, b])).toEqual(["log"]);
  });

  it("counts a parameter present on one body only", () => {
    expect(differingParameters([{ x: 1 }, { x: 1, y: 2 }])).toEqual(["y"]);
  });

  it("is empty for a single instance", () => {
    expect(differingParameters([{ x: 1 }])).toEqual([]);
  });
});
