import { describe, expect, it } from "vitest";
import {
  addTag,
  checkDescription,
  checkTag,
  MAX_DESCRIPTION_LENGTH,
  MAX_TAG_LENGTH,
  MAX_TAGS,
} from "./annotations";

describe("tag validation (the server's normalisation)", () => {
  it("trims, and refuses empty, too long and control characters", () => {
    expect(checkTag("  prod ")).toEqual({ ok: true, tag: "prod" });
    expect(checkTag("   ").ok).toBe(false);
    expect(checkTag("x".repeat(MAX_TAG_LENGTH)).ok).toBe(true);
    expect(checkTag("x".repeat(MAX_TAG_LENGTH + 1)).ok).toBe(false);
    expect(checkTag("a\tb").ok).toBe(false);
    expect(checkTag("a\u0085b").ok).toBe(false);
  });

  it("counts code points, as the server does", () => {
    // 64 astral characters are 128 UTF-16 units and still one valid tag.
    expect(checkTag("🙂".repeat(MAX_TAG_LENGTH)).ok).toBe(true);
  });

  it("keeps case: Prod and prod are two tags", () => {
    const first = addTag([], "prod");
    expect(first.ok && addTag(first.tags, "Prod")).toEqual({
      ok: true,
      tags: ["Prod", "prod"],
    });
  });

  it("adds sorted, ignores a duplicate and caps the count", () => {
    expect(addTag(["b"], "a")).toEqual({ ok: true, tags: ["a", "b"] });
    const tags = ["a"];
    expect(addTag(tags, " a ")).toEqual({ ok: true, tags });
    const full = Array.from({ length: MAX_TAGS }, (_, i) => `t${i}`);
    expect(addTag(full, "one-more").ok).toBe(false);
  });
});

describe("description validation", () => {
  it("trims, clears when empty and caps the length", () => {
    expect(checkDescription("  hi\nthere ")).toEqual({
      ok: true,
      description: "hi\nthere",
    });
    expect(checkDescription("   ")).toEqual({ ok: true, description: null });
    expect(checkDescription("x".repeat(MAX_DESCRIPTION_LENGTH + 1)).ok).toBe(false);
  });
});
