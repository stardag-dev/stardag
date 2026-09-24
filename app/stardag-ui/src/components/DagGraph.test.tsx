import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import type { PlanMember } from "../types/task";
import type { PlanView } from "../utils/planGraph";

vi.mock("../context/ThemeContext", () => ({ useTheme: () => ({ theme: "light" }) }));

import { DagGraph } from "./DagGraph";

beforeAll(() => {
  // React Flow measures its viewport; jsdom has no layout.
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

function member(id: string, name: string): PlanMember {
  return {
    task_id: `t-${id}`,
    instance_id: id,
    instance_hash: `h-${id}`,
    task_namespace: "demo",
    task_name: name,
    status: "completed",
    is_root: name === "Root",
    admitted_by: "static",
    excluded_at: null,
    excluded_reason: null,
    attempts: 0,
    interruptions: 0,
  };
}

function fanOut(n: number): PlanView {
  const shards = Array.from({ length: n }, (_, i) => member(`s${i}`, "Shard"));
  return {
    members: [...shards, member("root", "Root")],
    edges: shards.map((s) => ({
      upstream_instance_id: s.instance_id,
      downstream_instance_id: "root",
      is_dynamic: false,
    })),
  };
}

describe("DagGraph fan-out batching", () => {
  it("draws a wide fan-out as one batch node, expanded by a click", async () => {
    const onTaskClick = vi.fn();
    const { container } = render(
      <div style={{ width: 800, height: 600 }}>
        <DagGraph view={fanOut(8)} selectedTaskId={null} onTaskClick={onTaskClick} />
      </div>,
    );
    expect(await screen.findByText("×8")).toBeInTheDocument();
    expect(screen.getByText("(1 group)")).toBeInTheDocument();
    expect(screen.getByLabelText("Group after:")).toHaveValue(5);

    const batch = container.querySelector('[data-id^="batch:"]') as HTMLElement;
    await act(async () => fireEvent.click(batch));
    expect(screen.queryByText("×8")).not.toBeInTheDocument();
    expect(screen.getAllByText("Shard")).toHaveLength(8);
    expect(onTaskClick).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Regroup" })).toBeInTheDocument();
  });

  it("opens the batch holding the selected task", async () => {
    render(
      <div style={{ width: 800, height: 600 }}>
        <DagGraph view={fanOut(8)} selectedTaskId="t-s3" onTaskClick={() => {}} />
      </div>,
    );
    expect(await screen.findAllByText("Shard")).toHaveLength(8);
    expect(screen.queryByText("×8")).not.toBeInTheDocument();
  });

  it("expands a batch from the keyboard", async () => {
    render(
      <div style={{ width: 800, height: 600 }}>
        <DagGraph view={fanOut(8)} selectedTaskId={null} onTaskClick={() => {}} />
      </div>,
    );
    // React Flow hides nodes until measured, which jsdom never does.
    const batch = await screen.findByRole("button", {
      name: "Expand 8 demo.Shard (completed)",
      hidden: true,
    });
    await act(async () => fireEvent.keyDown(batch, { key: "Enter" }));
    expect(screen.getAllByText("Shard")).toHaveLength(8);
  });
});
