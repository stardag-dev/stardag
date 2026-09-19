import { describe, expect, it } from "vitest";

import { isSyntheticScope } from "./scope";

describe("isSyntheticScope", () => {
  it("recognises the server's exact build:<uuid> shape, in either case", () => {
    expect(isSyntheticScope("build:01a0b6e8-0f10-77d2-a309-a4ddc26d0316")).toBe(true);
    expect(isSyntheticScope("build:01A0B6E8-0F10-77D2-A309-A4DDC26D0316")).toBe(true);
  });

  it("does not read a code id that happens to be 'build' as synthetic", () => {
    expect(isSyntheticScope("build:ffffffffffffffff")).toBe(false);
    expect(isSyntheticScope("build:")).toBe(false);
  });

  it("is false for real scopes and for nothing", () => {
    expect(isSyntheticScope("abc:ffff")).toBe(false);
    expect(isSyntheticScope("405a900309f34d11aad5ee0184a79b26:44136fa355b3678a")).toBe(
      false,
    );
    expect(isSyntheticScope(null)).toBe(false);
    expect(isSyntheticScope(undefined)).toBe(false);
    expect(isSyntheticScope("")).toBe(false);
  });
});
