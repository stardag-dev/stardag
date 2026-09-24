import { describe, expect, it } from "vitest";
import { availableClaimActions, claimState } from "./claims";

const NOW = Date.parse("2026-09-24T12:00:00Z");

describe("claimState", () => {
  it("is live while running and before its expiry", () => {
    expect(
      claimState({ status: "running", claim_expires_at: "2026-09-24T12:05:00Z" }, NOW),
    ).toBe("live");
  });

  it("is lapsed while running past its expiry", () => {
    expect(
      claimState({ status: "running", claim_expires_at: "2026-09-24T11:55:00Z" }, NOW),
    ).toBe("lapsed");
  });

  it("holds none for any status but running, suspended included", () => {
    for (const status of [
      "suspended",
      "interrupted",
      "pending",
      "completed",
    ] as const) {
      expect(
        claimState({ status, claim_expires_at: "2026-09-24T12:05:00Z" }, NOW),
      ).toBe("none");
    }
  });
});

describe("availableClaimActions", () => {
  it("offers only the release on a live claim — a retry would be refused", () => {
    expect(availableClaimActions("running", "live")).toEqual(["release"]);
  });

  it("offers both on a lapsed claim", () => {
    expect(availableClaimActions("running", "lapsed")).toEqual(["release", "retry"]);
  });

  it("offers nothing on completed or pending", () => {
    expect(availableClaimActions("completed", "none")).toEqual([]);
    expect(availableClaimActions("pending", "none")).toEqual([]);
  });

  it("offers a retry on a suspended task, which holds no claim in v2", () => {
    expect(availableClaimActions("suspended", "none")).toEqual(["retry"]);
  });
});
