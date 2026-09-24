import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Deployment, TaskInstance } from "../types/task";
import { TaskParameters } from "./TaskParameters";

const DEPLOYMENT: Deployment = {
  id: "dep-1",
  kind: "modal",
  app_name: "etl",
  code_id: "c1",
  image_id: null,
  modal_app_id: null,
  generation: 3,
  deployed_at: "2026-09-24T00:00:00Z",
  activated_at: "2026-09-24T00:00:01Z",
  is_current: true,
};

function instance(id: string, log: string, hash: string): TaskInstance {
  return {
    id,
    deployment_id: "dep-1",
    settings_hash: "a".repeat(64),
    instance_hash: hash,
    body: { __namespace: "demo", __name: "Train", epochs: 3, log },
    expanded_at: "2026-09-24T00:00:02Z",
    created_at: "2026-09-24T00:00:02Z",
  };
}

const TWO = [
  instance("i-2", "debug", "h2".repeat(8)),
  instance("i-1", "info", "h1".repeat(8)),
];
const DEPLOYMENTS = new Map([["dep-1", DEPLOYMENT]]);

function box() {
  return screen.getByText("Task Parameters").closest("div.rounded-lg") as HTMLElement;
}

describe("TaskParameters", () => {
  it("shows the plan's instance's parameters, without the discriminator keys", () => {
    render(
      <TaskParameters
        instances={TWO}
        deploymentsById={DEPLOYMENTS}
        planInstanceId="i-1"
      />,
    );
    expect(within(box()).getByText(/"log": "info"/)).toBeInTheDocument();
    expect(screen.queryByText(/__name/)).not.toBeInTheDocument();
    expect(screen.getByText("2 instances")).toBeInTheDocument();
    // One expand icon, on the parameters box; no per-instance cards.
    expect(
      screen.getAllByRole("button", { name: "View parameters fullscreen" }),
    ).toHaveLength(1);
  });

  it("shows the newest instance outside a plan", () => {
    render(<TaskParameters instances={TWO} deploymentsById={DEPLOYMENTS} />);
    expect(within(box()).getByText(/"log": "debug"/)).toBeInTheDocument();
  });

  it("opens the instance's scope and the other instances from the info icon", () => {
    render(
      <TaskParameters
        instances={TWO}
        deploymentsById={DEPLOYMENTS}
        planInstanceId="i-1"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Instance info" }));
    const dialog = screen.getByRole("heading", { name: "Task instance" }).parentElement!
      .parentElement!;
    expect(within(dialog).getByText("In this plan")).toBeInTheDocument();
    expect(within(dialog).getByText("etl gen 3 · modal")).toBeInTheDocument();
    expect(within(dialog).getByText("h1".repeat(8))).toBeInTheDocument();
    expect(within(dialog).getByText("i-1")).toBeInTheDocument();
    expect(within(dialog).getByText("Other instances (1)")).toBeInTheDocument();
    // The other one, as history, with what differs.
    expect(within(dialog).getByText("h2h2h2h2h2h2")).toBeInTheDocument();
    expect(within(dialog).getByText('log="debug"')).toBeInTheDocument();
  });

  it("says a single instance has no history", () => {
    render(<TaskParameters instances={[TWO[1]]} deploymentsById={DEPLOYMENTS} />);
    expect(screen.queryByText(/instances$/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Instance info" }));
    expect(screen.getByText("Latest")).toBeInTheDocument();
    expect(screen.getByText(/only instance/)).toBeInTheDocument();
  });

  it("lists every instance fullscreen, the current one selected, and switches on a row click", () => {
    render(
      <TaskParameters
        instances={TWO}
        deploymentsById={DEPLOYMENTS}
        planInstanceId="i-1"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "View parameters fullscreen" }));
    const dialog = screen.getAllByRole("heading", { name: "Task Parameters" }).at(-1)!
      .parentElement!.parentElement!;
    const rows = within(dialog).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[1]).toHaveAttribute("aria-selected", "true");
    expect(within(rows[1]).getByText("in this plan")).toBeInTheDocument();
    expect(within(dialog).getByText(/"log": "info"/)).toBeInTheDocument();

    fireEvent.click(rows[0]);
    expect(rows[0]).toHaveAttribute("aria-selected", "true");
    // And from the keyboard.
    expect(rows[1]).toHaveAttribute("tabindex", "0");
    fireEvent.keyDown(rows[1], { key: "Enter" });
    expect(rows[1]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(rows[0], { key: " " });
    expect(rows[0]).toHaveAttribute("aria-selected", "true");
    expect(within(dialog).getByText(/"log": "debug"/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/"log": "info"/)).not.toBeInTheDocument();
  });

  it("says when no instance is recorded", () => {
    render(<TaskParameters instances={[]} deploymentsById={DEPLOYMENTS} />);
    expect(screen.getByText("No instance recorded.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Instance info" })).toBeNull();
  });
});
