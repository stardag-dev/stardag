import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TaskInstance } from "../types/task";
import { tooltipOf } from "../test/tooltip";
import { TaskRefSnippet } from "./TaskRefSnippet";

const TASK_ID = "df0c8b03-fab2-5ddd-9743-09fb4a634cf5";

function instance(id: string): TaskInstance {
  return {
    id,
    deployment_id: "dep-1",
    settings_hash: "s",
    instance_hash: "h",
    body: { __namespace: "pkg", __name: "Train" },
    expanded_at: null,
    created_at: "2026-10-01T00:00:00Z",
  };
}

describe("TaskRefSnippet", () => {
  it("pins the newest instance by default", () => {
    render(
      <TaskRefSnippet
        taskId={TASK_ID}
        taskName="Train"
        instances={[instance("new"), instance("old")]}
      />,
    );
    const code = screen.getByLabelText("Code referencing this task");
    // Only the ref line: the namespace is never offered as an import path.
    expect(code.textContent).toBe(`Train.ref("${TASK_ID}", instance_id="new")`);
  });

  it("pins the plan's instance even when the task read did not return it", () => {
    render(
      <TaskRefSnippet
        taskId={TASK_ID}
        taskName="Train"
        instances={[instance("new")]}
        preferredInstanceId="older-plan-instance"
      />,
    );
    expect(screen.getByLabelText("Code referencing this task")).toHaveTextContent(
      'instance_id="older-plan-instance"',
    );
    expect(screen.getByRole("combobox")).toHaveValue("older-plan-instance");
  });

  it("explains itself on hover and links the docs", () => {
    render(
      <TaskRefSnippet
        taskId={TASK_ID}
        taskName="Train"
        instances={[instance("new")]}
      />,
    );
    const link = screen.getByRole("link", { name: /About task references/ });
    expect(link).toHaveAttribute(
      "href",
      "https://docs.stardag.com/how-to/reference-existing-tasks/",
    );
    expect(tooltipOf(link)).toMatch(/exact task/);
  });

  it("copies sd.task_ref when the task has a ref field", () => {
    const shadowed = { ...instance("new"), body: { __name: "Checkout", ref: "main" } };
    render(
      <TaskRefSnippet taskId={TASK_ID} taskName="Checkout" instances={[shadowed]} />,
    );
    expect(screen.getByLabelText("Code referencing this task").textContent).toBe(
      `import stardag as sd\n\nsd.task_ref(Checkout, "${TASK_ID}", instance_id="new")`,
    );
  });
});
