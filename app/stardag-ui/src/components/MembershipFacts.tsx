import type { PlanMember } from "../types/task";
import { membershipFacts } from "../utils/membership";

/**
 * A plan member's facts — root / static / dynamic / closure, excluded,
 * attempts — each with a one-sentence hover explanation. Used in the task
 * table's "Membership" column and in the task detail header.
 */
export function MembershipFacts({
  member,
  variant = "text",
}: {
  member: PlanMember;
  // "text": dot-separated, for a table cell; "chips": for a header.
  variant?: "text" | "chips";
}) {
  const facts = membershipFacts(member);
  if (facts.length === 0) return <span>—</span>;
  if (variant === "chips") {
    return (
      <span className="flex flex-wrap items-center gap-1">
        {facts.map((fact) => (
          <span
            key={fact.key}
            title={fact.help}
            className={`cursor-help rounded px-1.5 py-0.5 text-[11px] ${
              fact.key === "excluded"
                ? "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"
                : "bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300"
            }`}
          >
            {fact.label}
          </span>
        ))}
      </span>
    );
  }
  return (
    <span>
      {facts.map((fact, index) => (
        <span key={fact.key}>
          {index > 0 && <span aria-hidden="true"> · </span>}
          <span
            title={fact.help}
            className="cursor-help underline decoration-dotted underline-offset-2"
          >
            {fact.label}
          </span>
        </span>
      ))}
    </span>
  );
}
