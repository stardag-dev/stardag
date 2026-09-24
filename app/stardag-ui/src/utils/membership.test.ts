import { describe, expect, it } from "vitest";
import type { PlanMember } from "../types/task";
import { MEMBERSHIP_COLUMN_HELP, MEMBERSHIP_HELP, membershipFacts } from "./membership";

function member(overrides: Partial<PlanMember>): PlanMember {
  return {
    task_id: "t",
    instance_id: "i",
    instance_hash: "h",
    task_namespace: "",
    task_name: "T",
    status: "pending",
    is_root: false,
    admitted_by: "static",
    excluded_at: null,
    excluded_reason: null,
    attempts: 0,
    interruptions: 0,
    ...overrides,
  };
}

describe("membershipFacts", () => {
  it("names a root once, with its explanation", () => {
    expect(membershipFacts(member({ is_root: true, admitted_by: "root" }))).toEqual([
      { key: "root", label: "root", help: MEMBERSHIP_HELP.root },
    ]);
  });

  it("explains each admission path", () => {
    for (const path of ["static", "dynamic", "closure"] as const) {
      const [fact] = membershipFacts(member({ admitted_by: path }));
      expect(fact).toEqual({ key: path, label: path, help: MEMBERSHIP_HELP[path] });
    }
  });

  it("explains an exclusion with its reason, and counts attempts", () => {
    const facts = membershipFacts(
      member({
        admitted_by: "dynamic",
        excluded_at: "2026-09-24T00:00:00Z",
        excluded_reason: "upstream_excluded",
        attempts: 2,
        interruptions: 1,
      }),
    );
    expect(facts.map((f) => f.label)).toEqual([
      "dynamic",
      "excluded (upstream excluded)",
      "2 attempts, 1 interrupted",
    ]);
    expect(facts[1].help).toBe(
      `${MEMBERSHIP_HELP.excluded} Reason: an upstream was excluded.`,
    );
  });

  it("carries every value's sentence in the column header's help", () => {
    for (const sentence of Object.values(MEMBERSHIP_HELP)) {
      expect(MEMBERSHIP_COLUMN_HELP).toContain(sentence);
    }
  });
});
