import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import type { PlanMember } from "../types/task";
import type { PlanView } from "../utils/planGraph";

vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({ theme: "light" }),
}));

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
    const onBatchCountChange = vi.fn();
    const { container } = render(
      <div style={{ width: 800, height: 600 }}>
        <DagGraph
          view={fanOut(8)}
          selectedTaskId={null}
          onTaskClick={onTaskClick}
          onBatchCountChange={onBatchCountChange}
        />
      </div>,
    );
    expect(await screen.findByText("×8")).toBeInTheDocument();
    // The control lives in the panel header now; the graph reports.
    expect(screen.queryByLabelText("Group after:")).toBeNull();
    expect(onBatchCountChange).toHaveBeenLastCalledWith(1);

    const batch = container.querySelector('[data-id^="batch:"]') as HTMLElement;
    await act(async () => fireEvent.click(batch));
    expect(screen.queryByText("×8")).not.toBeInTheDocument();
    expect(screen.getAllByText("Shard")).toHaveLength(8);
    expect(onTaskClick).not.toHaveBeenCalled();
    expect(onBatchCountChange).toHaveBeenLastCalledWith(0);
  });

  it("reports an opened batch through a controlled expansion", async () => {
    const onExpansionChange = vi.fn();
    const { container, rerender } = render(
      <div style={{ width: 800, height: 600 }}>
        <DagGraph
          view={fanOut(8)}
          selectedTaskId={null}
          onTaskClick={() => {}}
          groupAfter={5}
          expansion={{ cap: 5, ids: new Set() }}
          onExpansionChange={onExpansionChange}
        />
      </div>,
    );
    await screen.findByText("×8");
    const batch = container.querySelector('[data-id^="batch:"]') as HTMLElement;
    await act(async () => fireEvent.click(batch));
    const next = onExpansionChange.mock.calls[0][0];
    expect(next.cap).toBe(5);
    expect([...next.ids]).toEqual([batch.dataset.id]);

    // Opened under another cap: ignored, so the batch is drawn again.
    rerender(
      <div style={{ width: 800, height: 600 }}>
        <DagGraph
          view={fanOut(8)}
          selectedTaskId={null}
          onTaskClick={() => {}}
          groupAfter={6}
          expansion={next}
          onExpansionChange={onExpansionChange}
        />
      </div>,
    );
    expect(await screen.findByText("×8")).toBeInTheDocument();
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
    const { container } = render(
      <div style={{ width: 800, height: 600 }}>
        <DagGraph view={fanOut(8)} selectedTaskId={null} onTaskClick={() => {}} />
      </div>,
    );
    await screen.findByText("×8");
    // Queried by selector: React Flow hides unmeasured nodes from the
    // accessibility tree, and jsdom never measures.
    const batch = container.querySelector(
      '[role="button"][aria-label="Expand 8 demo.Shard (completed)"]',
    ) as HTMLElement;
    expect(batch).toHaveAttribute("tabindex", "0");
    await act(async () => fireEvent.keyDown(batch, { key: "Enter" }));
    expect(screen.getAllByText("Shard")).toHaveLength(8);
  });
});
