import type { AdmittedBy, ExclusionReason, PlanMember } from "../types/task";

/** One sentence per `plan_member` fact, for the hover explanations. */
export const MEMBERSHIP_HELP = {
  root: "root: requested by the build.",
  static: "static: found by walking requires() at planning.",
  dynamic: "dynamic: yielded by a running task.",
  closure: "closure: admitted as an upstream of a member.",
  excluded: "excluded: given up on, not scheduled and not gating completion.",
  attempts:
    "attempts: executions of the task under any of this build's plans, and how " +
    "many of them were interrupted or preempted.",
} as const;

/** The column header's explanation: every value's sentence, one per line. */
export const MEMBERSHIP_COLUMN_HELP = [
  "How the task is in this build's active plan.",
  MEMBERSHIP_HELP.root,
  MEMBERSHIP_HELP.static,
  MEMBERSHIP_HELP.dynamic,
  MEMBERSHIP_HELP.closure,
  MEMBERSHIP_HELP.excluded,
  MEMBERSHIP_HELP.attempts,
].join("\n");

const EXCLUSION_REASONS: Record<ExclusionReason, string> = {
  operator: "by an operator",
  discovery_failed: "its discovery failed",
  upstream_excluded: "an upstream was excluded",
};

export interface MembershipFact {
  key: string;
  label: string;
  help: string;
}

/**
 * The member's facts in display order: how it was admitted (`root` once,
 * even though a root is both `is_root` and `admitted_by: root`), whether it
 * is excluded and why, and its attempt count when it has any.
 */
export function membershipFacts(member: PlanMember): MembershipFact[] {
  const facts: MembershipFact[] = [];
  if (member.is_root || member.admitted_by === "root") {
    facts.push({ key: "root", label: "root", help: MEMBERSHIP_HELP.root });
  }
  const admitted: AdmittedBy | null = member.admitted_by;
  if (admitted && admitted !== "root") {
    facts.push({ key: admitted, label: admitted, help: MEMBERSHIP_HELP[admitted] });
  }
  if (member.excluded_at) {
    const reason = member.excluded_reason
      ? EXCLUSION_REASONS[member.excluded_reason]
      : null;
    facts.push({
      key: "excluded",
      label: `excluded (${member.excluded_reason?.replace(/_/g, " ") ?? "?"})`,
      help: reason
        ? `${MEMBERSHIP_HELP.excluded} Reason: ${reason}.`
        : MEMBERSHIP_HELP.excluded,
    });
  }
  if (member.attempts > 0) {
    facts.push({
      key: "attempts",
      label: `${member.attempts} attempt${member.attempts === 1 ? "" : "s"}${
        member.interruptions > 0 ? `, ${member.interruptions} interrupted` : ""
      }`,
      help: MEMBERSHIP_HELP.attempts,
    });
  }
  return facts;
}
