import { describe, expect, it } from "vitest";
import { shortBuildId, shortTaskId } from "./ids";

describe("shortBuildId", () => {
  // The defect this exists for: build ids are UUIDv7, so builds created in
  // the same millisecond share their leading bytes. A whole list of them
  // rendered `01a0c93c` on every row.
  it("distinguishes ids that share a timestamp prefix", () => {
    const a = "01a0c93c-78fc-7d03-a695-5809dbca2816";
    const b = "01a0c93c-7990-7ba0-a451-24ff31404caa";
    expect(a.slice(0, 8)).toBe(b.slice(0, 8));
    expect(shortBuildId(a)).not.toBe(shortBuildId(b));
  });

  it("keeps the trailing characters, marked as a truncation", () => {
    expect(shortBuildId("01a0c93c-7990-7ba0-a451-24ff31404caa")).toBe("…31404caa");
  });

  it("leaves an id that is already short alone", () => {
    expect(shortBuildId("abc123")).toBe("abc123");
  });
});

describe("shortTaskId", () => {
  // Task ids are content hashes, so the front is as discriminating as the
  // back — and it is the end everything else prints.
  it("keeps the leading characters", () => {
    expect(shortTaskId("df0c8b03-fab2-5ddd-9743-09fb4a634cf5")).toBe("df0c8b03");
  });
});
