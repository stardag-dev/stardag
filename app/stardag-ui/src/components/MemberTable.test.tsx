import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { PlanMember } from "../types/task";
import { MEMBERSHIP_COLUMN_HELP, MEMBERSHIP_HELP } from "../utils/membership";
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
    expect(header).toHaveAttribute("title", MEMBERSHIP_COLUMN_HELP);
    expect(screen.getByText("root")).toHaveAttribute("title", MEMBERSHIP_HELP.root);
    expect(screen.getByText("dynamic")).toHaveAttribute(
      "title",
      MEMBERSHIP_HELP.dynamic,
    );
    expect(screen.getByText("excluded (operator)")).toHaveAttribute(
      "title",
      `${MEMBERSHIP_HELP.excluded} Reason: by an operator.`,
    );
    expect(screen.getByText("1 attempt")).toHaveAttribute(
      "title",
      MEMBERSHIP_HELP.attempts,
    );
  });
});
