import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { PlanMember } from "../types/task";
import { MEMBERSHIP_COLUMN_HELP, MEMBERSHIP_HELP } from "../utils/membership";
import { tooltipOf } from "../test/tooltip";
import { MemberTable } from "./MemberTable";

const members: PlanMember[] = [
  {
    task_id: "t-root",
    instance_id: "i-root",
    instance_hash: "h1",
    task_namespace: "demo",
    task_name: "Root",
    status: "pending",
    is_root: true,
    admitted_by: "root",
    excluded_at: null,
    excluded_reason: null,
    attempts: 0,
    interruptions: 0,
  },
  {
    task_id: "t-dyn",
    instance_id: "i-dyn",
    instance_hash: "h2",
    task_namespace: "demo",
    task_name: "Yielded",
    status: "failed",
    is_root: false,
    admitted_by: "dynamic",
    excluded_at: "2026-09-24T00:00:00Z",
    excluded_reason: "operator",
    attempts: 1,
    interruptions: 0,
  },
];

describe("MemberTable", () => {
  it("explains the Membership column and each of its values on hover", () => {
    render(
      <MemberTable
        members={members}
        selectedTaskId={null}
        onSelectTask={() => {}}
        page={1}
        pageSize={20}
        onPageChange={() => {}}
      />,
    );
    const header = screen.getByRole("columnheader", { name: /Membership/ });
    // The shared tooltip, not a native title, and no help cursor.
    expect(header).not.toHaveAttribute("title");
    expect(header.className).not.toMatch(/cursor-help/);
    expect(tooltipOf(within(header).getByText("Membership"))).toBe(
      MEMBERSHIP_COLUMN_HELP,
    );
    expect(tooltipOf(screen.getByText("root"))).toBe(MEMBERSHIP_HELP.root);
    expect(tooltipOf(screen.getByText("dynamic"))).toBe(MEMBERSHIP_HELP.dynamic);
    expect(tooltipOf(screen.getByText("excluded (operator)"))).toBe(
      `${MEMBERSHIP_HELP.excluded} Reason: by an operator.`,
    );
    expect(tooltipOf(screen.getByText("1 attempt"))).toBe(MEMBERSHIP_HELP.attempts);
    expect(screen.getByText("root")).not.toHaveAttribute("title");
    expect(screen.getByText("root").className).not.toMatch(/cursor-help/);
  });
});
