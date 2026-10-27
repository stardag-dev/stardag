import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TaskInstance } from "../types/task";
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
        taskNamespace="pkg"
        taskName="Train"
        instances={[instance("new"), instance("old")]}
      />,
    );
    const code = screen.getByLabelText("Code referencing this task");
    expect(code).toHaveTextContent("from pkg import Train");
    expect(code).toHaveTextContent(`Train.ref("${TASK_ID}", instance_id="new")`);
  });

  it("pins the plan's instance even when the task read did not return it", () => {
    render(
      <TaskRefSnippet
        taskId={TASK_ID}
        taskNamespace="pkg"
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
});
